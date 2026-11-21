"""联购优惠经营应用的统一命令行入口。

在 ``python -I`` 隔离模式下运行，所有资料路径显式传入：

    python -I -m app.cli ingest --data data/history.json data/updates.json
    python -I -m app.cli determine --cutoff 2026-07-15T00:00:00+00:00
    python -I -m app.cli economics --basket b0005
    python -I -m app.cli ledger
    python -I -m app.cli analyze
    python -I -m app.cli budget
    python -I -m app.cli trace --basket b0005
    python -I -m app.cli run-all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# -I 模式下 cwd 不在 sys.path，显式加入项目根
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.models import FactStore, parse_ts  # noqa: E402
from app.basket_engine import determine_basket  # noqa: E402
from app.funding import build_ledger, decide_claim  # noqa: E402
from app.analysis import analyze  # noqa: E402
from app.planning import evaluate_budget, accept_offer  # noqa: E402
from app.repository import StateRepository, trace_basket  # noqa: E402

DEFAULT_DATA = ["data/history.json", "data/updates.json"]
DEFAULT_CUTOFF = "2026-07-15T00:00:00+00:00"


def _store(paths: list[str]) -> FactStore:
    return FactStore.load(paths)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_ingest(args) -> None:
    store = _store(args.data)
    summary = {
        "sources": store.sources,
        "tables": {
            table: sum(1 for (t, _) in store.facts if t == table)
            for table in (
                "baskets", "lines", "discounts", "cohort", "observations",
                "returns", "claims", "confirmed_funding", "budgets", "offers",
                "accepted_plans",
            )
        },
    }
    _print(summary)


def cmd_determine(args) -> None:
    store = _store(args.data)
    cutoff = parse_ts(args.cutoff)
    repo = StateRepository(args.state)
    meta = repo.save_run(args.run_id, args.cutoff, store)
    run = repo.load_run(args.run_id)
    if args.basket:
        _print(run["determinations"][args.basket])
    else:
        _print(meta)


def cmd_economics(args) -> None:
    store = _store(args.data)
    det = determine_basket(store, args.basket, parse_ts(args.cutoff))
    _print({
        "basket_id": det.basket_id, "cutoff": det.cutoff, "campaign": det.campaign,
        "gross_total": det.gross_total,
        "consumer_paid": det.consumer_paid,
        "purchase_discount_total": det.purchase_discount_total,
        "active_discount_total": det.active_discount_total,
        "merchant_funding": det.merchant_funding,
        "platform_funding": det.platform_funding,
        "cash_refund": det.total_cash_refund,
        "goodwill": det.total_goodwill,
        "receivable_pending_collection": det.total_receivable,
        "consumer_makeup": det.total_consumer_makeup,
        "reversed_merchant": det.reversed_merchant,
        "reversed_platform": det.reversed_platform,
        "retained_gross": det.retained_gross,
        "retained_cost": det.retained_cost,
        "retained_merchant_revenue": det.retained_merchant_revenue,
        "merchant_profit": det.merchant_profit,
        "platform_net_spend": det.platform_net_spend,
        "events": [vars(e) for e in det.events],
        "conservation": det.conservation,
        "notes": det.notes,
    })


def cmd_ledger(args) -> None:
    store = _store(args.data)
    report = build_ledger(store, parse_ts(args.cutoff),
                          basket_ids=[args.basket] if args.basket else None)
    _print(report.to_dict())


def cmd_claim(args) -> None:
    store = _store(args.data)
    result = decide_claim(store, args.claim_id, args.decision, args.evidence,
                          parse_ts(args.cutoff))
    _print(result)


def cmd_analyze(args) -> None:
    store = _store(args.data)
    report = analyze(store, parse_ts(args.cutoff),
                     caliper=args.caliper, n_bootstrap=args.bootstrap)
    out = report.to_dict()
    if not args.verbose:
        out.pop("per_account", None)
        out.pop("matches", None)
    _print(out)


def cmd_budget(args) -> None:
    store = _store(args.data)
    if args.accept:
        _print(accept_offer(store, parse_ts(args.cutoff), args.accept,
                            also_with=args.with_offer))
    else:
        _print(evaluate_budget(store, parse_ts(args.cutoff)).to_dict())


def cmd_trace(args) -> None:
    store = _store(args.data)
    _print(trace_basket(store, args.basket, parse_ts(args.cutoff)))


def cmd_diff(args) -> None:
    repo = StateRepository(args.state)
    _print(repo.diff_runs(args.old_run, args.new_run))


def cmd_runs(args) -> None:
    repo = StateRepository(args.state)
    _print(repo.list_runs())


def cmd_run_all(args) -> None:
    store = _store(args.data)
    cutoff = parse_ts(args.cutoff)
    repo = StateRepository(args.state)
    run_id = args.run_id
    meta = repo.save_run(run_id, args.cutoff, store)
    ledger = build_ledger(store, cutoff)
    analysis = analyze(store, cutoff, n_bootstrap=args.bootstrap)
    budget = evaluate_budget(store, cutoff)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "run_meta.json": meta,
        "ledger_summary.json": {
            "cutoff": ledger.cutoff,
            "total_current_required": ledger.total_current_required,
            "total_confirmed": ledger.total_confirmed,
            "total_pending_delta": ledger.total_pending_delta,
            "frozen_keys": ledger.frozen_keys,
            "claims": [vars(c) for c in ledger.claims],
        },
        "analysis_summary.json": {k: v for k, v in analysis.to_dict().items()
                                  if k not in ("per_account", "matches")},
        "budget_report.json": budget.to_dict(),
    }
    for name, payload in artifacts.items():
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    _print({"run_id": run_id, "output_dir": str(out_dir),
            "artifacts": list(artifacts), "meta": meta})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="联购优惠留存收益与商家共同出资核定")
    p.add_argument("--data", nargs="+", default=DEFAULT_DATA, help="资料包路径（可多个）")
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="资料可获知截点 ISO 8601")
    p.add_argument("--state", default="state", help="状态目录")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("ingest", help="导入并校验资料包")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("determine", help="重算全部篮子并保存一版核定")
    sp.add_argument("--run-id", default="current")
    sp.add_argument("--basket", help="只打印指定篮子")
    sp.set_defaults(func=cmd_determine)

    sp = sub.add_parser("economics", help="查看单篮四类金额归属与守恒")
    sp.add_argument("--basket", required=True)
    sp.set_defaults(func=cmd_economics)

    sp = sub.add_parser("ledger", help="费用确认台账与争议冻结")
    sp.add_argument("--basket")
    sp.set_defaults(func=cmd_ledger)

    sp = sub.add_parser("claim", help="对异议给出接受/驳回决定与凭据")
    sp.add_argument("--claim-id", required=True)
    sp.add_argument("--decision", choices=["accepted", "rejected"], required=True)
    sp.add_argument("--evidence", required=True)
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("analyze", help="净增收益的可比人群分析")
    sp.add_argument("--caliper", type=float, default=1.0)
    sp.add_argument("--bootstrap", type=int, default=1000)
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("budget", help="方案比较与预算占用核对")
    sp.add_argument("--accept", help="模拟接受指定 offer")
    sp.add_argument("--with-offer", nargs="*", default=[], help="同时接受的其他 offer")
    sp.set_defaults(func=cmd_budget)

    sp = sub.add_parser("trace", help="追溯篮子的事实、合同与争议状态")
    sp.add_argument("--basket", required=True)
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("diff", help="比较两版核定的差额")
    sp.add_argument("--old-run", required=True)
    sp.add_argument("--new-run", required=True)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("runs", help="列出已保存的核定版本")
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("run-all", help="导入→核定→台账→分析→预算，一次产出")
    sp.add_argument("--run-id", default="current")
    sp.add_argument("--output", default="output/current")
    sp.add_argument("--bootstrap", type=int, default=1000)
    sp.set_defaults(func=cmd_run_all)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # 输出被下游（如 head）关闭时安静退出
        sys.exit(0)
