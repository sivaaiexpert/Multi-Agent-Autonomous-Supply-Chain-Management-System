"""Optimiser vs two baselines on random synthetic quote sets (same inputs, same constraints):

  * naive      : lowest sticker price wins, constraints ignored (what a price-only agent does)
  * cheapest*  : lowest price among suppliers that satisfy every hard constraint (eligibility floors, MOQ <= qty <= capacity)

`cheapest*` is the fair comparison for cost / risk / lead time; `naive` shows how often a price-only choice would have been invalid.
Resilience is measured exactly: each supplier independently fails to deliver with probability (1 - reliability); we enumerate every failure
combination and report the probability that the majority of the ordered units never arrive.
"""
from __future__ import annotations

import itertools
import random

from evaluation.datasets.synthetic import random_supplier_options
from evaluation.metrics.stats import safe_mean
from scm.optimizer import SupplierOption, SupplierOptimizer
from scm.settings import OptimizerCfg


def p_majority_lost(parts: list[tuple[float, int]]) -> float:
    """parts = [(reliability 0..100, units)]. Probability that > 50 % of the units are not delivered."""
    total = sum(u for _, u in parts)
    p = 0.0
    for fails in itertools.product([0, 1], repeat=len(parts)):
        prob, lost = 1.0, 0
        for (rel, units), f in zip(parts, fails):
            q = 1 - rel / 100.0
            prob *= q if f else 1 - q
            lost += units if f else 0
        if lost > total / 2:
            p += prob
    return p


def _feasible_single(o: SupplierOptimizer, opt: SupplierOption, qty: int) -> bool:
    return (opt.quality_rate >= o.min_quality and opt.reliability >= o.min_reliability and opt.moq <= qty
            and (opt.capacity is None or opt.capacity >= qty))


def optimizer_vs_cheapest(n: int = 300, seed: int = 4, cfg: OptimizerCfg | None = None) -> dict:
    rng, o = random.Random(seed), SupplierOptimizer(cfg)
    rows, infeasible, naive_invalid, no_single_feasible = [], 0, 0, 0
    for _ in range(n):
        options = random_supplier_options(rng)
        qty = rng.choice([50, 120, 250, 400, 900])
        days = rng.choice([4, 8, 15, 40])
        plan = o.optimize(qty, options, days)
        if plan.status != "optimal":
            infeasible += 1
            continue
        by = {x.vendor_id: x for x in options}
        naive = min(options, key=lambda s: (s.unit_price, s.vendor_id))
        naive_invalid += not _feasible_single(o, naive, qty)
        feasible = [x for x in options if _feasible_single(o, x, qty)]
        if not feasible:
            no_single_feasible += 1                       # only a multi-supplier plan can serve this order at all
            continue
        base = min(feasible, key=lambda s: (s.unit_price, s.vendor_id))
        alloc = [(by[v].reliability * by[v].quality_rate, u) for v, u in plan.allocation.items()]
        rows.append({"cost": plan.expected_cost / (base.unit_price * qty) - 1,
                     "risk": plan.risk_score - (1 - base.reliability / 100 * base.quality_rate),
                     "wlead": plan.weighted_lead_time_days - base.lead_time_days,
                     "lead_max": plan.expected_lead_time_days - base.lead_time_days,
                     "split": len(plan.allocation) > 1,
                     "plost": p_majority_lost(alloc) - p_majority_lost([(base.reliability * base.quality_rate, qty)]),
                     "exposed_base": base.lead_time_days > days, "exposed_opt": plan.weighted_lead_time_days > days})
    return {"instances": n, "optimal_plans": n - infeasible, "infeasible": infeasible,
            "price_only_choice_invalid": naive_invalid, "orders_only_a_multi_supplier_plan_can_serve": no_single_feasible,
            "compared_with_constraint_aware_cheapest": len(rows),
            "mean_purchase_cost_premium": safe_mean([r["cost"] for r in rows]),
            "mean_risk_score_change": safe_mean([r["risk"] for r in rows]),
            "mean_weighted_lead_time_change_days": safe_mean([r["wlead"] for r in rows]),
            "mean_slowest_supplier_lead_change_days": safe_mean([r["lead_max"] for r in rows]),
            "split_orders": sum(r["split"] for r in rows),
            "mean_change_in_prob_majority_of_units_lost": safe_mean([r["plost"] for r in rows]),
            "split_orders_mean_change_in_prob_majority_lost": safe_mean([r["plost"] for r in rows if r["split"]]),
            "lead_time_exceeds_cover_cheapest_vs_optimizer": [sum(r["exposed_base"] for r in rows), sum(r["exposed_opt"] for r in rows)]}


def compare_configs(n: int = 300, seed: int = 4) -> dict:
    """Same instances under the default policy (70 % supplier-share cap) and with diversification switched off (share cap 100 %)."""
    return {"default_70pct_share_cap": optimizer_vs_cheapest(n, seed), "no_share_cap": optimizer_vs_cheapest(n, seed, OptimizerCfg(max_supplier_share=1.0))}
