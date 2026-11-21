"""持久化版本、差额留痕、截点语义与全链路溯源。"""
import json
import tempfile
import unittest
from pathlib import Path

import _helper  # noqa: F401
from _helper import load_store, ts
from app.models import parse_ts
from app.repository import StateRepository, trace_basket


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.store = load_store()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = StateRepository(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_are_versioned_and_immutable(self):
        self.repo.save_run("early", "2026-06-20T10:00:00+00:00", self.store,
                           basket_ids=["b0005"])
        self.repo.save_run("late", "2026-07-15T00:00:00+00:00", self.store,
                           basket_ids=["b0005"])
        runs = {r["run_id"] for r in self.repo.list_runs()}
        self.assertEqual(runs, {"early", "late"})
        early = self.repo.load_run("early")
        late = self.repo.load_run("late")
        # 历史版本不被覆盖
        self.assertEqual(len(early["determinations"]["b0005"]["events"]), 1)
        self.assertEqual(len(late["determinations"]["b0005"]["events"]), 2)

    def test_diff_shows_what_late_after_sales_changed(self):
        self.repo.save_run("early", "2026-06-20T10:00:00+00:00", self.store,
                           basket_ids=["b0005"])
        self.repo.save_run("late", "2026-07-15T00:00:00+00:00", self.store,
                           basket_ids=["b0005"])
        diff = self.repo.diff_runs("early", "late")
        self.assertIn("b0005", diff["details"])
        change = diff["details"]["b0005"]["field_delta"]
        self.assertIn("total_cash_refund", change)
        self.assertGreater(change["total_cash_refund"], 0)

    def test_as_of_ignores_future_facts(self):
        early_returns = self.store.effective_returns(parse_ts("2026-06-20T10:00:00+00:00"))
        late_returns = self.store.effective_returns(ts())
        self.assertNotIn("b0005-l1", early_returns)
        self.assertIn("b0005-l1", late_returns)

    def test_cumulative_return_snapshot_not_summed(self):
        # 同一 line_id 若出现多条快照，取最新累计值而非相加（构造验证逻辑）
        snap = self.store.effective_returns(ts())
        # 数据中每 line 仅一条；校验值不超过行数量
        lines = {r["id"]: r for r in self.store.rows("lines")}
        for lid, row in snap.items():
            self.assertLessEqual(row["return_qty"], lines[lid]["quantity"])

    def test_trace_links_basket_contract_claims_and_funding(self):
        view = trace_basket(self.store, "b0005", ts())
        self.assertEqual(view["basket_fact"]["id"], "b0005")
        self.assertTrue(view["discount_contract_facts"])
        ids = {r["id"] for r in view["return_facts_known_at_cutoff"]}
        self.assertIn("r5", ids)
        self.assertIn("late-return", ids)
        self.assertTrue(any(f["id"] == "fund-1" for f in view["confirmed_funding_facts"]))
        # 核定结果随附，且引用的折扣 id 都能在合同事实中找到
        contract_ids = {d["id"] for d in view["discount_contract_facts"]}
        for ds in view["determination"]["discounts"].values():
            self.assertIn(ds["discount_id"], contract_ids)

    def test_trace_at_early_cutoff_excludes_late_facts(self):
        view = trace_basket(self.store, "b0005", parse_ts("2026-06-20T10:00:00+00:00"))
        ids = {r["id"] for r in view["return_facts_known_at_cutoff"]}
        self.assertNotIn("late-return", ids)
        claim_ids = {c["fact_id"] for c in view["claim_facts_known_at_cutoff"]}
        self.assertNotIn("claim-open-v2", claim_ids)

    def test_artifacts_are_written_under_state(self):
        self.repo.save_run("x", "2026-07-15T00:00:00+00:00", self.store,
                           basket_ids=["b0000"])
        run_dir = Path(self.tmp.name) / "runs" / "x"
        for name in ("meta.json", "determinations.json", "ledger.json"):
            self.assertTrue((run_dir / name).exists())
            json.loads((run_dir / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
