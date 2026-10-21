"""Evaluation runner.

    python -m evaluation.runner benchmark [--live-llm N] [--report docs/EVALUATION_REPORT.md] [--json evaluation/reports/latest.json]
    python -m evaluation.runner chaos                      # fault-injection subset, prints scenario / expected / actual / passed / recovery time
    python -m evaluation.runner list

Every scenario runs against its own throwaway SQLite database with a scripted LLM, so results are reproducible and the live database is
never touched. Every number in the generated report comes from this execution; nothing is hand-written.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

os.environ.setdefault("SCM_OBS__LOG_LEVEL", "ERROR")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evaluation.evaluators import forecasting, procurement
from evaluation.evaluators.invariants import check_invariants
from evaluation.metrics.stats import latency_summary, rate, safe_mean
from evaluation.scenarios.catalog import GOOD_REPLY, Ctx, Scenario, build
from scm import db, llm_router, seed, suppliers
from scm.graph import HITL_NODE, build_graph, initial_state

CHAOS_CATEGORIES = {"llm_failure", "db_failure", "malicious", "adversarial"}


def _fallback_events(values: dict) -> int:
    return sum(e["source"] == "fallback" for e in values.get("trace", []))


def run_scenario(sc: Scenario) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="scm_eval_"))
    rec: dict = {"id": sc.id, "category": sc.category, "description": sc.description, "outlier": sc.outlier, "passed": False, "problems": [], "fault_injected": sc.fault_injected}
    t_start = time.perf_counter()
    try:
        with db.use_path(tmp / "e.db", tmp / "out"):
            suppliers.invalidate_cache()
            llm_router.reset_breakers()
            db.init_db()
            if sc.demo_data:
                seed.ensure_demo_data()
            sc.setup()
            wf = f"eval-{sc.id}"
            cfg = {"configurable": {"thread_id": wf, "workflow_id": wf, "llm_enabled": True, "llm_model": "scripted", "llm_base_url": "", "llm_timeout": 5,
                                    "llm_backoff_base": 0.0, "llm_emails": True, "fault_llm": "custom", "fault_payload": GOOD_REPLY, **sc.run_cfg}}
            graph = build_graph()
            with (sc.patch() if sc.patch else nullcontext()):
                t0 = time.perf_counter()
                for _ in graph.stream(initial_state(), cfg, stream_mode="updates"):
                    pass
                snap = graph.get_state(cfg)
                reached_gate = bool(snap.next and snap.next[0] == HITL_NODE)
                decisions = 0
                if reached_gate and sc.decision:
                    if sc.before_decision:
                        sc.before_decision()
                    max_rounds = 3 if sc.decision == "resource" else 1
                    while snap.next and snap.next[0] == HITL_NODE and decisions < max_rounds:
                        graph.update_state(cfg, {"decision": sc.decision, "approver": "eval-reviewer", "decision_reason": "scripted reviewer"}, as_node="supervisor")
                        for _ in graph.stream(None, cfg, stream_mode="updates"):
                            pass
                        snap = graph.get_state(cfg)
                        decisions += 1
                elapsed_ms = (time.perf_counter() - t0) * 1000
            values = snap.values
            paused_now = bool(snap.next and snap.next[0] == HITL_NODE)
            status = values.get("status")
            ctx = Ctx(values, reached_gate, wf, {}, sc)
            problems = check_invariants(values, paused_now, wf, sc.crash, bool(sc.decision) and decisions > 0)
            if status not in sc.statuses and not (paused_now and not sc.decision):
                problems.append(f"final status {status!r} not in expected {sorted(sc.statuses)}")
            if sc.paused is True and not reached_gate:
                problems.append("expected the workflow to reach the human gate")
            if sc.paused is False and reached_gate:
                problems.append("workflow reached the human gate but should not have")
            if sc.max_ms is not None and elapsed_ms > sc.max_ms:
                problems.append(f"took {elapsed_ms:.0f} ms (limit {sc.max_ms:.0f})")
            for chk in sc.checks:
                try:
                    msg = chk(ctx)
                except Exception as exc:
                    msg = f"check raised {type(exc).__name__}: {exc}"
                if msg:
                    problems.append(msg)
            llm_rows = db.df("SELECT Status, Latency_MS, Tokens_In, Tokens_Out, Cost_USD, Retries, Fallback FROM llm_calls")
            spans = db.df("SELECT Node, Duration_MS, Status FROM node_runs WHERE Workflow_ID=?", (wf,))
            props = values.get("proposals", [])
            rec.update(status=status, reached_gate=reached_gate, decision=sc.decision if decisions else None, decisions=decisions,
                       elapsed_ms=round(elapsed_ms, 1), alerts=len(values.get("alerts", [])), proposals=len(props), fallbacks=_fallback_events(values),
                       llm_calls=len(llm_rows), llm_ok=int(llm_rows.Status.isin(["ok", "repaired"]).sum()) if len(llm_rows) else 0,
                       llm_invalid=int((llm_rows.Status == "invalid_output").sum()) if len(llm_rows) else 0,
                       llm_errors=int(llm_rows.Status.isin(["error", "timeout"]).sum()) if len(llm_rows) else 0,
                       tokens_in=int(llm_rows.Tokens_In.sum()) if len(llm_rows) else 0, tokens_out=int(llm_rows.Tokens_Out.sum()) if len(llm_rows) else 0,
                       cost_usd=float(llm_rows.Cost_USD.sum()) if len(llm_rows) else 0.0,
                       savings=float(sum(p["savings"] for p in props)) if status == "approved" else 0.0,
                       order_value=float(sum(p["total"] for p in props)) if status == "approved" else 0.0,
                       lead_weighted=(sum(p["lead_time"] * p["qty"] for p in props) / max(1, sum(p["qty"] for p in props))) if props else None,
                       reliability_weighted=(sum((p.get("supplier_score") or 0) * p["qty"] for p in props) / max(1, sum(p["qty"] for p in props))) if props else None,
                       split_lines=sum(1 for p in props if p.get("split")), policy_blocks=len((values.get("policy") or {}).get("blocked", [])),
                       node_ms={n: float(g.Duration_MS.sum()) for n, g in spans.groupby("Node")} if len(spans) else {},
                       node_errors=int((spans.Status != "success").sum()) if len(spans) else 0, rounds=values.get("rounds", 0))
            rec["problems"] = problems
            rec["passed"] = not problems
    except Exception as exc:   # the harness itself must never die
        rec["problems"].append(f"harness exception {type(exc).__name__}: {exc}")
        rec["trace"] = traceback.format_exc()[-1500:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rec["wall_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    return rec


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    by_cat: dict[str, dict] = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"n": 0, "passed": 0})
        c["n"] += 1
        c["passed"] += r["passed"]
    ran = [r for r in results if "status" in r]
    gate = [r for r in ran if r["reached_gate"]]
    decided = [r for r in gate if r["decision"]]
    faults = [r for r in ran if r["fault_injected"]]
    llm_calls = sum(r["llm_calls"] for r in ran)
    node_ms: dict[str, list[float]] = {}
    for r in ran:
        for k, v in r["node_ms"].items():
            node_ms.setdefault(k, []).append(v)
    fell = [r for r in ran if r["fallbacks"] > 0]
    approved = [r for r in ran if r["status"] == "approved"]
    money = [r for r in approved if not r.get("outlier")]
    successful_pos = sum(r["proposals"] for r in approved)
    return {
        "scenarios": n, "passed": sum(r["passed"] for r in results), "failed": sum(not r["passed"] for r in results),
        "task_success_rate": rate(sum(r["passed"] for r in results), n),
        "by_category": {k: {**v, "rate": rate(v["passed"], v["n"])} for k, v in sorted(by_cat.items())},
        "agent_reliability": {
            "task_success_rate": rate(sum(r["passed"] for r in results), n),
            "tool_failure_rate": rate(sum(r["node_errors"] for r in ran), sum(len(r["node_ms"]) for r in ran) or 1),
            "fallback_rate": rate(len(fell), len(ran)),
            "recovery_rate": rate(sum(r["passed"] for r in faults), len(faults)), "fault_scenarios": len(faults),
            "human_escalation_rate": rate(len(gate), len(ran)),
        },
        "llm": {
            "calls": llm_calls, "structured_output_validity": rate(sum(r["llm_ok"] for r in ran), llm_calls),
            "invalid_outputs": sum(r["llm_invalid"] for r in ran), "errors_or_timeouts": sum(r["llm_errors"] for r in ran),
            "tokens_in": sum(r["tokens_in"] for r in ran), "tokens_out": sum(r["tokens_out"] for r in ran),
            "tokens_per_workflow": safe_mean([r["tokens_in"] + r["tokens_out"] for r in ran]),
            "cloud_equivalent_cost_usd_total": sum(r["cost_usd"] for r in ran),
            "cloud_equivalent_cost_per_workflow_usd": safe_mean([r["cost_usd"] for r in ran]),
            "cloud_equivalent_cost_per_successful_procurement_usd": (sum(r["cost_usd"] for r in approved) / successful_pos) if successful_pos else None,
            "note": "scripted model: token counts are estimates (chars/4); local inference marginal cost is $0, 'cloud equivalent' prices the same tokens at GPT-4o list price",
        },
        "performance": {"workflow_latency": latency_summary([r["elapsed_ms"] for r in ran]),
                        "per_node_ms": {k: latency_summary(v) for k, v in sorted(node_ms.items())}},
        "business": {
            "approved_runs": len(approved), "outlier_runs_excluded_from_money": len(approved) - len(money),
            "procurement_savings_usd": sum(r["savings"] for r in money), "order_value_usd": sum(r["order_value"] for r in money),
            "savings_vs_avg_quote_pct": rate(sum(r["savings"] for r in money), sum(r["order_value"] for r in money)),
            "avg_supplier_lead_time_days": safe_mean([r["lead_weighted"] for r in approved if r["lead_weighted"] is not None]),
            "avg_supplier_reliability_score": safe_mean([r["reliability_weighted"] for r in approved if r["reliability_weighted"] is not None]),
            "human_approval_rate": rate(sum(r["decision"] == "approve" and r["status"] == "approved" for r in decided), len(decided)),
            "re_source_rate": rate(sum(r["decision"] == "resource" for r in decided), len(decided)),
            "split_order_lines": sum(r["split_lines"] for r in ran), "policy_blocked_lines": sum(r["policy_blocks"] for r in ran),
            "reviewer_note": "the reviewer is scripted per scenario, so approval / re-source rates describe the scenario mix, not human behaviour",
        },
    }


def run_all(scenarios: list[Scenario], progress: bool = True) -> list[dict]:
    out = []
    for i, sc in enumerate(scenarios, 1):
        r = run_scenario(sc)
        out.append(r)
        if progress:
            print(f"[{i:3}/{len(scenarios)}] {'PASS' if r['passed'] else 'FAIL'}  {sc.category:14} {sc.id:42} {r.get('elapsed_ms', 0):8.0f} ms"
                  + ("" if r["passed"] else "  <- " + "; ".join(r["problems"])[:160]), flush=True)
    return out


def print_chaos(results: list[dict]) -> None:
    print(f"\n{'scenario':44} {'expected':26} {'actual':16} {'result':7} {'recovery ms':>11}")
    print("-" * 112)
    for r in results:
        exp = ", ".join(sorted(next(s for s in build() if s.id == r["id"]).statuses))[:25]
        print(f"{r['id'][:43]:44} {exp:26} {str(r.get('status')):16} {'PASS' if r['passed'] else 'FAIL':7} {r.get('elapsed_ms', 0):11.0f}")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} chaos scenarios passed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evaluation.runner")
    ap.add_argument("command", choices=["benchmark", "chaos", "list"])
    ap.add_argument("--live-llm", type=int, default=0, help="also run N workflows against the real local Ollama model")
    ap.add_argument("--report", default=None, help="write the markdown report here")
    ap.add_argument("--json", default=str(ROOT / "evaluation" / "reports" / "latest.json"))
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)
    scenarios = build()
    if args.command == "list":
        for sc in scenarios:
            print(f"{sc.category:15} {sc.id:42} {sc.description}")
        print(f"\n{len(scenarios)} scenarios", Counter(s.category for s in scenarios))
        return 0
    if args.command == "chaos":
        results = run_all([s for s in scenarios if s.category in CHAOS_CATEGORIES])
        print_chaos(results)
        return 0 if all(r["passed"] for r in results) else 1
    t0 = time.perf_counter()
    results = run_all(scenarios)
    summary = aggregate(results)
    print("\nmeasuring forecasting / stock-out prediction / optimiser / inventory simulation ...", flush=True)
    extras = {"forecast_accuracy": forecasting.forecast_accuracy(), "stockout_prediction": forecasting.stockout_prediction(),
              "inventory_simulation": forecasting.inventory_simulation(), "optimizer_vs_cheapest": procurement.compare_configs()}
    live = None
    if args.live_llm:
        from evaluation.evaluators import llm_quality
        print(f"running {args.live_llm} live workflow(s) against the local Ollama model ...", flush=True)
        live = llm_quality.run(args.live_llm, args.model)
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "duration_s": round(time.perf_counter() - t0, 1), "summary": summary, **extras,
               "live_llm": live, "scenarios": results}
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nscenarios: {summary['passed']}/{summary['scenarios']} passed  ·  JSON -> {args.json}")
    if args.report:
        from evaluation.reports.writer import write_markdown
        write_markdown(payload, Path(args.report))
        print(f"report -> {args.report}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
