"""Baseline vs AI-optimised procurement decision, on the tenant's current data.

BASELINE (explicit, documented): the buyer's habit without the platform = the lowest STICKER price among all listed quotes, whole quantity to one supplier,
no check of MOQ / capacity / reliability / lead time.   OPTIMISED: the platform's constrained allocation for the SAME required quantity.

What the numbers mean (and do not):
  * `like_for_like_savings` prices the SAME units at the baseline's lowest price vs what the optimiser pays: it isolates the supplier-selection effect.
    It is NEGATIVE whenever the optimiser deliberately pays more for reliability / lead time / constraints: that is reported, never hidden.
  * The baseline's `effective` cost adds what the supplier would actually do with a constraint-blind order: raise it to the MOQ, truncate it at capacity.
  * Differences are attributed to supplier selection and allocation. They are NOT labelled "AI savings" and are not customer results unless this tenant is a real customer
    AND the baseline is what that customer would really have done.
  * Total cost of ownership uses the optimiser's OWN objective, so it cannot serve as independent validation; the simulator (`enterprise.simulation`) is the independent check.
"""
from __future__ import annotations

from scm.settings import get_settings

from . import impact
from .simulation import quality_of, reliability_of
from .whatif import Scenario, evaluate, load_snapshot

METHODOLOGY = (
    "1. For every SKU the risk engine marks MEDIUM or worse (or below its reorder point) the required quantity is taken from the risk engine.\n"
    "2. BASELINE: the whole quantity goes to the lowest-priced listed quote. If the supplier's MOQ is higher the order is raised to the MOQ; if it exceeds the "
    "supplier's capacity the excess is NOT delivered (shortfall).\n"
    "3. OPTIMISED: the constrained optimiser allocates the same quantity under MOQ, capacity, quality/reliability floors, tenant rules and the diversification cap.\n"
    "4. like-for-like savings = baseline unit price x optimised units - optimised cost (same units, so only supplier choice differs).\n"
    "5. Lead time and risk are allocation-weighted; risk = 1 - reliability x quality using the platform's delivery-history scores.")


def baseline_vs_optimized(sku_filter: list[str] | None = None) -> dict:
    snap = load_snapshot()
    out = evaluate(snap, Scenario(name="base", sku_filter=sku_filter or []))
    rows, tot = [], {"base_cost": 0.0, "base_eff_cost": 0.0, "opt_cost": 0.0, "like4like": 0.0, "units": 0, "shortfall": 0, "lead_b": 0.0, "lead_o": 0.0, "risk_b": 0.0, "risk_o": 0.0}
    violations = exposed_b = exposed_o = 0
    for d in out.decisions:
        if d.required_qty <= 0 or not d.allocation:
            continue
        quotes = [q for q in snap.listings.get(d.sku, []) if q["Unit_Price"] > 0 and q["Lead_Time_Days"] >= 0 and q["Vendor_ID"] in snap.vendors]
        if not quotes:
            continue
        b = min(quotes, key=lambda q: (q["Unit_Price"], q["Lead_Time_Days"], q["Vendor_ID"]))
        qty = d.required_qty
        eff = max(qty, int(b.get("MOQ") or 1))
        cap = b.get("Capacity")
        cap = None if cap is None or cap != cap else int(cap)
        delivered = min(eff, cap) if cap else eff
        m = snap.metrics.get(b["Vendor_ID"])
        rel = (m.reliability_score if m else float(snap.vendors[b["Vendor_ID"]].get("Reliability") or 90)) / 100
        qual = m.quality_rate if m and m.quality_rate is not None else 1.0
        floor = get_settings().supplier.min_reliability_score
        violated = (int(b.get("MOQ") or 1) > qty) or (cap is not None and cap < qty) or rel * 100 < floor or qual < get_settings().supplier.min_quality_rate
        violations += violated
        o_units = sum(d.allocation.values())
        o_risk = 1 - sum(n * (reliability_of(snap.metrics, v) / 100) * quality_of(snap.metrics, v)
                         for v, n in d.allocation.items()) / o_units
        base_cost, base_eff = qty * b["Unit_Price"], delivered * b["Unit_Price"]
        like = b["Unit_Price"] * o_units - d.expected_cost
        cover = d.days_until_stockout
        exposed_b += cover is not None and b["Lead_Time_Days"] > cover
        exposed_o += cover is not None and (d.weighted_lead_days or 0) > cover
        for k, v in (("base_cost", base_cost), ("base_eff_cost", base_eff), ("opt_cost", d.expected_cost), ("like4like", like), ("units", o_units), ("shortfall", max(0, qty - delivered)),
                     ("lead_b", b["Lead_Time_Days"] * qty), ("lead_o", (d.weighted_lead_days or 0) * o_units), ("risk_b", (1 - rel * qual) * qty), ("risk_o", o_risk * o_units)):
            tot[k] += v
        rows.append({"sku": d.sku, "required_qty": qty, "baseline_vendor": b["Vendor_ID"], "baseline_unit_price": round(b["Unit_Price"], 4), "baseline_cost": round(base_cost, 2),
                     "baseline_violates_a_constraint": bool(violated), "baseline_shortfall_units": max(0, qty - delivered), "optimized_allocation": d.allocation,
                     "optimized_cost": round(d.expected_cost, 2), "like_for_like_savings": round(like, 2), "baseline_lead_days": b["Lead_Time_Days"],
                     "optimized_lead_days": round(d.weighted_lead_days or 0, 2), "baseline_risk": round(1 - rel * qual, 4), "optimized_risk": round(o_risk, 4)})
    units = tot["units"] or 1
    req = sum(r["required_qty"] for r in rows) or 1
    return {"baseline_definition": impact.BASELINES["manual_buyer"], "skus_compared": len(rows), "baseline_cost": round(tot["base_cost"], 2),
            "baseline_effective_cost": round(tot["base_eff_cost"], 2), "optimized_cost": round(tot["opt_cost"], 2),
            "savings": round(tot["base_cost"] - tot["opt_cost"], 2), "savings_pct": ((tot["base_cost"] - tot["opt_cost"]) / tot["base_cost"]) if tot["base_cost"] else None,
            "like_for_like_savings": round(tot["like4like"], 2), "like_for_like_savings_pct": (tot["like4like"] / (tot["opt_cost"] + tot["like4like"])) if (tot["opt_cost"] + tot["like4like"]) else None,
            "baseline_shortfall_units": tot["shortfall"], "baseline_orders_violating_a_constraint": violations,
            "lead_time_difference_days": tot["lead_o"] / units - tot["lead_b"] / req, "lead_time_baseline_days": tot["lead_b"] / req, "lead_time_optimized_days": tot["lead_o"] / units,
            "risk_difference": tot["risk_o"] / units - tot["risk_b"] / req, "risk_baseline": tot["risk_b"] / req, "risk_optimized": tot["risk_o"] / units,
            "lines_where_lead_time_exceeds_stock_cover": {"baseline": exposed_b, "optimized": exposed_o}, "methodology": METHODOLOGY,
            "attribution": "Supplier selection and allocation only (same required quantity). Not an 'AI savings' claim; see enterprise.simulation / experiments for the independent check.",
            "provenance": impact.OBSERVED if impact.data_provenance() != "SYNTHETIC" else impact.SIMULATED, "data_provenance": impact.data_provenance(), "lines": rows}
