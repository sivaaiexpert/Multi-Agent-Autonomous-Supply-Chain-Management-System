"""Deterministic inventory risk engine. No LLM is involved anywhere in this file: risk levels are pure functions of numbers and config.

Lead-time demand  ~ Normal(mu_L, sigma_L)   mu_L = sum of forecast over the lead time,
                                             sigma_L^2 = L * sigma_d^2 + d^2 * sigma_LT^2     (demand and lead-time variability)
stockout_probability = P(demand during lead time > stock position)  (stock on hand + open purchase orders)
safety_stock         = z * sigma_L          dynamic reorder point = mu_L + safety_stock
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum

from . import db
from .forecast import Forecast
from .settings import RiskCfg, get_settings


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(self.value)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class RiskAssessment:
    sku_id: str
    risk_level: RiskLevel
    current_stock: int
    on_order: int
    daily_demand: float | None
    days_until_stockout: float | None      # on-hand cover at the forecast rate; None = no demand expected
    stockout_probability: float
    safety_stock: int
    dynamic_reorder_point: int
    recommended_order_quantity: int
    lead_time_days: float
    basis: str                             # "forecast" | "static_rule"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        for k in ("daily_demand", "days_until_stockout", "stockout_probability"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        return d


def _round_up(qty: float, pack: int) -> int:
    return int(math.ceil(max(0.0, qty) / pack) * pack) if pack > 1 else int(math.ceil(max(0.0, qty)))


def static_assessment(sku_id: str, stock: int, reorder_point: int, target: int, on_order: int = 0, cfg: RiskCfg | None = None) -> RiskAssessment:
    """Legacy rule for SKUs without demand history (kept so imported data keeps working): urgency from stock / reorder-point ratio."""
    cfg = cfg or get_settings().risk
    stock = max(0, int(stock))
    rop = max(int(reorder_point), 0)
    if stock >= rop:
        level, reasons, prob = RiskLevel.LOW, ["stock at or above the static reorder point"], 0.0
    else:
        ratio = stock / max(rop, 1)
        level = (RiskLevel.CRITICAL if ratio <= cfg.static_ratio_critical else RiskLevel.HIGH if ratio <= cfg.static_ratio_high
                 else RiskLevel.MEDIUM)
        reasons = [f"no demand history: static rule, stock is {ratio:.0%} of the reorder point"]
        prob = 1.0 if stock == 0 else min(1.0, 1.0 - ratio)
    qty = _round_up(target - stock - on_order, cfg.pack_size) if target > stock + on_order else 0
    return RiskAssessment(sku_id, level, stock, on_order, None, None, prob, 0, rop, qty, 0.0, "static_rule", reasons)


def assess(sku_id: str, stock: int, forecast: Forecast, lead_time_days: float, target_stock: int, reorder_point: int = 0,
           on_order: int = 0, lead_time_std: float = 0.0, cfg: RiskCfg | None = None, z: float | None = None) -> RiskAssessment:
    cfg = cfg or get_settings().risk
    z = get_settings().forecast.service_level_z if z is None else z
    stock = max(0, int(stock))
    on_order = max(0, int(on_order))
    L = max(0.0, float(lead_time_days)) or float(cfg.default_lead_time_days)
    horizon = len(forecast.forecast)
    d = forecast.daily_mean
    # lead-time demand from the forecast curve (extend with the mean when the lead time exceeds the horizon)
    whole = int(math.floor(L))
    mu_l = sum(forecast.forecast[:whole]) + (d * (whole - horizon) if whole > horizon else 0.0) + (L - whole) * d
    sigma_l = math.sqrt(max(0.0, L * forecast.daily_std ** 2 + (d * lead_time_std) ** 2))
    position = stock + on_order
    if sigma_l <= 1e-9:
        prob = 1.0 if mu_l > position else 0.0
    else:
        prob = 1.0 - norm_cdf((position - mu_l) / sigma_l)
    safety = int(math.ceil(z * sigma_l))
    rop_dyn = int(math.ceil(mu_l + safety))
    days_left = None if d <= 1e-9 else stock / d
    # order-up-to level covers lead time + review period at the forecast rate, plus safety stock; never below the configured target
    dynamic_level = mu_l + d * cfg.review_period_days + safety
    up_to = dynamic_level if cfg.order_up_to == "dynamic" else max(float(target_stock), dynamic_level)
    need = _round_up(up_to - position, cfg.pack_size) if position <= max(rop_dyn, reorder_point) else 0
    reasons = []
    if stock <= 0 and d > 0:
        level = RiskLevel.CRITICAL
        reasons.append("stock is zero while demand is forecast")
    elif prob >= cfg.prob_critical or (days_left is not None and days_left < L * 0.5 and position < mu_l):
        level = RiskLevel.CRITICAL
    elif prob >= cfg.prob_high or (days_left is not None and days_left < L and position < mu_l):
        level = RiskLevel.HIGH
    elif prob >= cfg.prob_medium or position < rop_dyn or (reorder_point and stock < reorder_point):
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW
    reasons.append(f"P(stock-out in {L:g}d lead time) = {prob:.0%}")
    if days_left is not None:
        reasons.append(f"on-hand cover {days_left:.1f} days vs lead time {L:g} days")
    if position < rop_dyn:
        reasons.append(f"stock position {position} below dynamic reorder point {rop_dyn}")
    if on_order:
        reasons.append(f"{on_order} units already on order")
    return RiskAssessment(sku_id, level, stock, on_order, d, days_left, prob, safety, rop_dyn, need, L, "forecast", reasons)


def open_order_quantity(item_id: str) -> int:
    """Units on order and not yet received. Orders awaiting approval do NOT count (they may be rejected). Note: the dispatch step currently
    simulates instant receipt (status SENT adds stock immediately), so only IN_TRANSIT orders, which an ERP integration would set, count here."""
    with db.connect() as c:
        return int(c.execute("SELECT COALESCE(SUM(Quantity),0) FROM purchase_orders WHERE Item_ID=? AND Status='IN_TRANSIT'",
                             (item_id,)).fetchone()[0])


def open_orders_by_item() -> dict[str, int]:
    """Same rule as open_order_quantity, for every SKU in one query (the analytics node calls this once instead of once per SKU)."""
    with db.connect() as c:
        return {r[0]: int(r[1]) for r in c.execute("SELECT Item_ID, SUM(Quantity) FROM purchase_orders WHERE Status='IN_TRANSIT' GROUP BY Item_ID")}
