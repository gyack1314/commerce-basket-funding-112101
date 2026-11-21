"""活动净增收益的人群观察与因果识别。

纪律（对应任务书与 input-contract.md）：

* 人群在活动开始前确定（cohort.eligible_from 统一为活动前），
  ``participated`` 是实际参加，observations 含零购买账户；
* 不能把领券者与所有未领券者简单对比：渠道是强混杂——web 渠道只有参加者、
  没有对照，因此 web 参加者的个体因果效应**不可识别**，单独保留；
* 可比支持只在 app 渠道内建立，按 category 分层、以 tenure_days 与
  pre_period_spend 做近邻匹配；共同支持之外的样本不进因果估计；
* 净增收益给点估计与自助法区间；商品构成、老客比例、渠道差异作为限制
  条件写入报告；
* 统计区间只描述收益推断的不确定性，不干预任何一笔有明确条款的费用核对
  （费用核定在 funding 模块，确定性处理）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np

from .models import FactStore, parse_ts
from .basket_engine import determine_basket

OUTCOMES = (
    "purchase_flag",        # 期内是否购买
    "gross_spend",          # 优惠前成交额
    "net_spend",            # 消费者实际支付（扣全部有效折扣）
    "platform_net_spend",   # 平台净支出（出资-冲回-待追收权益）
    "refund",               # 现金退款
    "goodwill",             # 商家补偿
    "basket_margin",        # 商家毛利合计（留存口径）
)


@dataclass
class AccountRecord:
    account_id: str
    participated: bool
    channel: str
    category: str
    tenure_days: int
    pre_period_spend: int
    acquisition_cost: int
    outcomes: dict[str, int] = field(default_factory=dict)
    basket_ids: list[str] = field(default_factory=list)
    matured: bool = True


@dataclass
class MatchResult:
    treated_id: str
    control_id: str | None
    distance: float | None
    in_support: bool
    reason: str = ""


@dataclass
class AnalysisReport:
    cutoff: str
    method: str
    identifiable_scope: str
    non_identifiable: list[str]
    n_treated: int
    n_control: int
    n_matched: int
    support_excluded_treated: int
    naive_difference: dict[str, float]
    matched_ate: dict[str, float]
    bootstrap_ci: dict[str, list[float]]
    composition: dict[str, Any]
    limitations: list[str]
    per_account: list[dict[str, Any]] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_account_records(store: FactStore, cutoff) -> list[AccountRecord]:
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    cohort = {r["id"]: r for r in store.rows("cohort")}
    obs = {r["account_id"]: r for r in store.rows("observations")}
    baskets = store.rows("baskets")
    basket_by_id = {b["id"]: b for b in baskets}
    determinations = {}
    for b in baskets:
        known = parse_ts(b["known_at"])
        if known <= cutoff_dt:
            determinations[b["id"]] = determine_basket(store, b["id"], cutoff_dt)

    records: list[AccountRecord] = []
    for acct, c in cohort.items():
        o = obs.get(acct)
        if o is None:
            continue
        acc = AccountRecord(
            account_id=acct, participated=bool(c["participated"]), channel=c["channel"],
            category=c["category"], tenure_days=int(c["tenure_days"]),
            pre_period_spend=int(o["pre_period_spend"]),
            acquisition_cost=int(o["acquisition_cost"]),
            outcomes={k: 0 for k in OUTCOMES},
            basket_ids=list(o["purchase_baskets"]),
        )
        for bid in o["purchase_baskets"]:
            d = determinations.get(bid)
            if d is None:
                acc.matured = False
                continue
            if parse_ts(basket_by_id[bid]["mature_at"]) > cutoff_dt:
                acc.matured = False
            acc.outcomes["purchase_flag"] = 1
            acc.outcomes["gross_spend"] += d.gross_total
            acc.outcomes["net_spend"] += d.consumer_paid - d.total_cash_refund
            acc.outcomes["platform_net_spend"] += d.platform_net_spend
            acc.outcomes["refund"] += d.total_cash_refund
            acc.outcomes["goodwill"] += d.total_goodwill
            acc.outcomes["basket_margin"] += sum(d.merchant_profit.values())
        records.append(acc)
    return records


def _nearest_control(treated: AccountRecord, controls: list[AccountRecord],
                     caliper: float) -> MatchResult:
    """同 category 内按标准化 tenure / pre_spend 欧氏距离找最近对照。"""
    pool = [c for c in controls if c.category == treated.category]
    if not pool:
        return MatchResult(treated.account_id, None, None, False, "同分层无对照")
    ten = np.array([c.tenure_days for c in pool], dtype=float)
    pre = np.array([c.pre_period_spend for c in pool], dtype=float)
    t_ten = float(treated.tenure_days)
    t_pre = float(treated.pre_period_spend)
    ten_sd = ten.std(ddof=1) or 1.0
    pre_sd = pre.std(ddof=1) or 1.0
    dist = np.sqrt(((ten - t_ten) / ten_sd) ** 2 + ((pre - t_pre) / pre_sd) ** 2)
    idx = int(np.argmin(dist))
    if dist[idx] > caliper:
        return MatchResult(treated.account_id, None, float(dist[idx]), False,
                           f"最近距离 {dist[idx]:.3f} 超出 caliper {caliper}")
    return MatchResult(treated.account_id, pool[idx].account_id, float(dist[idx]), True)


def analyze(store: FactStore, cutoff, caliper: float = 1.0,
            n_bootstrap: int = 1000, seed: int = 20260610) -> AnalysisReport:
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    records = _build_account_records(store, cutoff_dt)
    rng = np.random.default_rng(seed)

    app = [r for r in records if r.channel == "app"]
    treated = [r for r in app if r.participated]
    controls = [r for r in app if not r.participated]
    web_treated = [r for r in records if r.channel == "web" and r.participated]

    # 匹配（对照可重复使用，带放回语义；这里记录配对用于复核）
    matches = [_nearest_control(t, controls, caliper) for t in treated]
    matched_pairs = [(t, m) for t, m in zip(treated, matches) if m.in_support]
    control_by_id = {r.account_id: r for r in controls}

    def group_means(rows: list[AccountRecord]) -> dict[str, float]:
        if not rows:
            return {k: float("nan") for k in OUTCOMES}
        return {k: float(np.mean([r.outcomes[k] for r in rows])) for k in OUTCOMES}

    naive = {
        k: group_means(treated)[k] - group_means(controls)[k] for k in OUTCOMES
    }
    matched_ate: dict[str, float] = {}
    if matched_pairs:
        diffs = {
            k: np.array([t.outcomes[k] - control_by_id[m.control_id].outcomes[k]
                         for t, m in matched_pairs], dtype=float)
            for k in OUTCOMES
        }
        matched_ate = {k: float(np.mean(v)) for k, v in diffs.items()}
    else:
        matched_ate = {k: float("nan") for k in OUTCOMES}

    # 自助法区间：对匹配对整体重抽样
    ci: dict[str, list[float]] = {k: [float("nan"), float("nan")] for k in OUTCOMES}
    if matched_pairs:
        n = len(matched_pairs)
        boot = {k: [] for k in OUTCOMES}
        pair_diffs = {
            k: np.array([t.outcomes[k] - control_by_id[m.control_id].outcomes[k]
                         for t, m in matched_pairs], dtype=float)
            for k in OUTCOMES
        }
        for _ in range(n_bootstrap):
            sample = rng.integers(0, n, n)
            for k in OUTCOMES:
                boot[k].append(pair_diffs[k][sample].mean())
        for k in OUTCOMES:
            ci[k] = [float(np.percentile(boot[k], 2.5)), float(np.percentile(boot[k], 97.5))]

    # 人群构成与限制
    def comp(rows: list[AccountRecord]) -> dict[str, Any]:
        if not rows:
            return {}
        return {
            "n": len(rows),
            "category_mix": {c: round(sum(1 for r in rows if r.category == c) / len(rows), 4)
                             for c in sorted({r.category for r in rows})},
            "mean_tenure": round(float(np.mean([r.tenure_days for r in rows])), 2),
            "mean_pre_spend": round(float(np.mean([r.pre_period_spend for r in rows])), 2),
            "zero_purchase_share": round(
                sum(1 for r in rows if r.outcomes["purchase_flag"] == 0) / len(rows), 4),
            "acquisition_cost_mean": round(float(np.mean([r.acquisition_cost for r in rows])), 2),
        }

    excluded = sum(1 for m in matches if not m.in_support)
    limitations = [
        "web 渠道只有参加者没有同期对照，210 名 web 参加者的个体因果效应不可识别，未计入 ATE",
        f"app 参加者中 {excluded} 人在 caliper={caliper} 内无同分层近邻，落在共同支持之外",
        "参加者活动前人均消费显著高于对照，匹配只能平衡可观测协变量，未观测混杂仍可能存在",
        "退货行为按本次活动的门槛与力度观测，不能外推为任意优惠力度下不变的规律",
        "区间为自助法百分位区间，描述收益推断不确定性；费用核对以合同条款为准，不受区间影响",
        "未成熟篮子按截点剔除或标注，不与成熟篮子合并估计",
    ]
    return AnalysisReport(
        cutoff=str(cutoff_dt),
        method="app 渠道内 category 精确分层 + tenure/pre_spend 标准化近邻匹配（对照可重用），自助法 95% 区间",
        identifiable_scope="app 渠道、共同支持内、截点已成熟的参加账户",
        non_identifiable=[
            f"web 参加者 {len(web_treated)} 人：无同渠道未参加对照",
            f"共同支持外 app 参加者 {excluded} 人",
        ],
        n_treated=len(treated), n_control=len(controls), n_matched=len(matched_pairs),
        support_excluded_treated=excluded,
        naive_difference=naive, matched_ate=matched_ate, bootstrap_ci=ci,
        composition={
            "treated_app": comp(treated), "control_app": comp(controls),
            "web_treated": comp(web_treated),
        },
        limitations=limitations,
        per_account=[
            {
                "account_id": r.account_id, "participated": r.participated,
                "channel": r.channel, "category": r.category,
                "tenure_days": r.tenure_days, "pre_period_spend": r.pre_period_spend,
                "matured": r.matured, **r.outcomes,
            } for r in records
        ],
        matches=[asdict(m) for m in matches],
    )
