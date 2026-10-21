"""Supplier allocation as a constrained optimisation problem (deterministic; no LLM).

    minimise   sum_i  e_i * x_i  +  F * |{i : x_i > 0}|
    where      e_i = price_i * (1 + stockout_rate * exposure_days_i + lead_rate * lead_i + risk_rate * (1 - reliability_i * quality_i))
    subject to sum_i x_i >= Q            (required quantity; MOQ may force slightly more)
               x_i = 0  or  MOQ_i <= x_i <= capacity_i          (selected suppliers only)
               x_i <= max_share * T  when the order is large and >= 2 suppliers are eligible (diversification)
               sum_i price_i * x_i <= budget
               eligibility: quality >= floor, reliability >= floor, region allowed

Because every cost term is linear in x_i, the optimum for a FIXED set of suppliers is a greedy fill (MOQ first, then cheapest effective
cost up to the caps). Choosing the set is a small combinatorial problem solved exactly by enumerating subsets (<= 2^max_suppliers).
A deterministic single-supplier fallback exists in case anything fails. No external solver is justified at this size.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field

from .settings import OptimizerCfg, get_settings


@dataclass(frozen=True)
class SupplierOption:
    vendor_id: str
    name: str
    unit_price: float
    lead_time_days: float
    reliability: float            # 0..100
    quality_rate: float = 1.0     # 0..1
    moq: int = 1
    capacity: int | None = None   # None = unlimited
    region: str = "GLOBAL"


@dataclass
class AllocationPlan:
    status: str                                   # optimal | fallback | infeasible
    method: str
    required_quantity: int
    allocation: dict[str, int] = field(default_factory=dict)
    recommended_supplier: str | None = None
    expected_cost: float = 0.0                    # purchase cost only
    objective: float = 0.0                        # purchase + stock-out + lead-time + risk (+ fixed) cost
    expected_lead_time_days: float = 0.0          # slowest supplier in the plan (when the order is complete)
    weighted_lead_time_days: float = 0.0
    risk_score: float = 0.0                       # 0 (safe) .. 1 (risky), allocation-weighted
    explanation: str = ""
    excluded: dict[str, str] = field(default_factory=dict)    # vendor_id -> reason
    breakdown: dict[str, dict] = field(default_factory=dict)  # vendor_id -> cost components

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("expected_cost", "objective", "expected_lead_time_days", "weighted_lead_time_days", "risk_score"):
            d[k] = round(d[k], 4)
        return d


def _packs(n: float, pack: int, up: bool) -> int:
    return int((math.ceil if up else math.floor)(n / pack) * pack)


class SupplierOptimizer:
    def __init__(self, cfg: OptimizerCfg | None = None, min_quality: float | None = None, min_reliability: float | None = None):
        self.cfg = cfg or get_settings().optimizer
        sup = get_settings().supplier
        self.min_quality = sup.min_quality_rate if min_quality is None else min_quality
        self.min_reliability = sup.min_reliability_score if min_reliability is None else min_reliability

    # ---- cost model
    def effective_unit_cost(self, o: SupplierOption, days_until_stockout: float | None) -> tuple[float, dict]:
        c = self.cfg
        exposure = max(0.0, o.lead_time_days - days_until_stockout) if days_until_stockout is not None else 0.0
        risk = 1.0 - (max(0.0, min(100.0, o.reliability)) / 100.0) * max(0.0, min(1.0, o.quality_rate))
        parts = {"purchase": o.unit_price, "stockout": o.unit_price * c.stockout_rate_per_day * exposure,
                 "lead_time": o.unit_price * c.lead_time_rate_per_day * o.lead_time_days, "risk": o.unit_price * c.risk_rate * risk}
        return sum(parts.values()), parts

    def _eligible(self, options: list[SupplierOption]) -> tuple[list[SupplierOption], dict[str, str]]:
        ok, why = [], {}
        regions = {r.upper() for r in self.cfg.allowed_regions}
        rules = get_settings().rules
        allowed = {v for v in rules.allowed_vendors}
        for o in options:
            if not (o.unit_price > 0) or o.lead_time_days < 0 or math.isnan(o.unit_price):
                why[o.vendor_id] = "invalid quote (price <= 0 or negative lead time)"
            elif o.quality_rate < self.min_quality:
                why[o.vendor_id] = f"quality {o.quality_rate:.0%} below the {self.min_quality:.0%} floor"
            elif o.reliability < self.min_reliability:
                why[o.vendor_id] = f"reliability {o.reliability:.0f} below the {self.min_reliability:.0f} floor"
            elif regions and o.region.upper() not in regions:
                why[o.vendor_id] = f"region {o.region} not allowed"
            elif allowed and o.vendor_id not in allowed:
                why[o.vendor_id] = "supplier is not on the tenant's approved-supplier list"
            elif rules.max_lead_time_days is not None and o.lead_time_days > rules.max_lead_time_days:
                why[o.vendor_id] = f"lead time {o.lead_time_days:g}d exceeds the policy maximum {rules.max_lead_time_days:g}d"
            elif o.capacity is not None and o.capacity < max(1, o.moq):
                why[o.vendor_id] = "capacity below its own minimum order quantity"
            else:
                ok.append(o)
        return ok, why

    def optimize(self, required_quantity: int, options: list[SupplierOption], days_until_stockout: float | None = None,
                 budget: float | None = None) -> AllocationPlan:
        q = int(required_quantity)
        if q <= 0:
            return AllocationPlan("infeasible", "none", q, explanation="required quantity must be positive")
        try:
            return self._solve(q, options, days_until_stockout, self.cfg.budget if budget is None else budget)
        except Exception as exc:   # deterministic safety net: never let an optimiser bug block procurement
            plan = self._fallback(q, options, days_until_stockout, f"optimizer error ({type(exc).__name__}: {exc})")
            return plan

    # ---- exact solver
    def _solve(self, q: int, options: list[SupplierOption], days: float | None, budget: float | None) -> AllocationPlan:
        cfg, pack = self.cfg, self.cfg.pack_size
        elig, excluded = self._eligible(options)
        if not elig:
            return AllocationPlan("infeasible", "none", q, excluded=excluded, explanation="no eligible supplier: " + (
                "; ".join(f"{k}: {v}" for k, v in excluded.items()) or "no quotes available"))
        costs = {o.vendor_id: self.effective_unit_cost(o, days) for o in elig}
        ranked = sorted(elig, key=lambda o: (costs[o.vendor_id][0], o.lead_time_days, o.vendor_id))[:cfg.max_suppliers]
        diversify = q >= cfg.diversify_min_qty and len(ranked) >= 2 and cfg.max_supplier_share < 1.0
        best = self._search(q, ranked, costs, diversify, pack, budget)
        relaxed = False
        if best is None and diversify:                   # diversification is a PREFERENCE: never leave a critical SKU unordered because of it
            best, relaxed = self._search(q, ranked, costs, False, pack, budget), True
        if best is None:
            reason = "no allocation satisfies the MOQ / capacity / diversification" + (" / budget" if budget is not None else "") + " constraints"
            return AllocationPlan("infeasible", "exhaustive_subset", q, excluded=excluded, explanation=reason)
        _, subset, alloc, purchase, objective = best
        note = (f"Diversification cap of {cfg.max_supplier_share:.0%} per supplier was RELAXED because no allocation can satisfy it with the eligible suppliers' "
                "capacity / minimum order quantities.") if relaxed else ""
        return self._plan("optimal", "exhaustive_subset", q, subset, alloc, costs, purchase, objective, excluded, days, note=note, pool=elig)

    def _search(self, q: int, ranked: list[SupplierOption], costs: dict, diversify: bool, pack: int, budget: float | None) -> tuple | None:
        cfg, best = self.cfg, None
        for r in range(1, len(ranked) + 1):
            for subset in itertools.combinations(ranked, r):
                alloc = self._fill(q, subset, costs, diversify, pack, cfg.max_supplier_share)
                if alloc is None:
                    continue
                cost_purchase = sum(o.unit_price * alloc[o.vendor_id] for o in subset)
                if budget is not None and cost_purchase > budget + 1e-9:
                    continue
                objective = sum(costs[o.vendor_id][0] * alloc[o.vendor_id] for o in subset) + cfg.order_fixed_cost * len(subset)
                key = (round(objective, 6), len(subset), -sum(o.reliability for o in subset), tuple(sorted(o.vendor_id for o in subset)))
                if best is None or key < best[0]:
                    best = (key, subset, alloc, cost_purchase, objective)
        return best

    @staticmethod
    def _fill(q: int, subset: tuple[SupplierOption, ...], costs: dict, diversify: bool, pack: int, share: float) -> dict[str, int] | None:
        lows = {o.vendor_id: _packs(max(1, o.moq), pack, up=True) for o in subset}
        total = max(q, sum(lows.values()))
        total = _packs(total, pack, up=True)
        if total > get_settings().optimizer.max_overorder_factor * q + pack:       # a MOQ must not turn a small need into a huge purchase
            return None
        highs = {}
        for o in subset:
            cap = _packs(o.capacity, pack, up=False) if o.capacity is not None else total
            if diversify and len(subset) > 1:
                cap = min(cap, _packs(share * total, pack, up=False))
            highs[o.vendor_id] = min(cap, total)
            if highs[o.vendor_id] < lows[o.vendor_id]:
                return None
        if diversify and len(subset) == 1:
            return None                                     # a diversified order must use at least two suppliers
        if sum(highs.values()) < total or sum(lows.values()) > total:
            return None
        alloc, remaining = dict(lows), total - sum(lows.values())
        for o in sorted(subset, key=lambda s: (costs[s.vendor_id][0], s.vendor_id)):
            add = min(remaining, highs[o.vendor_id] - alloc[o.vendor_id])
            alloc[o.vendor_id] += add
            remaining -= add
        return alloc if remaining == 0 else None

    def _plan(self, status: str, method: str, q: int, subset, alloc: dict[str, int], costs: dict, purchase: float, objective: float,
              excluded: dict[str, str], days: float | None, note: str = "",
              pool: list[SupplierOption] | None = None) -> AllocationPlan:
        used = [o for o in subset if alloc.get(o.vendor_id, 0) > 0]
        total = sum(alloc[o.vendor_id] for o in used)
        top = max(used, key=lambda o: (alloc[o.vendor_id], o.reliability, o.vendor_id))
        wl = sum(o.lead_time_days * alloc[o.vendor_id] for o in used) / total
        risk = 1.0 - sum(alloc[o.vendor_id] * (o.reliability / 100.0) * o.quality_rate for o in used) / total
        cheapest = min(pool or used, key=lambda o: (o.unit_price, o.vendor_id))
        parts = [f"{alloc[o.vendor_id]} x {o.name} @ ${o.unit_price:.2f} ({o.lead_time_days:g}d, reliability {o.reliability:.0f})" for o in used]
        why = ("Single supplier: lowest effective cost after lead-time and risk adjustments." if len(used) == 1 else
               "Split order to balance price against delivery risk (diversification / capacity / MOQ constraints): " + "; ".join(parts) + ".")
        if len(used) == 1:
            why += f" {parts[0]}."
        if cheapest.vendor_id not in alloc and len(used) == 1:
            why += f" The cheapest sticker price ({cheapest.name}) was rejected on risk/lead-time grounds."
        if days is not None and wl > days:
            why += f" Warning: weighted lead time {wl:.1f}d exceeds the {days:.1f}d of stock cover."
        if total > q:
            why += f" Quantity raised from {q} to {total} to satisfy minimum order quantities."
        return AllocationPlan(status, method, q, {o.vendor_id: alloc[o.vendor_id] for o in used}, top.vendor_id, round(purchase, 2), round(objective, 2),
                              max(o.lead_time_days for o in used), wl, risk, (note + " " + why).strip(), excluded,
                              {o.vendor_id: {k: round(v, 4) for k, v in costs[o.vendor_id][1].items()} | {"effective_unit_cost": round(costs[o.vendor_id][0], 4)}
                               for o in used})

    def _fallback(self, q: int, options: list[SupplierOption], days: float | None, why: str) -> AllocationPlan:
        """Deterministic last resort: cheapest valid quote, whole order, MOQ ignored only if it cannot be met (flagged for the policy layer)."""
        valid = [o for o in options if o.unit_price > 0 and o.lead_time_days >= 0]
        if not valid:
            return AllocationPlan("infeasible", "fallback_cheapest", q, explanation=f"{why}; no valid quote to fall back to")
        o = min(valid, key=lambda s: (s.unit_price, s.lead_time_days, s.vendor_id))
        costs = {o.vendor_id: self.effective_unit_cost(o, days)}
        return self._plan("fallback", "fallback_cheapest", q, (o,), {o.vendor_id: q}, costs, o.unit_price * q, costs[o.vendor_id][0] * q, {}, days,
                          note=f"Fallback policy used: {why}.", pool=valid)
