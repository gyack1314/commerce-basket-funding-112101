"""篮子级经营核定引擎（确定性、纯函数、可复算）。

核定回答一个篮子在某一资料截点上的四类归属：
消费者实际负担、商家让利与成本、平台支出、留存商品贡献。
所有规则直接对应 docs/input-contract.md：

* 店铺折扣 → 平台券 → 跨店补贴，顺序固定；每步仅以“尚未优惠的正金额”为
  分摊权重，余分按 line_id 字典序分配；
* 跨店补贴以适用行的优惠前金额判断门槛；退货后剩余适用金额不足 threshold
  时全额失效，原出资按原归属冲回；
* return_qty 是累计退回数量，取消与退货都减少留存量；goodwill 是商家主动
  给予的额外现金补偿，与退货原因分开；
* 失效时消费者应补部分先抵减本次应退金额，最低退款为零，抵不掉的列入
  待追收（应收），绝不能记作已收；
* 不允许负成本、不允许重复扣券；未参与某优惠的商家不替别人承担费用。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .models import FactStore, parse_ts
from .money import allocate_by_weight, per_unit_split

DISCOUNT_ORDER = ("shop", "platform", "cross")


@dataclass
class LineState:
    line_id: str
    merchant_id: str
    category: str
    quantity: int
    unit_price: int
    unit_cost: int
    gross: int = 0
    returned_qty: int = 0
    return_records: list[str] = field(default_factory=list)
    goodwill: int = 0
    # 各折扣在本行购买时点的分摊额
    discount_by_kind: dict[str, int] = field(default_factory=dict)
    # 退货按件回冲 + 门槛失效一次性冲回，在本行的落点
    reversals_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def retained_qty(self) -> int:
        return self.quantity - self.returned_qty

    @property
    def retained_gross(self) -> int:
        return self.unit_price * self.retained_qty

    @property
    def retained_cost(self) -> int:
        return self.unit_cost * self.retained_qty


@dataclass
class DiscountState:
    discount_id: str
    kind: str
    eligible_lines: list[str]
    amount: int
    threshold: int
    merchant_share: dict[str, float]
    valid_at_purchase: bool = True
    active: bool = True
    invalidated_by: str | None = None
    merchant_funding: dict[str, int] = field(default_factory=dict)
    platform_funding: int = 0
    reversed_merchant: dict[str, int] = field(default_factory=dict)
    reversed_platform: int = 0

    @property
    def reversed_total(self) -> int:
        return sum(self.reversed_merchant.values()) + self.reversed_platform


@dataclass
class ReturnEvent:
    return_id: str
    line_id: str
    qty: int
    cumulative_qty: int
    goodwill: int
    occurred_at: str
    known_at: str
    confirmed: bool
    gross_refund_due: int = 0       # 折后单价 × 本次件数
    discount_reversed: dict[str, int] = field(default_factory=dict)
    cross_invalidated: bool = False
    consumer_makeup: int = 0        # 补贴失效收回的消费者已享补贴
    refund_offset: int = 0          # 应补抵减本次退款
    receivable: int = 0             # 抵不掉的待追收
    cash_refund: int = 0            # 本次实际应退现金（最低零）
    goodwill_cash: int = 0
    receivable_split: dict[str, int] = field(default_factory=dict)  # merchant -> 额
    receivable_platform: int = 0


@dataclass
class BasketDetermination:
    basket_id: str
    cutoff: str
    campaign: str | None
    account_id: str
    sources: list[str]
    lines: dict[str, LineState]
    discounts: dict[str, DiscountState]
    events: list[ReturnEvent]
    gross_total: int = 0
    purchase_discount_total: int = 0
    active_discount_total: int = 0
    consumer_paid: int = 0
    merchant_funding: dict[str, int] = field(default_factory=dict)
    platform_funding: int = 0
    total_cash_refund: int = 0
    total_goodwill: int = 0
    total_receivable: int = 0
    total_consumer_makeup: int = 0
    reversed_merchant: dict[str, int] = field(default_factory=dict)
    reversed_platform: int = 0
    retained_gross: int = 0
    retained_cost: dict[str, int] = field(default_factory=dict)
    retained_merchant_revenue: dict[str, int] = field(default_factory=dict)
    receivable_merchant: dict[str, int] = field(default_factory=dict)
    receivable_platform: int = 0
    merchant_profit: dict[str, int] = field(default_factory=dict)
    platform_net_spend: int = 0
    notes: list[str] = field(default_factory=list)
    conservation: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _apply_unit_cost_claims(store: FactStore, basket_id: str, cutoff,
                            lines: dict[str, LineState]) -> list[str]:
    notes = []
    for (b_id, target, target_id), claim in store.effective_claims(cutoff).items():
        if b_id != basket_id or target != "unit_cost":
            continue
        if claim["status"] == "accepted" and target_id in lines:
            old = lines[target_id].unit_cost
            lines[target_id].unit_cost = int(claim["proposed"])
            notes.append(
                f"accepted 异议 {claim['id']} 替换 {target_id}.unit_cost: {old} -> {claim['proposed']}"
            )
        elif claim["status"] != "accepted":
            notes.append(f"pending 异议 {claim['id']} 不是有效更正，unit_cost 保持 {lines[target_id].unit_cost}")
    return notes


def _merge_returns(store: FactStore, basket_id: str, cutoff,
                   lines: dict[str, LineState]) -> tuple[list[dict[str, Any]], list[str]]:
    notes = []
    latest = {
        lid: dict(row)
        for lid, row in store.effective_returns(cutoff).items()
        if lid in lines
    }
    for (b_id, target, target_id), claim in store.effective_claims(cutoff).items():
        if b_id != basket_id or target != "return_qty":
            continue
        # target_id 是退货事实 id，先定位其行商品；更正作用于该行的有效快照
        if not store.has("returns", target_id):
            notes.append(f"异议 {claim['id']} 指向的退货 {target_id} 在资料中不存在，未生效")
            continue
        line_id = store.fact("returns", target_id).payload["line_id"]
        if line_id not in latest:
            notes.append(f"异议 {claim['id']} 指向的退货 {target_id} 在截点不可知，未生效")
            continue
        if claim["status"] == "accepted":
            old = latest[line_id]["return_qty"]
            latest[line_id]["return_qty"] = int(claim["proposed"])
            notes.append(
                f"accepted 异议 {claim['id']} 替换 {target_id}.return_qty（行 {line_id}）: "
                f"{old} -> {claim['proposed']}"
            )
        else:
            notes.append(f"pending 异议 {claim['id']} 不是有效更正，保留原 return_qty")
    return sorted(latest.values(), key=lambda r: parse_ts(r["occurred_at"])), notes


def _split_funding(amount: int, merchant_share: dict[str, float],
                   eligible_merchants: set[str]) -> tuple[dict[str, int], int]:
    """按合同的绝对比例在参与商家间分摊，平台承担余量。

    ``merchant_share`` 的值是该商家承担折扣额的比例（不是待归一化的权重）：
    例如 {m0:0.3, m1:0.2} 表示商家合计承担 50%，其余 50% 由平台承担。
    商家份额按放大后的整数权重一次性分摊，保证商家合计 + 平台 = amount，
    余分按 merchant_id 字典序处理。
    """
    if amount <= 0:
        return {}, 0
    scale = 1_000_000
    weights = {
        m: round(w * scale)
        for m, w in merchant_share.items()
        if m in eligible_merchants and round(w * scale) > 0
    }
    if not weights:
        return {}, amount
    # 商家合计承担额按权重在 scale 基数上分摊；平台拿剩余
    merchant_weight_total = sum(weights.values())
    if merchant_weight_total >= scale:
        # 合同比例合计达到/超过 100%：商家承担全部，按权重归一
        fund = allocate_by_weight(amount, weights)
        return fund, amount - sum(fund.values())
    merchant_amount = amount * merchant_weight_total // scale
    fund = allocate_by_weight(merchant_amount, weights)
    return fund, amount - sum(fund.values())


def _reverse_by_original_funding(ds: DiscountState, amount: int,
                                 basis: str = "remaining") -> tuple[dict[str, int], int]:
    """把一笔冲回拆给商家与平台。

    basis="remaining"（默认）：按各出资方“尚未冲回的净余额”为权重，保证累计
    冲回不超过原出资，用于退货冲回与失效一次性冲回。
    basis="original"：按原出资归属比例，用于待追收权益的归属挂账——待追收是
    失效补贴转换的应收，不是新冲回，此时净余额可能已为零。
    """
    if amount <= 0:
        return {}, 0
    if basis == "original":
        weights = {m: w for m, w in ds.merchant_funding.items() if w > 0}
        platform_weight = ds.platform_funding
    else:
        weights = {
            m: max(0, ds.merchant_funding.get(m, 0) - ds.reversed_merchant.get(m, 0))
            for m in ds.merchant_funding
        }
        platform_weight = max(0, ds.platform_funding - ds.reversed_platform)
        weights = {m: w for m, w in weights.items() if w > 0}
    if platform_weight <= 0 and not weights:
        raise AssertionError("无剩余出资可冲回")
    combined = dict(weights)
    if platform_weight > 0:
        combined["__platform__"] = platform_weight
    part = allocate_by_weight(amount, combined)
    m_part = {m: v for m, v in part.items() if m != "__platform__"}
    return m_part, part.get("__platform__", 0)


def determine_basket(store: FactStore, basket_id: str, cutoff) -> BasketDetermination:
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    basket = store.fact("baskets", basket_id).payload
    line_rows = sorted(
        [r for r in store.rows("lines") if r["basket_id"] == basket_id],
        key=lambda r: r["line_id"],
    )
    lines: dict[str, LineState] = {}
    for r in line_rows:
        lines[r["line_id"]] = LineState(
            line_id=r["line_id"], merchant_id=r["merchant_id"], category=r["category"],
            quantity=r["quantity"], unit_price=r["unit_price"], unit_cost=r["unit_cost"],
            gross=r["unit_price"] * r["quantity"],
        )

    notes = _apply_unit_cost_claims(store, basket_id, cutoff_dt, lines)
    events_raw, rn = _merge_returns(store, basket_id, cutoff_dt, lines)
    notes.extend(rn)

    disc_rows = sorted(
        [r for r in store.rows("discounts") if r["basket_id"] == basket_id],
        key=lambda r: DISCOUNT_ORDER.index(r["kind"]),
    )
    discounts: dict[str, DiscountState] = {}
    remaining = {lid: ls.gross for lid, ls in lines.items()}

    # ---------------- 购买时点：顺序分摊 ---------------- #
    for r in disc_rows:
        ds = DiscountState(
            discount_id=r["id"], kind=r["kind"], eligible_lines=list(r["eligible_lines"]),
            amount=r["amount"], threshold=r["threshold"], merchant_share=dict(r["merchant_share"]),
        )
        eligible_base = sum(
            lines[lid].unit_price * lines[lid].quantity for lid in r["eligible_lines"]
        )
        if r["kind"] == "cross" and eligible_base < r["threshold"]:
            ds.valid_at_purchase = False
            ds.active = False
            ds.invalidated_by = "threshold-at-purchase"
            for lid in r["eligible_lines"]:
                lines[lid].discount_by_kind[r["kind"]] = 0
                lines[lid].reversals_by_kind.setdefault(r["kind"], 0)
            notes.append(
                f"{r['id']} 购买时点适用金额 {eligible_base} < 门槛 {r['threshold']}，全额失效，原出资无需计提"
            )
            discounts[r["kind"]] = ds
            continue

        weights = {lid: remaining[lid] for lid in r["eligible_lines"] if remaining[lid] > 0}
        allocated = allocate_by_weight(r["amount"], weights)
        for lid in r["eligible_lines"]:
            v = allocated.get(lid, 0)
            remaining[lid] -= v
            lines[lid].discount_by_kind[r["kind"]] = v
            lines[lid].reversals_by_kind.setdefault(r["kind"], 0)
        eligible_merchants = {lines[lid].merchant_id for lid in r["eligible_lines"]}
        ds.merchant_funding, ds.platform_funding = _split_funding(
            r["amount"], r["merchant_share"], eligible_merchants
        )
        discounts[r["kind"]] = ds

    # ---------------- 退货事件（按发生时间） ---------------- #
    events: list[ReturnEvent] = []
    prev_cum: dict[str, int] = {}
    for row in events_raw:
        lid = row["line_id"]
        ls = lines[lid]
        cum = min(int(row["return_qty"]), ls.quantity)
        new_qty = max(0, cum - prev_cum.get(lid, 0))
        prev_cum[lid] = cum
        ls.returned_qty = cum
        ls.return_records.append(row["id"])
        ls.goodwill += int(row.get("goodwill") or 0)

        ev = ReturnEvent(
            return_id=row["id"], line_id=lid, qty=new_qty, cumulative_qty=cum,
            goodwill=int(row.get("goodwill") or 0), occurred_at=row["occurred_at"],
            known_at=row["known_at"], confirmed=bool(row["confirmed"]),
            goodwill_cash=int(row.get("goodwill") or 0),
        )
        if new_qty == 0:
            notes.append(f"退货 {row['id']} 无新增退回数量（累计更正快照），不产生现金冲回")
            events.append(ev)
            continue

        # 1) 先判定跨店补贴是否因本次退货失效：若失效，其全额走“失效冲回”，
        #    不再参与按件回冲，避免同一金额被扣两次。
        cross = discounts.get("cross")
        cross_will_invalidate = False
        retained_eligible = None
        if cross is not None and cross.active:
            retained_eligible = sum(
                lines[x].unit_price * lines[x].retained_qty for x in cross.eligible_lines
            )
            cross_will_invalidate = retained_eligible < cross.threshold

        # 2) 按件回冲该行在事件后“仍有效”折扣上享受的份额
        for kind in DISCOUNT_ORDER:
            ds = discounts.get(kind)
            if ds is None or not ds.active:
                ev.discount_reversed[kind] = 0
                continue
            if kind == "cross" and cross_will_invalidate:
                ev.discount_reversed[kind] = 0
                continue
            line_alloc = ls.discount_by_kind.get(kind, 0)
            per_unit = per_unit_split(line_alloc, ls.quantity, lid)
            rev = sum(per_unit[:new_qty])
            ev.discount_reversed[kind] = rev
            ls.reversals_by_kind[kind] = ls.reversals_by_kind.get(kind, 0) + rev
            m_part, p_part = _reverse_by_original_funding(ds, rev)
            for m, v in m_part.items():
                ds.reversed_merchant[m] = ds.reversed_merchant.get(m, 0) + v
            ds.reversed_platform += p_part

        # 3) 商品应退 = 折后单价 × 件数；折后单价只扣事件后仍有效的折扣
        effective_unit_discount = 0
        for kind in DISCOUNT_ORDER:
            if kind == "cross" and cross_will_invalidate:
                continue
            ds = discounts.get(kind)
            if ds is not None and not ds.active:
                continue
            effective_unit_discount += ls.discount_by_kind.get(kind, 0)
        per_unit_price = per_unit_split(ls.gross - effective_unit_discount, ls.quantity, lid)
        ev.gross_refund_due = sum(per_unit_price[:new_qty])

        # 4) 执行跨店补贴失效：剩余未冲回余额一次性按原归属冲回
        if cross_will_invalidate:
            cross.active = False
            cross.invalidated_by = row["id"]
            ev.cross_invalidated = True
            outstanding = cross.amount - cross.reversed_total
            if outstanding < 0:
                raise AssertionError("跨店补贴冲回超过原出资")
            if outstanding > 0:
                m_part, p_part = _reverse_by_original_funding(cross, outstanding)
                for m, v in m_part.items():
                    cross.reversed_merchant[m] = cross.reversed_merchant.get(m, 0) + v
                cross.reversed_platform += p_part
                # 冲回落在本次退货行，供行级对账
                ls.reversals_by_kind["cross"] = (
                    ls.reversals_by_kind.get("cross", 0) + outstanding
                )
                ev.discount_reversed["cross"] = outstanding
            ev.consumer_makeup = cross.amount  # 消费者被收回的全部 cross 补贴
            notes.append(
                f"退货 {row['id']} 后跨店补贴 {cross.discount_id} 剩余适用金额 "
                f"{retained_eligible} < {cross.threshold}，全额失效；消费者应补 {ev.consumer_makeup}"
            )

        # 4) 应补先抵退款，最低零，抵不掉的待追收按失效补贴原归属挂账
        ev.refund_offset = min(ev.gross_refund_due, ev.consumer_makeup)
        ev.cash_refund = ev.gross_refund_due - ev.refund_offset
        ev.receivable = ev.consumer_makeup - ev.refund_offset
        if ev.receivable > 0 and ev.cross_invalidated:
            m_part, p_part = _reverse_by_original_funding(
                cross, ev.receivable, basis="original")
            ev.receivable_split = m_part
            ev.receivable_platform = p_part
        elif ev.receivable > 0:
            ev.receivable_platform = ev.receivable
        events.append(ev)

    det = BasketDetermination(
        basket_id=basket_id, cutoff=str(cutoff_dt), campaign=basket.get("campaign"),
        account_id=basket["account_id"], sources=list(store.sources),
        lines=lines, discounts=discounts, events=events,
    )
    _summarize(det)
    det.notes[:0] = notes
    return det


def _summarize(det: BasketDetermination) -> None:
    lines = det.lines
    det.gross_total = sum(ls.gross for ls in lines.values())
    det.purchase_discount_total = sum(
        ls.discount_by_kind.get(k, 0) for ls in lines.values() for k in DISCOUNT_ORDER
    )
    det.active_discount_total = sum(
        ls.discount_by_kind.get(k, 0)
        for ls in lines.values() for k, ds in det.discounts.items() if ds.active
    )
    det.consumer_paid = det.gross_total - det.purchase_discount_total

    for ds in det.discounts.values():
        for m, v in ds.merchant_funding.items():
            det.merchant_funding[m] = det.merchant_funding.get(m, 0) + v
        det.platform_funding += ds.platform_funding
        for m, v in ds.reversed_merchant.items():
            det.reversed_merchant[m] = det.reversed_merchant.get(m, 0) + v
        det.reversed_platform += ds.reversed_platform

    det.total_cash_refund = sum(ev.cash_refund for ev in det.events)
    det.total_goodwill = sum(ev.goodwill_cash for ev in det.events)
    det.total_receivable = sum(ev.receivable for ev in det.events)
    det.total_consumer_makeup = sum(ev.consumer_makeup for ev in det.events)
    for ev in det.events:
        for m, v in ev.receivable_split.items():
            det.receivable_merchant[m] = det.receivable_merchant.get(m, 0) + v
        det.receivable_platform += ev.receivable_platform

    # 留存商品：优惠前金额、成本、折后已付收入（按件拆分，余分字典序）
    for ls in lines.values():
        det.retained_gross += ls.retained_gross
        det.retained_cost[ls.merchant_id] = (
            det.retained_cost.get(ls.merchant_id, 0) + ls.retained_cost
        )
        if ls.retained_qty:
            unit_discount = sum(ls.discount_by_kind.get(k, 0) for k in DISCOUNT_ORDER)
            per_unit_price = per_unit_split(ls.gross - unit_discount, ls.quantity, ls.line_id)
            det.retained_merchant_revenue[ls.merchant_id] = (
                det.retained_merchant_revenue.get(ls.merchant_id, 0)
                + sum(per_unit_price[:ls.retained_qty])
            )
    for m, v in det.receivable_merchant.items():
        det.retained_merchant_revenue[m] = det.retained_merchant_revenue.get(m, 0) + v

    # 商家利润 = 留存折后收入（含其待追收权益）- 留存成本 - 商家净出资 - goodwill
    merchants = sorted({ls.merchant_id for ls in lines.values()})
    for m in merchants:
        net_funding = det.merchant_funding.get(m, 0) - det.reversed_merchant.get(m, 0)
        goodwill = sum(ls.goodwill for ls in lines.values() if ls.merchant_id == m)
        det.merchant_profit[m] = (
            det.retained_merchant_revenue.get(m, 0)
            - det.retained_cost.get(m, 0)
            - net_funding
            - goodwill
        )
    # 平台净支出（经济口径）= 平台出资 - 平台冲回。
    # 冲回已包含转为待追收的部分：待追收是平台的应收权益，不是新增支出，
    # 单独在 receivable_platform 列示，不能在此重复扣减。
    det.platform_net_spend = det.platform_funding - det.reversed_platform

    # ---------------- 守恒 ---------------- #
    reversed_on_lines = sum(
        ls.reversals_by_kind.get(k, 0) for ls in lines.values() for k in DISCOUNT_ORDER
    )
    refund_basis = sum(ev.gross_refund_due for ev in det.events)
    offset_total = sum(ev.refund_offset for ev in det.events)
    det.conservation = {
        # 消费者支付 + 购买时点折扣 = 优惠前总额
        "purchase_identity":
            det.consumer_paid + det.purchase_discount_total - det.gross_total,
        # 商家出资 + 平台出资 = 购买时点折扣
        "discount_funding_identity":
            sum(det.merchant_funding.values()) + det.platform_funding
            - det.purchase_discount_total,
        # 定义式守恒（逐事件成立，汇总也成立）：
        #   应补 = 抵减 + 待追收；退款 = 应退 - 抵减
        "makeup_identity":
            det.total_consumer_makeup - offset_total - det.total_receivable,
        "refund_identity":
            det.total_cash_refund + offset_total - refund_basis,
        # 冲回（商家+平台）= 各行冲回落点
        "reversal_identity":
            sum(det.reversed_merchant.values()) + det.reversed_platform - reversed_on_lines,
        # 冲回不超过原出资
        "reversal_within_funding":
            min(0, min((ds.amount - ds.reversed_total for ds in det.discounts.values()),
                       default=0)),
        # 非负性守卫：任何一项为负即失败
        "nonnegative_cost": min(0, min((ls.unit_cost for ls in lines.values()), default=0)),
        "nonnegative_retained_qty": min(0, min((ls.retained_qty for ls in lines.values()), default=0)),
    }
    bad = {k: v for k, v in det.conservation.items() if v != 0}
    if bad:
        raise AssertionError(f"篮子 {det.basket_id} 守恒校验失败: {bad}")
