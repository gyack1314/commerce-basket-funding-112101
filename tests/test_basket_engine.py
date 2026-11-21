"""篮子核定引擎：顺序分摊、门槛失效、退货冲回、待追收、归属守恒。"""
import unittest

import _helper  # noqa: F401
from _helper import load_store, ts
from app.models import parse_ts
from app.basket_engine import determine_basket, DISCOUNT_ORDER


class BasketEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = load_store()
        cls.cut = ts()

    def test_all_baskets_conserve(self):
        for b in self.store.rows("baskets"):
            d = determine_basket(self.store, b["id"], self.cut)
            self.assertEqual(
                {k: v for k, v in d.conservation.items() if v != 0}, {},
                f"{b['id']} 守恒失败 {d.conservation}",
            )

    def test_purchase_identity_and_funding_split(self):
        d = determine_basket(self.store, "b0001", self.cut)
        self.assertEqual(d.consumer_paid + d.purchase_discount_total, d.gross_total)
        # 店铺折扣 900 全归 m0；cross 3000 商家按绝对比例 m0 30%=900、m1 20%=600，
        # 平台承担余量 1500；平台券 1100 全平台
        self.assertEqual(d.merchant_funding, {"m0": 1800, "m1": 600})
        self.assertEqual(d.platform_funding, 2600)
        # 未参与优惠的 m2 不出资
        self.assertNotIn("m2", d.merchant_funding)

    def test_sequential_allocation_uses_remaining_only(self):
        # 手算 b0001：shop 900 全落 l0；platform 1100 在 l0/l1 剩余额上分摊
        d = determine_basket(self.store, "b0001", self.cut)
        l0, l1 = d.lines["b0001-l0"], d.lines["b0001-l1"]
        self.assertEqual(l0.discount_by_kind["shop"], 900)
        self.assertEqual(
            l0.discount_by_kind["platform"] + l1.discount_by_kind["platform"], 1100
        )
        # cross 3000 只在 l0/l1 的剩余正金额上分摊，l2 不参与
        self.assertEqual(l0.discount_by_kind["cross"]
                         + l1.discount_by_kind["cross"], 3000)
        self.assertEqual(d.lines["b0001-l2"].discount_by_kind.get("cross", 0), 0)

    def test_cross_invalid_at_purchase_when_below_threshold(self):
        d = determine_basket(self.store, "b0002", self.cut)  # 适用额 20800 < 30000
        cross = d.discounts["cross"]
        self.assertFalse(cross.valid_at_purchase)
        self.assertFalse(cross.active)
        self.assertEqual(cross.invalidated_by, "threshold-at-purchase")
        self.assertEqual(cross.merchant_funding, {})
        self.assertEqual(cross.platform_funding, 0)
        self.assertEqual(d.purchase_discount_total, 2000)  # 只剩 shop+platform

    def test_return_invalidates_cross_and_reverses_by_original_funding(self):
        d = determine_basket(self.store, "b0005", self.cut)
        cross = d.discounts["cross"]
        self.assertFalse(cross.active)
        self.assertEqual(cross.invalidated_by, "r5")
        # cross 3000：商家按绝对比例 m0 900/m1 600，平台余量 1500；失效全额冲回
        self.assertEqual(cross.merchant_funding, {"m0": 900, "m1": 600})
        self.assertEqual(cross.platform_funding, 1500)
        self.assertEqual(cross.reversed_merchant, {"m0": 900, "m1": 600})
        self.assertEqual(cross.reversed_platform, 1500)
        # 平台券 1100 按退货件冲回：r5 退 l0 一件 325 + late-return 退 l1 两件 450
        platform = d.discounts["platform"]
        self.assertEqual(platform.reversed_platform, 775)
        # 任何一方累计冲回不超过原出资
        for ds in d.discounts.values():
            self.assertLessEqual(ds.reversed_total, ds.amount)
        # 失效事件：应补 3000 全额抵减商品应退，无待追收，现金退款不为负
        inv_event = next(e for e in d.events if e.cross_invalidated)
        self.assertEqual(inv_event.consumer_makeup, 3000)
        self.assertEqual(inv_event.cash_refund, inv_event.gross_refund_due - 3000)
        self.assertGreaterEqual(inv_event.cash_refund, 0)
        self.assertEqual(inv_event.receivable, 0)
        self.assertEqual(d.total_receivable, 0)

    def test_late_return_changes_retained_revenue(self):
        # 6/20 截点只有 r5；7/15 截点含 late-return（l1 全退）
        early = determine_basket(self.store, "b0005", parse_ts("2026-06-20T10:00:00+00:00"))
        late = determine_basket(self.store, "b0005", self.cut)
        self.assertEqual(len(early.events), 1)
        self.assertEqual(len(late.events), 2)
        self.assertGreater(early.retained_gross, late.retained_gross)
        # l1 在早截点有留存收入，晚截点全退后为 0
        self.assertGreater(early.retained_merchant_revenue.get("m1", 0), 0)
        self.assertEqual(late.retained_merchant_revenue.get("m1", 0), 0)

    def test_goodwill_is_separate_merchant_cash_cost(self):
        d = determine_basket(self.store, "b0000", self.cut)  # 非活动篮，r0 goodwill 200
        self.assertEqual(d.total_goodwill, 200)
        # goodwill 不进入折扣、不进入退款抵减
        self.assertEqual(d.total_consumer_makeup, 0)
        # m0 利润含 -200 补偿
        self.assertEqual(d.merchant_profit["m0"], -200)

    def test_no_negative_cost_and_no_uninvolved_merchant_burden(self):
        for b in self.store.rows("baskets"):
            d = determine_basket(self.store, b["id"], self.cut)
            for ls in d.lines.values():
                self.assertGreaterEqual(ls.unit_cost, 0)
                self.assertGreaterEqual(ls.retained_qty, 0)
            # m2 从不参与任何折扣的 merchant_share，故其净出资恒为 0
            self.assertEqual(d.merchant_funding.get("m2", 0), 0)
            self.assertEqual(d.reversed_merchant.get("m2", 0), 0)

    def test_return_qty_claim_accepted_changes_basis(self):
        # claim-ok 把 r5 累计退货从 2 改为 1（6/17 获知）
        before = determine_basket(self.store, "b0005", parse_ts("2026-06-16T10:00:00+00:00"))
        after = determine_basket(self.store, "b0005", parse_ts("2026-06-20T10:00:00+00:00"))
        self.assertEqual(before.lines["b0005-l0"].returned_qty, 2)
        self.assertEqual(after.lines["b0005-l0"].returned_qty, 1)
        # 留一件后 l0 仍有留存收入
        self.assertGreater(after.retained_gross, before.retained_gross)
        self.assertTrue(any("claim-ok" in n for n in after.notes))

    def test_pending_claim_does_not_change_fact(self):
        d = determine_basket(self.store, "b0001", parse_ts("2026-06-20T10:00:00+00:00"))
        self.assertEqual(d.lines["b0001-l1"].unit_cost, 4592)
        d2 = determine_basket(self.store, "b0001", self.cut)
        self.assertEqual(d2.lines["b0001-l1"].unit_cost, 9000)  # v2 accepted 后生效

    def test_non_campaign_basket_has_no_discounts(self):
        d = determine_basket(self.store, "b0000", self.cut)
        self.assertEqual(d.discounts, {})
        self.assertEqual(d.consumer_paid, d.gross_total)
        self.assertEqual(d.platform_funding, 0)

    def test_reversal_does_not_exceed_original_funding(self):
        for b in self.store.rows("baskets"):
            d = determine_basket(self.store, b["id"], self.cut)
            for ds in d.discounts.values():
                self.assertLessEqual(ds.reversed_total, ds.amount)


if __name__ == "__main__":
    unittest.main()
