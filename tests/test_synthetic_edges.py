"""合成最小事实集，覆盖真实资料未触发的边界：待追收、零权重分摊、跨商家隔离。

合成事实不修改 data/ 下的原始资料包，只在内存中构建 FactStore。
"""
import unittest

import _helper  # noqa: F401
from app.models import FactStore, Fact, parse_ts
from app.basket_engine import determine_basket

KNOWN = "2026-05-01T10:00:00+00:00"
RET_KNOWN = "2026-05-10T10:00:00+00:00"


def _fact(table, row, known=KNOWN):
    return Fact(table=table, fid=row["id"], payload=row,
                source="synthetic.json", known_at=parse_ts(known),
                revises=row.get("revises"))


def build_store(basket_rows, line_rows, discount_rows, return_rows=None):
    store = FactStore()
    store.sources = ["synthetic.json"]
    for row in basket_rows:
        store.facts[("baskets", row["id"])] = _fact("baskets", row)
    for row in line_rows:
        store.facts[("lines", row["id"])] = _fact("lines", row)
    for row in discount_rows:
        store.facts[("discounts", row["id"])] = _fact("discounts", row)
    for row in (return_rows or []):
        store.facts[("returns", row["id"])] = _fact("returns", row, row["known_at"])
    return store


class ReceivableTests(unittest.TestCase):
    def test_makeup_exceeding_refund_becomes_receivable_never_negative_refund(self):
        # 单行：售价 10000，店铺折扣 900（商家全担），跨店补贴 8000（平台全担，门槛 0）
        basket = [{"id": "b1", "account_id": "a1", "paid_at": KNOWN, "known_at": KNOWN,
                   "mature_at": "2026-05-31T10:00:00+00:00", "campaign": "spring"}]
        lines = [{"id": "b1-l0", "line_id": "b1-l0", "basket_id": "b1", "merchant_id": "m0",
                  "quantity": 1, "unit_price": 10000, "unit_cost": 5000, "category": "food"}]
        discounts = [
            {"id": "d-shop", "basket_id": "b1", "kind": "shop", "eligible_lines": ["b1-l0"],
             "amount": 900, "threshold": 0, "merchant_share": {"m0": 1.0}},
            # 门槛 8000：购买时适用额 10000 达标；整行退货后留存 0 < 8000 而失效
            {"id": "d-cross", "basket_id": "b1", "kind": "cross", "eligible_lines": ["b1-l0"],
             "amount": 8000, "threshold": 8000, "merchant_share": {}},
        ]
        returns = [{"id": "r1", "line_id": "b1-l0", "return_qty": 1, "confirmed": True,
                    "goodwill": 0, "occurred_at": "2026-05-08T10:00:00+00:00",
                    "known_at": RET_KNOWN}]
        store = build_store(basket, lines, discounts, returns)
        # 购买时点：折后单价 10000-900-8000=1100；退货后 cross 失效
        before = determine_basket(store, "b1", parse_ts("2026-05-02T00:00:00+00:00"))
        self.assertEqual(before.consumer_paid, 1100)
        d = determine_basket(store, "b1", parse_ts(RET_KNOWN))
        ev = d.events[0]
        # 商品应退只扣事件后仍有效的 shop 900：10000-900=9100；应补 8000；现金退款 1100
        self.assertEqual(ev.gross_refund_due, 9100)
        self.assertEqual(ev.consumer_makeup, 8000)
        self.assertEqual(ev.cash_refund, 1100)
        self.assertEqual(ev.receivable, 0)

        # 加大补贴到 9500：购买折后 0；失效后应退只扣 shop=9100，应补 9500，
        # 抵减 9100 后现金退款 0，剩余 400 转待追收
        discounts[1]["amount"] = 9500
        store2 = build_store(basket, lines, discounts, returns)
        d2 = determine_basket(store2, "b1", parse_ts(RET_KNOWN))
        ev2 = d2.events[0]
        self.assertEqual(ev2.gross_refund_due, 9100)
        self.assertEqual(ev2.cash_refund, 0)          # 最低退款为零
        self.assertEqual(ev2.receivable, 400)         # 抵不掉的单列待追收
        self.assertEqual(ev2.receivable_platform, 400)  # 平台全担 -> 平台应收
        self.assertEqual(d2.total_receivable, 400)
        # 待追收不是已收：平台净支出 = 出资 9500 - 冲回 9100 - 应收 400 = 0
        self.assertEqual(d2.platform_funding, 9500)
        self.assertEqual(d2.platform_net_spend, 0)

    def test_uninvolved_merchant_never_burdens_cross(self):
        # 两个适用行商家 m0/m1，另一个不参与的 m2 行；m2 不应承担任何 cross 出资或冲回
        basket = [{"id": "b2", "account_id": "a2", "paid_at": KNOWN, "known_at": KNOWN,
                   "mature_at": "2026-05-31T10:00:00+00:00", "campaign": "spring"}]
        lines = [
            {"id": "b2-l0", "line_id": "b2-l0", "basket_id": "b2", "merchant_id": "m0",
             "quantity": 1, "unit_price": 20000, "unit_cost": 10000, "category": "food"},
            {"id": "b2-l1", "line_id": "b2-l1", "basket_id": "b2", "merchant_id": "m1",
             "quantity": 1, "unit_price": 20000, "unit_cost": 10000, "category": "home"},
            {"id": "b2-l2", "line_id": "b2-l2", "basket_id": "b2", "merchant_id": "m2",
             "quantity": 1, "unit_price": 5000, "unit_cost": 2500, "category": "wear"},
        ]
        discounts = [{
            "id": "d-cross", "basket_id": "b2", "kind": "cross",
            "eligible_lines": ["b2-l0", "b2-l1"], "amount": 4000, "threshold": 0,
            "merchant_share": {"m0": 0.3, "m1": 0.2},
        }]
        store = build_store(basket, lines, discounts)
        d = determine_basket(store, "b2", parse_ts(KNOWN))
        self.assertEqual(set(d.merchant_funding), {"m0", "m1"})
        self.assertEqual(d.merchant_funding.get("m2", 0), 0)
        # l2 不参与 cross，分摊为 0
        self.assertEqual(d.lines["b2-l2"].discount_by_kind.get("cross", 0), 0)
        # 商家出资合计 2000，平台余量 2000
        self.assertEqual(sum(d.merchant_funding.values()), 2000)
        self.assertEqual(d.platform_funding, 2000)


if __name__ == "__main__":
    unittest.main()
