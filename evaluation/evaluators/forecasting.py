"""Forecast accuracy, stock-out prediction accuracy and an inventory simulation (static reorder point vs predictive engine).

All inputs come from the synthetic generators in evaluation.datasets, so the *true* demand process is known and the comparison is fair.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from evaluation.datasets.synthetic import DemandProcess, demand_series, random_process
from evaluation.metrics.stats import precision_recall, rate, safe_mean
from scm.forecast import DemandForecaster, MovingAverage, error_metrics
from scm.risk import RiskLevel, assess, static_assessment


def forecast_accuracy(n_series: int = 60, history: int = 84, horizon: int = 14, seed: int = 1) -> dict:
    """Out-of-sample accuracy of the DemandForecaster vs a naive 'last 7 days mean' baseline on synthetic series."""
    rng, f = random.Random(seed), DemandForecaster()
    ours, naive = [], []
    methods: dict[str, int] = {}
    for i in range(n_series):
        proc = random_process(rng)
        full = demand_series(proc, history + horizon, seed * 1000 + i)
        train, test = full[:history], full[history:]
        fc = f.forecast(f"S{i}", train, horizon)
        methods[fc.method] = methods.get(fc.method, 0) + 1
        ours.append(error_metrics(test, fc.forecast))
        naive.append(error_metrics(test, MovingAverage(7).predict(train, horizon)))

    def agg(rows):
        return {k: safe_mean([r[k] for r in rows if r[k] is not None]) for k in ("mae", "rmse", "mape", "wape")}
    a, b = agg(ours), agg(naive)
    return {"series": n_series, "history_days": history, "horizon_days": horizon, "engine": a, "naive_last7_mean": b, "methods_chosen": methods,
            "mape_defined_for": sum(r["mape"] is not None for r in ours),
            "rmse_improvement_vs_naive": None if not b["rmse"] else 1 - a["rmse"] / b["rmse"]}


def stockout_prediction(n: int = 300, seed: int = 2, history: int = 84) -> dict:
    """Does a HIGH/CRITICAL assessment predict a real stock-out within the lead time? Ground truth = simulate the true process forward.
    Compared with the legacy rule (stock below the static reorder point)."""
    rng, f = random.Random(seed), DemandForecaster()
    eng = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    old = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for i in range(n):
        proc = random_process(rng)
        past = demand_series(proc, history, seed * 7919 + i)
        lead = rng.randint(3, 14)
        mean_d = sum(past[-14:]) / 14
        stock = int(mean_d * lead * rng.uniform(0.4, 2.0))
        reorder_point = int(mean_d * 7)                      # a typical fixed rule: ~1 week of average demand
        target = int(mean_d * 30)
        future = demand_series(proc, lead, seed * 104729 + i, start_day=history)
        actual = sum(future) > stock                         # real stock-out before the order could arrive
        ra = assess("S", stock, f.forecast("S", past, 30), lead, target, reorder_point)
        pred_eng = ra.risk_level.rank >= RiskLevel.HIGH.rank
        pred_old = static_assessment("S", stock, reorder_point, target).risk_level.rank >= RiskLevel.MEDIUM.rank
        for pred, bucket in ((pred_eng, eng), (pred_old, old)):
            key = ("tp" if actual else "fp") if pred else ("fn" if actual else "tn")
            bucket[key] += 1

    def summarise(b):
        pr = precision_recall(b["tp"], b["fp"], b["fn"])
        total = sum(b.values())
        return {**pr, "accuracy": rate(b["tp"] + b["tn"], total), "f1": (2 * pr["precision"] * pr["recall"] / (pr["precision"] + pr["recall"]))
                if pr["precision"] and pr["recall"] else None, **b}
    base_rate = (eng["tp"] + eng["fn"]) / n
    return {"cases": n, "actual_stockout_rate": base_rate, "risk_engine_high_or_critical": summarise(eng), "legacy_static_rule": summarise(old)}


# ------------------------------------------------------------------ inventory simulation
@dataclass
class SimResult:
    stockout_days: int
    orders: int
    purchase_cost: float
    holding_units_days: float


def _simulate(policy: str, proc: DemandProcess, seed: int, days: int, lead: int, reliability: float, price: float, start_stock: int,
              target_mult: float = 30.0) -> SimResult:
    rng = random.Random(seed)
    hist = demand_series(proc, 84, seed + 1)
    forecaster = DemandForecaster()
    mean_d = sum(hist[-14:]) / 14
    reorder_point, target = int(mean_d * 7), int(mean_d * target_mult)
    pipeline: list[tuple[int, int]] = []
    stock, out_days, orders_n, cost, hold = start_stock, 0, 0, 0.0, 0.0
    history = list(hist)
    for day in range(days):
        pipeline = [(t - 1, q) for t, q in pipeline]
        stock += sum(q for t, q in pipeline if t <= 0)
        pipeline = [(t, q) for t, q in pipeline if t > 0]
        d = proc.sample(84 + day, rng)
        history.append(d)
        if d > stock:
            out_days += 1
        stock = max(0, int(stock - d))
        hold += stock
        on_order = sum(q for _, q in pipeline)
        qty = 0
        if policy == "static":
            if stock + on_order < reorder_point:
                qty = max(0, target - stock - on_order)
        else:
            fc = forecaster.forecast("S", history[-84:], 30)
            ra = assess("S", stock, fc, lead, target, reorder_point, on_order)
            if ra.risk_level.rank >= RiskLevel.MEDIUM.rank:
                qty = max(0, ra.recommended_order_quantity)
        if qty > 0:
            orders_n += 1
            cost += qty * price
            late = 0 if rng.random() < reliability / 100.0 else rng.randint(2, 6)
            pipeline.append((lead + late, qty))
    return SimResult(out_days, orders_n, cost, hold)


def inventory_simulation(n: int = 40, days: int = 120, seed: int = 3) -> dict:
    """Same demand, same suppliers, same random delivery delays: only the ordering policy differs."""
    rng = random.Random(seed)
    rows: dict[str, list[SimResult]] = {"static": [], "predictive": []}
    for i in range(n):
        proc = random_process(rng)
        lead, rel, price = rng.randint(3, 14), rng.uniform(70, 98), rng.uniform(5, 50)
        start = int(proc.base * rng.uniform(5, 25))
        for pol in rows:
            rows[pol].append(_simulate(pol, proc, seed * 31 + i, days, lead, rel, price, start))

    def agg(rs):
        return {"stockout_days_per_sku": safe_mean([r.stockout_days for r in rs]), "skus_with_any_stockout": sum(r.stockout_days > 0 for r in rs),
                "orders_per_sku": safe_mean([r.orders for r in rs]), "avg_stock_units": safe_mean([r.holding_units_days / days for r in rs]),
                "purchase_cost": sum(r.purchase_cost for r in rs)}
    s, p = agg(rows["static"]), agg(rows["predictive"])
    red = None if not s["stockout_days_per_sku"] else 1 - p["stockout_days_per_sku"] / s["stockout_days_per_sku"]
    return {"skus": n, "days": days, "static_reorder_point": s, "predictive_engine": p, "stockout_day_reduction": red,
            "note": "synthetic Monte-Carlo: same demand and delivery delays under both policies"}
