"""Supplier intelligence: performance metrics from delivery history and a deterministic, configurable reliability score.

score (0-100) = 100 * (w_otd * on_time + w_fill * fulfillment + w_q * quality + w_lt * lead_time_stability + w_c * (1 - cancellation))
Few observations are blended toward the vendor's static reliability (a Bayesian-style prior), so a supplier with 2 deliveries cannot
look perfect or terrible.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean, pvariance

from . import db
from .settings import SupplierCfg, get_settings


@dataclass
class SupplierMetrics:
    vendor_id: str
    on_time_delivery_rate: float | None
    average_lead_time: float | None
    lead_time_variance: float | None
    quality_rate: float | None
    defect_rate: float | None
    historical_price: float | None
    price_volatility: float | None          # mean coefficient of variation of the price per SKU
    order_fulfillment_rate: float | None
    cancellation_rate: float | None
    reliability_score: float                # 0-100
    observations: int
    source: str                             # "history" | "blended" | "prior"

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def compute_metrics(vendor_id: str, deliveries: list[dict], prior_reliability: float, cfg: SupplierCfg | None = None) -> SupplierMetrics:
    """Pure function: delivery rows (dicts) in, metrics + score out."""
    cfg = cfg or get_settings().supplier
    prior = max(0.0, min(100.0, float(prior_reliability)))
    n = len(deliveries)
    if n == 0:
        return SupplierMetrics(vendor_id, None, None, None, None, None, None, None, None, None, round(prior, 2), 0, "prior")
    delivered = [d for d in deliveries if d["Status"] == "DELIVERED" and d.get("Actual_Days") is not None]
    cancelled = n - len([d for d in deliveries if d["Status"] != "CANCELLED"])
    cancellation = cancelled / n
    ordered = sum(d["Qty_Ordered"] for d in deliveries if d["Status"] != "CANCELLED")
    received = sum(d["Qty_Received"] for d in delivered)
    fill = min(1.0, received / ordered) if ordered else None
    otd = (sum(d["Actual_Days"] <= d["Promised_Days"] for d in delivered) / len(delivered)) if delivered else None
    lead = [d["Actual_Days"] for d in delivered]
    avg_lead = mean(lead) if lead else None
    var_lead = pvariance(lead) if len(lead) > 1 else (0.0 if lead else None)
    defects = sum(d["Qty_Defective"] for d in delivered)
    quality = max(0.0, 1.0 - defects / received) if received else None
    prices_by_sku: dict[str, list[float]] = {}
    for d in deliveries:
        prices_by_sku.setdefault(d["Item_ID"], []).append(d["Unit_Price"])
    cvs = [math.sqrt(pvariance(p)) / mean(p) for p in prices_by_sku.values() if len(p) > 1 and mean(p) > 0]
    all_prices = [p for v in prices_by_sku.values() for p in v]
    stability = 1.0 / (1.0 + (math.sqrt(var_lead) / avg_lead if (avg_lead and var_lead is not None) else 0.0))
    parts = [(cfg.w_on_time, otd), (cfg.w_fulfillment, fill), (cfg.w_quality, quality), (cfg.w_lead_time_stability, stability if lead else None),
             (cfg.w_cancellation, 1.0 - cancellation)]
    used = [(w, v) for w, v in parts if v is not None]
    wsum = sum(w for w, _ in used)
    observed = 100.0 * sum(w * v for w, v in used) / wsum if wsum else prior
    score = (n * observed + cfg.prior_strength * prior) / (n + cfg.prior_strength)
    return SupplierMetrics(vendor_id, otd, avg_lead, var_lead, quality, None if quality is None else 1.0 - quality,
                           mean(all_prices) if all_prices else None, mean(cvs) if cvs else 0.0, fill, cancellation,
                           round(score, 2), n, "history" if n >= 20 else "blended")


# ------------------------------------------------------------------ persistence + cache
_CACHE: dict[str, tuple[float, dict[str, SupplierMetrics]]] = {}
_LOCK = threading.Lock()


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def record_delivery(vendor_id: str, item_id: str, ordered_at: str, promised_days: int, actual_days: int | None, qty_ordered: int,
                    qty_received: int, qty_defective: int, unit_price: float, status: str = "DELIVERED") -> None:
    with db.connect(write=True) as c:
        c.execute("INSERT INTO supplier_deliveries (Vendor_ID, Item_ID, Ordered_At, Promised_Days, Actual_Days, Qty_Ordered, Qty_Received, "
                  "Qty_Defective, Unit_Price, Status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (vendor_id, item_id, ordered_at, promised_days, actual_days, qty_ordered, qty_received, qty_defective, unit_price, status))
    invalidate_cache()          # a new fact changes the score: never serve the stale one


def all_metrics(persist: bool = False) -> dict[str, SupplierMetrics]:
    """Metrics for every vendor, cached for `cache_ttl_seconds` (and invalidated by `record_delivery`)."""
    cfg = get_settings().supplier
    key = str(db.current_path())
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < cfg.cache_ttl_seconds and not persist:
            return hit[1]
    with db.connect() as c:
        vendors = [dict(r) for r in c.execute("SELECT Vendor_ID, Reliability FROM vendors")]
        rows = [dict(r) for r in c.execute("SELECT * FROM supplier_deliveries")]
    by_vendor: dict[str, list[dict]] = {}
    for r in rows:
        by_vendor.setdefault(r["Vendor_ID"], []).append(r)
    out = {v["Vendor_ID"]: compute_metrics(v["Vendor_ID"], by_vendor.get(v["Vendor_ID"], []), v["Reliability"] or 90.0, cfg) for v in vendors}
    if persist:
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect(write=True) as c:
            for m in out.values():
                c.execute("INSERT INTO supplier_metrics (Vendor_ID, On_Time_Rate, Avg_Lead_Time, Lead_Time_Variance, Quality_Rate, Defect_Rate, "
                          "Historical_Price, Price_Volatility, Fulfillment_Rate, Cancellation_Rate, Reliability_Score, Observations, Source, Updated_At) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(Vendor_ID) DO UPDATE SET On_Time_Rate=excluded.On_Time_Rate, "
                          "Avg_Lead_Time=excluded.Avg_Lead_Time, Lead_Time_Variance=excluded.Lead_Time_Variance, Quality_Rate=excluded.Quality_Rate, "
                          "Defect_Rate=excluded.Defect_Rate, Historical_Price=excluded.Historical_Price, Price_Volatility=excluded.Price_Volatility, "
                          "Fulfillment_Rate=excluded.Fulfillment_Rate, Cancellation_Rate=excluded.Cancellation_Rate, "
                          "Reliability_Score=excluded.Reliability_Score, Observations=excluded.Observations, Source=excluded.Source, "
                          "Updated_At=excluded.Updated_At",
                          (m.vendor_id, m.on_time_delivery_rate, m.average_lead_time, m.lead_time_variance, m.quality_rate, m.defect_rate,
                           m.historical_price, m.price_volatility, m.order_fulfillment_rate, m.cancellation_rate, m.reliability_score,
                           m.observations, m.source, now))
    with _LOCK:
        _CACHE[key] = (time.monotonic(), out)
    return out
