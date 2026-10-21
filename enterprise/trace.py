"""Decision trace and customer-facing explanation.

For every procurement decision the trace stores the complete chain, captured when the workflow reaches the human gate (and completed afterwards):

    input data -> forecast -> risk analysis -> supplier candidates -> optimisation result -> policy checks -> approval -> execution

Explanations are generated FROM that stored data by deterministic code. A language model is never asked for a fact: it cannot invent a supplier's
on-time rate, a price or a capacity, because it is never the source of any of them.
"""
from __future__ import annotations

import json
from datetime import datetime
from statistics import mean, pstdev

from scm import audit, db, obs
from scm.settings import get_settings



def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build(workflow_id: str, values: dict) -> dict:
    """Assemble the trace from the workflow state + the database."""
    alerts = {a["Item_ID"]: a for a in values.get("alerts", [])}
    proposals = values.get("proposals", [])
    skus = sorted({p["Item_ID"] for p in proposals} | set(alerts))
    scores = values.get("supplier_scores", {})
    s = get_settings()
    with db.connect() as c:
        inv = {r["Item_ID"]: dict(r) for r in c.execute("SELECT * FROM inventory")}
        hist: dict[str, list[float]] = {}
        for sku in skus:
            hist[sku] = [r[0] for r in c.execute("SELECT Quantity FROM demand_history WHERE Item_ID=? ORDER BY Day", (sku,))]
        fcs = {r["Item_ID"]: dict(r) for r in c.execute(
            "SELECT * FROM forecasts WHERE Workflow_ID=? AND Forecast_ID IN (SELECT MAX(Forecast_ID) FROM forecasts WHERE Workflow_ID=? GROUP BY Item_ID)", (workflow_id, workflow_id))}
    inputs = {"inventory": [{"sku": k, "name": inv[k]["Item_Name"], "category": inv[k]["Category"], "stock": inv[k]["Stock_Level"], "reorder_point": inv[k]["Reorder_Point"],
                             "target_stock": inv[k]["Target_Stock"]} for k in skus if k in inv],
              "demand_history": {k: {"days": len(v), "mean": round(mean(v), 3) if v else None, "std": round(pstdev(v), 3) if len(v) > 1 else None,
                                     "last_7_mean": round(mean(v[-7:]), 3) if v else None, "last_28_mean": round(mean(v[-28:]), 3) if v else None} for k, v in hist.items()}}
    forecasts = [{"sku": k, "method": f["Method"], "horizon_days": f["Horizon_Days"], "expected_demand": f["Expected_Demand"], "confidence": f["Confidence"],
                  "mae": f["MAE"], "rmse": f["RMSE"]} for k, f in fcs.items() if k in skus]
    risk = [{k: a.get(k) for k in ("Item_ID", "urgency", "risk_level", "basis", "predictive", "days_until_stockout", "stockout_probability", "safety_stock", "rec_qty",
                                   "forecast_daily", "forecast_method", "forecast_confidence", "lead_time_est", "Stock_Level", "Reorder_Point", "Target_Stock", "insight")}
            for a in alerts.values()]
    candidates: dict[str, list[dict]] = {}
    optimization: dict[str, dict] = {}
    for p in proposals:
        sku = p["Item_ID"]
        plan = p.get("plan") or {}
        if sku not in optimization:
            optimization[sku] = {"required_quantity": p.get("required_qty"), "allocation": plan.get("allocation", {}), "status": plan.get("status", p.get("optimizer_status")),
                                 "method": plan.get("method"), "objective": plan.get("objective"), "expected_cost": plan.get("expected_cost"),
                                 "weighted_lead_time_days": plan.get("weighted_lead_time_days"), "risk_score": plan.get("risk_score"),
                                 "breakdown": plan.get("breakdown", {}), "excluded": plan.get("excluded", {}), "explanation": plan.get("explanation")}
        if sku not in candidates:
            excl = plan.get("excluded", {})
            chosen = set(plan.get("allocation", {}))
            candidates[sku] = [{"vendor_id": q["Vendor_ID"], "vendor_name": q["Vendor_Name"], "unit_price": q["Unit_Price"], "lead_time_days": q["Lead_Time_Days"],
                                "moq": int(q.get("MOQ") or 1), "capacity": None if q.get("Capacity") is None or q.get("Capacity") != q.get("Capacity") else int(q["Capacity"]),
                                "region": q.get("Region"), "static_reliability": q.get("Reliability"), "platform_score": (scores.get(q["Vendor_ID"]) or {}).get("score"),
                                "on_time_rate": (scores.get(q["Vendor_ID"]) or {}).get("on_time"), "quality_rate": (scores.get(q["Vendor_ID"]) or {}).get("quality"),
                                "observations": (scores.get(q["Vendor_ID"]) or {}).get("observations"), "selected": q["Vendor_ID"] in chosen,
                                "excluded_reason": excl.get(q["Vendor_ID"])} for q in p.get("quotes", [])]
    return {"workflow_id": workflow_id, "tenant_id": _tenant(), "created_at": _now(), "data_provenance": _provenance(),
            "inputs": inputs, "forecasts": forecasts, "risk": risk, "supplier_candidates": candidates, "optimization": optimization,
            "proposals": [{k: p.get(k) for k in ("po_id", "Item_ID", "Vendor_ID", "Vendor_Name", "qty", "unit_price", "total", "lead_time", "share", "savings")} for p in proposals],
            "policy": values.get("policy", {}),
            "policy_rules": {"tenant_rules": s.rules.model_dump(), "min_reliability_score": s.supplier.min_reliability_score, "min_quality_rate": s.supplier.min_quality_rate,
                             "max_supplier_share": s.optimizer.max_supplier_share, "service_level_z": s.forecast.service_level_z},
            "execution": {"status": "AWAITING_APPROVAL"}}


def _provenance() -> str:
    with db.connect() as c:
        r = c.execute("SELECT Value FROM app_state WHERE Key='data_provenance'").fetchone()
    return r[0] if r else "CUSTOMER_DATA"


def store(trace: dict) -> None:
    now = _now()
    with db.connect(write=True) as c:
        c.execute("INSERT INTO decision_traces (Tenant_ID, Workflow_ID, Trace_JSON, Created_At, Updated_At) VALUES (?,?,?,?,?) ON CONFLICT(Tenant_ID, Workflow_ID) DO UPDATE SET "
                  "Trace_JSON=excluded.Trace_JSON, Updated_At=excluded.Updated_At", (_tenant(), trace["workflow_id"], json.dumps(trace, default=str), now, now))


def load(workflow_id: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT Trace_JSON FROM decision_traces WHERE Tenant_ID=? AND Workflow_ID=?", (_tenant(), workflow_id)).fetchone()
    return json.loads(r[0]) if r else None


def update_execution(workflow_id: str, execution: dict) -> None:
    tr = load(workflow_id)
    if tr is None:
        return
    tr["execution"] = {**tr.get("execution", {}), **execution, "updated_at": _now()}
    store(tr)


def full(workflow_id: str) -> dict | None:
    """The stored trace merged with the live approval chain and the workflow's audit events (the complete chain an auditor needs)."""
    tr = load(workflow_id)
    if tr is None:
        return None
    with db.connect() as c:
        steps = [{k.lower(): v for k, v in dict(r).items()} for r in c.execute(
            "SELECT Step, Required_Level, Status, Decided_By, Decided_Role, Decided_At, Reason, Due_At FROM approvals WHERE Tenant_ID=? AND Workflow_ID=? ORDER BY Approval_ID",
            (_tenant(), workflow_id))]
    tr["approval"] = steps
    tr["audit"] = [{k: e[k] for k in ("timestamp", "actor", "event_type")} | {"payload": e["payload"]} for e in reversed(audit.events(workflow_id, limit=500))]
    tr["explanations"] = explain_all(tr)
    return tr


# ------------------------------------------------------------------------------------------------ explanations (facts only from the trace)
def explain(tr: dict, sku: str) -> dict:
    cands = tr.get("supplier_candidates", {}).get(sku, [])
    opt = tr.get("optimization", {}).get(sku, {})
    props = [p for p in tr.get("proposals", []) if p["Item_ID"] == sku]
    risk: dict = next((r for r in tr.get("risk", []) if r["Item_ID"] == sku), {})
    if not props or not cands:
        return {"sku": sku, "headline": f"No supplier was recommended for {sku}.", "reasons": [f"{k}: {v}" for k, v in (opt.get("excluded") or {}).items()] or ["no eligible quote"]}
    total = sum(p["qty"] for p in props)
    by_v = {c["vendor_id"]: c for c in cands}
    names = " + ".join(f"{p['Vendor_Name']} ({p['qty'] / total:.0%})" if len(props) > 1 else p["Vendor_Name"] for p in props)
    paid = sum(p["qty"] * p["unit_price"] for p in props) / total
    prices = [c["unit_price"] for c in cands]
    avg, cheapest = mean(prices), min(prices)
    reasons: list[str] = []
    d_avg = (avg - paid) / avg if avg else 0
    reasons.append(f"{abs(d_avg):.0%} {'lower' if d_avg >= 0 else 'higher'} weighted unit price than the average of {len(cands)} quotes (paid {paid:,.2f} vs average {avg:,.2f})")
    if paid > cheapest * 1.0001:
        cheap = min(cands, key=lambda c: c["unit_price"])
        why = cheap.get("excluded_reason") or "its lead time, reliability or risk made it more expensive overall once those costs were included"
        reasons.append(f"the cheapest quote ({cheap['vendor_name']}, {cheap['unit_price']:,.2f}) was not chosen: {why}")
    for p in props:
        c = by_v.get(p["Vendor_ID"], {})
        bits = []
        if c.get("on_time_rate") is not None:
            bits.append(f"{c['on_time_rate']:.0%} historical on-time delivery over {c.get('observations') or 0} deliveries")
        if c.get("platform_score") is not None:
            bits.append(f"reliability score {c['platform_score']:.0f}/100")
        if c.get("quality_rate") is not None:
            bits.append(f"{c['quality_rate']:.1%} quality rate")
        cap = c.get("capacity")
        bits.append(f"allocation {p['qty']} " + (f"within its capacity of {cap}" if cap else "(no capacity limit on file)") + (f" and above its minimum order of {c['moq']}" if c.get("moq", 1) > 1 else ""))
        reasons.append(f"{p['Vendor_Name']}: " + "; ".join(bits))
    lead = opt.get("weighted_lead_time_days")
    rule_lead = (tr.get("policy_rules", {}).get("tenant_rules", {}) or {}).get("max_lead_time_days")
    if lead is not None:
        txt = f"weighted lead time {lead:.1f} days"
        if rule_lead:
            txt += f", within the policy maximum of {rule_lead:g} days"
        cover = risk.get("days_until_stockout")
        if cover is not None:
            txt += f"; current stock covers {cover:.1f} days of forecast demand ({'the order arrives AFTER stock runs out' if lead > cover else 'the order arrives before stock runs out'})"
        reasons.append(txt)
    if risk.get("stockout_probability") is not None:
        reasons.append(f"stock-out risk {risk.get('risk_level')} (probability {risk['stockout_probability']:.0%} within the supplier lead time, {risk.get('basis')})")
    if opt.get("risk_score") is not None:
        reasons.append(f"allocation-weighted supplier risk {opt['risk_score']:.1%}")
    split = [c for c in cands if c.get("excluded_reason")]
    for c in split[:4]:
        reasons.append(f"excluded: {c['vendor_name']}: {c['excluded_reason']}")
    return {"sku": sku, "headline": f"Recommended supplier{'s' if len(props) > 1 else ''}: {names}", "reasons": reasons, "source": "deterministic, derived from the stored decision trace"}


def explain_all(tr: dict) -> list[dict]:
    return [explain(tr, s) for s in sorted({p["Item_ID"] for p in tr.get("proposals", [])})]


# ------------------------------------------------------------------------------------------------ workflow hooks
def install_hooks() -> None:
    from scm import services

    def trace_on_gate(svc, wf: str, summary: dict) -> None:
        values = svc.graph.get_state({"configurable": {"thread_id": wf}}).values
        store(build(wf, values))
    trace_on_gate.__name__ = "decision_trace_on_gate"

    def trace_on_done(svc, wf: str, summary: dict) -> None:
        out = summary.get("outcome") or {}
        update_execution(wf, {"status": (summary.get("graph_status") or "").upper(), "po_ids": out.get("po_ids", []), "total": out.get("total"), "emails": out.get("emails")})
    trace_on_done.__name__ = "decision_trace_on_done"
    for hooks, fn in ((services.GATE_HOOKS, trace_on_gate), (services.DONE_HOOKS, trace_on_done)):
        if not any(getattr(h, "__name__", "") == fn.__name__ for h in hooks):
            hooks.append(fn)

