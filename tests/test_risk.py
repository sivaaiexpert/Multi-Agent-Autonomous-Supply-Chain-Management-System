import math

import pytest

from scm.forecast import Forecast
from scm.risk import RiskLevel, assess, norm_cdf, static_assessment


def fc(daily=10.0, std=2.0, horizon=30):
    return Forecast("S", [daily] * horizon, daily * horizon, 0.9, "moving_average", horizon, daily_mean=daily, daily_std=std, history_days=60)


def test_norm_cdf_reference_values():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_plenty_of_stock_is_low_risk():
    r = assess("S", 1000, fc(), lead_time_days=7, target_stock=500, reorder_point=50)
    assert r.risk_level is RiskLevel.LOW and r.stockout_probability < 0.01 and r.recommended_order_quantity == 0
    assert r.days_until_stockout == pytest.approx(100.0)


def test_stock_below_lead_time_demand_is_critical():
    r = assess("S", 20, fc(), lead_time_days=7, target_stock=500, reorder_point=50)
    assert r.risk_level is RiskLevel.CRITICAL and r.stockout_probability > 0.9
    assert r.days_until_stockout == pytest.approx(2.0)
    assert r.recommended_order_quantity > 0 and r.recommended_order_quantity % 5 == 0


def test_zero_stock_with_demand_is_critical():
    r = assess("S", 0, fc(), 5, 300)
    assert r.risk_level is RiskLevel.CRITICAL and "zero" in " ".join(r.reasons)


def test_probability_is_monotone_in_stock():
    probs = [assess("S", s, fc(), 7, 300).stockout_probability for s in (0, 40, 70, 100, 150, 300)]
    assert probs == sorted(probs, reverse=True)


def test_open_orders_reduce_risk():
    without = assess("S", 40, fc(), 7, 300)
    with_po = assess("S", 40, fc(), 7, 300, on_order=200)
    assert with_po.stockout_probability < without.stockout_probability
    assert with_po.risk_level.rank < without.risk_level.rank


def test_longer_lead_time_raises_risk():
    short = assess("S", 120, fc(), 3, 300).stockout_probability
    long = assess("S", 120, fc(), 14, 300).stockout_probability
    assert long > short


def test_lead_time_uncertainty_raises_safety_stock_and_probability():
    base = assess("S", 90, fc(std=0), 7, 300)
    noisy = assess("S", 90, fc(std=0), 7, 300, lead_time_std=3.0)
    assert noisy.safety_stock > base.safety_stock
    assert noisy.stockout_probability >= base.stockout_probability


def test_demand_variability_raises_safety_stock():
    assert assess("S", 90, fc(std=6), 7, 300).safety_stock > assess("S", 90, fc(std=1), 7, 300).safety_stock


def test_zero_variance_is_a_deterministic_step():
    # lead-time demand is exactly 70 units: 60 in stock is a certain stock-out, 80 is a certain no-stock-out
    assert assess("S", 60, fc(std=0), 7, 300).stockout_probability == 1.0
    assert assess("S", 80, fc(std=0), 7, 300).stockout_probability == 0.0


def test_no_demand_means_no_days_until_stockout():
    r = assess("S", 50, Forecast("S", [0.0] * 30, 0.0, 0.5, "moving_average", 30), 7, 100)
    assert r.days_until_stockout is None and r.risk_level is RiskLevel.LOW


def test_lead_time_beyond_horizon_extends_with_mean():
    r = assess("S", 100, fc(horizon=5), lead_time_days=20, target_stock=500)
    assert r.stockout_probability == 1.0 or r.risk_level.rank >= RiskLevel.HIGH.rank


def test_risk_never_uses_nan():
    r = assess("S", 10, fc(), 7, 100).to_dict()
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in r.values())
    assert r["risk_level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def test_static_rule_matches_legacy_urgency():
    assert static_assessment("S", 5, 40, 200).risk_level is RiskLevel.CRITICAL      # 12 %
    assert static_assessment("S", 20, 40, 200).risk_level is RiskLevel.HIGH         # 50 %
    assert static_assessment("S", 35, 40, 200).risk_level is RiskLevel.MEDIUM       # 87 %
    assert static_assessment("S", 40, 40, 200).risk_level is RiskLevel.LOW
    assert static_assessment("S", 5, 40, 200).basis == "static_rule"


def test_static_rule_handles_bad_data_without_crashing():
    r = static_assessment("S", -10, 0, 0)
    assert r.recommended_order_quantity == 0 and r.current_stock == 0


def test_static_rule_quantity_rounds_to_pack():
    assert static_assessment("S", 12, 30, 120).recommended_order_quantity == 110
