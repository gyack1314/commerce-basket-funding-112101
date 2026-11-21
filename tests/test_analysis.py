"""收益分析：可识别范围、共同支持、朴素对比偏差、区间与不确定性分离。"""
import unittest

import _helper  # noqa: F401
from _helper import load_store, ts
from app.analysis import analyze, OUTCOMES


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = load_store()
        cls.rep = analyze(cls.store, ts(), n_bootstrap=300)

    def test_web_treated_marked_non_identifiable(self):
        self.assertTrue(any("web" in s for s in self.rep.non_identifiable))
        # web 参加者 210 人无对照
        self.assertEqual(self.rep.composition["web_treated"]["n"], 210)

    def test_identifiable_scope_is_app_only(self):
        self.assertIn("app", self.rep.identifiable_scope)
        self.assertEqual(self.rep.n_treated, 105)
        self.assertEqual(self.rep.n_control, 105)
        self.assertEqual(self.rep.n_matched, 105)

    def test_matching_controls_naive_bias(self):
        # 活动前消费差异巨大，朴素净支出对比会低估活动带来的平台支出以外的变化
        self.assertNotEqual(
            round(self.rep.naive_difference["gross_spend"], 2),
            round(self.rep.matched_ate["gross_spend"], 2),
        )
        # 平台净支出是确定性规则产物：参加组相对对照为正且区间不跨零
        lo, hi = self.rep.bootstrap_ci["platform_net_spend"]
        self.assertGreater(lo, 0)

    def test_uncertain_outcome_carries_interval_and_may_cross_zero(self):
        # 商家毛利 ATE 区间允许跨零：收益推断保留不确定性
        lo, hi = self.rep.bootstrap_ci["basket_margin"]
        self.assertLess(lo, hi)
        # 区间字段齐全
        for k in OUTCOMES:
            self.assertEqual(len(self.rep.bootstrap_ci[k]), 2)

    def test_zero_purchase_accounts_retained(self):
        zeros = [r for r in self.rep.per_account if r["purchase_flag"] == 0]
        self.assertEqual(len(zeros), 60)  # 含零购买，不被丢弃

    def test_limitations_documented(self):
        text = " ".join(self.rep.limitations)
        self.assertIn("不可识别", text)
        self.assertIn("退货", text)
        self.assertIn("费用核对", text)

    def test_statistical_interval_does_not_block_deterministic_ledger(self):
        # 同一截点下，分析区间存在但费用核定仍是确定整数
        from app.funding import build_ledger
        ledger = build_ledger(self.store, ts())
        self.assertTrue(all(
            isinstance(e.current_required, int) for e in ledger.entries
        ))


if __name__ == "__main__":
    unittest.main()
