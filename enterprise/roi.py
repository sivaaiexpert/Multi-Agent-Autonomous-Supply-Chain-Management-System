"""Procurement ROI with EXPLICIT assumptions.

    annual ROI = (annual benefit - annual cost) / annual cost

Benefits are decomposed exactly (they sum to the gross-profit difference between the two policy arms of a simulation / back-test):
    purchase effect   (baseline cost per unit served - platform cost per unit served) x units the platform served
    service effect    extra units served x (revenue per unit - baseline cost per unit)
    holding effect    baseline carrying cost - platform carrying cost            (negative when the platform holds more stock)
    labour effect     buyer time avoided - review time spent                      (ASSUMPTION_BASED)
All of it is multiplied by a REALISATION FACTOR (default 0.5): simulated benefits are haircut because real-world execution is never perfect.
Costs are only what the caller provides (licence, infrastructure, integration) plus the MEASURED LLM cost; if no cost basis exists the ROI is reported as
undefined instead of dividing by zero or inventing a price. Nothing here is a customer result unless the input came from that customer's own back-test.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RoiAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_days: int = Field(60, ge=7, le=365)                 # length of the simulated / back-tested period the benefits come from
    realization_factor: float = Field(0.5, ge=0, le=1)         # share of the simulated benefit assumed to materialise
    manual_minutes_per_order_line: float = Field(12.0, ge=0)   # ASSUMPTION: buyer effort per order line without the platform
    review_minutes_per_decision: float = Field(5.0, ge=0)      # ASSUMPTION: effort to review one batch with the platform
    hourly_labor_cost: float = Field(30.0, ge=0)
    gross_margin: float = Field(0.25, ge=0, lt=1)
    annual_platform_license: float = Field(0.0, ge=0)          # NOT invented: the caller must supply their own price
    annual_infrastructure: float = Field(0.0, ge=0)
    annual_integration_and_support: float = Field(0.0, ge=0)
    llm_cost_per_workflow: float = Field(0.0, ge=0)            # measured per workflow (usage module); 0 for local inference
    workflows_per_year: int = Field(52, ge=0)                  # ASSUMPTION: one replenishment run per week
    currency: str = Field("USD", pattern="^[A-Z]{3}$")


def compute(baseline: dict, platform: dict, a: RoiAssumptions | None = None, provenance: str = "SIMULATED", data_provenance: str = "SYNTHETIC") -> dict:
    """`baseline` / `platform` are `ArmSummary.to_dict()` of two policy arms run on identical inputs."""
    a = a or RoiAssumptions()
    k = 365 / a.window_days
    s_b, s_p = baseline["units_sold"], platform["units_sold"]
    c_b = baseline["cost_per_unit_served"] or 0.0
    c_p = platform["cost_per_unit_served"] or 0.0
    rev_per_unit = (platform["revenue"] / s_p) if s_p else 0.0
    comps = [
        {"component": "purchase effect (price paid per unit served)", "window_value": (c_b - c_p) * s_p, "provenance": provenance,
         "formula": "(baseline cost per unit - platform cost per unit) x platform units served"},
        {"component": "service effect (additional demand served)", "window_value": (s_p - s_b) * (rev_per_unit - c_b), "provenance": provenance,
         "formula": "(platform units served - baseline units served) x (revenue per unit - baseline cost per unit)"},
        {"component": "inventory carrying cost", "window_value": baseline["holding_cost"] - platform["holding_cost"], "provenance": provenance,
         "formula": "baseline holding cost - platform holding cost"},
        {"component": "buyer time (assumption-based)", "window_value": (baseline["manual_touches"] * a.manual_minutes_per_order_line - platform["manual_touches"] * a.review_minutes_per_decision) / 60 * a.hourly_labor_cost,
         "provenance": "ASSUMPTION_BASED", "formula": "(baseline decisions x manual minutes - platform review decisions x review minutes) / 60 x hourly cost"},
    ]
    gp_diff = platform["gross_profit"] - baseline["gross_profit"]
    sim_sum = sum(c["window_value"] for c in comps[:3])
    for c in comps:
        c["annual_value_before_haircut"] = c["window_value"] * k
        c["annual_value"] = c["window_value"] * k * a.realization_factor
    benefit = sum(c["annual_value"] for c in comps)
    cost_items = {"platform licence": a.annual_platform_license, "infrastructure": a.annual_infrastructure, "integration and support": a.annual_integration_and_support,
                  "LLM usage (measured x workflows per year)": a.llm_cost_per_workflow * a.workflows_per_year}
    cost = sum(cost_items.values())
    roi = (benefit - cost) / cost if cost > 0 else None
    return {"provenance": provenance, "data_provenance": data_provenance, "assumptions": a.model_dump(),
            "window": {"days": a.window_days, "annualisation_factor": k, "gross_profit_difference_in_window": gp_diff,
                       "decomposition_check": {"sum_of_simulated_components": sim_sum, "difference": gp_diff - sim_sum,
                                               "note": "equals zero when revenue per unit is the same in both arms (the decomposition is exact then)"}},
            "components": comps, "annual_benefit": benefit, "annual_cost": cost, "cost_items": cost_items, "net_annual_benefit": benefit - cost, "annual_roi": roi,
            "payback_months": (cost / benefit * 12) if benefit > 0 and cost > 0 else None,
            "roi_note": ("ROI is undefined because no cost basis was supplied: set annual_platform_license / infrastructure / integration (the platform does not invent a price)."
                         if roi is None else "ROI = (annual benefit - annual cost) / annual cost, with every assumption listed above."),
            "caveat": "Benefits are SIMULATED / back-tested and haircut by the realisation factor; they are not measured customer results."}
