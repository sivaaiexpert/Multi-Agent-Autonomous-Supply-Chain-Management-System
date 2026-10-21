import random

import pytest

from scm.optimizer import SupplierOption, SupplierOptimizer
from scm.settings import OptimizerCfg


def opt(**kw):
    return SupplierOptimizer(OptimizerCfg(**kw))


def S(vid, price, lead=5, rel=95, q=0.99, moq=1, cap=None, region="GLOBAL"):
    return SupplierOption(vid, f"Vendor {vid}", price, lead, rel, q, moq, cap, region)


def test_single_cheapest_supplier_for_a_small_order():
    plan = opt().optimize(50, [S("A", 10), S("B", 12)], days_until_stockout=30)
    assert plan.status == "optimal" and plan.allocation == {"A": 50} and plan.recommended_supplier == "A"
    assert plan.expected_cost == 500.0


def test_reliability_and_lead_time_can_beat_the_lowest_price():
    cheap_slow_risky = S("A", 9.5, lead=25, rel=62, q=0.91)
    pricey_fast_safe = S("B", 10.0, lead=3, rel=98, q=0.995)
    plan = opt().optimize(100, [cheap_slow_risky, pricey_fast_safe], days_until_stockout=5)
    assert plan.recommended_supplier == "B"
    assert "rejected on risk" in plan.explanation


def test_large_order_is_split_by_the_diversification_cap():
    plan = opt().optimize(420, [S("A", 9.0, lead=10, rel=70), S("B", 10.5, lead=4, rel=96)], days_until_stockout=6)
    assert plan.status == "optimal" and set(plan.allocation) == {"A", "B"}
    assert sum(plan.allocation.values()) == 420
    assert max(plan.allocation.values()) <= 0.7 * 420 + 5            # pack rounding
    assert plan.risk_score < 0.2


def test_share_cap_is_read_from_the_optimizers_own_config():
    options = [S("A", 9.0, lead=5, rel=95), S("B", 9.4, lead=5, rel=95)]
    capped = opt(max_supplier_share=0.6).optimize(400, options)
    uncapped = opt(max_supplier_share=1.0).optimize(400, options)
    assert len(capped.allocation) == 2 and max(capped.allocation.values()) <= 0.6 * 400 + 5
    assert uncapped.allocation == {"A": 400}


def test_diversify_min_qty_is_configurable():
    options = [S("A", 9.0), S("B", 9.4)]
    assert len(opt(diversify_min_qty=100).optimize(150, options).allocation) == 2
    assert len(opt(diversify_min_qty=1000).optimize(150, options).allocation) == 1


def test_capacity_forces_a_split_even_without_diversification():
    plan = opt(max_supplier_share=1.0).optimize(300, [S("A", 8.0, cap=100), S("B", 9.0, cap=300)])
    assert plan.allocation == {"A": 100, "B": 200}


def test_moq_is_respected_for_every_selected_supplier():
    plan = opt().optimize(60, [S("A", 8.0, moq=100), S("B", 9.0, moq=20)])
    assert plan.allocation == {"B": 60}                                # A would need 100 > required, B satisfies exactly


def test_moq_can_raise_the_quantity_and_says_so():
    plan = opt().optimize(20, [S("A", 8.0, moq=50)])
    assert plan.allocation == {"A": 50} and "raised from 20 to 50" in plan.explanation


def test_quantity_above_total_capacity_is_infeasible():
    plan = opt().optimize(1000, [S("A", 8.0, cap=100), S("B", 9.0, cap=200)])
    assert plan.status == "infeasible" and plan.allocation == {}


def test_budget_is_a_hard_constraint():
    assert opt().optimize(100, [S("A", 10.0)], budget=999).status == "infeasible"
    assert opt().optimize(100, [S("A", 10.0)], budget=1000).status == "optimal"


def test_budget_steers_towards_the_cheaper_plan():
    a, b = S("A", 8.0, cap=100, rel=96), S("B", 20.0, rel=96)
    assert opt(max_supplier_share=1.0).optimize(200, [a, b], budget=1700).status == "infeasible"
    assert opt(max_supplier_share=1.0).optimize(200, [a, b], budget=2800).allocation == {"A": 100, "B": 100}


def test_quality_and_reliability_floors_exclude_suppliers_with_reasons():
    plan = opt().optimize(100, [S("A", 5.0, q=0.80), S("B", 6.0, rel=40), S("C", 7.0)])
    assert plan.allocation == {"C": 100}
    assert "quality" in plan.excluded["A"] and "reliability" in plan.excluded["B"]


def test_region_restriction():
    plan = opt(allowed_regions=["EU"]).optimize(50, [S("A", 5.0, region="US"), S("B", 9.0, region="EU")])
    assert plan.allocation == {"B": 50} and "region" in plan.excluded["A"]


def test_invalid_quotes_are_never_selected():
    plan = opt().optimize(50, [S("A", 0.0), S("B", -3.0), S("C", float("nan")), S("D", 9.0, lead=-1), S("E", 10.0)])
    assert plan.allocation == {"E": 50}


def test_no_eligible_supplier():
    plan = opt().optimize(50, [S("A", 0.0)])
    assert plan.status == "infeasible" and "no eligible supplier" in plan.explanation
    assert opt().optimize(50, []).status == "infeasible"


def test_non_positive_quantity_is_infeasible():
    assert opt().optimize(0, [S("A", 5.0)]).status == "infeasible"
    assert opt().optimize(-5, [S("A", 5.0)]).status == "infeasible"


def test_lead_time_beyond_stock_cover_is_penalised():
    fast, slow = S("F", 10.5, lead=2), S("L", 10.0, lead=20)
    near = opt().optimize(100, [fast, slow], days_until_stockout=3)
    far = opt().optimize(100, [fast, slow], days_until_stockout=60)
    assert near.recommended_supplier == "F"
    assert far.recommended_supplier == "L"


def test_deterministic_tie_breaking():
    a, b = S("A", 10), S("B", 10)
    assert opt().optimize(50, [a, b]).allocation == opt().optimize(50, [b, a]).allocation


def test_optimizer_failure_falls_back_to_cheapest_valid_quote(monkeypatch):
    o = opt()
    monkeypatch.setattr(o, "_solve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    plan = o.optimize(100, [S("A", 9.0), S("B", 8.0)])
    assert plan.status == "fallback" and plan.allocation == {"B": 100} and "boom" in plan.explanation


def test_fallback_with_no_valid_quote_is_infeasible(monkeypatch):
    o = opt()
    monkeypatch.setattr(o, "_solve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert o.optimize(10, [S("A", 0.0)]).status == "infeasible"


def test_allocations_are_whole_packs():
    plan = opt().optimize(420, [S("A", 9.0, rel=80), S("B", 9.5, rel=90)])
    assert all(q % 5 == 0 for q in plan.allocation.values())


def test_randomised_plans_always_satisfy_every_constraint():
    rng = random.Random(2024)
    for _ in range(300):
        n = rng.randint(1, 6)
        options = [S(f"V{i}", round(rng.uniform(1, 50), 2), lead=rng.randint(1, 30), rel=rng.uniform(55, 100), q=rng.uniform(0.85, 1.0),
                     moq=rng.choice([1, 5, 10, 25, 60]), cap=rng.choice([None, 50, 120, 400]), region=rng.choice(["US", "EU"]))
                   for i in range(n)]
        qty = rng.choice([5, 25, 90, 250, 400, 900])
        o = opt()
        plan = o.optimize(qty, options, days_until_stockout=rng.choice([None, 2, 10, 40]))
        assert plan.status in ("optimal", "infeasible")
        if plan.status != "optimal":
            continue
        by = {x.vendor_id: x for x in options}
        total = sum(plan.allocation.values())
        assert total >= qty and total <= o.cfg.max_overorder_factor * qty + 5            # MOQs never inflate an order beyond the over-order factor
        for vid, amount in plan.allocation.items():
            x = by[vid]
            assert amount >= x.moq and amount % 5 == 0
            assert x.capacity is None or amount <= x.capacity
            assert x.quality_rate >= o.min_quality and x.reliability >= o.min_reliability
        if qty >= o.cfg.diversify_min_qty and len(plan.allocation) > 1 and "RELAXED" not in plan.explanation:
            assert max(plan.allocation.values()) <= o.cfg.max_supplier_share * total + 5
        assert plan.expected_cost == pytest.approx(sum(by[v].unit_price * q for v, q in plan.allocation.items()), abs=0.01)


def test_exhaustive_search_matches_brute_force_objective():
    """The subset enumeration must find the true optimum: compare against an independent brute force on tiny instances."""
    import itertools
    rng = random.Random(7)
    o = opt(max_supplier_share=1.0, order_fixed_cost=0.0)
    for _ in range(40):
        options = [S(f"V{i}", round(rng.uniform(5, 20), 2), lead=rng.randint(1, 15), rel=rng.uniform(70, 99), cap=rng.choice([30, 60, 100, 200]))
                   for i in range(3)]
        qty = rng.choice([40, 80, 150])
        plan = o.optimize(qty, options, days_until_stockout=8)
        if plan.status != "optimal":
            continue
        unit = {x.vendor_id: o.effective_unit_cost(x, 8)[0] for x in options}
        best = float("inf")
        caps = [(x.vendor_id, (x.capacity or qty) // 5) for x in options]
        for combo in itertools.product(*[range(c + 1) for _, c in caps]):
            if sum(combo) * 5 < qty:
                continue
            if sum(combo) * 5 > qty + 0:
                continue
            best = min(best, sum(unit[v] * k * 5 for (v, _), k in zip(caps, combo)))
        assert plan.objective == pytest.approx(best, abs=0.05)


def test_diversification_cap_is_relaxed_instead_of_leaving_the_order_unfilled():
    # S1 capacity 300, S2 unlimited: with a 70 % cap the two together can serve at most 0.7*Q + 300 < Q for a large Q -> the cap must give way
    options = [S("S1", 9.0, cap=300), S("S2", 9.5)]
    plan = opt().optimize(1470, options)
    assert plan.status == "optimal" and sum(plan.allocation.values()) == 1470 and "RELAXED" in plan.explanation
    assert opt().optimize(300, options).allocation and "RELAXED" not in opt().optimize(300, options).explanation


def test_an_impossible_supplier_never_blocks_the_other_suppliers():
    plan = opt().optimize(1470, [S("S1", 0.01, moq=100000), S("S2", 9.0, cap=300), S("S3", 9.5)])
    assert plan.status == "optimal" and "S1" not in plan.allocation
