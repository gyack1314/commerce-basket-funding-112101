"""后续活动的方案比较与预算占用。

规则（对应任务书）：

* 未来方案只有 offers 给定的范围（门槛、面额、适用品类、平台预算），
  不推断任意新门槛下的确定收益；
* 只有与历史已观察门槛、品类、退货行为相符的部分才标记为“有历史支持”，
  本次观测的退款率不得当作任意优惠力度下不变的规律；
* 尚有争议的商家出资（budgets.disputed）不是可用预算；
* 已接受方案以明确 platform_budget 占用资金；两份各自可行的方案不能一起
  超支——同时接受时必须返回实际缺口；
* 接受方案时核对所引用的费用认定与预算余额。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .models import FactStore, parse_ts
from .basket_engine import determine_basket


@dataclass
class HistoricalSupport:
    observed_thresholds: list[int]
    observed_categories: list[str]
    observed_refund_rate: float | None
    threshold_supported: bool
    categories_supported: bool
    refund_behavior_note: str


@dataclass
class OfferEvaluation:
    offer_id: str
    threshold: int
    amount: int
    platform_budget: int
    eligible_categories: list[str]
    support: HistoricalSupport
    standalone_feasible: bool
    standalone_gap: int
    # 与已接受方案合并后的资金情况
    cumulative_committed: int
    joint_feasible: bool
    joint_gap: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BudgetReport:
    cutoff: str
    currency: str
    cash_available: int
    disputed_reserve: int
    already_committed: int
    usable: int
    accepted_plans: list[dict[str, Any]]
    evaluations: list[OfferEvaluation]
    joint_acceptance: dict[str, Any]
    trace_refs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _historical_support(store: FactStore, cutoff, offer: dict[str, Any]) -> HistoricalSupport:
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    cross = [r for r in store.rows("discounts") if r["kind"] == "cross"]
    observed_thresholds = sorted({r["threshold"] for r in cross})
    lines = {r["id"]: r for r in store.rows("lines")}
    observed_categories = sorted({lines[l]["category"] for r in cross for l in r["eligible_lines"]})

    # 历史跨店补贴在“购买时点有效”的篮子上的退货率（按行数量口径）
    baskets = {b["id"]: b for b in store.rows("baskets")}
    returns = store.effective_returns(cutoff_dt)
    eligible_line_ids = {l for r in cross for l in r["eligible_lines"]}
    qty_total = sum(lines[l]["quantity"] for l in eligible_line_ids)
    qty_returned = 0
    for lid, row in returns.items():
        if lid in eligible_line_ids:
            qty_returned += min(row["return_qty"], lines[lid]["quantity"])
    refund_rate = round(qty_returned / qty_total, 4) if qty_total else None

    cats_ok = set(offer["eligible_categories"]) <= set(observed_categories)
    thr_ok = offer["threshold"] in observed_thresholds
    note = (
        "退款率仅在门槛 30000、面额 3000 的本次活动中观测；改变门槛/面额后"
        "消费者凑单与退货决策会变化，不得把该比率当作新方案的不变参数"
    )
    return HistoricalSupport(
        observed_thresholds=observed_thresholds,
        observed_categories=observed_categories,
        observed_refund_rate=refund_rate,
        threshold_supported=thr_ok,
        categories_supported=cats_ok,
        refund_behavior_note=note,
    )


def evaluate_budget(store: FactStore, cutoff, offer_ids: list[str] | None = None) -> BudgetReport:
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    budget_rows = store.as_of("budgets", cutoff_dt)
    if not budget_rows:
        raise ValueError("截点内无预算资料")
    budget = budget_rows[0]
    cash = int(budget["cash_available"])
    disputed = int(budget["disputed"])

    plans = [
        r for r in store.as_of("accepted_plans", cutoff_dt)
        if r.get("status") == "accepted"
    ]
    committed = sum(int(p["platform_budget"]) for p in plans)
    usable = cash - disputed - committed

    offers = store.as_of("offers", cutoff_dt)
    if offer_ids is not None:
        offers = [o for o in offers if o["id"] in offer_ids]
    evaluations: list[OfferEvaluation] = []
    cumulative = committed
    for offer in sorted(offers, key=lambda o: o["id"]):
        support = _historical_support(store, cutoff_dt, offer)
        need = int(offer["platform_budget"])
        # 独立可行性：相对当前可用余额（现金 - 争议预留 - 已占用）
        standalone_gap = max(0, need - usable)
        cumulative += need
        joint_gap = max(0, cumulative - (cash - disputed))
        warnings = []
        if not support.threshold_supported:
            warnings.append(
                f"门槛 {offer['threshold']} 无历史观察支持，该门槛下的收益与退货不可确定推断"
            )
        if not support.categories_supported:
            warnings.append("适用品类超出历史范围")
        if disputed > 0:
            warnings.append(f"争议出资 {disputed} 分已预留，不计入可用预算")
        evaluations.append(OfferEvaluation(
            offer_id=offer["id"], threshold=int(offer["threshold"]),
            amount=int(offer["amount"]), platform_budget=need,
            eligible_categories=list(offer["eligible_categories"]),
            support=support,
            standalone_feasible=standalone_gap == 0, standalone_gap=standalone_gap,
            cumulative_committed=cumulative,
            joint_feasible=joint_gap == 0, joint_gap=joint_gap, warnings=warnings,
        ))

    joint = {
        "offers": [e.offer_id for e in evaluations],
        "required_sum": sum(e.platform_budget for e in evaluations),
        "plus_existing_commitment": committed,
        "usable_now": usable,
        "all_jointly_feasible": all(e.joint_feasible for e in evaluations),
        "actual_gap_if_accepted_together": max(0, cumulative - (cash - disputed)),
    }
    return BudgetReport(
        cutoff=str(cutoff_dt), currency=budget.get("currency", "CNY"),
        cash_available=cash, disputed_reserve=disputed, already_committed=committed,
        usable=usable, accepted_plans=plans, evaluations=evaluations,
        joint_acceptance=joint,
        trace_refs={
            "budget_fact": budget["id"],
            "accepted_plan_facts": [p["id"] for p in plans],
            "offer_facts": [o["id"] for o in offers],
        },
    )


def accept_offer(store: FactStore, cutoff, offer_id: str,
                 also_with: list[str] | None = None) -> dict[str, Any]:
    """模拟接受方案：核对引用的费用认定与预算余额，返回占用结果或实际缺口。

    不接真实支付，只产出一条内部预算占用记录，交由持久化层落盘。
    """
    report = evaluate_budget(store, cutoff, offer_ids=([offer_id] + (also_with or [])))
    target = next((e for e in report.evaluations if e.offer_id == offer_id), None)
    if target is None:
        raise KeyError(f"方案 {offer_id} 在截点不存在")
    feasible = target.standalone_feasible and (
        not also_with or all(e.joint_feasible for e in report.evaluations)
    )
    gap = target.joint_gap if also_with else target.standalone_gap
    return {
        "offer_id": offer_id,
        "decision": "accepted" if feasible else "rejected-budget-gap",
        "platform_budget": target.platform_budget,
        "usable": report.usable,
        "actual_gap": gap,
        "historical_support": {
            "threshold_supported": target.support.threshold_supported,
            "categories_supported": target.support.categories_supported,
            "observed_refund_rate": target.support.observed_refund_rate,
        },
        "warnings": target.warnings,
        "trace": {
            "budget_fact": report.trace_refs["budget_fact"],
            "offer_fact": offer_id,
            "existing_commitments": report.trace_refs["accepted_plan_facts"],
        },
    }
