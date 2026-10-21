"""What-if simulation: change demand, inventory, supplier price / capacity / lead time, budget or risk tolerance and see the resulting procurement decision.

Deterministic and reproducible: a pure function of (data snapshot, scenario). No randomness, no LLM, no database writes. It re-runs the SAME
forecast -> risk -> optimiser -> tenant-rules chain the workflow uses, on an in-memory copy of the tenant's data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean

from pydantic import BaseModel, ConfigDict, Field

from scm import db, suppliers
from scm.forecast import DemandForecaster, Forecast, load_all_histories
from scm.optimizer import SupplierOption, SupplierOptimizer
from scm.risk import RiskLevel, assess, static_assessment
from scm.settings import get_settings, use_settings

from .policies import RISK_PROFILES
from .simulation import quality_of, reliability_of


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field("scenario", max_length=80)
    demand_multiplier: float = Field(1.0, gt=0, le=10)                     # 1.3 = demand +30 %
    stock_multiplier: float = Field(1.0, ge=0, le=10)
    stock_delta: int = Field(0, ge=-10_000_000, le=10_000_000)
    price_multiplier: float = Field(1.0, gt=0, le=10)
    price_multiplier_by_supplier: dict[str, float] = Field(default_factory=dict)
    capacity_multiplier: float = Field(1.0, gt=0, le=10)
    lead_time_delta_days: float = Field(0.0, ge=-30, le=365)
    budget: float | None = Field(None, gt=0)                               # cap on the total value of the recommended orders
    risk_tolerance: str | None = Field(None, pattern="^(low|medium|high)$")
    order_up_to: str | None = Field(None, pattern="^(static_floor|dynamic)$")      # None = the platform / tenant setting
    sku_filter: list[str] = Field(default_factory=list, max_length=2000)


@dataclass
class Snapshot:
    inventory: list[dict]
    history: dict[str, list[float]]
    listings: dict[str, list[dict]]                    # sku -> quotes
    metrics: dict[str, suppliers.SupplierMetrics]
    vendors: dict[str, dict]


def load_snapshot(max_history_days: int = 180) -> Snapshot:
    with db.connect() as c:
        inv = [dict(r) for r in c.execute("SELECT * FROM inventory ORDER BY Item_ID")]
        vendors = {r["Vendor_ID"]: dict(r) for r in c.execute("SELECT * FROM vendors")}
        listings: dict[str, list[dict]] = {}
        for r in c.execute("SELECT * FROM vendor_listings"):
            listings.setdefault(r["Item_ID"], []).append(dict(r))
    hist = {k: v[-max_history_days:] for k, v in load_all_histories().items()}
    return Snapshot(inv, hist, listings, suppliers.all_metrics(), vendors)


@dataclass
class SkuDecision:
    sku: str
    name: str
    category: str
    stock: int
    daily_demand: float | None
    risk_level: str
    stockout_probability: float
    days_until_stockout: float | None
    required_qty: int
    demand_driven_qty: int = 0                         # what the forecast alone would require (dynamic order-up-to), independent of the customer's static target
    allocation: dict[str, int] = field(default_factory=dict)
    expected_cost: float = 0.0
    weighted_lead_days: float | None = None
    plan_status: str = ""
    funded: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k in ("daily_demand", "stockout_probability", "days_until_stockout", "expected_cost", "weighted_lead_days"):
            if isinstance(d[k], float):
                d[k] = round(d[k], 4)
        return d


@dataclass
class Outcome:
    scenario: Scenario
    decisions: list[SkuDecision]

    @property
    def totals(self) -> dict:
        ds = self.decisions
        ordered = [d for d in ds if d.required_qty > 0 and d.funded and d.allocation]
        n = len(ds) or 1
        return {"skus": len(ds), "skus_to_order": len(ordered), "required_qty": sum(d.required_qty for d in ds), "demand_driven_qty": sum(d.demand_driven_qty for d in ds), "funded_qty": sum(sum(d.allocation.values()) for d in ordered),
                "expected_cost": round(sum(d.expected_cost for d in ordered), 2), "unfunded_skus": [d.sku for d in ds if d.required_qty > 0 and not d.funded],
                "skus_without_feasible_plan": [d.sku for d in ds if d.required_qty > 0 and not d.allocation and d.funded],
                "high_or_critical_risk_skus": sum(d.risk_level in ("HIGH", "CRITICAL") for d in ds), "critical_risk_skus": sum(d.risk_level == "CRITICAL" for d in ds),
                "mean_stockout_probability": sum(d.stockout_probability for d in ds) / n,
                "avg_lead_time_days": (sum((d.weighted_lead_days or 0) * sum(d.allocation.values()) for d in ordered) / max(1, sum(sum(d.allocation.values()) for d in ordered)))
                if ordered else None}


def _scaled_forecast(fc: Forecast, m: float) -> Forecast:
    return Forecast(fc.sku_id, [x * m for x in fc.forecast], fc.expected_demand * m, fc.confidence, fc.method, fc.horizon_days, fc.daily_mean * m, fc.daily_std * m,
                    fc.history_days, fc.metrics, fc.candidates, fc.notes)


def evaluate(snap: Snapshot, sc: Scenario) -> Outcome:
    base = get_settings()
    s = base.model_copy(deep=True)
    if sc.risk_tolerance:
        z, pc, ph, pm = RISK_PROFILES[sc.risk_tolerance]
        s.forecast.service_level_z, s.risk.prob_critical, s.risk.prob_high, s.risk.prob_medium = z, pc, ph, pm
    if sc.order_up_to:
        s.risk.order_up_to = sc.order_up_to                                # type: ignore[assignment]
    dyn_cfg = s.risk.model_copy(update={"order_up_to": "dynamic"})
    fcr, decisions = DemandForecaster(s.forecast), []
    with use_settings(s):
        optimizer = SupplierOptimizer()
        for r in snap.inventory:
            sku = r["Item_ID"]
            if sc.sku_filter and sku not in sc.sku_filter:
                continue
            stock = max(0, int(round(r["Stock_Level"] * sc.stock_multiplier)) + sc.stock_delta)
            quotes = []
            for q in snap.listings.get(sku, []):
                vid = q["Vendor_ID"]
                if vid not in snap.vendors or q["Unit_Price"] <= 0 or q["Lead_Time_Days"] < 0:
                    continue
                cap = q.get("Capacity")
                cap = None if cap is None or (isinstance(cap, float) and math.isnan(cap)) else max(1, int(cap * sc.capacity_multiplier))
                quotes.append({**q, "Unit_Price": q["Unit_Price"] * sc.price_multiplier * sc.price_multiplier_by_supplier.get(vid, 1.0),
                               "Lead_Time_Days": max(0.0, q["Lead_Time_Days"] + sc.lead_time_delta_days), "Capacity": cap})
            lead = mean(q["Lead_Time_Days"] for q in quotes) if quotes else s.risk.default_lead_time_days
            hist = snap.history.get(sku)
            if hist:
                fc = _scaled_forecast(fcr.forecast(sku, hist, s.forecast.horizon_days), sc.demand_multiplier)
                ra = assess(sku, stock, fc, lead, r["Target_Stock"], r["Reorder_Point"], 0, 0.0, s.risk, s.forecast.service_level_z)
            else:
                ra = static_assessment(sku, stock, r["Reorder_Point"], r["Target_Stock"], 0, s.risk)
            need = ra.recommended_order_quantity if ra.risk_level.rank >= RiskLevel.MEDIUM.rank or stock < r["Reorder_Point"] else 0
            dyn_need = (assess(sku, stock, fc, lead, r["Target_Stock"], r["Reorder_Point"], 0, 0.0, dyn_cfg, s.forecast.service_level_z).recommended_order_quantity
                        if hist else need)
            d = SkuDecision(sku, r["Item_Name"], r["Category"] or "", stock, ra.daily_demand, ra.risk_level.value, ra.stockout_probability, ra.days_until_stockout, int(need), int(dyn_need))
            if need > 0:
                if not quotes:
                    d.note = "no usable supplier quote"
                else:
                    opts = [SupplierOption(q["Vendor_ID"], snap.vendors[q["Vendor_ID"]]["Vendor_Name"], q["Unit_Price"], q["Lead_Time_Days"],
                                           reliability_of(snap.metrics, q["Vendor_ID"], float(snap.vendors[q["Vendor_ID"]].get("Reliability") or 90)),
                                           quality_of(snap.metrics, q["Vendor_ID"]),
                                           int(q.get("MOQ") or 1), q["Capacity"], str(snap.vendors[q["Vendor_ID"]].get("Region") or "GLOBAL")) for q in quotes]
                    plan = optimizer.optimize(int(need), opts, ra.days_until_stockout)
                    d.plan_status, d.allocation = plan.status, dict(plan.allocation)
                    d.expected_cost, d.weighted_lead_days = plan.expected_cost, plan.weighted_lead_time_days if plan.allocation else None
                    if plan.status == "infeasible":
                        d.note = plan.explanation[:200]
            decisions.append(d)
    if sc.budget is not None:                                           # fund the most urgent SKUs first until the budget is used
        spent = 0.0
        for d in sorted((d for d in decisions if d.allocation), key=lambda x: (-RiskLevel(x.risk_level).rank, x.days_until_stockout if x.days_until_stockout is not None else 1e9)):
            if spent + d.expected_cost <= sc.budget:
                spent += d.expected_cost
            else:
                d.funded, d.note = False, f"not funded: would exceed the budget of {sc.budget:,.2f}"
    return Outcome(sc, decisions)


def compare(snap: Snapshot, sc: Scenario) -> dict:
    """Base case vs the scenario: what changes in quantity, risk, supplier allocation and cost."""
    b, a = evaluate(snap, Scenario(name="base", sku_filter=sc.sku_filter)), evaluate(snap, sc)
    bt, at = b.totals, a.totals
    bd = {d.sku: d for d in b.decisions}
    changes = []
    for d in a.decisions:
        prev = bd.get(d.sku)
        if prev and (prev.allocation != d.allocation or prev.required_qty != d.required_qty or prev.risk_level != d.risk_level):
            changes.append({"sku": d.sku, "risk_before": prev.risk_level, "risk_after": d.risk_level, "required_qty_before": prev.required_qty, "required_qty_after": d.required_qty,
                            "allocation_before": prev.allocation, "allocation_after": d.allocation, "cost_before": round(prev.expected_cost, 2), "cost_after": round(d.expected_cost, 2)})

    def delta(k: str) -> float | None:
        return None if bt.get(k) is None or at.get(k) is None else at[k] - bt[k]
    pct = lambda k: None if not bt.get(k) else (at[k] - bt[k]) / bt[k]
    return {"scenario": sc.model_dump(), "base": {**bt, "decisions": [d.to_dict() for d in b.decisions]}, "result": {**at, "decisions": [d.to_dict() for d in a.decisions]},
            "delta": {"required_qty": delta("required_qty"), "required_qty_pct": pct("required_qty"), "demand_driven_qty": delta("demand_driven_qty"),
                      "demand_driven_qty_pct": pct("demand_driven_qty"), "expected_cost": delta("expected_cost"), "expected_cost_pct": pct("expected_cost"),
                      "high_or_critical_risk_skus": delta("high_or_critical_risk_skus"), "mean_stockout_probability": delta("mean_stockout_probability"),
                      "mean_stockout_probability_pct": pct("mean_stockout_probability"), "avg_lead_time_days": delta("avg_lead_time_days")},
            "allocation_changes": changes, "deterministic": True,
            "reading": "required_qty follows the customer's order-up-to policy (static target stock unless 'dynamic'); demand_driven_qty is what the forecast alone would require, "
                       "so a demand change is visible even while the static target dominates the order size.",
            "note": "Computed by the same forecast / risk / optimiser engines the workflow uses, on a copy of the data. Nothing was written and no model was called."}
