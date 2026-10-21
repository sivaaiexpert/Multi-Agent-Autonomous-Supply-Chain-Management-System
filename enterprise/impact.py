"""Business Impact Engine: measurable KPIs computed from the CURRENT tenant's own data (call inside `tenancy.scope`).

Every metric carries its PROVENANCE so nothing simulated or assumed can be mistaken for a measurement:
    OBSERVED          computed directly from recorded data (orders, deliveries, audit timestamps, demand history)
    BACKTEST          computed by replaying the tenant's own history through the simulator (a model of what would have happened)
    SIMULATED         computed on synthetic data through the simulator
    ASSUMPTION_BASED  depends on an explicit, configurable assumption (e.g. minutes a buyer needs per order line)
and the tenant's data provenance (SYNTHETIC demo data vs customer data) is attached to every report.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, median

from scm import db, obs
from scm.forecast import DemandForecaster, MovingAverage, error_metrics, load_all_histories
from scm.risk import assess, static_assessment
from scm.settings import get_settings

from . import simulation as sim

OBSERVED, BACKTEST, SIMULATED, ASSUMED = "OBSERVED", "BACKTEST", "SIMULATED", "ASSUMPTION_BASED"

BASELINES = {
    "manual_buyer": "lowest sticker price among all quotes the supplier master lists (what a buyer without the platform typically does)",
    "average_quote": "mean of all listed quotes for the SKU (the 'vs market' convention used by the console's savings KPI)",
    "last_paid": "the price of the previous purchase order for the same SKU (falls back to nothing when there is none)",
}


@dataclass
class Metric:
    key: str
    value: float | None
    unit: str
    provenance: str
    method: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"key": self.key, "value": None if self.value is None else round(self.value, 6), "unit": self.unit, "provenance": self.provenance,
                "method": self.method, "details": self.details}


def data_provenance() -> str:
    with db.connect() as c:
        r = c.execute("SELECT Value FROM app_state WHERE Key='data_provenance'").fetchone()
    return r[0] if r else "CUSTOMER_DATA"


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _pct(a: float, b: float) -> float | None:
    return None if not b else a / b


# ------------------------------------------------------------------------------------------------ procurement
def procurement_kpis(baseline: str = "manual_buyer", since: str | None = None) -> dict:
    """baseline_cost vs optimised_cost over the APPROVED purchase orders (same SKUs, same quantities, only the price paid differs).
    savings = baseline_cost - optimised_cost  (negative when the platform paid more than the baseline: that is reported, not hidden).
    The quote snapshot stored in the decision trace at the time of the decision is used, so later price changes cannot distort the comparison."""
    if baseline not in BASELINES:
        raise ValueError(f"baseline must be one of {list(BASELINES)}")
    from . import trace as T
    with db.connect() as c:
        pos = [dict(r) for r in c.execute("SELECT PO_ID, Item_ID, Vendor_ID, Quantity, Unit_Price, Timestamp, Workflow_ID FROM purchase_orders "
                                          "WHERE Status IN ('SENT','RECEIVED') AND Workflow_ID IS NOT NULL AND Timestamp >= ? ORDER BY Timestamp", (since or "0000",))]
    base_cost = opt_cost = lead_base = lead_opt = qty_total = 0.0
    n_lines = no_snapshot = 0
    rows = []
    traces: dict[str, dict | None] = {}
    for po in pos:
        tr = traces.setdefault(po["Workflow_ID"], T.load(po["Workflow_ID"]))
        quotes = ((tr or {}).get("supplier_candidates", {}) or {}).get(po["Item_ID"], [])
        if not quotes:
            no_snapshot += 1
            continue
        prices = [q["unit_price"] for q in quotes if q.get("unit_price", 0) > 0]
        if baseline == "manual_buyer":
            b = min(prices)
            lead = min(quotes, key=lambda q: (q["unit_price"], q["lead_time_days"]))["lead_time_days"]
        elif baseline == "average_quote":
            b, lead = mean(prices), mean(q["lead_time_days"] for q in quotes)
        else:
            with db.connect() as c:
                r = c.execute("SELECT Unit_Price FROM purchase_orders WHERE Item_ID=? AND Timestamp<? AND Status IN ('SENT','RECEIVED') ORDER BY Timestamp DESC LIMIT 1",
                              (po["Item_ID"], po["Timestamp"])).fetchone()
            if not r:
                continue
            b, lead = r[0], mean(q["lead_time_days"] for q in quotes)
        chosen = next((q for q in quotes if q["vendor_id"] == po["Vendor_ID"]), None)
        qty = po["Quantity"]
        base_cost += qty * b
        opt_cost += qty * po["Unit_Price"]
        qty_total += qty
        lead_base += qty * lead
        lead_opt += qty * (chosen["lead_time_days"] if chosen else lead)
        n_lines += 1
        rows.append({"po_id": po["PO_ID"], "sku": po["Item_ID"], "qty": qty, "baseline_unit_price": round(b, 4), "paid_unit_price": po["Unit_Price"]})
    savings = base_cost - opt_cost
    return {"baseline": baseline, "baseline_definition": BASELINES[baseline], "lines": n_lines, "lines_without_decision_snapshot": no_snapshot,
            "baseline_cost": round(base_cost, 2), "optimized_cost": round(opt_cost, 2), "savings": round(savings, 2),
            "savings_pct": _pct(savings, base_cost), "units": qty_total, "avg_lead_time_baseline_days": lead_base / qty_total if qty_total else None,
            "avg_lead_time_optimized_days": lead_opt / qty_total if qty_total else None, "provenance": OBSERVED, "data_provenance": data_provenance(),
            "attribution": "Differences are attributable to SUPPLIER SELECTION / allocation only: quantities are identical by construction. "
                           "They are not 'AI savings' unless the baseline is what the customer would really have done.",
            "lines_detail": rows[:200]}


# ------------------------------------------------------------------------------------------------ inventory
def inventory_kpis(run_backtest: bool = True, sim_cfg: sim.SimConfig | None = None) -> dict:
    """OBSERVED now: safety-stock adherence, excess inventory, SKUs at risk.  BACKTEST: stock-outs / turnover under each policy arm on the tenant's own history."""
    fc, cfg = DemandForecaster(), get_settings()
    with db.connect() as c:
        inv = [dict(r) for r in c.execute("SELECT * FROM inventory")]
        lead = {r[0]: float(r[1]) for r in c.execute("SELECT Item_ID, AVG(Lead_Time_Days) FROM vendor_listings WHERE Lead_Time_Days>=0 GROUP BY Item_ID")}
        price = {r[0]: float(r[1]) for r in c.execute("SELECT Item_ID, AVG(Unit_Price) FROM vendor_listings WHERE Unit_Price>0 GROUP BY Item_ID")}
    hist = load_all_histories()
    adherent = at_risk = excess_units = 0
    excess_value = 0.0
    for r in inv:
        stock = max(0, r["Stock_Level"])
        h = hist.get(r["Item_ID"])
        ra = (assess(r["Item_ID"], stock, fc.forecast(r["Item_ID"], h, 30), lead.get(r["Item_ID"], cfg.risk.default_lead_time_days), r["Target_Stock"], r["Reorder_Point"])
              if h else static_assessment(r["Item_ID"], stock, r["Reorder_Point"], r["Target_Stock"]))
        adherent += stock >= ra.safety_stock
        at_risk += ra.risk_level.rank >= 2
        ex = max(0, stock - r["Target_Stock"])
        excess_units += ex
        excess_value += ex * price.get(r["Item_ID"], 0.0)
    out: dict = {"skus": len(inv), "safety_stock_adherence_now": _pct(adherent, len(inv)), "skus_high_or_critical_risk_now": at_risk,
                 "excess_units_now": excess_units, "excess_value_now": round(excess_value, 2), "provenance_now": OBSERVED, "data_provenance": data_provenance()}
    if run_backtest:
        try:
            inp = sim.from_current_tenant(sim_cfg)
            res = sim.run_all_arms(inp, ("manual", "rule_based", "ai", "ai_lean"))
            out["backtest"] = {"provenance": BACKTEST if data_provenance() != "SYNTHETIC" else SIMULATED, "skus": len(inp.skus), "eval_days": inp.config.eval_days,
                               "assumptions": {"holding_rate_annual": inp.config.holding_rate_annual, "gross_margin": inp.config.gross_margin,
                                               "starting_stock": "today's stock level (stock history is not imported)"},
                               "arms": {a: r["summary"].to_dict() for a, r in res.items()}}
        except ValueError as exc:
            out["backtest"] = {"unavailable": str(exc)}
    return out


# ------------------------------------------------------------------------------------------------ forecasting
def forecast_accuracy(horizon: int = 14, origins: int = 4, min_history: int = 28) -> dict:
    """Rolling-origin back-test of the platform's forecaster on the tenant's history. Each origin forecasts the next `horizon` days using ONLY earlier data."""
    with db.connect() as c:
        cats = {r[0]: r[1] or "Uncategorised" for r in c.execute("SELECT Item_ID, Category FROM inventory")}
        series: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for item, day, qty in c.execute("SELECT Item_ID, Day, Quantity FROM demand_history ORDER BY Item_ID, Day"):
            series[item].append((day, qty))
    fcr = DemandForecaster()
    naive = MovingAverage(7)
    per_sku: dict[str, dict] = {}
    by_cat: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    by_period: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    all_a: list[float] = []
    all_p: list[float] = []
    naive_p: list[float] = []
    for sku, pts in series.items():
        n = len(pts)
        if n < min_history + horizon * origins:
            continue
        vals = [q for _, q in pts]
        a_s: list[float] = []
        p_s: list[float] = []
        for k in range(origins, 0, -1):
            o = n - horizon * k
            pred = fcr.forecast(sku, vals[:o], horizon).forecast
            actual = vals[o:o + horizon]
            a_s += actual
            p_s += pred
            all_a += actual
            all_p += pred
            naive_p += naive.predict(vals[:o], horizon)
            for (day, _), act, pr in zip(pts[o:o + horizon], actual, pred):
                m = day[:7]
                by_period[m][0].append(act)
                by_period[m][1].append(pr)
            ca = by_cat[cats.get(sku, "Uncategorised")]
            ca[0].extend(actual)
            ca[1].extend(pred)
        em = error_metrics(a_s, p_s)
        per_sku[sku] = {**{k: em[k] for k in ("mae", "rmse", "mape", "wape")}, "bias": (sum(p - a for a, p in zip(a_s, p_s)) / sum(a_s)) if sum(a_s) else None,
                        "points": len(a_s), "category": cats.get(sku)}

    def pack(a: list[float], p: list[float]) -> dict:
        if not a:
            return {}
        m = error_metrics(a, p)
        return {**m, "bias": (sum(x - y for x, y in zip(p, a)) / sum(a)) if sum(a) else None, "points": len(a)}
    return {"horizon_days": horizon, "origins": origins, "skus_evaluated": len(per_sku), "provenance": BACKTEST if data_provenance() != "SYNTHETIC" else SIMULATED,
            "data_provenance": data_provenance(), "overall": pack(all_a, all_p), "naive_last7_mean": pack(all_a, naive_p),
            "by_sku": per_sku, "by_category": {k: pack(v[0], v[1]) for k, v in sorted(by_cat.items())}, "by_period": {k: pack(v[0], v[1]) for k, v in sorted(by_period.items())},
            "note": "MAPE is None where it is mathematically invalid (zero actuals / intermittent demand); bias > 0 means the forecast over-predicts."}


# ------------------------------------------------------------------------------------------------ suppliers
def supplier_kpis() -> dict:
    """Per-supplier metrics from delivery history, plus the CHANGE between the earlier and the later half of the history (supplier improvement)."""
    from scm.suppliers import compute_metrics
    with db.connect() as c:
        vendors = {r[0]: (r[1], r[2] or 90.0) for r in c.execute("SELECT Vendor_ID, Vendor_Name, Reliability FROM vendors")}
        rows = [dict(r) for r in c.execute("SELECT * FROM supplier_deliveries ORDER BY Ordered_At")]
    by_v: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_v[r["Vendor_ID"]].append(r)
    out = []
    for vid, (name, prior) in vendors.items():
        recs = by_v.get(vid, [])
        m = compute_metrics(vid, recs, prior)
        half = len(recs) // 2
        trend = None
        if half >= 5:
            early, late = compute_metrics(vid, recs[:half], prior), compute_metrics(vid, recs[half:], prior)
            trend = {"on_time_delta": None if early.on_time_delivery_rate is None or late.on_time_delivery_rate is None else late.on_time_delivery_rate - early.on_time_delivery_rate,
                     "defect_rate_delta": None if early.defect_rate is None or late.defect_rate is None else late.defect_rate - early.defect_rate,
                     "lead_time_delta_days": None if early.average_lead_time is None or late.average_lead_time is None else late.average_lead_time - early.average_lead_time,
                     "score_delta": late.reliability_score - early.reliability_score}
        out.append({"vendor_id": vid, "name": name, **m.to_dict(), "trend": trend})
    deltas = [o["trend"]["score_delta"] for o in out if o["trend"]]
    return {"suppliers": out, "mean_reliability_score": mean(o["reliability_score"] for o in out) if out else None,
            "mean_score_change_early_vs_late": mean(deltas) if deltas else None, "provenance": OBSERVED, "data_provenance": data_provenance(),
            "note": "price_volatility is the mean coefficient of variation of the price paid per SKU; trend compares the earlier and later half of each supplier's deliveries."}


# ------------------------------------------------------------------------------------------------ operations
def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "median": None, "p90": None, "mean": None}
    s = sorted(xs)
    return {"n": len(xs), "median": median(s), "p90": s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)], "mean": mean(s)}


def operations_kpis(manual_minutes_per_line: float = 12.0) -> dict:
    """Cycle times from workflow rows and audit timestamps (OBSERVED); the manual-effort comparison depends on `manual_minutes_per_line` (ASSUMPTION_BASED)."""
    tid = _tenant()
    with db.connect() as c:
        runs = [dict(r) for r in c.execute("SELECT * FROM workflow_runs WHERE Tenant_ID=?", (tid,))]
        ev: dict[str, dict[str, str]] = defaultdict(dict)
        counts: dict[str, int] = defaultdict(int)
        for wf, et, ts in c.execute("SELECT Workflow_ID, Event_Type, Ts FROM audit_events WHERE Tenant_ID=? AND Workflow_ID IS NOT NULL ORDER BY Event_ID", (tid,)):
            ev[wf].setdefault(et, ts)
            counts[et] += 1
        lines = c.execute("SELECT COUNT(*), COUNT(DISTINCT Workflow_ID) FROM purchase_orders WHERE Tenant_ID=? AND Workflow_ID IS NOT NULL AND Status IN ('SENT','RECEIVED')",
                          (tid,)).fetchone()
    t_rec, t_appr, t_disp, t_total = [], [], [], []
    for r in runs:
        start = _ts(r["Started_At"]) or _ts(r["Created_At"])
        fin = _ts(r["Finished_At"])
        e = ev.get(r["Workflow_ID"], {})
        gate = _ts(e.get("HUMAN_APPROVAL_REQUESTED"))
        appr = _ts(e.get("HUMAN_APPROVED") or e.get("HUMAN_REJECTED") or e.get("HUMAN_RESOURCED"))
        disp = _ts(e.get("DISPATCH_COMPLETED"))
        done = _ts(e.get("WORKFLOW_COMPLETED"))
        if start and gate:
            t_rec.append((gate - start).total_seconds())
        if gate and appr:
            t_appr.append((appr - gate).total_seconds())
        if appr and disp:
            t_disp.append((disp - appr).total_seconds())
        if start and (done or fin) and r["Status"] in ("COMPLETED", "AWAITING_APPROVAL"):
            t_total.append(((done or fin) - start).total_seconds())            # type: ignore[operator]
    approved, rejected, resourced, edited = counts["HUMAN_APPROVED"], counts["HUMAN_REJECTED"], counts["HUMAN_RESOURCED"], counts["PROCUREMENT_EDITED"]
    decided = approved + rejected + resourced
    n_lines, n_wf = lines
    manual_minutes = n_lines * manual_minutes_per_line
    touch_minutes = (approved + rejected + resourced + edited) * 5.0           # ASSUMPTION: 5 minutes per review decision
    return {"workflows": len(runs), "provenance": OBSERVED, "data_provenance": data_provenance(),
            "time_to_recommendation_s": _stats(t_rec), "time_to_approval_s": _stats(t_appr), "time_to_dispatch_s": _stats(t_disp), "workflow_completion_s": _stats(t_total),
            "manual_interventions": {"approvals": approved, "rejections": rejected, "re_sources": resourced, "edits": edited, "total": approved + rejected + resourced + edited},
            "human_approval_rate": _pct(approved, decided), "re_source_rate": _pct(resourced, decided),
            "automation_rate": _pct(max(0, n_wf - edited - resourced - rejected), n_wf) if n_wf else None,
            "effort_comparison": {"provenance": ASSUMED, "order_lines": n_lines, "assumed_manual_minutes_per_line": manual_minutes_per_line,
                                  "assumed_minutes_per_review_decision": 5.0, "manual_process_minutes": manual_minutes, "platform_assisted_minutes": touch_minutes,
                                  "minutes_saved": manual_minutes - touch_minutes,
                                  "cycle_time_reduction_pct": _pct(manual_minutes - touch_minutes, manual_minutes)}}


# ------------------------------------------------------------------------------------------------ persistence
def snapshot_all(period: str | None = None, run_backtest: bool = True) -> list[Metric]:
    """Compute the headline KPIs, store them in `business_metrics` and return them."""
    period = period or datetime.now().strftime("%Y-%m")
    out: list[Metric] = []
    p = procurement_kpis()
    out += [Metric("procurement.baseline_cost", p["baseline_cost"], "currency", OBSERVED, f"baseline={p['baseline']}"),
            Metric("procurement.optimized_cost", p["optimized_cost"], "currency", OBSERVED, "price paid x quantity"),
            Metric("procurement.savings", p["savings"], "currency", OBSERVED, "baseline_cost - optimized_cost"),
            Metric("procurement.savings_pct", p["savings_pct"], "ratio", OBSERVED, "savings / baseline_cost")]
    f = forecast_accuracy()
    if f["overall"]:
        out += [Metric(f"forecast.{k}", f["overall"].get(k), "ratio" if k in ("wape", "bias") else "units", f["provenance"], "rolling-origin back-test")
                for k in ("mae", "rmse", "wape", "mape", "bias")]
    s = supplier_kpis()
    out.append(Metric("supplier.mean_reliability_score", s["mean_reliability_score"], "score", OBSERVED, "mean of supplier scores"))
    o = operations_kpis()
    out += [Metric("operations.human_approval_rate", o["human_approval_rate"], "ratio", OBSERVED, "approved / decided"),
            Metric("operations.automation_rate", o["automation_rate"], "ratio", OBSERVED, "workflows without edit / re-source / rejection")]
    i = inventory_kpis(run_backtest)
    out += [Metric("inventory.safety_stock_adherence_now", i["safety_stock_adherence_now"], "ratio", OBSERVED, "stock >= risk-engine safety stock"),
            Metric("inventory.excess_value_now", i["excess_value_now"], "currency", OBSERVED, "units above target x mean quote price")]
    now, tid = datetime.now().isoformat(timespec="seconds"), _tenant()
    with db.connect(write=True) as c:
        c.executemany("INSERT INTO business_metrics (Tenant_ID, Metric_Key, Period, Value, Unit, Provenance, Computed_At, Details_JSON) VALUES (?,?,?,?,?,?,?,?)",
                      [(tid, m.key, period, m.value, m.unit, m.provenance, now, json.dumps({"method": m.method, **m.details}, default=str)) for m in out])
    return out


def latest_metrics(limit: int = 200) -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT Metric_Key, Period, Value, Unit, Provenance, Computed_At, Details_JSON FROM business_metrics WHERE Tenant_ID=? ORDER BY Metric_ID DESC LIMIT ?",
                         (_tenant(), limit)).fetchall()
    seen, out = set(), []
    for r in rows:
        if (r[0], r[1]) in seen:
            continue
        seen.add((r[0], r[1]))
        out.append({"metric": r[0], "period": r[1], "value": r[2], "unit": r[3], "provenance": r[4], "computed_at": r[5], "details": json.loads(r[6] or "{}")})
    return out
