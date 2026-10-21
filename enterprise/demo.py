"""End-to-end commercial demo on SYNTHETIC data:  python -m enterprise.demo  (`make demo`).

One real run through the whole lifecycle: tenant -> data -> onboarding -> before/after comparison -> what-if -> workflow -> tiered approval ->
mock-ERP dispatch -> decision trace -> ROI -> usage -> reports. Every number printed is computed live from the engine; nothing is canned.
Everything is labelled DEMO / SYNTHETIC DATA: the tenant is a demo tenant (it can never be switched to production), the ERP is the in-repo mock.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BANNER = "=" * 100
TENANT = "demo-retail"


class Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def h(self, title: str) -> None:
        self("")
        self(BANNER)
        self(title)
        self(BANNER)


def money(x) -> str:
    return "n/a" if x is None else f"{x:,.0f}"


def pct(x, d=1) -> str:
    return "n/a" if x is None else f"{100 * x:.{d}f}%"


def run(out: Out, data_dir: Path, scale: float, seed: int, quick: bool) -> dict:
    os.environ.update({"SCM_DATA_DIR": str(data_dir), "SCM_CONTROL_DB": str(data_dir / "control.db"), "SCM_ENV": "development", "SCM_ALLOW_PRIVATE_URLS": "1",
                       "SCM_OBS__LOG_LEVEL": "ERROR", "SCM_LLM_ENABLED": os.environ.get("SCM_LLM_ENABLED", "0")})
    from enterprise import comparison, controlplane, impact, onboarding, profiles, reports, roi, secrets_store, simulation, tenancy, trace, usage, whatif
    os.environ.setdefault("SCM_SECRET_KEY", secrets_store.generate_secret_key())
    from enterprise import outbound
    from enterprise.approvals import ApprovalService
    from enterprise.hooks import install_all
    from enterprise.integrations import dispatch, registry
    from scm import db
    from scm.security import PermissionDenied, Principal, Role
    from scm.services import Conflict, WorkflowService
    db.DB_PATH, db.OUTBOX = data_dir / "default.db", data_dir / "default_outbox"
    controlplane.reset()
    tenancy.forget_workspaces()
    install_all()
    dispatch.install_hooks()
    outbound.install_event_bridge()
    summary: dict = {}

    out(BANNER)
    out("  DEMO  /  SYNTHETIC DATA   -   generated data, an in-repo MOCK ERP, no customer information")
    out("  Every figure below is computed live. Nothing here is a customer result.")
    out(BANNER)

    # 1 -- tenant and data
    out.h("1. Tenant and data   [DEMO tenant, SYNTHETIC retailer profile]")
    tenancy.provision(TENANT, "Demo Retail Co. (synthetic)", "professional", "SANDBOX", is_demo=True)
    ds = profiles.generate("retailer", seed, scale)
    with tenancy.scope(TENANT):
        loaded = profiles.load_into_current_tenant(ds)
        out(f"  loaded: {loaded}")
        gate = onboarding.data_quality_gate()
        out(f"  data-quality gate: {gate.get('status', gate)}")

        # 2 -- onboarding
        out.h("2. Onboarding (stateful, each step verified from real state)")
        onboarding.create_organization("Demo Retail Co. (synthetic)", "US", "USD", "demo")
        onboarding.confirm_policies("demo")
        sim = onboarding.run_simulation("demo")
        out(f"  simulation step: {sim.get('status', 'done')}")
        try:
            onboarding.acknowledge_review("demo", "demo run")
        except Exception as exc:
            out(f"  review step: {exc}")
        st = onboarding.status()
        for s in st["steps"]:
            out(f"  [{s['status']:<11}] {s['step']}")
        out("  production mode: NOT enabled (a demo tenant can never be promoted to production)")

        # 3 -- risk now
        out.h("3. Inventory risk today   [OBSERVED on synthetic data]")
        from scm.dashboard import inventory_risk
        risky = [r for r in inventory_risk() if r["risk_level"] in ("HIGH", "CRITICAL")]
        out(f"  {len(risky)} of {len(ds.skus) if hasattr(ds, 'skus') else '?'} SKUs at HIGH/CRITICAL stock-out risk")
        for r in risky[:5]:
            out(f"    {r['Item_ID']:<10} {r.get('Item_Name', ''):<26} risk={r['risk_level']:<8} p(stock-out)={pct(r.get('stockout_probability'))}")

        # 4 -- before vs after
        out.h("4. BEFORE vs AFTER   [SIMULATED back-test on the tenant's own history; same demand, same suppliers]")
        bt = impact.inventory_kpis(True, simulation.SimConfig(eval_days=45 if quick else 60))["backtest"]
        arms = bt["arms"]
        out(f"  provenance: {bt['provenance']} (common random numbers; no look-ahead)  window: {bt['eval_days']} days")
        out(f"  {'metric':<30}{'manual buyer':>14}{'rule-based':>14}{'AI':>14}{'AI (lean)':>14}")
        rows = [("total cost (purchase+hold+stockout)", "total_cost", money), ("stock-out events", "stockout_events", str), ("stock-out days", "stockout_days", str),
                ("fill rate", "fill_rate", pct), ("inventory turnover (annualised)", "turnover_annualized", lambda v: f"{v:.1f}"), ("safety-stock adherence", "safety_stock_adherence", pct),
                ("gross profit", "gross_profit", money), ("manual touches (assumption-based)", "manual_touches", str)]
        for label, key, f in rows:
            out(f"  {label:<30}{f(arms['manual'][key]):>14}{f(arms['rule_based'][key]):>14}{f(arms['ai'][key]):>14}{f(arms['ai_lean'][key]):>14}")
        summary["backtest"] = {k: {m: arms[k][m] for m in ("total_cost", "stockout_events", "fill_rate", "gross_profit")} for k in arms}
        out("  Reading: differences come from the policy only. This is ONE seed of a simulation of synthetic history - it can favour either side; see 4c for the 20 pre-registered runs.")
        out("  'AI' keeps safety stock for service; 'AI (lean)' orders to forecast demand (less inventory). Neither is guaranteed to beat a manual buyer, as 4c shows.")

        out.h("4b. Procurement decision vs a buyer taking the lowest sticker price   [OBSERVED on current data]")
        cmp_ = comparison.baseline_vs_optimized()
        out(f"  baseline: {cmp_['baseline_definition']}")
        out(f"  SKUs compared {cmp_['skus_compared']}: baseline sticker cost {money(cmp_['baseline_cost'])}, optimised cost {money(cmp_['optimized_cost'])}")
        out(f"  baseline effective cost after the units it fails to cover and constraint violations: {money(cmp_['baseline_effective_cost'])}")
        out(f"  baseline orders violating a constraint: {cmp_['baseline_orders_violating_a_constraint']}, baseline shortfall units: {cmp_['baseline_shortfall_units']}")
        out("  (a negative 'savings' on sticker price is reported as-is: the optimiser buys what is needed, from suppliers that can deliver, at a higher unit price when it must)")

        out.h("4c. Across the 20 PRE-REGISTERED synthetic runs (4 profiles x 5 seeds)   [SIMULATED; protocol hash-locked before running]")
        exp_path = ROOT / "evaluation" / "reports" / "experiment.json"
        if exp_path.exists():
            import json as _json
            ex = _json.loads(exp_path.read_text(encoding="utf-8"))
            out(f"  protocol sha256 {ex['protocol_sha256'][:16]}...  units={ex['units']}  inclusion rule: {ex['protocol']['inclusion_rule']}")
            for cmpname in ("ai_lean_vs_manual", "ai_vs_manual", "ai_lean_vs_rule_based"):
                d = ex["paired_deltas"][cmpname]
                better = ex["units_where_first_arm_is_better"][cmpname]
                out(f"  {cmpname:<22} gross profit delta {money(d['gross_profit']['mean']):>9} (95% CI {money(d['gross_profit']['low'])} .. {money(d['gross_profit']['high'])}); "
                    f"better in {better['gross_profit']}/{ex['units']} runs on gross profit, {better['stockout_days']}/{ex['units']} on stock-out days")
            out("  Honest summary: results are mixed by arm and metric (see the CIs); they are simulated on synthetic data and are not customer outcomes.")
        else:
            out("  (run `make evaluate` to generate evaluation/reports/experiment.json)")

        # 5 -- what-if
        out.h("5. What-if   [deterministic; same input -> same output]")
        w = whatif.compare(whatif.load_snapshot(), whatif.Scenario(name="demand +30%", demand_multiplier=1.3))
        out(f"  baseline : order {w['base']['funded_qty']} units for {money(w['base']['expected_cost'])}, critical SKUs {w['base']['critical_risk_skus']}")
        out(f"  +30% demand: order {w['result']['funded_qty']} units for {money(w['result']['expected_cost'])}, critical SKUs {w['result']['critical_risk_skus']}")
        summary["whatif"] = {"base_cost": w["base"]["expected_cost"], "scenario_cost": w["result"]["expected_cost"]}

        # 6 -- workflow + approval + ERP
        out.h("6. Workflow -> tiered human approval -> mock ERP   [LangGraph engine, real execution]")
        registry.create("erp_mock", "Mock ERP (not SAP/Oracle/...)", {"push_on_approve": True}, None, "demo")
        svc = WorkflowService()
        t0 = time.perf_counter()
        runr, _ = svc.start(Principal("demo-operator", Role.OPERATOR, TENANT))
        wf = runr["workflow_id"]
        gate_run = svc.wait(wf, 300)
        out(f"  workflow {wf[:12]}... reached '{gate_run['status']}' in {time.perf_counter() - t0:.1f}s; {len((gate_run['result'] or {}).get('proposals', []))} purchase proposals")
        for p in (gate_run["result"] or {}).get("proposals", [])[:5]:
            out(f"    {p['po_id']}  {p['Item_ID']:<9} {p['Vendor_Name']:<22} qty={p['qty']:<6} unit={p['unit_price']:<8} total={money(p['total'])}  lead={p['lead_time']}d  risk={p['risk_level']}")
        svc_a = ApprovalService(svc)
        out(f"  approval chain: {[(s['step'], s['required_level'], s['status']) for s in svc_a.steps(wf)]}")
        crew = [Principal("approver-1", Role.APPROVER, TENANT), Principal("approver-2", Role.APPROVER, TENANT), Principal("tenant-admin", Role.TENANT_ADMIN, TENANT)]
        for _ in range(6):
            if svc.get(wf)["status"] != "AWAITING_APPROVAL":
                break
            for who in crew:
                try:
                    svc_a.act(who, wf, "approve", "demo approval")
                    out(f"  step approved by {who.actor} ({who.role.value})")
                    break
                except (PermissionDenied, Conflict, Exception) as exc:
                    out(f"  {who.actor} could not act: {str(exc)[:70]}")
        done = svc.wait(wf, 300)
        out(f"  final: status={done['status']} stage={done['stage']}")
        rows = dispatch.status(wf)
        out(f"  ERP dispatch (MOCK): {[(r['po_id'], r['status'], r['external_id']) for r in rows][:6]}")
        summary["workflow"] = {"status": done["status"], "erp_pushed": sum(r["status"] == "SENT" for r in rows)}

        # 7 -- trace + explanations
        out.h("7. Decision trace and explanations   [every sentence generated from stored data; nothing invented]")
        tr = trace.load(wf)
        if tr:
            out(f"  trace sections: {sorted(tr)}")
            for e in trace.explain_all(tr)[:2]:
                out(f"  {e['sku']}: {e['headline']}")
                for reason in e["reasons"][:3]:
                    out(f"      - {reason}")
        svc.shutdown()

        # 8 -- ROI
        out.h("8. ROI   [SIMULATED benefit, ILLUSTRATIVE cost assumptions - replace them with your own]")
        a = roi.RoiAssumptions(annual_platform_license=24000, annual_infrastructure=6000, annual_integration_and_support=10000, workflows_per_year=52, realization_factor=0.5)
        r = roi.compute(arms["manual"], arms["ai_lean"], a, bt["provenance"], "SYNTHETIC")
        out(f"  window benefit (simulated): {money(r['window']['gross_profit_difference_in_window'])} over {r['window']['days']} days")
        for c in r["components"]:
            out(f"    {c['component']:<34} annual {money(c['annual_value']):>12}   [{c['provenance']}]  {c['formula'][:60]}")
        out(f"  annual benefit after {a.realization_factor:.0%} realisation haircut: {money(r['annual_benefit'])}; assumed annual cost {money(r['annual_cost'])}; ROI {r['annual_roi']}")
        out(f"  caveat: {r['caveat']}")
        out("  The licence / infrastructure / support figures above are PLACEHOLDERS for this demo, not prices.")
        summary["roi"] = {"annual_benefit": r["annual_benefit"], "annual_roi": r["annual_roi"]}

        # 9 -- usage + reports
        out.h("9. Usage, cost and downloadable reports")
        u = usage.usage_report()
        out(f"  plan={u['plan']}  workflows this period={u['usage']['workflows_per_month']}  LLM tokens={u['usage']['llm_tokens_per_month']}  costs={u['costs']}")
        outdir = data_dir / "demo_reports"
        outdir.mkdir(exist_ok=True)
        for kind in ("monthly_procurement", "supplier_performance", "forecast_accuracy", "inventory_risk", "savings", "ai_usage", "audit"):
            rep = reports.generate(kind, None)
            body, _ = reports.render(rep, "csv")
            (outdir / f"{kind}.csv").write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
        out(f"  7 reports written to {outdir}")
        out("")
        out("  DEMO / SYNTHETIC DATA - end of run.")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end commercial demo on synthetic data")
    ap.add_argument("--scale", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--keep", action="store_true", help="keep the demo data directory (default: temporary)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--transcript", default=str(ROOT / "evaluation" / "reports" / "demo_run.txt"))
    a = ap.parse_args(argv)
    d = Path(tempfile.mkdtemp(prefix="scm_demo_"))
    out = Out()
    try:
        run(out, d, a.scale, a.seed, a.quick)
    finally:
        Path(a.transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(a.transcript).write_text("\n".join(out.lines) + "\n", encoding="utf-8")
        if a.keep:
            print(f"data kept in {d}")
        else:
            shutil.rmtree(d, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
