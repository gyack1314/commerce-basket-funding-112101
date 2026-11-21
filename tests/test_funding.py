"""费用确认台账：确认差额、争议冻结范围、逐件决定凭据。"""
import unittest

import _helper  # noqa: F401
from _helper import load_store, ts
from app.models import parse_ts
from app.funding import build_ledger, decide_claim


class FundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = load_store()
        cls.cut = ts()

    def test_confirmed_amount_never_silently_replaced(self):
        # 6/20 截点：claim-ok 已接受（r5 退 1 件），fund-1 确认 m1 cross 出资 600
        rep = build_ledger(self.store, parse_ts("2026-06-20T10:00:00+00:00"),
                           basket_ids=["b0005"])
        entry = next(e for e in rep.entries
                     if e.merchant_id == "m1" and e.discount_id == "d5-cross")
        # 退 1 件后 cross 仍失效，当前应值为 0，但已确认 600 保留，差额显式挂账
        self.assertEqual(entry.confirmed_amount, 600)
        self.assertEqual(entry.current_required, 0)
        self.assertEqual(entry.confirmation_delta, 600)
        self.assertEqual(entry.status, "delta-pending-confirmation")

    def test_pending_claim_freezes_only_affected_scope(self):
        rep = build_ledger(self.store, parse_ts("2026-06-20T10:00:00+00:00"),
                           basket_ids=["b0001", "b0005"])
        frozen = set(rep.frozen_keys)
        # unit_cost 争议只冻结 b0001/m1 的费用键
        self.assertIn(("b0001", "m1", "d1-cross"), frozen)
        self.assertIn(("b0001", "m1", "d1-platform"), frozen)
        self.assertNotIn(("b0001", "m0", "d1-cross"), frozen)
        self.assertFalse(any(k[0] == "b0005" for k in frozen))
        # 未冻结条目仍可正常核定
        self.assertTrue(all(not e.frozen for e in rep.entries
                            if (e.basket_id, e.merchant_id, e.discount_id) not in frozen))

    def test_accepted_claim_unfreezes_and_recomputes_basket(self):
        rep = build_ledger(self.store, self.cut, basket_ids=["b0001"])
        self.assertEqual(rep.frozen_keys, [])
        claim = next(c for c in rep.claims if c.claim_id == "claim-open-v2")
        self.assertEqual(claim.status, "accepted")
        self.assertIn("整篮", claim.decision_note)

    def test_return_qty_claim_affects_all_basket_merchants(self):
        rep = build_ledger(self.store, parse_ts("2026-06-20T10:00:00+00:00"),
                           basket_ids=["b0005"])
        claim = next(c for c in rep.claims if c.claim_id == "claim-ok")
        # 退货数量改变全篮冲回基数，影响 m0 与 m1（不只申诉的 m0）
        affected_merchants = {k[1] for k in claim.affected_funding_keys}
        self.assertIn("m0", affected_merchants)
        self.assertIn("m1", affected_merchants)

    def test_decide_claim_requires_evidence_and_returns_record(self):
        rec = decide_claim(self.store, "claim-open", "rejected",
                           "合同成本单价 4592", parse_ts("2026-06-20T10:00:00+00:00"))
        self.assertEqual(rec["decision"], "rejected")
        self.assertEqual(rec["evidence"], "合同成本单价 4592")
        with self.assertRaises(ValueError):
            decide_claim(self.store, "claim-open", "maybe", "x", self.cut)
        with self.assertRaises(KeyError):
            decide_claim(self.store, "missing", "accepted", "x", self.cut)

    def test_aggregate_totals_balance(self):
        rep = build_ledger(self.store, self.cut)
        # 条目级 current_required 之和等于汇总
        for m in ("m0", "m1", "m2"):
            s = sum(e.current_required for e in rep.entries if e.merchant_id == m)
            self.assertEqual(s, rep.total_current_required.get(m, 0))


if __name__ == "__main__":
    unittest.main()
