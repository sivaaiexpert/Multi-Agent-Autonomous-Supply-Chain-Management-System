"""PAGE 4 · Executive console: inventory health, stock-out risk, procurement, savings, supplier reliability, workflow success, LLM cost,
the node-by-node view of a run, and the immutable audit trail."""
import json

import pandas as pd
import streamlit as st

from scm import audit, dashboard, forecast, security, suppliers
from ui import core
from ui.core import *

core.restore_widgets()
core.save_widgets()

summary = dashboard.executive_summary()
RISK_COLOR = {"CRITICAL": "#DC2626", "HIGH": "#EA580C", "MEDIUM": "#D97706", "LOW": "#059669"}


def kpi(label: str, value: str, sub: str, color: str = "#2563EB") -> str:
    return f"<div class='card kpi' style='--c:{color}'><div class='l'>{esc(label)}</div><div class='v'>{value}</div><div class='s'>{esc(sub)}</div></div>"


def pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


md(st, H("<div class='sh reveal' style='margin-top:1rem'><span class='eyebrow'>Executive console</span><h2>Operations at a glance</h2>",
         "<p>Forecast-driven inventory risk, supplier performance, procurement exposure, AI cost and an immutable audit trail. "
         "Every number is computed from the same database the agents use.</p></div>"))
lv = summary["risk_levels"]
hot = lv["CRITICAL"] + lv["HIGH"]
md(st, "<div class='kpis'>" + H(
    kpi("Inventory health", pct(summary["inventory_health"]), f"{summary['skus']} SKUs at or above reorder point", "#059669"),
    kpi("Stock-out risk", str(hot), f"{lv['CRITICAL']} critical · {lv['HIGH']} high · {lv['MEDIUM']} medium", "#DC2626" if hot else "#059669"),
    kpi("Open procurement", f"${summary['open_procurement']['value']:,.0f}", f"{summary['open_procurement']['orders']} order(s) awaiting approval", "#D97706"),
    kpi("Savings booked", f"${summary['savings']:,.0f}", "vs average market quote", "#2563EB")) + "</div>")
md(st, "<div class='kpis'>" + H(
    kpi("Supplier reliability", "n/a" if summary["supplier_reliability"] is None else f"{summary['supplier_reliability']:.0f}", "mean score, 0-100", "#2563EB"),
    kpi("Workflow success", pct(summary["workflow_success_rate"]), f"{sum(summary['workflow_runs'].values())} API workflow run(s)", "#059669"),
    kpi("LLM calls", f"{summary['llm']['calls']:,}", f"{summary['llm']['tokens_in'] + summary['llm']['tokens_out']:,} tokens · {summary['llm']['fallbacks']} fallback(s)", "#7C3AED"),
    kpi("LLM cost", f"${summary['llm']['cloud_equivalent_cost_usd']:.4f}", "cloud-equivalent; local inference is $0", "#7C3AED")) + "</div>")

t_risk, t_flow, t_sup, t_fc, t_audit = st.tabs(["Stock-out risk", "Workflow view", "Suppliers", "Forecasts", "Audit trail"])

with t_risk, guarded("risk table"):
    risk = pd.DataFrame(dashboard.inventory_risk())
    show = risk[["Item_ID", "Item_Name", "Stock_Level", "Reorder_Point", "risk_level", "days_until_stockout", "stockout_probability", "recommended_order_quantity", "basis"]]
    show = show.sort_values(by="risk_level", key=lambda s: s.map({"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}))
    st.dataframe(show, hide_index=True, width="stretch", column_config={
        "stockout_probability": st.column_config.ProgressColumn("P(stock-out)", format="percent", min_value=0, max_value=1),
        "days_until_stockout": st.column_config.NumberColumn("Cover (d)", format="%.1f"), "recommended_order_quantity": "Reorder qty", "basis": "Basis"})
    st.caption("Risk levels come from the deterministic risk engine (no LLM): lead-time demand, safety stock and stock-out probability. "
               "SKUs without demand history fall back to the static reorder-point rule (basis = static_rule).")

with t_flow, guarded("workflow view"):
    wf = ss.get("thread_id")
    st.caption(f"Run: {wf or 'none yet'}. Start one on the Operations Centre page.")
    steps = [("Supervisor", "supervisor"), ("Demand", "demand"), ("Risk", "analytics"), ("Suppliers", "supplier_intel"), ("Optimiser", "procurement"),
             ("Policy", "policy_check"), ("Vendor comms", "vendor_comm"), ("Human gate / dispatch", "vendor_dispatch")]
    spans = pd.DataFrame(dashboard.workflow_timeline(wf))
    vals = (current_state().values if current_state() else {}) or {}
    trace = vals.get("trace", [])
    rows = []
    for label, node in steps:
        mine = spans[spans.Node == node] if len(spans) else spans
        events = [e for e in trace if AGENT_KEY.get(e["agent"]) == node and e["agent"] != "Supervisor"]
        if node == "vendor_dispatch":
            events = [e for e in trace if e["step"] in ("dispatch", "approval refused")]
        status = "not reached" if not len(mine) and node != "vendor_dispatch" else ("paused at gate" if node == "vendor_dispatch" and not len(mine) and vals.get("stage") == "drafted"
                                                                                  else "not reached" if not len(mine) else ("error" if (mine.Status != "success").any() else "success"))
        rows.append({"Step": label, "Status": status, "Runs": len(mine), "Duration (ms)": round(float(mine.Duration_MS.sum()), 1) if len(mine) else None,
                     "Decision": (events[-1]["message"][:140] if events else ""),
                     "Fallbacks": sum(e["source"] == "fallback" for e in events), "Errors": sum(e["level"] == "error" for e in events) + (int((mine.Status != "success").sum()) if len(mine) else 0)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    nl = pd.DataFrame(dashboard.node_latency())
    if len(nl):
        with st.expander("Node latency across all runs (P50 / P95 / P99)"):
            st.dataframe(nl.round(1), hide_index=True, width="stretch")

with t_sup, guarded("supplier scoreboard"):
    m = suppliers.all_metrics()
    vendors = db.df("SELECT Vendor_ID, Vendor_Name, Region FROM vendors")
    board = pd.DataFrame([{**v, **{k: val for k, val in m[v["Vendor_ID"]].to_dict().items() if k != "vendor_id"}} for v in vendors.to_dict("records") if v["Vendor_ID"] in m])
    if len(board):
        board = board.sort_values("reliability_score", ascending=False)
        st.dataframe(board, hide_index=True, width="stretch", column_config={
            "on_time_delivery_rate": st.column_config.ProgressColumn("On-time", format="percent", min_value=0, max_value=1),
            "quality_rate": st.column_config.ProgressColumn("Quality", format="percent", min_value=0, max_value=1),
            "order_fulfillment_rate": st.column_config.ProgressColumn("Fulfilment", format="percent", min_value=0, max_value=1),
            "reliability_score": st.column_config.NumberColumn("Score", format="%.1f")})
    st.caption("Scores blend delivery history with the vendor's static reliability while few observations exist (source = blended / prior).")

with t_fc, guarded("forecast table"):
    fcs = pd.DataFrame(forecast.latest_forecasts(100))
    if not len(fcs):
        st.info("No forecasts yet: run the pipeline once (Operations Centre).")
    else:
        st.dataframe(fcs[["Item_ID", "Method", "Horizon_Days", "Expected_Demand", "Confidence", "MAE", "RMSE", "MAPE", "Created_At"]].round(3),
                     hide_index=True, width="stretch", column_config={"Confidence": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
        st.caption("MAPE is empty where it is mathematically invalid (intermittent demand / zero actuals).")

with t_audit, guarded("audit trail"):
    c1, c2, c3 = st.columns([2, 2, 1])
    wf_filter = c1.text_input("Workflow id (optional)", key="sb_audit_wf", max_chars=100)
    etype = c2.selectbox("Event type", ["(all)"] + [e.value for e in audit.EventType], key="sb_audit_type")
    limit = c3.number_input("Rows", 10, 500, 100, step=10, key="sb_audit_rows")
    events = audit.events(wf_filter.strip() or None if security.valid_id(wf_filter.strip() or "x") else None, None if etype == "(all)" else etype, int(limit))
    st.dataframe(pd.DataFrame([{"#": e["event_id"], "Time": e["timestamp"], "Actor": e["actor"], "Event": e["event_type"], "Workflow": e["workflow_id"],
                                "Payload": json.dumps(e["payload"], default=str)[:220]} for e in events]), hide_index=True, width="stretch")
    chain = summary["audit"]["chain"]
    (st.success if chain["ok"] else st.error)(f"Hash chain: {'intact' if chain['ok'] else 'BROKEN at event ' + str(chain['first_bad_event_id'])} · {chain['checked']} event(s) verified · "
                                               "rows are append-only (SQLite triggers) and each hash covers the previous one.")

core.save_widgets()
