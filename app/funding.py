"""费用确认台账与商家异议处理。

两条不可违背的纪律：

1. **已确认出资不被重跑无声替换。** ``confirmed_funding`` 是商家对
   (basket, merchant, discount) 范围的确认值；引擎按新事实重算得到的是
   “当前应值”，两者之差显式记为确认差额，等待对方确认，绝不直接改历史。
2. **争议只冻结受影响范围。** pending 异议按其 (basket, target, target_id)
   定位受影响的费用键；其余篮子、其余折扣照常核定与确认。

异议成立（accepted）会改变分摊基数与活动利润，因此接受后必须重算整篮，
而不是只改该商家看到的一行。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .models import FactStore, parse_ts
from .basket_engine import determine_basket, BasketDetermination


@dataclass
class FundingEntry:
    basket_id: str
    merchant_id: str
    discount_id: str
    kind: str
    original_funding: int          # 购买时点计提的商家出资
    reversed_funding: int          # 退货/失效按原归属冲回
    current_required: int          # 引擎按截点事实重算的净出资 = original - reversed
    confirmed_amount: int | None   # 已确认金额（confirmed_funding）
    confirmed_at: str | None
    # 已确认与当前应值的差额：>0 商家多确认需退回确认，<0 需追加确认
    confirmation_delta: int = 0
    frozen: bool = False           # 是否被未决争议冻结
    freeze_reasons: list[str] = field(default_factory=list)
    status: str = "unconfirmed"


@dataclass
class ClaimView:
    claim_id: str
    basket_id: str
    merchant_id: str
    target: str
    target_id: str
    proposed: int
    status: str
    known_at: str
    revises: str | None
    affected_funding_keys: list[tuple[str, str, str]]
    decision_note: str


@dataclass
class LedgerReport:
    cutoff: str
    entries: list[FundingEntry]
    claims: list[ClaimView]
    # 汇总
    total_current_required: dict[str, int] = field(default_factory=dict)
    total_confirmed: dict[str, int] = field(default_factory=dict)
    total_pending_delta: dict[str, int] = field(default_factory=dict)
    frozen_keys: list[tuple[str, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        return out


def _affected_funding_keys(store: FactStore, claim: dict[str, Any],
                           determinations: dict[str, BasketDetermination]
                           ) -> list[tuple[str, str, str]]:
    """定位异议影响的 (basket, merchant, discount) 费用键。

    * unit_cost（商品行）：改变该商家在本篮的成本与利润，不改变折扣出资额，
      但仍冻结该商家本篮的费用确认，直到双方采用同一成本事实；
    * return_qty（退货事实）：改变本篮全部仍有效折扣的冲回，影响篮内所有
      参与商家的出资基数，因此冻结篮内全部商家的费用键；
    * 优惠适用性（target=discount/eligibility）：冻结该折扣的全部出资键。
    """
    b_id = claim["basket_id"]
    det = determinations.get(b_id)
    keys: list[tuple[str, str, str]] = []
    if det is None:
        return keys
    target = claim["target"]
    if target == "unit_cost":
        line = det.lines.get(claim["target_id"])
        merchant = line.merchant_id if line else claim["merchant_id"]
        for ds in det.discounts.values():
            if merchant in ds.merchant_funding or any(
                det.lines[x].merchant_id == merchant for x in ds.eligible_lines
            ):
                keys.append((b_id, merchant, ds.discount_id))
    elif target == "return_qty":
        for ds in det.discounts.values():
            merchants = set(ds.merchant_funding) | {
                det.lines[x].merchant_id for x in ds.eligible_lines
            }
            for m in sorted(merchants):
                keys.append((b_id, m, ds.discount_id))
    elif target in ("discount", "eligibility"):
        discount_id = claim["target_id"]
        for d in det.discounts.values():
            if d.discount_id == discount_id:
                for m in sorted(set(d.merchant_funding) |
                                {det.lines[x].merchant_id for x in d.eligible_lines}):
                    keys.append((b_id, m, d.discount_id))
    else:
        # 未知 target：保守冻结该商家本篮全部费用键
        for ds in det.discounts.values():
            keys.append((b_id, claim["merchant_id"], ds.discount_id))
    return sorted(set(keys))


def build_ledger(store: FactStore, cutoff, basket_ids: list[str] | None = None) -> LedgerReport:
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    if basket_ids is None:
        basket_ids = [r["id"] for r in store.rows("baskets")]
    determinations = {b: determine_basket(store, b, cutoff_dt) for b in basket_ids}

    # 已确认出资索引：(basket, merchant, discount) -> fact
    confirmed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in store.effective_confirmed_funding(cutoff_dt):
        confirmed[(row["basket_id"], row["merchant_id"], row["discount_id"])] = row

    # 未决异议（折叠更正链后的最新状态 != accepted/rejected 视为未决）
    effective_claims = store.effective_claims(cutoff_dt)
    frozen_map: dict[tuple[str, str, str], list[str]] = {}
    claim_views: list[ClaimView] = []
    for (b_id, target, target_id), claim in effective_claims.items():
        keys = _affected_funding_keys(store, claim, determinations)
        if claim["status"] not in ("accepted", "rejected"):
            for k in keys:
                frozen_map.setdefault(k, []).append(claim["id"])
        note = {
            "accepted": "异议成立：目标字段已替换为 proposed，整篮按新事实重算",
            "rejected": "异议驳回：维持原事实，采用原始凭据",
            "pending": "未决：仅冻结受影响费用键，其余核定照常处理",
        }.get(claim["status"], f"未知状态 {claim['status']}，按未决处理")
        claim_views.append(ClaimView(
            claim_id=claim["id"], basket_id=b_id, merchant_id=claim["merchant_id"],
            target=target, target_id=target_id, proposed=claim["proposed"],
            status=claim["status"], known_at=claim["known_at"],
            revises=claim.get("revises"), affected_funding_keys=keys, decision_note=note,
        ))
    claim_views.sort(key=lambda c: (c.known_at, c.claim_id))

    entries: list[FundingEntry] = []
    for b_id in basket_ids:
        det = determinations[b_id]
        for ds in det.discounts.values():
            merchants = set(ds.merchant_funding) | {
                det.lines[x].merchant_id for x in ds.eligible_lines
            }
            # 净冲回按商家
            for m in sorted(merchants):
                original = ds.merchant_funding.get(m, 0)
                reversed_amt = ds.reversed_merchant.get(m, 0)
                current = original - reversed_amt
                cf = confirmed.get((b_id, m, ds.discount_id))
                confirmed_amount = cf["amount"] if cf else None
                delta = 0
                if cf is not None:
                    delta = confirmed_amount - current  # >0 多确认，<0 需补确认
                key = (b_id, m, ds.discount_id)
                is_frozen = key in frozen_map
                if is_frozen:
                    status = "frozen"
                elif cf is None:
                    status = "unconfirmed"
                elif delta == 0:
                    status = "confirmed"
                else:
                    status = "delta-pending-confirmation"
                entries.append(FundingEntry(
                    basket_id=b_id, merchant_id=m, discount_id=ds.discount_id, kind=ds.kind,
                    original_funding=original, reversed_funding=reversed_amt,
                    current_required=current, confirmed_amount=confirmed_amount,
                    confirmed_at=cf["confirmed_at"] if cf else None,
                    confirmation_delta=delta,
                    frozen=is_frozen,
                    freeze_reasons=list(frozen_map.get(key, ())),
                    status=status,
                ))

    report = LedgerReport(cutoff=str(cutoff_dt), entries=entries, claims=claim_views,
                          frozen_keys=sorted(frozen_map))
    for e in entries:
        report.total_current_required[e.merchant_id] = (
            report.total_current_required.get(e.merchant_id, 0) + e.current_required
        )
        if e.confirmed_amount is not None:
            report.total_confirmed[e.merchant_id] = (
                report.total_confirmed.get(e.merchant_id, 0) + e.confirmed_amount
            )
            report.total_pending_delta[e.merchant_id] = (
                report.total_pending_delta.get(e.merchant_id, 0) + e.confirmation_delta
            )
    return report


def decide_claim(store: FactStore, claim_id: str, decision: str,
                 evidence: str, cutoff) -> dict[str, Any]:
    """对一条异议给出平台逐项决定（接受/驳回）与采用凭据。

    该函数只输出决定记录，不篡改原始事实包；接受的事实通过后续资料包
    （accepted 状态的 claims）进入仓库，保证历史可回查。
    """
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision 必须是 accepted 或 rejected")
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    fact = None
    for (t, fid), f in store.facts.items():
        if t == "claims" and fid == claim_id and (f.known_at is None or f.known_at <= cutoff_dt):
            fact = f
            break
    if fact is None:
        raise KeyError(f"截点内找不到异议 {claim_id}")
    payload = fact.payload
    return {
        "claim_id": claim_id,
        "decision": decision,
        "evidence": evidence,
        "target": payload["target"],
        "target_id": payload["target_id"],
        "proposed": payload["proposed"],
        "basket_id": payload["basket_id"],
        "merchant_id": payload["merchant_id"],
        "known_at": payload["known_at"],
        "revises": payload.get("revises"),
        "effect": (
            "替换目标字段并重算整篮及关联商家分摊基数"
            if decision == "accepted"
            else "维持原事实，目标费用键解除冻结前提是无其他未决争议"
        ),
    }
