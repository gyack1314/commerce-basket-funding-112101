"""核定结果的版本化持久化与全链路溯源。

state/ 目录保存每次运行的核定快照（不入库真实支付）：

* 每次重算生成新版本，历史版本不删除；
* 已确认出资与新事实重算值之间的差额显式保存，标注待确认状态；
* 任一核定/方案都能反查原篮子、采用的合同折扣、截点时的争议状态与
  后补售后改变了哪部分收益和可用资金。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import FactStore, parse_ts
from .basket_engine import determine_basket
from .funding import build_ledger


class StateRepository:
    def __init__(self, root: str | Path = "state"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)

    def _run_dir(self, run_id: str) -> Path:
        p = self.root / "runs" / run_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_run(self, run_id: str, cutoff: str, store: FactStore,
                 basket_ids: list[str] | None = None) -> dict[str, Any]:
        """生成一版全量核定与费用台账，返回运行摘要。"""
        cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
        if basket_ids is None:
            basket_ids = [r["id"] for r in store.rows("baskets")]
        run_dir = self._run_dir(run_id)
        determinations = {}
        for bid in basket_ids:
            det = determine_basket(store, bid, cutoff_dt)
            determinations[bid] = det.to_dict()
        (run_dir / "determinations.json").write_text(
            json.dumps(determinations, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ledger = build_ledger(store, cutoff_dt, basket_ids)
        (run_dir / "ledger.json").write_text(
            json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        meta = {
            "run_id": run_id, "cutoff": str(cutoff_dt),
            "sources": list(store.sources), "basket_count": len(basket_ids),
        }
        (run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted((self.root / "runs").iterdir()):
            meta = p / "meta.json"
            if meta.exists():
                out.append(json.loads(meta.read_text(encoding="utf-8")))
        return out

    def load_run(self, run_id: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        return {
            "meta": json.loads((run_dir / "meta.json").read_text(encoding="utf-8")),
            "determinations": json.loads(
                (run_dir / "determinations.json").read_text(encoding="utf-8")),
            "ledger": json.loads((run_dir / "ledger.json").read_text(encoding="utf-8")),
        }

    def diff_runs(self, old_run: str, new_run: str) -> dict[str, Any]:
        """两版核定之间的差额：后补售后究竟改变了哪部分收益与资金。"""
        old = self.load_run(old_run)["determinations"]
        new = self.load_run(new_run)["determinations"]
        changed = {}
        for bid in sorted(set(old) | set(new)):
            if bid not in old or bid not in new:
                changed[bid] = {"status": "added_or_removed"}
                continue
            fields = (
                "consumer_paid", "total_cash_refund", "total_goodwill",
                "total_receivable", "platform_net_spend", "active_discount_total",
            )
            delta = {
                f: new[bid][f] - old[bid][f]
                for f in fields if new[bid][f] != old[bid][f]
            }
            mp_old = old[bid]["merchant_profit"]
            mp_new = new[bid]["merchant_profit"]
            profit_delta = {m: mp_new.get(m, 0) - mp_old.get(m, 0)
                            for m in set(mp_old) | set(mp_new)
                            if mp_new.get(m, 0) != mp_old.get(m, 0)}
            if delta or profit_delta:
                changed[bid] = {"field_delta": delta, "merchant_profit_delta": profit_delta,
                                "old_notes": old[bid]["notes"], "new_notes": new[bid]["notes"]}
        return {"old_run": old_run, "new_run": new_run,
                "changed_baskets": len(changed), "details": changed}


def trace_basket(store: FactStore, basket_id: str, cutoff) -> dict[str, Any]:
    """从一笔核定追溯到原篮子、合同折扣、截点争议状态与售后事件。"""
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    det = determine_basket(store, basket_id, cutoff_dt)
    basket_fact = store.fact("baskets", basket_id)
    line_facts = [store.fact("lines", lid).payload for lid in det.lines]
    discount_facts = [store.fact("discounts", ds.discount_id).payload
                      for ds in det.discounts.values()]
    return_facts = []
    for lid in det.lines:
        for (t, _), f in store.facts.items():
            if t == "returns" and f.payload["line_id"] == lid \
                    and (f.known_at is None or f.known_at <= cutoff_dt):
                return_facts.append(f.payload)
    claim_facts = []
    for (t, fid), f in store.facts.items():
        if t == "claims" and f.payload["basket_id"] == basket_id \
                and (f.known_at is None or f.known_at <= cutoff_dt):
            claim_facts.append({"fact_id": fid, **f.payload})
    funding_facts = [
        f.payload for (t, _), f in store.facts.items()
        if t == "confirmed_funding" and f.payload["basket_id"] == basket_id
        and (f.known_at is None or f.known_at <= cutoff_dt)
    ]
    return {
        "basket_id": basket_id,
        "cutoff": str(cutoff_dt),
        "basket_fact": {"source": basket_fact.source, **basket_fact.payload},
        "line_facts": line_facts,
        "discount_contract_facts": discount_facts,
        "return_facts_known_at_cutoff": sorted(return_facts, key=lambda r: r["known_at"]),
        "claim_facts_known_at_cutoff": sorted(claim_facts, key=lambda r: r["known_at"]),
        "confirmed_funding_facts": funding_facts,
        "determination": det.to_dict(),
    }
