"""预算与方案比较：争议预留、历史支持、独立与合并缺口。"""
import unittest

import _helper  # noqa: F401
from _helper import load_store, ts
from app.planning import evaluate_budget, accept_offer


class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = load_store()
        cls.rep = evaluate_budget(cls.store, ts())

    def test_disputed_and_committed_excluded_from_usable(self):
        # 200000 - 30000(争议) - 90000(已接受方案) = 80000
        self.assertEqual(self.rep.cash_available, 200000)
        self.assertEqual(self.rep.disputed_reserve, 30000)
        self.assertEqual(self.rep.already_committed, 90000)
        self.assertEqual(self.rep.usable, 80000)

    def test_offer_budget_exceeds_usable_even_standalone(self):
        for e in self.rep.evaluations:
            self.assertFalse(e.standalone_feasible)
            self.assertEqual(e.standalone_gap, e.platform_budget - 80000)

    def test_joint_acceptance_reports_actual_gap(self):
        joint = self.rep.joint_acceptance
        self.assertFalse(joint["all_jointly_feasible"])
        # 两方案 250000 + 已占用 90000 - 可用池 170000 = 170000 缺口
        self.assertEqual(joint["actual_gap_if_accepted_together"], 170000)

    def test_historical_support_scope(self):
        low = next(e for e in self.rep.evaluations if e.offer_id == "next-low")
        high = next(e for e in self.rep.evaluations if e.offer_id == "next-high")
        self.assertFalse(low.support.threshold_supported)  # 25000 未观察过
        self.assertTrue(high.support.threshold_supported)  # 30000 有历史
        self.assertTrue(all(e.support.categories_supported for e in self.rep.evaluations))
        self.assertTrue(any("退款率" in e.support.refund_behavior_note
                            for e in self.rep.evaluations))

    def test_accept_returns_gap_not_silent_overspend(self):
        rec = accept_offer(self.store, ts(), "next-low", also_with=["next-high"])
        self.assertEqual(rec["decision"], "rejected-budget-gap")
        self.assertEqual(rec["actual_gap"], 170000)
        single = accept_offer(self.store, ts(), "next-high")
        self.assertEqual(single["actual_gap"], 50000)
        self.assertEqual(single["decision"], "rejected-budget-gap")

    def test_trace_refs_point_to_facts(self):
        self.assertEqual(self.rep.trace_refs["budget_fact"], "platform")
        self.assertIn("other-event", self.rep.trace_refs["accepted_plan_facts"])


if __name__ == "__main__":
    unittest.main()
