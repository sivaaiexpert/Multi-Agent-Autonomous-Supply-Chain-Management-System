"""LangGraph multi-agent network.

    Supervisor ─► Demand ─► Analytics (risk) ─► Supplier Intel ─► Procurement (optimiser) ─► Policy ─► Vendor Comms ─► [HUMAN GATE] ─► Dispatch
                     ▲                                                                                                      │
                     └──────────────────────────────── re-source loop (rejected vendors excluded) ◄───────────────────────────┘

Division of labour (see docs/AI_SYSTEM_DESIGN.md):
  * Deterministic engines decide: forecasting, risk levels, supplier scoring, allocation, policy validation, idempotent order creation.
  * The LLM only writes advisory text (summaries, rationale, email tone) through strict schemas; it can never change a number.
  * A human approves before anything is dispatched; every step is traced, audited and (for orders) transactional.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from operator import add
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from . import audit, db, ingest, obs, orders, suppliers
from .events import BUS, EventName
from .forecast import DemandForecaster, cached_forecast, load_all_histories, save_forecasts
from .llm import FORMAT_WARNING, ask_json, last_ignored, last_raw, salvage_order
from .optimizer import SupplierOption, SupplierOptimizer
from .policy import BLOCK, PolicyValidator
from .risk import RiskLevel, assess, open_orders_by_item, static_assessment
from .schemas import ProcurementRecommendation, VendorMessage, DemandAnalysis
from .security import UNTRUSTED_NOTICE, sanitize_untrusted
from .settings import get_settings

MAX_RESOURCE_ROUNDS = 2
HITL_NODE = "vendor_dispatch"
URGENCY_RANK = {"medium": 0, "high": 1, "critical": 2}


class SupplyState(TypedDict, total=False):
    stage: str                                # start|forecasted|analyzed|scored|procured|validated|drafted|dispatched|failed
    workflow_id: str
    trace_id: str
    alerts: list[dict]                        # low-stock / forecast-risk findings
    proposals: list[dict]                     # one line per (item, supplier) allocation
    drafts: list[dict]                        # vendor emails / PO bundles
    decision: str                             # "" | approve | reject | resource
    approver: str                             # who made the human decision
    decision_reason: str
    human_choices: Annotated[list, add]
    trace: Annotated[list, add]               # agent reasoning log
    excluded: dict                            # item_id -> vendor ids already rejected
    rounds: int
    llm_ok: bool                              # circuit breaker: false after first LLM failure
    next: str
    status: str                               # healthy|approved|rejected|no_vendor|policy_blocked|resourcing|error|...
    outcome: dict
    errors: Annotated[list, add]              # captured node exceptions (stack traces)
    telemetry: Annotated[list, add]           # per-LLM-call token/latency stats
    forecast_summary: dict
    supplier_scores: dict
    policy: dict


def ev(agent: str, step: str, message: str, detail: str = "", level: str = "info", source: str = "rules") -> dict:
    return {"ts": datetime.now().strftime("%H:%M:%S"), "agent": agent, "step": step,
            "message": message, "detail": detail, "level": level, "source": source}


def _cfg(config: RunnableConfig) -> dict:
    return (config or {}).get("configurable", {})


def _wf(state: SupplyState, config: RunnableConfig) -> str:
    return state.get("workflow_id") or _cfg(config).get("workflow_id") or _cfg(config).get("thread_id") or "adhoc"


def _llm(state: SupplyState, config: RunnableConfig, agent: str, system: str, user: str, required: list[str] | dict, schema=None):
    """LLM call guarded by the circuit breaker. Returns (data|None, note, llm_ok, telemetry_entries)."""
    if not state.get("llm_ok", True):
        return None, "skipped (LLM failed earlier in this run)", False, []
    data, note, stats = ask_json(_cfg(config), system + " " + UNTRUSTED_NOTICE, user, required, schema)
    tele = [{"agent": agent, "ok": data is not None, **stats}] if stats else []
    if data is None and "disabled" in note:
        return None, note, True, tele
    return data, note, (data is not None) or note.startswith(FORMAT_WARNING), tele


def _fmt_events(agent: str, note: str) -> list[dict]:
    """Trace lines for output-format problems: a subtle warning (unrecoverable) or an info note (regex repaired)."""
    out: list[dict] = []
    if note.startswith(FORMAT_WARNING):
        out.append(ev(agent, "LLM format exception", FORMAT_WARNING, note[len(FORMAT_WARNING):].strip(" ()") + " — safe template layout used.",
                      level="warn", source="fallback"))
    elif note == "llm (regex-recovered)":
        out.append(ev(agent, "LLM format recovered", "Malformed model output repaired by the regex parser.", level="info", source="llm"))
    ignored = last_ignored()
    if ignored:
        out.append(ev(agent, "LLM decision fields ignored", "The model tried to set " + ", ".join(ignored) +
                      "; decision fields are never read from model output (deterministic engines decide).", level="warn", source="fallback"))
        audit.safe_record(audit.EventType.LLM_FALLBACK, obs.context().get("workflow_id"), "agent:" + agent.lower(),
                          {"reason": "decision fields in model output ignored", "fields": ignored})
    return out


def _order_salvage_events(p: dict, raw: str) -> list[dict]:
    """Field-level Regex Extraction of an order-style model reply (vendor / quantity / unit price). Missing fields get the deterministic
    policy values as safe operational defaults. Model-suggested values are reported but NEVER override the procurement policy."""
    sal = salvage_order(raw)
    if not sal:
        return []
    got, policy = sal["found"], {"selected_vendor": p["Vendor_Name"], "recommended_order_quantity": p["qty"], "unit_price_usd": p["unit_price"]}
    lines = [f"{k}: {got[k]!r}  (salvaged by regex)" if k in got else f"{k}: {policy[k]!r}  (missing -> safe default from policy)" for k in policy]
    diffs = [f"{k} model={got[k]!r} vs policy={policy[k]!r}" for k in got if str(got[k]).lower() != str(policy[k]).lower()]
    msg = ("Regex Extraction Engine salvaged " + ", ".join(got) + (f"; defaulted {', '.join(sal['missing'])}" if sal["missing"] else "") + ".")
    if diffs:
        lines.append("Policy values retained (model suggestion differs): " + "; ".join(diffs))
    return [ev("Procurement", "LLM regex salvage", msg, "\n".join(lines), level="warn", source="fallback")]


def _src(note: str) -> str:
    return "rules" if "disabled" in note else "fallback"


def _safe_text(label: str, text: object, findings: list[str], who: str, max_len: int = 80) -> str:
    """Third-party text (item / vendor names) is sanitised before it can reach a prompt; injection attempts are recorded."""
    clean, found = sanitize_untrusted(text, max_len)
    findings += [f"{label}: {f}" for f in found]
    return clean


def _injection_events(agent: str, findings: list[str]) -> list[dict]:
    if not findings:
        return []
    audit.safe_record(audit.EventType.PROMPT_INJECTION_DETECTED, obs.context().get("workflow_id"), "agent:" + agent.lower(), {"findings": findings[:20]})
    return [ev(agent, "prompt injection blocked", f"{len(findings)} instruction-like fragment(s) in third-party text were neutralised before reaching the model.",
               "\n".join(findings[:6]), level="warn", source="rules")]


# --------------------------------------------------------------------------- nodes
def supervisor(state: SupplyState, config: RunnableConfig) -> dict:
    stage, alerts, props = state.get("stage", "start"), state.get("alerts", []), state.get("proposals", [])
    status = state.get("status", "running")
    extra: dict = {}
    if stage == "start":
        wf = _wf(state, config)
        extra = {"workflow_id": wf, "trace_id": state.get("trace_id") or _cfg(config).get("trace_id") or obs.new_id()}
        obs.bind(workflow_id=wf, trace_id=extra["trace_id"])
        audit.safe_record(audit.EventType.WORKFLOW_STARTED, wf, _cfg(config).get("actor", "system"), {"trace_id": extra["trace_id"]})
        nxt, why = "demand", "New run: dispatching the Demand Agent to forecast demand and the Analytics Agent to assess risk."
    elif stage == "failed":
        nxt, why, status = "end", "A node raised an exception. Halting safely; pending orders were rolled back.", "error"
    elif stage == "forecasted":
        nxt, why = "analytics", "Forecasts ready. Routing to Analytics for stock-out risk assessment."
    elif stage == "analyzed" and not alerts:
        nxt, why, status = "end", "No items below reorder point and no forecast stock-out risk. Nothing to procure.", "healthy"
    elif stage == "analyzed":
        rounds = state.get("rounds", 0)
        nxt = "supplier_intel"
        why = (f"{len(alerts)} alerts raised. Routing to Supplier Intelligence" +
               (f" (re-sourcing round {rounds}, excluding rejected vendors)." if rounds else " to score suppliers."))
    elif stage == "scored":
        nxt, why = "procurement", "Supplier scores ready. Routing to Procurement for constrained allocation."
    elif stage == "procured" and not props:
        nxt, why, status = "end", "No eligible vendors remain for any alerted item. Terminating safely.", "no_vendor"
    elif stage == "procured":
        nxt, why = "policy_check", f"{len(props)} allocation line(s) proposed. Routing to the deterministic Policy Validator."
    elif stage == "validated" and not props:
        nxt, why, status = "end", "Every proposed line was blocked by business policy. Terminating safely; nothing was ordered.", "policy_blocked"
    elif stage == "validated":
        nxt, why = "vendor_comm", f"{len(props)} line(s) passed policy. Routing to Vendor Communication for PO drafting."
    elif stage == "drafted":
        nxt, why = HITL_NODE, "Drafts ready. Final dispatch is gated: graph will PAUSE for human approval before executing."
    else:  # dispatched
        nxt = "end"
        why = {"approved": "Human approved. Orders dispatched and database updated. Run complete.",
               "rejected": "Human rejected the order. Run terminated with no orders sent."}.get(status, "Run complete.")
    if nxt == "end" and status != "error":        # failures are audited where they happen (safe_node), even if the supervisor itself crashed
        audit.safe_record(audit.EventType.WORKFLOW_COMPLETED, state.get("workflow_id") or extra.get("workflow_id"), "system", {"status": status})
    lvl = ("error" if status == "error" else "success" if nxt == "end" and status in ("healthy", "approved")
           else "warn" if nxt == "end" else "info")
    return {**extra, "next": nxt, "status": status, "trace": [ev("Supervisor", f"route → {nxt}", why, level=lvl)]}


def demand(state: SupplyState, config: RunnableConfig) -> dict:
    """Demand Agent: deterministic forecasts (best model per SKU by backtest), persisted with their error metrics."""
    cfg = get_settings()
    hist = load_all_histories(cfg.risk.max_forecast_skus)
    if not hist:
        return {"stage": "forecasted", "forecast_summary": {"skus": 0},
                "trace": [ev("Demand", "forecast", "No demand history in the database: stock-out risk falls back to static reorder-point rules.",
                             level="info")]}
    forecaster = DemandForecaster()
    fcs = [cached_forecast(forecaster, sku, h) for sku, h in hist.items()]
    wf = _wf(state, config)
    save_forecasts(fcs, wf)
    methods: dict[str, int] = {}
    for f in fcs:
        methods[f.method] = methods.get(f.method, 0) + 1
    maes = [f.metrics["mae"] for f in fcs if f.metrics.get("mae") is not None]
    low_conf = [f.sku_id for f in fcs if f.confidence < 0.3]
    summary = {"skus": len(fcs), "methods": methods, "mean_mae": round(sum(maes) / len(maes), 3) if maes else None,
               "mean_confidence": round(sum(f.confidence for f in fcs) / len(fcs), 3), "low_confidence": low_conf[:20]}
    audit.safe_record(audit.EventType.FORECAST_GENERATED, wf, "agent:demand", summary)
    BUS.publish(EventName.DEMAND_FORECAST_UPDATED, summary, wf)
    detail = "\n".join(f"- {f.sku_id}: {f.expected_demand:,.0f} units / {f.horizon_days}d via {f.method} (confidence {f.confidence:.0%}"
                       + (f", MAE {f.metrics['mae']:.2f}" if f.metrics.get("mae") is not None else "") + ")" for f in fcs[:30])
    msg = f"Forecast {len(fcs)} SKU(s): " + ", ".join(f"{k}×{v}" for k, v in sorted(methods.items())) + f"; mean confidence {summary['mean_confidence']:.0%}."
    trace = [ev("Demand", "forecast", msg, detail)]
    if low_conf:
        trace.append(ev("Demand", "forecast confidence", f"{len(low_conf)} SKU(s) have low forecast confidence (<30%); static rules still apply to them.",
                        ", ".join(low_conf[:10]), level="warn"))
    return {"stage": "forecasted", "forecast_summary": summary, "trace": trace}


def _lead_inputs() -> tuple[dict[str, float], float]:
    with db.connect() as c:
        lead = {r[0]: float(r[1]) for r in c.execute(
            "SELECT Item_ID, AVG(Lead_Time_Days) FROM vendor_listings WHERE Lead_Time_Days>=0 AND Unit_Price>0 GROUP BY Item_ID")}
    stds = [m.lead_time_variance ** 0.5 for m in suppliers.all_metrics().values() if m.lead_time_variance]
    return lead, (sum(stds) / len(stds) if stds else 0.0)


def analytics(state: SupplyState, config: RunnableConfig) -> dict:
    """Risk Agent: stock-out risk per SKU from the deterministic risk engine (+ optional LLM narrative)."""
    rcfg = get_settings().risk
    wf = _wf(state, config)
    inv = db.df("SELECT * FROM inventory").to_dict("records")
    hist = load_all_histories(rcfg.max_forecast_skus)
    forecaster, (lead, lead_std) = DemandForecaster(), _lead_inputs()
    low_ids = {r["Item_ID"] for r in db.low_stock_items()}
    in_transit = open_orders_by_item()
    alerts, integrity, flagged = [], [], 0
    for r in inv:
        legacy_low = r["Item_ID"] in low_ids
        if r["Stock_Level"] < 0 or r["Reorder_Point"] <= 0 or r["Target_Stock"] <= r["Reorder_Point"]:
            if legacy_low:
                integrity.append(f"- {r['Item_ID']}: stock={r['Stock_Level']} reorder={r['Reorder_Point']} target={r['Target_Stock']}")
        stock = max(r["Stock_Level"], 0)  # negative stock is a data error; plan from zero
        on_order = in_transit.get(r["Item_ID"], 0)
        h = hist.get(r["Item_ID"])
        if h and rcfg.predictive_alerts:
            fc = cached_forecast(forecaster, r["Item_ID"], h)
            ra = assess(r["Item_ID"], stock, fc, lead.get(r["Item_ID"], rcfg.default_lead_time_days), r["Target_Stock"], r["Reorder_Point"], on_order, lead_std)
        else:
            fc, ra = None, static_assessment(r["Item_ID"], stock, r["Reorder_Point"], r["Target_Stock"], on_order)
        predictive = (ra.risk_level.rank >= RiskLevel.MEDIUM.rank) and not legacy_low
        if not legacy_low and not predictive:
            continue
        ratio = stock / max(r["Reorder_Point"], 1)  # reorder point 0 must not divide by zero
        legacy_urg = "critical" if ratio <= 0.35 else "high" if ratio <= 0.7 else "medium"
        risk_urg = {RiskLevel.CRITICAL: "critical", RiskLevel.HIGH: "high"}.get(ra.risk_level, "medium")
        urgency = max(legacy_urg, risk_urg, key=lambda u: URGENCY_RANK[u]) if legacy_low else risk_urg
        insight = (f"Stock is {ratio:.0%} of the reorder point ({stock}/{r['Reorder_Point']}); {r['Target_Stock'] - stock} units short of target."
                   if legacy_low else f"Above the static reorder point but forecast risk is {ra.risk_level.value}.")
        if ra.basis == "forecast" and fc is not None:
            insight += (f" Forecast {ra.daily_demand:.1f}/day ({fc.method}), P(stock-out) {ra.stockout_probability:.0%}"
                        + (f", {ra.days_until_stockout:.1f}d of cover vs {ra.lead_time_days:g}d lead time." if ra.days_until_stockout is not None else "."))
        alerts.append({**r, "Stock_Level": stock, "ratio": round(ratio, 2), "urgency": urgency, "insight": insight, "predictive": predictive,
                       "risk_level": ra.risk_level.value, "days_until_stockout": None if ra.days_until_stockout is None else round(ra.days_until_stockout, 2),
                       "stockout_probability": round(ra.stockout_probability, 3), "safety_stock": ra.safety_stock, "rec_qty": ra.recommended_order_quantity,
                       "basis": ra.basis, "forecast_daily": None if ra.daily_demand is None else round(ra.daily_demand, 3),
                       "forecast_method": fc.method if fc else None, "forecast_confidence": fc.confidence if fc else None,
                       "lead_time_est": ra.lead_time_days or None})
    alerts.sort(key=lambda a: (-URGENCY_RANK[a["urgency"]], a["ratio"]))
    for a in alerts[:50]:
        if a["risk_level"] in ("CRITICAL", "HIGH"):
            flagged += 1
            audit.safe_record(audit.EventType.RISK_DETECTED, wf, "agent:analytics", {k: a.get(k) for k in ("Item_ID", "risk_level", "days_until_stockout", "stockout_probability")})
    if alerts:
        BUS.publish(EventName.INVENTORY_LOW, {"skus": [a["Item_ID"] for a in alerts][:50], "count": len(alerts)}, wf)
    if flagged:
        BUS.publish(EventName.STOCKOUT_RISK_DETECTED, {"critical_or_high": flagged}, wf)
    n_pred = sum(a["predictive"] for a in alerts)
    trace = [ev("Analytics", "scan inventory",
                f"Scanned inventory: {len(alerts)} item(s) at risk" + (f" ({n_pred} predictive: above reorder point but forecast to run out)." if n_pred else " below reorder point."),
                "\n".join(f"- {a['Item_ID']} {a['Item_Name']}: {a['Stock_Level']}/{a['Reorder_Point']} → {a['urgency'].upper()}"
                          + (f" · {a['days_until_stockout']}d cover · P(stock-out) {a['stockout_probability']:.0%}" if a['basis'] == 'forecast' and a['days_until_stockout'] is not None else "")
                          for a in alerts) or "All items healthy.")]
    if integrity:
        trace.append(ev("Analytics", "data integrity", f"{len(integrity)} item(s) have inconsistent inventory data; "
                        "negative stock clamped to 0 and reorder point floored at 1 for planning.", "\n".join(integrity), level="warn"))
    llm_ok, tele = state.get("llm_ok", True), []
    if alerts:
        findings: list[str] = []
        brief = "\n".join(f"{a['Item_ID']}|{_safe_text('item_name', a['Item_Name'], findings, 'a')}|stock={a['Stock_Level']}|reorder={a['Reorder_Point']}|"
                          f"urgency={a['urgency']}" + (f"|cover_days={a['days_until_stockout']}|p_stockout={a['stockout_probability']}"
                                                         if a['basis'] == 'forecast' and a['days_until_stockout'] is not None else "") for a in alerts)
        trace += _injection_events("Analytics", findings)
        data, note, llm_ok, tele = _llm(
            state, config, "Analytics",
            "You are a supply-chain risk analyst. The risk numbers were computed by deterministic engines; you only describe them. Reply ONLY with JSON: "
            '{"summary": "<2 short sentences on overall stock-out risk>"}',
            brief, {"summary": str}, DemandAnalysis)
        trace += _fmt_events("Analytics", note)
        if data:
            for a in alerts:
                ins = (data.get("insights") or {}).get(a["Item_ID"]) if isinstance(data.get("insights"), dict) else None
                if isinstance(ins, str) and ins.strip():
                    a["insight"] = ins.strip()
            trace.append(ev("Analytics", "risk assessment", str(data["summary"]).strip(),
                            f"Model: {_cfg(config).get('llm_model')}", source="llm"))
        else:
            crit = sum(a["urgency"] == "critical" for a in alerts)
            trace.append(ev("Analytics", "risk assessment",
                            f"{crit} critical and {len(alerts) - crit} non-critical items need replenishment.",
                            f"Fallback used: {note}", level="warn" if "disabled" not in note else "info", source=_src(note)))
    return {"alerts": alerts, "stage": "analyzed", "llm_ok": llm_ok, "trace": trace, "telemetry": tele}


def supplier_intel(state: SupplyState, config: RunnableConfig) -> dict:
    """Supplier Intelligence Agent: deterministic reliability scoring from delivery history (blended with the static prior)."""
    suppliers.invalidate_cache()
    metrics = suppliers.all_metrics(persist=True)
    scores = {vid: {"score": m.reliability_score, "on_time": m.on_time_delivery_rate, "quality": m.quality_rate, "observations": m.observations,
                    "source": m.source} for vid, m in metrics.items()}
    ranked = sorted(metrics.values(), key=lambda m: -m.reliability_score)
    detail = "\n".join(f"- {m.vendor_id}: score {m.reliability_score:.1f} ({m.source}, n={m.observations})"
                       + (f", on-time {m.on_time_delivery_rate:.0%}" if m.on_time_delivery_rate is not None else "")
                       + (f", quality {m.quality_rate:.1%}" if m.quality_rate is not None else "") for m in ranked)
    msg = (f"Scored {len(metrics)} supplier(s); best {ranked[0].vendor_id} ({ranked[0].reliability_score:.0f}), "
           f"weakest {ranked[-1].vendor_id} ({ranked[-1].reliability_score:.0f})." if ranked else "No suppliers on file.")
    return {"stage": "scored", "supplier_scores": scores, "trace": [ev("Supplier Intel", "score suppliers", msg, detail)]}


def _why(quotes: list[dict], best: dict, avg: float, qty: int) -> str:
    fastest = min(quotes, key=lambda q: (q["Lead_Time_Days"], q["Unit_Price"]))
    reliable = max(quotes, key=lambda q: q["Reliability"])
    why = (f"Unit price ${best['Unit_Price']:.2f} is {(avg - best['Unit_Price']) / avg:.0%} below the ${avg:.2f} average of {len(quotes)} quotes")
    if fastest["Vendor_ID"] == best["Vendor_ID"]:
        why += " and it is also the fastest lead time"
    else:
        extra = fastest["Unit_Price"] - best["Unit_Price"]
        why += (f". {fastest['Vendor_Name']} delivers {best['Lead_Time_Days'] - fastest['Lead_Time_Days']}d sooner "
                f"but costs ${extra:.2f}/unit more (+${extra * qty:,.0f} on this order)")
    why += f". Static reliability {best['Reliability']:.0f}%"
    if reliable["Vendor_ID"] != best["Vendor_ID"]:
        why += f" ({reliable['Vendor_Name']} is highest at {reliable['Reliability']:.0f}%)"
    return why + "."


def _capacity(v) -> int | None:
    """NULL capacity (pandas turns it into NaN) means unlimited."""
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else int(v)


def procurement(state: SupplyState, config: RunnableConfig) -> dict:
    """Procurement / Optimisation Agent: sizes each order and allocates it across suppliers with the constrained optimiser."""
    excluded = state.get("excluded", {})
    metrics = suppliers.all_metrics()
    optimizer = SupplierOptimizer()
    proposals, trace = [], []
    for a in state["alerts"]:
        raw = [q for q in db.vendor_quotes(a["Item_ID"]) if q["Vendor_ID"] not in excluded.get(a["Item_ID"], [])]
        quotes = [q for q in raw if q["Unit_Price"] > 0 and q["Lead_Time_Days"] >= 0]
        if len(quotes) != len(raw):
            trace.append(ev("Procurement", a["Item_ID"], f"Ignored {len(raw) - len(quotes)} invalid quote(s) "
                            "(price <= 0 or negative lead time).", level="warn"))
        if not quotes:
            trace.append(ev("Procurement", f"{a['Item_ID']}", f"No eligible vendor for {a['Item_Name']}; skipped.", level="warn"))
            continue
        legacy_qty = math.ceil((a["Target_Stock"] - a["Stock_Level"]) / 5) * 5
        qty = a["rec_qty"] if a.get("basis") == "forecast" and a.get("rec_qty", 0) > 0 else legacy_qty
        if qty <= 0:
            trace.append(ev("Procurement", a["Item_ID"], f"Computed order quantity is {qty} (target {a['Target_Stock']} <= stock "
                            f"{a['Stock_Level']}); fix the item's Target_Stock. Skipped.", level="warn"))
            continue
        options = []
        for q in quotes:
            m = metrics.get(q["Vendor_ID"])
            options.append(SupplierOption(q["Vendor_ID"], q["Vendor_Name"], float(q["Unit_Price"]), float(q["Lead_Time_Days"]),
                                          m.reliability_score if m else float(q["Reliability"] or 90.0),
                                          m.quality_rate if (m and m.quality_rate is not None) else 1.0, int(q.get("MOQ") or 1),
                                          _capacity(q.get("Capacity")), str(q.get("Region") or "GLOBAL")))
        plan = optimizer.optimize(qty, options, a.get("days_until_stockout"))
        if plan.status == "infeasible":
            trace.append(ev("Procurement", a["Item_ID"], f"No feasible allocation for {a['Item_Name']}: {plan.explanation}", level="warn"))
            continue
        avg = db.avg_quote(quotes)
        by_id = {q["Vendor_ID"]: q for q in quotes}
        for vid, alloc_qty in plan.allocation.items():
            best = by_id[vid]
            m = metrics.get(vid)
            savings = max(0.0, (avg - best["Unit_Price"]) * alloc_qty)
            if alloc_qty * best["Unit_Price"] > 1_000_000:
                trace.append(ev("Procurement", a["Item_ID"], f"Line value ${alloc_qty * best['Unit_Price']:,.0f} exceeds $1M — "
                                "flagged for executive review.", level="warn"))
            proposals.append({"Item_ID": a["Item_ID"], "Item_Name": a["Item_Name"], "urgency": a["urgency"], "qty": alloc_qty,
                              "Vendor_ID": vid, "Vendor_Name": best["Vendor_Name"], "Contact_Email": best["Contact_Email"],
                              "unit_price": best["Unit_Price"], "lead_time": best["Lead_Time_Days"], "total": round(alloc_qty * best["Unit_Price"], 2),
                              "savings": round(savings, 2), "avg_quote": round(avg, 2), "n_quotes": len(quotes),
                              "note": plan.explanation, "quotes": quotes,
                              "why": plan.explanation + " " + _why(quotes, best, avg, alloc_qty),
                              "share": round(alloc_qty / max(sum(plan.allocation.values()), 1), 3), "split": len(plan.allocation) > 1,
                              "optimizer_status": plan.status, "plan_risk_score": round(plan.risk_score, 3),
                              "supplier_score": m.reliability_score if m else None, "risk_level": a.get("risk_level"),
                              "days_until_stockout": a.get("days_until_stockout"), "forecast_confidence": a.get("forecast_confidence"),
                              "required_qty": qty, "plan": plan.to_dict()})
        table = "\n".join(f"{'★' if q['Vendor_ID'] in plan.allocation else ' '} {q['Vendor_Name']:<26} ${q['Unit_Price']:>8.2f}  {q['Lead_Time_Days']}d"
                          + (f"  → {plan.allocation[q['Vendor_ID']]} units" if q['Vendor_ID'] in plan.allocation else "")
                          for q in quotes)
        split = " + ".join(f"{n} from {by_id[v]['Vendor_Name']} @ ${by_id[v]['Unit_Price']:.2f}" for v, n in plan.allocation.items())
        trace.append(ev("Procurement", a["Item_ID"],
                        f"{a['Item_Name']}: order {sum(plan.allocation.values())} units as {split} "
                        f"(required {qty}, packs of 5; objective ${plan.objective:,.2f}, plan risk {plan.risk_score:.0%}).",
                        f"Quote comparison:\n{table}\n{plan.explanation}"
                        + ("".join(f"\nExcluded {k}: {v}" for k, v in plan.excluded.items())),
                        level="warn" if plan.status == "fallback" else "info"))
        audit.safe_record(audit.EventType.SUPPLIER_SELECTED, _wf(state, config), "agent:procurement",
                          {"item_id": a["Item_ID"], "allocation": plan.allocation, "status": plan.status, "expected_cost": plan.expected_cost})
    llm_ok, tele = state.get("llm_ok", True), []
    if proposals:
        findings: list[str] = []
        brief = "\n".join(f"{p['Item_ID']}|{_safe_text('item_name', p['Item_Name'], findings, 'p')}|qty={p['qty']}|"
                          f"vendor={_safe_text('vendor_name', p['Vendor_Name'], findings, 'p')}|price={p['unit_price']}|"
                          f"avg_quote={p['avg_quote']}|lead={p['lead_time']}d|share={p['share']}" for p in proposals)
        trace += _injection_events("Procurement", findings)
        data, note, llm_ok, tele = _llm(
            state, config, "Procurement",
            "You explain a procurement decision that was ALREADY made by an optimiser; you cannot change it. Reply ONLY with JSON: "
            '{"summary": "<1-2 short sentences of sourcing rationale>"}',
            brief, {"summary": str}, ProcurementRecommendation)
        trace += _fmt_events("Procurement", note)
        trace += _order_salvage_events(proposals[0], last_raw())
        if data:
            notes = data.get("notes") if isinstance(data.get("notes"), dict) else {}
            for p in proposals:
                if isinstance(notes.get(p["Item_ID"]), str) and notes[p["Item_ID"]].strip():
                    p["note"] = notes[p["Item_ID"]].strip()
            trace.append(ev("Procurement", "negotiation strategy", str(data["summary"]).strip(),
                            f"Model: {_cfg(config).get('llm_model')}", source="llm"))
        else:
            trace.append(ev("Procurement", "negotiation strategy",
                            "Allocated by the constrained optimiser (price, lead time, reliability, MOQ, capacity).",
                            f"Fallback used: {note}", level="warn" if "disabled" not in note else "info", source=_src(note)))
    return {"proposals": proposals, "stage": "procured", "llm_ok": llm_ok, "trace": trace, "telemetry": tele}


def policy_check(state: SupplyState, config: RunnableConfig) -> dict:
    """Policy Validator: deterministic gate. Blocked lines are removed; warnings travel with the proposal to the human."""
    wf = _wf(state, config)
    rep = PolicyValidator().validate(state.get("proposals", []))
    kept = [p for p in state.get("proposals", []) if (p["Item_ID"], p["Vendor_ID"]) not in rep.blocked]
    for p in kept:
        p["policy_flags"] = [v.to_dict() for v in rep.for_line(p["Item_ID"], p["Vendor_ID"])]
    trace = []
    for v in rep.violations:
        blocked = v.severity == BLOCK
        trace.append(ev("Policy", v.code, ("BLOCKED: " if blocked else "") + v.message, f"{v.item_id} / {v.vendor_id}",
                        level="error" if blocked else "warn"))
        if blocked:
            audit.safe_record(audit.EventType.POLICY_VIOLATION, wf, "policy-validator", v.to_dict())
    if not rep.violations:
        trace.append(ev("Policy", "validate", f"All {len(kept)} line(s) passed the business-policy checks."))
    summary = {"blocked": [list(k) for k in rep.blocked], "violations": [v.to_dict() for v in rep.violations],
               "requires_executive_review": rep.requires_executive_review, "requires_elevated_review": rep.requires_elevated_review}
    return {"proposals": kept, "stage": "validated", "policy": summary, "trace": trace}


def compose_body(vendor_name: str, lines: list[dict], total: float, intro: str, closing: str, need_by: str) -> str:
    table = "\n".join(f"  {l['po_id']}  {l['Item_ID']}  {l['Item_Name'][:26]:<26} qty {l['qty']:>5}  "
                      f"@ ${l['unit_price']:>8.2f}  = ${l['total']:>10,.2f}" for l in lines)
    return (f"Dear {vendor_name} Sales Team,\n\n{intro}\n\n{table}\n\n"
            f"  ORDER TOTAL: ${total:,.2f}\n  REQUESTED DELIVERY: by {need_by}\n\n{closing}\n\n"
            f"Best regards,\nProcurement Desk — Northwind Operations\nprocurement@northwind-ops.example")


def vendor_comm(state: SupplyState, config: RunnableConfig) -> dict:
    wf, rounds = _wf(state, config), state.get("rounds", 0)
    created = orders.create_pending_pos(state["proposals"], wf, rounds)      # ONE transaction, idempotent per (workflow, round, item, vendor)
    by_vendor: dict[str, list[dict]] = {}
    for p, row in zip(state["proposals"], created):
        p["po_id"] = row["po_id"]
        by_vendor.setdefault(p["Vendor_ID"], []).append(p)
    drafts, trace, llm_ok, tele = [], [], state.get("llm_ok", True), []
    need_by = (datetime.now() + timedelta(days=14)).strftime("%d %b %Y")
    findings: list[str] = []

    def _email_llm(item):  # one model call per vendor; run concurrently instead of one after another
        _vid, _lines = item
        _total = round(sum(l["total"] for l in _lines), 2)
        vname = sanitize_untrusted(_lines[0]["Vendor_Name"], 80)[0]
        return _llm(state, config, "Vendor Comms",
                    "You write concise, professional B2B purchase-order emails for Northwind Operations. Reply ONLY with JSON: "
                    '{"intro": "<2 sentences>", "closing": "<1 sentence>"}. Do not include greetings, tables, prices or signatures.',
                    f"Vendor: {vname}. Items: " + ", ".join(f"{l['qty']}x {sanitize_untrusted(l['Item_Name'], 60)[0]}" for l in _lines) + f". Total ${_total:,.2f}.",
                    {"intro": str}, VendorMessage)
    for _vid, lines in by_vendor.items():
        for l in lines:
            _safe_text("vendor_name", l["Vendor_Name"], findings, "v")
            _safe_text("item_name", l["Item_Name"], findings, "v")
    trace += _injection_events("Vendor Comms", findings)
    if _cfg(config).get("llm_emails", True):
        with ThreadPoolExecutor(max_workers=max(1, len(by_vendor))) as pool:
            results = list(pool.map(db.propagate(_email_llm), by_vendor.items()))
    else:  # fast mode: templated emails, no model calls (each call costs ~20s on a CPU-only Ollama)
        results = [(None, "LLM disabled for emails (fast mode)", state.get("llm_ok", True), []) for _ in by_vendor]
    llm_ok = all(r[2] for r in results) and llm_ok
    for (vid, lines), (data, note, _ok, t) in zip(by_vendor.items(), results):
        v = lines[0]
        total = round(sum(l["total"] for l in lines), 2)
        intro = ("We would like to place the purchase order below and kindly request written confirmation "
                 "of pricing, availability and ship date.")
        closing = "Thank you for your continued partnership."
        tele += t
        trace += _fmt_events("Vendor Comms", note)
        src = "llm" if data else _src(note)
        if data:
            intro, closing = data["intro"].strip(), (data["closing"].strip() if isinstance(data.get("closing"), str) and data["closing"].strip() else closing)
        body = compose_body(v["Vendor_Name"], lines, total, intro, closing, need_by)
        drafts.append({"Vendor_ID": vid, "Vendor_Name": v["Vendor_Name"], "to": v["Contact_Email"],
                       "subject": f"Purchase Order {', '.join(l['po_id'] for l in lines)} — Northwind Operations",
                       "body": body, "po_ids": [l["po_id"] for l in lines], "total": total, "lines": lines, "source": src})
        trace.append(ev("Vendor Comms", f"draft → {v['Vendor_Name']}",
                        f"Drafted PO email covering {len(lines)} line(s), total ${total:,.2f}. Saved as PENDING_APPROVAL.",
                        "" if data else f"Template used: {note}", source=src,
                        level="info" if data or "disabled" in note else "warn"))
    trace.append(ev("Vendor Comms", "awaiting approval",
                    "Final dispatch step is intercepted by the HITL breakpoint. Nothing has been sent.", level="warn"))
    BUS.publish(EventName.PROCUREMENT_CREATED, {"po_ids": [p["po_id"] for p in state["proposals"]]}, wf)
    BUS.publish(EventName.APPROVAL_REQUESTED, {"vendors": [d["Vendor_ID"] for d in drafts]}, wf)
    audit.safe_record(audit.EventType.HUMAN_APPROVAL_REQUESTED, wf, "agent:vendor_comm",
                      {"po_ids": [p["po_id"] for p in state["proposals"]], "batch_total": round(sum(d["total"] for d in drafts), 2),
                       "requires_executive_review": bool((state.get("policy") or {}).get("requires_executive_review"))})
    return {"drafts": drafts, "proposals": state["proposals"], "stage": "drafted", "llm_ok": llm_ok, "trace": trace,
            "telemetry": tele}


def erp_payload(drafts: list[dict], proposals: list[dict], status: str = "PENDING_HUMAN_REVIEW") -> dict:
    """Structured PO batch that would be POSTed to an ERP (SAP / NetSuite / DB). Simulated: nothing is sent."""
    return {
        "event": "purchase_order.batch.created", "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"), "source_system": "supply-chain-engine",
        "approval": {"status": status, "approver_required": True},
        "currency": "USD",
        "purchase_orders": [{
            "po_number": p["po_id"],
            "vendor": {"id": p["Vendor_ID"], "name": p["Vendor_Name"], "email": p["Contact_Email"]},
            "lines": [{"line_no": 1, "sku": p["Item_ID"], "description": p["Item_Name"], "quantity": p["qty"],
                       "unit_price": p["unit_price"], "line_total": p["total"]}],
            "lead_time_days": p["lead_time"], "priority": p["urgency"], "order_total": p["total"],
            "estimated_savings": p["savings"]} for p in proposals],
        "batch_total": round(sum(p["total"] for p in proposals), 2),
        "notifications": [{"channel": "email", "to": d["to"], "subject": d["subject"]} for d in drafts],
    }


def _stage_files(out, staging, drafts: list[dict], payload: dict, first_po: str) -> list[str]:
    staging.mkdir(parents=True, exist_ok=True)
    names = []
    for d in drafts:
        n = f"{d['po_ids'][0]}_{d['Vendor_ID']}.txt"
        (staging / n).write_text(f"To: {d['to']}\nSubject: {d['subject']}\n\n{d['body']}\n", encoding="utf-8")
        names.append(n)
    n = f"ERP_{first_po}.json"
    (staging / n).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return names + [n]


def vendor_dispatch(state: SupplyState, config: RunnableConfig) -> dict:
    """Final step of the Vendor Communication agent. Graph pauses BEFORE this node (interrupt_before)."""
    decision = state.get("decision") or "reject"
    drafts = state.get("drafts", [])
    wf = _wf(state, config)
    actor = state.get("approver") or "unknown-approver"
    reason = state.get("decision_reason", "")
    all_pos = [po for d in drafts for po in d["po_ids"]]
    rounds = state.get("rounds", 0)
    choice = [{"decision": decision, "ts": datetime.now().strftime("%H:%M:%S"), "by": actor}]
    if decision == "resource" and rounds >= MAX_RESOURCE_ROUNDS:
        decision = "reject"
    for d in drafts:
        if d.get("edited"):
            audit.safe_record(audit.EventType.PROCUREMENT_EDITED, wf, actor,
                              {"po_ids": d["po_ids"], "vendor_id": d["Vendor_ID"], "field": "email_body",
                               "previous_value": (d.get("original_body") or "")[:4000], "new_value": d["body"][:4000], "reason": reason})
    if decision == "approve":
        stale = PolicyValidator().validate(state.get("proposals", []))       # data may have changed while the human was reviewing
        if stale.blocked:
            msgs = "; ".join(v.message for v in stale.violations if v.severity == BLOCK)[:500]
            orders.finalize_orders(all_pos, "REJECTED", False, wf, actor, audit.EventType.POLICY_VIOLATION,
                                   {"stage": "dispatch", "reason": "re-validation at approval time failed", "violations": msgs})
            return {"stage": "dispatched", "status": "policy_blocked", "human_choices": choice, "decision": "reject", "outcome": {"po_ids": all_pos},
                    "trace": [ev("Policy", "approval refused", "Approval refused: business data changed since review and re-validation failed. "
                                 "No order was sent; the POs were rejected.", msgs, level="error")]}
        out = db.outbox_dir()
        out.mkdir(parents=True, exist_ok=True)
        payload = erp_payload(drafts, state["proposals"], "APPROVED")
        staging = out / f".staging-{obs.new_id()[:8]}"
        audit.safe_record(audit.EventType.DISPATCH_STARTED, wf, actor, {"po_ids": all_pos})
        try:
            names = _stage_files(out, staging, drafts, payload, all_pos[0])
            orders.finalize_orders(all_pos, "SENT", True, wf, actor, audit.EventType.HUMAN_APPROVED,
                                   {"reason": reason, "vendors": [d["Vendor_ID"] for d in drafts], "batch_total": round(sum(d["total"] for d in drafts), 2)})
        except Exception as exc:                      # nothing was committed: files are discarded, orders stay pending -> safe_node rolls back
            shutil.rmtree(staging, ignore_errors=True)
            audit.safe_record(audit.EventType.DISPATCH_FAILED, wf, actor, {"error": str(exc)[:300], "po_ids": all_pos})
            raise
        for n in names:                               # promote staged files only AFTER the database commit
            os.replace(staging / n, out / n)
        shutil.rmtree(staging, ignore_errors=True)
        hook_events = ingest.dispatch_hook(payload)   # no-op unless a webhook is armed
        edited = sum(bool(d.get("edited")) for d in drafts)
        total, sav = sum(d["total"] for d in drafts), sum(p["savings"] for p in state["proposals"])
        audit.safe_record(audit.EventType.DISPATCH_COMPLETED, wf, "system", {"po_ids": all_pos, "emails": len(drafts), "files": names})
        BUS.publish(EventName.PROCUREMENT_APPROVED, {"po_ids": all_pos, "approver": actor}, wf)
        BUS.publish(EventName.DISPATCH_COMPLETED, {"po_ids": all_pos, "total": round(total, 2)}, wf)
        return {"stage": "dispatched", "status": "approved", "human_choices": choice, "decision": "approve",
                "outcome": {"po_ids": all_pos, "total": round(total, 2), "savings": round(sav, 2), "emails": len(drafts)},
                "trace": [ev("Vendor Comms", "dispatch",
                             f"Approved by {actor}. {len(drafts)} email(s) ({edited} edited by reviewer) + ERP payload written to /outbox, "
                             f"{len(all_pos)} PO(s) marked SENT, "
                             f"stock updated (simulated receipt). Savings booked: ${sav:,.2f}.", level="success")] + hook_events}
    final = "REJECTED"
    orders.finalize_orders(all_pos, final, False, wf, actor,
                           audit.EventType.HUMAN_RESOURCED if decision == "resource" else audit.EventType.HUMAN_REJECTED, {"reason": reason})
    BUS.publish(EventName.PROCUREMENT_REJECTED, {"po_ids": all_pos, "decision": decision}, wf)
    if decision == "resource":
        excl = {k: list(v) for k, v in state.get("excluded", {}).items()}
        for p in state["proposals"]:
            excl.setdefault(p["Item_ID"], []).append(p["Vendor_ID"])
        return {"stage": "analyzed", "status": "resourcing", "decision": "", "proposals": [], "drafts": [],
                "excluded": excl, "rounds": rounds + 1, "human_choices": choice,
                "trace": [ev("Vendor Comms", "re-source", "Orders denied. Rejected vendors excluded; looping back to "
                             "Procurement for next-best suppliers.", level="warn")]}
    return {"stage": "dispatched", "status": "rejected", "human_choices": choice, "decision": "reject",
            "outcome": {"po_ids": all_pos},
            "trace": [ev("Vendor Comms", "dispatch", f"Denied. {len(all_pos)} PO(s) marked REJECTED; no emails sent.", level="error")]}


# --------------------------------------------------------------------------- graph
NODE_LABEL = {"supervisor": "Supervisor", "demand": "Demand", "analytics": "Analytics", "supplier_intel": "Supplier Intel",
              "procurement": "Procurement", "policy_check": "Policy", "vendor_comm": "Vendor Comms", HITL_NODE: "Vendor Comms"}


def safe_node(name: str, fn, takes_config: bool = True):
    """Global try/except around every node: exceptions become structured state instead of crashing the app. Every node is a traced span."""
    def wrapper(state: SupplyState, config: RunnableConfig):
        wf = _wf(state, config)
        token = obs.bind(workflow_id=wf, trace_id=state.get("trace_id") or _cfg(config).get("trace_id"), run_id=_cfg(config).get("thread_id"))
        try:
            with obs.span(NODE_LABEL[name], name) as sp:
                try:
                    if _cfg(config).get("fault_crash") == name:
                        raise RuntimeError(f"Injected fault: simulated crash inside node '{name}'")
                    result = fn(state, config) if takes_config else fn(state)
                    for t in (result or {}).get("telemetry", []) if isinstance(result, dict) else []:
                        sp.add_llm(_cfg(config).get("llm_model"), t.get("in", 0), t.get("out", 0))
                    return result
                except GraphBubbleUp:  # LangGraph control flow (interrupts) must pass through untouched
                    raise
                except Exception as exc:
                    sp.fail(exc)
                    tb = traceback.format_exc()
                    try:
                        db.rollback_pending()
                    except Exception:
                        tb += "\n(rollback of pending POs also failed)\n" + traceback.format_exc()
                    err = {"node": name, "type": type(exc).__name__, "message": str(exc), "traceback": tb,
                           "ts": datetime.now().strftime("%H:%M:%S")}
                    audit.safe_record(audit.EventType.WORKFLOW_FAILED, wf, "system", {"node": name, "error": f"{type(exc).__name__}: {exc}"[:300]})
                    return {"stage": "failed", "status": "error", "next": "end", "errors": [err],
                            "trace": [ev(NODE_LABEL[name], "EXCEPTION", f"{type(exc).__name__}: {exc}",
                                         "\n".join(tb.strip().splitlines()[-6:]), level="error")]}
        finally:
            obs.unbind(token)
    wrapper.__name__ = name
    return wrapper


def build_graph(checkpointer=None):
    g = StateGraph(SupplyState)
    for name, fn in [("supervisor", supervisor), ("demand", demand), ("analytics", analytics), ("supplier_intel", supplier_intel),
                     ("procurement", procurement), ("policy_check", policy_check), ("vendor_comm", vendor_comm), (HITL_NODE, vendor_dispatch)]:
        g.add_node(name, safe_node(name, fn))
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["next"],
                            {"demand": "demand", "analytics": "analytics", "supplier_intel": "supplier_intel", "procurement": "procurement",
                             "policy_check": "policy_check", "vendor_comm": "vendor_comm", HITL_NODE: HITL_NODE, "end": END})
    for worker in ("demand", "analytics", "supplier_intel", "procurement", "policy_check", "vendor_comm", HITL_NODE):
        g.add_edge(worker, "supervisor")
    return g.compile(checkpointer=checkpointer or MemorySaver(), interrupt_before=[HITL_NODE])


def initial_state() -> dict:
    return {"stage": "start", "alerts": [], "proposals": [], "drafts": [], "decision": "", "human_choices": [],
            "trace": [], "excluded": {}, "rounds": 0, "llm_ok": True, "status": "running", "outcome": {}, "telemetry": [], "errors": []}
