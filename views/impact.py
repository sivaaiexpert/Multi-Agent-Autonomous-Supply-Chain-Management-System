"""PAGE 5 · Business impact: baseline vs optimised, back-test, what-if, ROI with explicit assumptions, usage and cost, downloadable reports, decision trace.

Every table says where its numbers come from (OBSERVED / BACKTEST / SIMULATED / ASSUMPTION_BASED) and whether the data is SYNTHETIC or CUSTOMER_DATA.
"""
import pandas as pd
import streamlit as st

from enterprise import comparison, impact, reports, roi, simulation, trace, usage, whatif
from scm import db
from ui import core
from ui.core import *

core.restore_widgets()
core.save_widgets()

PROV = impact.data_provenance()
BADGE = {"SYNTHETIC": "#7C3AED", "CUSTOMER_DATA": "#059669"}


def money(x) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


md(st, H("<div class='sh reveal' style='margin-top:1rem'><span class='eyebrow'>Business impact</span><h2>What the platform changes, and how we know</h2>",
         "<p>Baselines, back-tests, what-if scenarios and ROI. Every figure carries its provenance; simulated or assumption-based values are never presented as measured results.</p></div>"))
label = "DEMO / SYNTHETIC DATA: generated, not customer information" if PROV == "SYNTHETIC" else "CUSTOMER DATA"
md(st, f"<div class='card' style='border-left:4px solid {BADGE.get(PROV, '#D97706')}'><b>{esc(label)}</b> &nbsp; Provenance legend: "
       "<b>OBSERVED</b> read from stored records · <b>BACKTEST</b> replay of a forecast on history · <b>SIMULATED</b> policy replay with modelled supplier outcomes · "
       "<b>ASSUMPTION_BASED</b> depends on inputs you provide.</div>")

t_kpi, t_cmp, t_wi, t_roi, t_cost, t_rep, t_trace = st.tabs(["KPIs", "Before vs after", "What-if", "ROI", "Usage & cost", "Reports", "Decision trace"])

with t_kpi, guarded("impact KPIs"):
    fc = impact.forecast_accuracy(14, 4)
    inv = impact.inventory_kpis(False)
    sup = impact.supplier_kpis()
    ops = impact.operations_kpis(12.0)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Forecast WAPE (14 d)", pct(fc.get("overall", {}).get("wape")) if isinstance(fc, dict) else "n/a", help="Weighted absolute percentage error over rolling-origin forecasts (BACKTEST).")
    c2.metric("Safety-stock adherence", pct(inv.get("safety_stock_adherence_now")), help="Share of SKUs at or above their safety stock now (OBSERVED).")
    c3.metric("SKUs at high/critical risk", inv.get("skus_high_or_critical_risk_now", "n/a"))
    c4.metric("Excess inventory value", money(inv.get("excess_value_now")))
    with st.expander("Forecast accuracy by SKU and category (MAE / RMSE / WAPE / MAPE / bias)", expanded=False):
        st.json(fc, expanded=False)
    with st.expander("Supplier KPIs (OBSERVED from delivery history)", expanded=False):
        st.json(sup, expanded=False)
    with st.expander("Operational KPIs (cycle times, manual interventions)", expanded=False):
        st.json(ops, expanded=False)

with t_cmp, guarded("comparison"):
    st.caption("Same demand, same suppliers, different policy. 'Manual' = lowest sticker price with a fixed reorder rule; 'Rule-based' = reliability filter, cheapest eligible; 'AI' = forecast + optimiser.")
    days = st.slider("Back-test window (days)", 21, 120, 60, 7, key="imp_days")
    if st.button("Run back-test", key="imp_bt"):
        st.session_state["imp_bt_result"] = impact.inventory_kpis(True, simulation.SimConfig(eval_days=days)).get("backtest")
    bt = st.session_state.get("imp_bt_result")
    if bt is None:
        st.info("Press 'Run back-test'. It needs demand history and supplier delivery history; without them the platform says so instead of guessing.")
    elif "arms" not in bt:
        st.warning(bt.get("unavailable", "back-test unavailable"))
    else:
        st.caption(f"Provenance: {bt['provenance']} · window {bt['eval_days']} days · common random numbers, no look-ahead")
        keys = [("Total cost", "total_cost", money), ("Gross profit", "gross_profit", money), ("Stock-out events", "stockout_events", str), ("Stock-out days", "stockout_days", str),
                ("Fill rate", "fill_rate", pct), ("Turnover (annualised)", "turnover_annualized", lambda v: f"{v:.1f}"), ("Safety-stock adherence", "safety_stock_adherence", pct),
                ("Excess value (avg)", "excess_value_avg", money), ("Manual touches (ASSUMPTION_BASED)", "manual_touches", str)]
        arms = bt["arms"]
        st.dataframe(pd.DataFrame({lab: {a: f(arms[a][k]) for a in arms} for lab, k, f in keys}).T, width="stretch")
    st.divider()
    st.caption("Procurement decision vs lowest-sticker-price buyer on the current data (OBSERVED). A negative saving is shown as-is.")
    st.json(comparison.baseline_vs_optimized(), expanded=False)

with t_wi, guarded("what-if"):
    st.caption("Deterministic: the same inputs always give the same output. Compare against the unchanged base case.")
    a, b, c = st.columns(3)
    dm = a.slider("Demand ×", 0.5, 2.0, 1.0, 0.05, key="wi_dm")
    pm = b.slider("Supplier price ×", 0.7, 1.5, 1.0, 0.05, key="wi_pm")
    cm = c.slider("Supplier capacity ×", 0.3, 1.5, 1.0, 0.05, key="wi_cm")
    a, b, c = st.columns(3)
    lt = a.slider("Lead time Δ (days)", -5.0, 15.0, 0.0, 0.5, key="wi_lt")
    sm = b.slider("Stock on hand ×", 0.3, 2.0, 1.0, 0.05, key="wi_sm")
    budget = c.number_input("Budget (0 = none)", 0, 10_000_000, 0, 1000, key="wi_bud")
    if st.button("Simulate", key="wi_go"):
        sc = whatif.Scenario(name="console", demand_multiplier=dm, price_multiplier=pm, capacity_multiplier=cm, lead_time_delta_days=lt, stock_multiplier=sm, budget=budget or None)
        r = whatif.compare(whatif.load_snapshot(), sc)
        rows = {k: {"base": r["base"][k], "scenario": r["result"][k]} for k in ("required_qty", "funded_qty", "expected_cost", "critical_risk_skus", "high_or_critical_risk_skus", "mean_stockout_probability", "avg_lead_time_days")}
        st.dataframe(pd.DataFrame(rows).T, width="stretch")
        if r["result"]["skus_without_feasible_plan"]:
            st.warning(f"No feasible plan under these constraints: {r['result']['skus_without_feasible_plan']}")

with t_roi, guarded("roi"):
    st.caption("ROI is SIMULATED benefit divided by cost, haircut by a realisation factor. The cost fields are YOURS to enter: they default to 0 and ROI is 'undefined' without a cost basis.")
    a, b, c = st.columns(3)
    lic = a.number_input("Annual licence", 0, 10_000_000, 0, 1000, key="roi_lic")
    infra = b.number_input("Annual infrastructure", 0, 10_000_000, 0, 500, key="roi_inf")
    sup_cost = c.number_input("Annual integration & support", 0, 10_000_000, 0, 500, key="roi_sup")
    a, b, c = st.columns(3)
    rf = a.slider("Realisation factor", 0.1, 1.0, 0.5, 0.05, key="roi_rf", help="Share of the simulated benefit you expect to realise in practice.")
    wpy = b.number_input("Workflows per year", 1, 5000, 52, key="roi_w")
    arm = c.selectbox("Platform arm", ["ai_lean", "ai"], key="roi_arm")
    bt = st.session_state.get("imp_bt_result")
    if bt is None or "arms" not in bt:
        st.info("Run the back-test on the 'Before vs after' tab first.")
    else:
        res = roi.compute(bt["arms"]["manual"], bt["arms"][arm], roi.RoiAssumptions(annual_platform_license=lic, annual_infrastructure=infra, annual_integration_and_support=sup_cost,
                                                                                    realization_factor=rf, workflows_per_year=wpy), bt["provenance"], PROV)
        st.dataframe(pd.DataFrame(res["components"]), width="stretch", hide_index=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("Annual benefit (haircut)", money(res["annual_benefit"]))
        m2.metric("Annual cost (your inputs)", money(res["annual_cost"]))
        m3.metric("ROI", "undefined" if res["annual_roi"] is None else f"{res['annual_roi']:.2f}")
        st.caption(res["caveat"])

with t_cost, guarded("usage"):
    u = usage.usage_report()
    st.caption(f"Plan: {u['plan']} (a plan is a packaging concept: no prices are defined here). Quotas are advisory unless enforcement is switched on.")
    st.dataframe(pd.DataFrame(u["quotas"]).T, width="stretch")
    st.json({"llm": u["llm"], "costs": u["costs"]}, expanded=False)

with t_rep, guarded("reports"):
    kind = st.selectbox("Report", ["monthly_procurement", "supplier_performance", "forecast_accuracy", "inventory_risk", "savings", "ai_usage", "audit"], key="rep_kind")
    rep = reports.generate(kind, None)
    st.caption(f"{rep.title} · period {rep.meta['period']} · provenance {rep.meta['provenance']} · data {rep.meta['data_provenance']}")
    st.dataframe(pd.DataFrame(rep.rows), width="stretch", hide_index=True)
    for fmt, mime in (("csv", "text/csv"), ("json", "application/json"), ("md", "text/markdown"), ("html", "text/html")):
        body, _ = reports.render(rep, fmt)
        st.download_button(f"Download {fmt.upper()}", body, file_name=f"{kind}-{rep.meta['period']}.{fmt}", mime=mime, key=f"dl_{kind}_{fmt}")

with t_trace, guarded("trace"):
    try:
        with db.connect() as c:
            wfs = [r[0] for r in c.execute("SELECT Workflow_ID FROM decision_traces ORDER BY Updated_At DESC LIMIT 30")]
    except Exception:                                                  # table not created yet on a brand-new database
        wfs = []
    if not wfs:
        st.info("No decision traces yet: run a workflow on the Operations Centre page.")
    else:
        wf = st.selectbox("Workflow", wfs, key="tr_wf")
        tr = trace.load(wf)
        for e in trace.explain_all(tr):
            st.markdown(f"**{e['sku']}** — {e['headline']}")
            for reason in e["reasons"]:
                st.markdown(f"- {reason}")
        with st.expander("Full decision chain (inputs, forecast, risk, candidates, optimisation, policy, approval, execution)", expanded=False):
            st.json(tr, expanded=False)
