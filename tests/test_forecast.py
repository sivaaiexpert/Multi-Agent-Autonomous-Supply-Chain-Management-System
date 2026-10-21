import math
import random

import pytest

from scm.forecast import (DemandForecaster, ExponentialSmoothing, MovingAverage, SeasonalBaseline, WeightedMovingAverage, backtest, error_metrics,
                          mae, mape, rmse, wape)
from scm.settings import ForecastCfg


def seasonal_series(n=70, seed=1, noise=1.0):
    rng = random.Random(seed)
    return [max(0.0, 10 + 3 * math.sin(2 * math.pi * (i % 7) / 7) + rng.gauss(0, noise)) for i in range(n)]


def test_moving_average_is_flat_mean_of_window():
    assert MovingAverage(3).predict([1, 2, 3, 10, 20, 30], 2) == [20.0, 20.0]


def test_weighted_moving_average_weights_recent_days_more():
    wma = WeightedMovingAverage(3).predict([0, 0, 0, 10], 1)[0]
    assert wma > MovingAverage(3).predict([0, 0, 0, 10], 1)[0]
    assert WeightedMovingAverage(3).predict([5, 5, 5], 1) == [5.0]


def test_exponential_smoothing_converges_on_constant_series():
    assert ExponentialSmoothing(0.3).predict([7.0] * 30, 3) == pytest.approx([7.0] * 3)


def test_holt_follows_an_upward_trend_but_is_damped():
    pred = ExponentialSmoothing(0.5, 0.3).predict([float(i) for i in range(40)], 30)
    assert pred[0] > 38 and pred[-1] > pred[0]
    assert pred[-1] < 40 + 30 + 1          # damped: well below the naive linear extrapolation


def test_seasonal_baseline_reproduces_the_weekly_pattern():
    pattern = [10, 12, 14, 9, 8, 20, 22]
    hist = pattern * 6
    pred = SeasonalBaseline(7).predict(hist, 7)
    assert pred == pytest.approx(pattern)


def test_seasonal_baseline_needs_two_full_seasons():
    assert SeasonalBaseline(7).min_points == 14


def test_forecaster_picks_the_seasonal_model_on_seasonal_data():
    f = DemandForecaster().forecast("S", seasonal_series(), 14)
    assert f.method == "seasonal_baseline"
    assert len(f.forecast) == 14 and f.expected_demand == pytest.approx(sum(f.forecast))
    assert f.metrics["rmse"] < 2.0 and 0.5 < f.confidence <= 1.0
    assert f.candidates["seasonal_baseline"] == min(v for v in f.candidates.values() if v is not None)


def test_forecaster_result_contract():
    d = DemandForecaster().forecast("SKU-1", seasonal_series(), 7).to_dict()
    assert {"sku_id", "forecast", "expected_demand", "confidence", "method", "horizon_days", "metrics"} <= set(d)
    assert 0.0 <= d["confidence"] <= 1.0


def test_empty_history_gives_zero_confidence_zero_demand():
    f = DemandForecaster().forecast("S", [], 5)
    assert f.expected_demand == 0 and f.confidence == 0 and f.method == "none" and f.forecast == [0.0] * 5


def test_short_history_is_low_confidence_not_an_error():
    f = DemandForecaster().forecast("S", [5, 6], 7)
    assert f.confidence <= 0.2 and f.expected_demand > 0
    assert any("history" in n for n in f.notes)


def test_negative_nan_and_garbage_points_are_treated_as_zero():
    f = DemandForecaster().forecast("S", [10, -5, float("nan"), "x", None, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10], 3)
    assert all(v >= 0 and math.isfinite(v) for v in f.forecast)


def test_invalid_horizon_rejected():
    with pytest.raises(ValueError):
        DemandForecaster().forecast("S", [1, 2, 3], 0)


def test_confidence_grows_with_history_length():
    short = DemandForecaster(ForecastCfg(min_history_days=3)).forecast("S", seasonal_series(20, noise=0.2), 7)
    long = DemandForecaster().forecast("S", seasonal_series(80, noise=0.2), 7)
    assert long.confidence > short.confidence


def test_metrics_values():
    a, p = [10.0, 20.0, 30.0], [12.0, 18.0, 33.0]
    assert mae(a, p) == pytest.approx((2 + 2 + 3) / 3)
    assert rmse(a, p) == pytest.approx(math.sqrt((4 + 4 + 9) / 3))
    assert mape(a, p) == pytest.approx((0.2 + 0.1 + 0.1) / 3 * 100)
    assert wape(a, p) == pytest.approx(7 / 60)


def test_mape_is_not_computed_when_it_is_meaningless():
    assert mape([0, 0, 0, 0], [1, 1, 1, 1]) is None              # no non-zero actuals
    assert mape([0, 0, 0, 5, 0, 0, 0, 6], [1] * 8) is None       # intermittent demand: mostly zeros
    assert wape([0, 0, 0], [1, 1, 1]) is None
    m = error_metrics([0, 0, 0, 5], [1, 1, 1, 1])
    assert m["mape"] is None and m["mae"] > 0


def test_mape_ignores_zero_actuals_when_enough_non_zero():
    assert mape([10, 10, 10, 10, 0], [10, 10, 10, 10, 3]) == pytest.approx(0.0)


def test_rolling_backtest_pools_errors():
    hist = [10.0] * 40
    r = backtest(hist, MovingAverage(7), horizon=5, initial=20)
    assert r["mae"] == 0 and r["points"] == 20
    with pytest.raises(ValueError):
        backtest(hist, MovingAverage(7), horizon=5, initial=40)


def test_forecast_is_deterministic():
    h = seasonal_series()
    assert DemandForecaster().forecast("S", h, 10).forecast == DemandForecaster().forecast("S", h, 10).forecast
