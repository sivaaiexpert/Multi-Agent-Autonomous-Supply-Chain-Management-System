import pytest

from scm import db, suppliers
from scm.settings import SupplierCfg
from scm.suppliers import compute_metrics


def d(status="DELIVERED", promised=5, actual=5, ordered=100, received=100, defective=0, price=10.0, item="SKU-1"):
    return {"Status": status, "Promised_Days": promised, "Actual_Days": actual if status == "DELIVERED" else None, "Qty_Ordered": ordered,
            "Qty_Received": received if status == "DELIVERED" else 0, "Qty_Defective": defective, "Unit_Price": price, "Item_ID": item}


def test_no_history_uses_the_prior_reliability():
    m = compute_metrics("V", [], 88.0)
    assert m.reliability_score == 88.0 and m.source == "prior" and m.observations == 0 and m.on_time_delivery_rate is None


def test_perfect_history_approaches_100():
    m = compute_metrics("V", [d() for _ in range(40)], 70.0)
    assert m.on_time_delivery_rate == 1.0 and m.quality_rate == 1.0 and m.order_fulfillment_rate == 1.0 and m.cancellation_rate == 0.0
    assert m.reliability_score > 95


def test_metrics_are_computed_correctly():
    rows = [d(promised=5, actual=5), d(promised=5, actual=8), d(promised=5, actual=5, received=90, defective=9, price=12.0),
            d(status="CANCELLED")]
    m = compute_metrics("V", rows, 90.0)
    assert m.on_time_delivery_rate == pytest.approx(2 / 3)
    assert m.average_lead_time == pytest.approx(6.0)
    assert m.lead_time_variance == pytest.approx(((5 - 6) ** 2 + (8 - 6) ** 2 + (5 - 6) ** 2) / 3)
    assert m.cancellation_rate == pytest.approx(0.25)
    assert m.order_fulfillment_rate == pytest.approx(290 / 300)
    assert m.defect_rate == pytest.approx(9 / 290) and m.quality_rate == pytest.approx(1 - 9 / 290)
    assert m.historical_price == pytest.approx((10 + 10 + 12 + 10) / 4)


def test_price_volatility_is_the_coefficient_of_variation_per_sku():
    stable = compute_metrics("V", [d(price=10.0) for _ in range(5)], 90)
    jumpy = compute_metrics("V", [d(price=p) for p in (8, 12, 8, 12, 8)], 90)
    assert stable.price_volatility == 0 and jumpy.price_volatility > 0.1


def test_few_observations_are_shrunk_toward_the_prior():
    bad = [d(promised=5, actual=12, received=60, defective=20) for _ in range(2)]
    few = compute_metrics("V", bad, 95.0)
    many = compute_metrics("V", bad * 30, 95.0)
    assert few.reliability_score > many.reliability_score > 0
    assert few.source == "blended"


def test_weights_are_configurable():
    rows = [d(promised=5, actual=9) for _ in range(30)]               # always late but perfect quality
    otd_heavy = compute_metrics("V", rows, 90, SupplierCfg(w_on_time=0.9, w_fulfillment=0.025, w_quality=0.025, w_lead_time_stability=0.025, w_cancellation=0.025))
    quality_heavy = compute_metrics("V", rows, 90, SupplierCfg(w_on_time=0.025, w_fulfillment=0.025, w_quality=0.9, w_lead_time_stability=0.025, w_cancellation=0.025))
    assert quality_heavy.reliability_score > otd_heavy.reliability_score + 20


def test_score_is_bounded():
    for rows in ([], [d(status="CANCELLED")] * 10, [d(actual=50, received=1, defective=1)] * 10):
        m = compute_metrics("V", rows, 90)
        assert 0 <= m.reliability_score <= 100


def test_all_cancelled_history_scores_low():
    m = compute_metrics("V", [d(status="CANCELLED") for _ in range(30)], 90)
    assert m.cancellation_rate == 1.0 and m.reliability_score < 60


def test_database_metrics_are_cached_and_invalidated_by_new_facts(demo_db):
    first = suppliers.all_metrics()
    assert suppliers.all_metrics() is first                              # served from cache
    suppliers.record_delivery("V01", "SKU-1001", "2026-01-01", 3, 30, 100, 10, 5, 50.0)
    second = suppliers.all_metrics()
    assert second is not first
    assert second["V01"].observations == first["V01"].observations + 1


def test_persisted_metrics_row_per_vendor(demo_db):
    suppliers.all_metrics(persist=True)
    rows = db.df("SELECT * FROM supplier_metrics")
    assert len(rows) == 6 and rows.Reliability_Score.between(0, 100).all()


def test_delivery_constraints_reject_impossible_rows(demo_db):
    with pytest.raises(Exception):
        suppliers.record_delivery("V01", "SKU-1001", "2026-01-01", 3, 3, 100, 10, 5, -1.0)      # negative price
    with pytest.raises(Exception):
        suppliers.record_delivery("NOPE", "SKU-1001", "2026-01-01", 3, 3, 100, 10, 5, 5.0)      # unknown vendor (foreign key)
