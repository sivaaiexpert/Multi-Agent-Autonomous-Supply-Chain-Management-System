"""Demand forecasting: a small, honest model zoo plus rolling backtests.

    history (daily, oldest -> newest) -> candidate models -> holdout backtest (RMSE) -> best model -> Forecast(+confidence, error metrics)

Models: moving average, weighted moving average, simple/Holt exponential smoothing, seasonal baseline (weekday profile x level).
New models only need to implement `Model.predict(history, horizon)` and be registered in `MODELS`.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from . import db, obs
from .settings import ForecastCfg, get_settings


class Model(Protocol):
    name: str
    min_points: int

    def predict(self, history: Sequence[float], horizon: int) -> list[float]: ...


def _clean(history: Sequence[float]) -> list[float]:
    """Demand cannot be negative or NaN: treat such points as data errors (0)."""
    out = []
    for v in history:
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 0.0
        out.append(0.0 if math.isnan(x) or math.isinf(x) or x < 0 else x)
    return out


class MovingAverage:
    name, min_points = "moving_average", 1

    def __init__(self, window: int):
        self.window = window

    def predict(self, history, horizon):
        w = history[-self.window:]
        return [sum(w) / len(w)] * horizon


class WeightedMovingAverage:
    name, min_points = "weighted_moving_average", 2

    def __init__(self, window: int):
        self.window = window

    def predict(self, history, horizon):
        w = history[-self.window:]
        weights = range(1, len(w) + 1)                 # linearly increasing: recent days count more
        level = sum(x * k for x, k in zip(w, weights)) / sum(weights)
        return [level] * horizon


class ExponentialSmoothing:
    """Simple exponential smoothing; with beta > 0 it is Holt's linear trend method (damped so long horizons stay sane)."""
    min_points = 3

    def __init__(self, alpha: float, beta: float = 0.0, damping: float = 0.9):
        self.alpha, self.beta, self.damping = alpha, beta, damping
        self.name = "holt_trend" if beta else "exponential_smoothing"

    def predict(self, history, horizon):
        level, trend = history[0], (history[1] - history[0]) if len(history) > 1 else 0.0
        for x in history[1:]:
            prev = level
            level = self.alpha * x + (1 - self.alpha) * (level + (self.damping * trend if self.beta else 0.0))
            if self.beta:
                trend = self.beta * (level - prev) + (1 - self.beta) * self.damping * trend
        out, acc = [], 0.0
        for h in range(1, horizon + 1):
            acc += self.damping ** h
            out.append(max(0.0, level + (trend * acc if self.beta else 0.0)))
        return out


class SeasonalBaseline:
    """Level (mean of the last full season) x seasonal index of each position in the cycle. Needs >= 2 full seasons."""
    name = "seasonal_baseline"

    def __init__(self, season: int):
        self.season, self.min_points = season, 2 * season

    def predict(self, history, horizon):
        s, n = self.season, len(history)
        usable = history[n - (n // s) * s:]            # whole seasons only, aligned to the end of the series
        level = sum(usable) / len(usable)
        if level <= 0:
            return [0.0] * horizon
        idx: list[list[float]] = [[] for _ in range(s)]
        for i, x in enumerate(usable):
            idx[i % s].append(x)
        factors = [(sum(v) / len(v)) / level for v in idx]
        start = len(usable) % s
        return [max(0.0, level * factors[(start + h) % s]) for h in range(horizon)]


def build_models(cfg: ForecastCfg) -> list[Model]:
    models: list[Model] = [MovingAverage(cfg.ma_window), WeightedMovingAverage(cfg.wma_window), ExponentialSmoothing(cfg.es_alpha, 0.0)]
    if cfg.es_beta > 0:
        models.append(ExponentialSmoothing(cfg.es_alpha, cfg.es_beta))
    models.append(SeasonalBaseline(cfg.season_length))
    return models


# ------------------------------------------------------------------ error metrics
def mae(actual: Sequence[float], pred: Sequence[float]) -> float:
    return sum(abs(a - p) for a, p in zip(actual, pred)) / len(actual)


def rmse(actual: Sequence[float], pred: Sequence[float]) -> float:
    return math.sqrt(sum((a - p) ** 2 for a, p in zip(actual, pred)) / len(actual))


def mape(actual: Sequence[float], pred: Sequence[float]) -> float | None:
    """Mean absolute percentage error over points with a non-zero actual. None when it is mathematically meaningless
    (no non-zero actuals, or most days are zero: intermittent demand), because dividing by ~0 explodes the metric."""
    pts = [(a, p) for a, p in zip(actual, pred) if a > 0]
    if not pts or len(pts) < 0.6 * len(actual):
        return None
    return sum(abs(a - p) / a for a, p in pts) / len(pts) * 100.0


def wape(actual: Sequence[float], pred: Sequence[float]) -> float | None:
    """Weighted absolute percentage error = sum|e| / sum(actual): well defined for intermittent demand."""
    tot = sum(actual)
    return None if tot <= 0 else sum(abs(a - p) for a, p in zip(actual, pred)) / tot


def error_metrics(actual: Sequence[float], pred: Sequence[float]) -> dict:
    return {"mae": mae(actual, pred), "rmse": rmse(actual, pred), "mape": mape(actual, pred), "wape": wape(actual, pred)}


# ------------------------------------------------------------------ forecaster
@dataclass
class Forecast:
    sku_id: str
    forecast: list[float]
    expected_demand: float            # total over the horizon
    confidence: float                 # 0..1
    method: str
    horizon_days: int
    daily_mean: float = 0.0
    daily_std: float = 0.0            # residual std-dev of the chosen model on the backtest (demand variability)
    history_days: int = 0
    metrics: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)   # method -> backtest rmse (None where the model could not run)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["forecast"] = [round(x, 4) for x in self.forecast]
        return d


class DemandForecaster:
    def __init__(self, cfg: ForecastCfg | None = None, models: list[Model] | None = None):
        self.cfg = cfg or get_settings().forecast
        self.models = models if models is not None else build_models(self.cfg)

    def forecast(self, sku_id: str, historical_demand: Sequence[float], horizon_days: int | None = None) -> Forecast:
        cfg, horizon = self.cfg, int(self.cfg.horizon_days if horizon_days is None else horizon_days)
        if horizon < 1:
            raise ValueError("horizon_days must be >= 1")
        hist = _clean(historical_demand)
        n = len(hist)
        if n == 0:
            return Forecast(sku_id, [0.0] * horizon, 0.0, 0.0, "none", horizon, history_days=0,
                            notes=["no demand history: no statistical forecast is possible"])
        hold = min(cfg.backtest_holdout_days, max(1, n // 4))
        train, test = hist[:-hold], hist[-hold:]
        scores: dict[str, float | None] = {}
        best: Model | None = None
        best_rmse = math.inf
        for m in self.models:
            if len(train) < m.min_points:
                scores[m.name] = None                          # not enough data for this model
                continue
            err = rmse(test, m.predict(train, hold))
            scores[m.name] = err
            if err < best_rmse - 1e-9:
                best, best_rmse = m, err
        notes: list[str] = []
        if best is None:                                       # too little data for any backtest: plain mean, flagged low confidence
            best = MovingAverage(max(1, n))
            notes.append(f"only {n} day(s) of history: mean baseline, backtest unavailable")
            preds = best.predict(hist, horizon)
            return Forecast(sku_id, preds, sum(preds), min(0.2, n / cfg.full_confidence_days), best.name, horizon,
                            daily_mean=preds[0], daily_std=0.0, history_days=n, metrics={}, candidates=scores, notes=notes)
        bt_pred = best.predict(train, hold)
        metrics = error_metrics(test, bt_pred)
        # the chosen model is then refit on ALL history for the production forecast
        preds = best.predict(hist, horizon)
        resid_std = math.sqrt(max(0.0, sum((a - p) ** 2 for a, p in zip(test, bt_pred)) / max(1, hold - 0)))
        w = metrics["wape"]
        accuracy = 0.0 if w is None else max(0.0, 1.0 - w)
        confidence = round(accuracy * min(1.0, n / cfg.full_confidence_days), 4)
        if n < cfg.min_history_days:
            confidence = min(confidence, 0.3)
            notes.append(f"history ({n}d) below the {cfg.min_history_days}d minimum: confidence capped")
        return Forecast(sku_id, preds, sum(preds), confidence, best.name, horizon, daily_mean=sum(preds) / horizon,
                        daily_std=resid_std, history_days=n, metrics=metrics, candidates=scores, notes=notes)


def backtest(historical_demand: Sequence[float], model: Model, horizon: int, initial: int, step: int | None = None) -> dict:
    """Rolling-origin evaluation: refit at every origin and score the next `horizon` days. Returns pooled MAE/RMSE/MAPE/WAPE."""
    hist = _clean(historical_demand)
    step = step or horizon
    actual, pred = [], []
    origin = initial
    while origin + horizon <= len(hist):
        pred += model.predict(hist[:origin], horizon)
        actual += hist[origin:origin + horizon]
        origin += step
    if not actual:
        raise ValueError("history too short for the requested backtest window")
    return {**error_metrics(actual, pred), "points": len(actual)}


_CACHE: dict[str, Forecast] = {}


def cached_forecast(forecaster: DemandForecaster, sku_id: str, history: Sequence[float], horizon: int | None = None) -> Forecast:
    """Forecasting is a pure function of (history, config, horizon), so memoising on a content hash can never return a stale answer."""
    h = forecaster.cfg.horizon_days if horizon is None else horizon
    key = hashlib.sha1(json.dumps([sku_id, list(map(float, history)), h, forecaster.cfg.model_dump()], sort_keys=True).encode()).hexdigest()
    hit = _CACHE.get(key)
    if hit is None:
        if len(_CACHE) > 2048:
            _CACHE.clear()
        hit = _CACHE[key] = forecaster.forecast(sku_id, history, h)
    return hit


# ------------------------------------------------------------------ persistence
def load_history(item_id: str, days: int | None = None) -> list[float]:
    sql = "SELECT Quantity FROM demand_history WHERE Item_ID=? ORDER BY Day"
    with db.connect() as c:
        rows = [r[0] for r in c.execute(sql, (item_id,))]
    return rows[-days:] if days else rows


def save_forecast(fc: Forecast, workflow_id: str | None = None) -> int:
    m = fc.metrics or {}
    with db.connect(write=True) as c:
        cur = c.execute("INSERT INTO forecasts (Workflow_ID, Item_ID, Created_At, Method, Horizon_Days, Expected_Demand, Confidence, Daily_JSON, "
                        "MAE, RMSE, MAPE) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (workflow_id, fc.sku_id, datetime.now().isoformat(timespec="seconds"), fc.method, fc.horizon_days,
                         round(fc.expected_demand, 4), fc.confidence, json.dumps([round(x, 3) for x in fc.forecast]),
                         m.get("mae"), m.get("rmse"), m.get("mape")))
        return int(cur.lastrowid or 0)


def latest_forecasts(limit: int = 50) -> list[dict]:
    d = db.df("SELECT * FROM forecasts f WHERE Forecast_ID = (SELECT MAX(Forecast_ID) FROM forecasts WHERE Item_ID=f.Item_ID) "
              "ORDER BY Item_ID LIMIT ?", (int(limit),))
    return d.to_dict("records")


def load_all_histories(limit_skus: int | None = None) -> dict[str, list[float]]:
    """One query for every SKU's demand series (oldest -> newest)."""
    out: dict[str, list[float]] = {}
    with db.connect() as c:
        for item, qty in c.execute("SELECT Item_ID, Quantity FROM demand_history ORDER BY Item_ID, Day"):
            if item not in out and limit_skus is not None and len(out) >= limit_skus:
                break
            out.setdefault(item, []).append(qty)
    return out


def save_forecasts(fcs: list[Forecast], workflow_id: str | None = None) -> int:
    """Persist a whole run's forecasts in ONE transaction."""
    now = datetime.now().isoformat(timespec="seconds")
    tenant = obs.context().get("tenant_id") or "default"
    rows = [(workflow_id, f.sku_id, now, f.method, f.horizon_days, round(f.expected_demand, 4), f.confidence,
             json.dumps([round(x, 3) for x in f.forecast]), (f.metrics or {}).get("mae"), (f.metrics or {}).get("rmse"),
             (f.metrics or {}).get("mape"), tenant) for f in fcs]
    with db.connect(write=True) as c:
        c.executemany("INSERT INTO forecasts (Workflow_ID, Item_ID, Created_At, Method, Horizon_Days, Expected_Demand, Confidence, Daily_JSON, "
                      "MAE, RMSE, MAPE, Tenant_ID) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)
