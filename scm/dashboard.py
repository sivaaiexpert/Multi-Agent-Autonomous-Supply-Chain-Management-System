"""Read models for the executive console (and anything else that wants headline numbers). Pure queries: no side effects, no business logic."""
from __future__ import annotations

from . import audit, db, forecast, obs, suppliers
from .risk import RiskLevel, assess, static_assessment
from .settings import get_settings


def inventory_risk(limit_skus: int = 500) -> list[dict]:
    """Current risk level per SKU (forecast-based when history exists, static rule otherwise)."""
    cfg = get_settings()
    hist = forecast.load_all_histories(limit_skus)
    fc = forecast.DemandForecaster()
    with db.connect() as c:
        inv = [dict(r) for r in c.execute("SELECT * FROM inventory ORDER BY Item_ID")]
        lead = {r[0]: float(r[1]) for r in c.execute("SELECT Item_ID, AVG(Lead_Time_Days) FROM vendor_listings WHERE Lead_Time_Days>=0 GROUP BY Item_ID")}
    out = []
    for r in inv:
        stock = max(0, r["Stock_Level"])
        h = hist.get(r["Item_ID"])
        if h:
            ra = assess(r["Item_ID"], stock, forecast.cached_forecast(fc, r["Item_ID"], h), lead.get(r["Item_ID"], cfg.risk.default_lead_time_days),
                        r["Target_Stock"], r["Reorder_Point"])
        else:
            ra = static_assessment(r["Item_ID"], stock, r["Reorder_Point"], r["Target_Stock"])
        out.append({**r, "risk_level": ra.risk_level.value, "days_until_stockout": ra.days_until_stockout, "stockout_probability": ra.stockout_probability,
                    "basis": ra.basis, "recommended_order_quantity": ra.recommended_order_quantity})
    return out


def executive_summary() -> dict:
    risk = inventory_risk()
    n = len(risk) or 1
    healthy = sum(1 for r in risk if r["Stock_Level"] >= r["Reorder_Point"])
    levels = {lv.value: sum(r["risk_level"] == lv.value for r in risk) for lv in RiskLevel}
    metrics = suppliers.all_metrics()
    with db.connect() as c:
        pending = c.execute("SELECT COUNT(*), COALESCE(SUM(Quantity*Unit_Price),0) FROM purchase_orders WHERE Status=?", (db.PENDING,)).fetchone()
        savings = c.execute("SELECT COALESCE(SUM(Savings),0) FROM purchase_orders WHERE Status='SENT'").fetchone()[0]
        runs = {r[0]: r[1] for r in c.execute("SELECT Status, COUNT(*) FROM workflow_runs GROUP BY Status")}
        spans = c.execute("SELECT COUNT(*), COALESCE(SUM(Status<>'success'),0) FROM node_runs").fetchone()
        llm = c.execute("SELECT COUNT(*), COALESCE(SUM(Tokens_In),0), COALESCE(SUM(Tokens_Out),0), COALESCE(SUM(Cost_USD),0), COALESCE(SUM(Fallback),0) FROM llm_calls").fetchone()
    done = runs.get("COMPLETED", 0) + runs.get("AWAITING_APPROVAL", 0)
    finished = done + runs.get("FAILED", 0)
    rel = [m.reliability_score for m in metrics.values()]
    return {"inventory_health": healthy / n, "skus": len(risk), "risk_levels": levels,
            "open_procurement": {"orders": pending[0], "value": pending[1]}, "savings": savings,
            "supplier_reliability": sum(rel) / len(rel) if rel else None,
            "workflow_success_rate": (done / finished) if finished else None, "workflow_runs": runs,
            "node_success_rate": (1 - spans[1] / spans[0]) if spans[0] else None,
            "llm": {"calls": llm[0], "tokens_in": llm[1], "tokens_out": llm[2], "cloud_equivalent_cost_usd": llm[3], "fallbacks": llm[4]},
            "audit": {"events": audit.count(), "chain": audit.verify_chain()}}


def workflow_timeline(workflow_id: str | None) -> list[dict]:
    """One row per executed node of a workflow: status, duration, the agent's headline decision, errors and fallbacks."""
    if not workflow_id:
        return []
    with db.connect() as c:
        spans = [dict(r) for r in c.execute("SELECT Node, Agent, Started_At, Duration_MS, Status, Error, Tokens_In, Tokens_Out FROM node_runs "
                                            "WHERE Workflow_ID=? ORDER BY Started_At", (workflow_id,))]
    return spans


def recent_workflows(limit: int = 20) -> list[dict]:
    d = db.df("SELECT Workflow_ID, Status, Stage, Created_By, Created_At FROM workflow_runs ORDER BY Created_At DESC LIMIT ?", (limit,))
    return d.to_dict("records")


def node_latency() -> list[dict]:
    return obs.node_latency_summary()
