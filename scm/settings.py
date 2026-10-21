"""Central, typed configuration. Every business threshold, weight, timeout and retry limit lives here (no magic numbers in the engines).

Override with environment variables using the `SCM_` prefix and `__` for nesting, e.g.
    SCM_OPTIMIZER__MAX_SUPPLIER_SHARE=0.6   SCM_LLM__RETRIES=1   SCM_APPROVAL__EXECUTIVE_REVIEW_TOTAL=500000
or point SCM_CONFIG_FILE at a JSON file with the same structure. Secrets (API keys) are never part of this object.
"""
from __future__ import annotations

import contextvars
import json
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ForecastCfg(BaseModel):
    horizon_days: int = Field(30, ge=1, le=365)
    min_history_days: int = Field(14, ge=3)          # below this the forecaster refuses to claim statistical confidence
    full_confidence_days: int = Field(56, ge=7)      # history length at which the length-penalty on confidence reaches 1
    ma_window: int = Field(7, ge=1)
    wma_window: int = Field(7, ge=2)
    es_alpha: float = Field(0.3, gt=0, le=1)
    es_beta: float = Field(0.1, ge=0, le=1)          # trend smoothing (Holt); 0 disables the trend component
    season_length: int = Field(7, ge=2)              # weekly seasonality for daily data
    backtest_holdout_days: int = Field(7, ge=1)
    service_level_z: float = Field(1.65, ge=0)       # 95 % cycle service level


class RiskCfg(BaseModel):
    prob_critical: float = 0.90
    prob_high: float = 0.60
    prob_medium: float = 0.25
    review_period_days: int = 7                      # order-up-to cover beyond the lead time
    default_lead_time_days: int = 7
    pack_size: int = 5                               # orders are rounded up to whole packs
    static_ratio_critical: float = 0.35              # legacy rule for SKUs without demand history
    static_ratio_high: float = 0.70
    order_up_to: Literal["static_floor", "dynamic"] = "static_floor"   # static_floor: never below the customer's target stock; dynamic: lead-time + review demand + safety only
    predictive_alerts: bool = True                   # alert on forecast risk even when stock is above the static reorder point
    max_forecast_skus: int = 1000                    # per-run bound on forecasting work


class SupplierCfg(BaseModel):
    w_on_time: float = 0.35
    w_fulfillment: float = 0.20
    w_quality: float = 0.25
    w_lead_time_stability: float = 0.10
    w_cancellation: float = 0.10
    prior_strength: float = 5.0                      # pseudo-observations given to the vendor's static reliability score
    min_quality_rate: float = 0.90                   # eligibility floor
    min_reliability_score: float = 60.0              # eligibility floor (0-100)
    cache_ttl_seconds: int = 60


class OptimizerCfg(BaseModel):
    # objective = purchase cost + expected stock-out cost + lead-time cost + supplier-risk cost (all as fractions of unit price)
    stockout_rate_per_day: float = 0.01              # unit-value lost per day of expected stock-out exposure
    lead_time_rate_per_day: float = 0.002            # cost of capital / waiting per day of lead time
    risk_rate: float = 0.50                          # multiplies (1 - reliability * quality)
    order_fixed_cost: float = 25.0                   # per supplier used; discourages pointless splitting
    max_supplier_share: float = Field(0.70, gt=0, le=1)
    diversify_min_qty: int = 250                     # share cap only applies to orders at least this large
    max_overorder_factor: float = Field(3.0, ge=1.0)    # MOQs may raise an order, but never beyond this multiple of the required quantity
    max_suppliers: int = 8                           # exhaustive search bound (2^8 subsets)
    budget: float | None = None                      # optional hard cap per item
    allowed_regions: list[str] = Field(default_factory=list)   # empty = all regions
    pack_size: int = 5


class ApprovalCfg(BaseModel):
    executive_review_total: float = 1_000_000.0
    elevated_review_total: float = 250_000.0
    max_qty_multiple_of_target: float = 3.0          # order quantity above N x target stock is blocked
    max_price_deviation: float = 0.25                # proposed unit price must be within 25 % of the catalogue quote
    max_unit_price: float = 1_000_000.0


class LLMCfg(BaseModel):
    primary_model: str = "llama3.2"
    secondary_model: str = ""                        # tried when the primary fails / its breaker is open ("" = none)
    fallback_model: str = "llama3.2:1b"              # smaller local model; "" = none
    complex_model_override: str = ""                 # optional stronger model for complex tasks
    base_url: str = "http://localhost:11434"
    timeout_seconds: int = 60
    retries: int = Field(2, ge=0, le=5)              # bounded: never infinite
    backoff_base_seconds: float = 0.25
    backoff_max_seconds: float = 4.0
    breaker_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0
    price_in_per_m: float = 2.50                     # reference cloud price used for the *estimated* cost comparison
    price_out_per_m: float = 10.00
    local_cost_per_m: float = 0.0                    # local inference marginal cost


class ObsCfg(BaseModel):
    log_level: str = "INFO"
    log_json: bool = True
    persist_spans: bool = True


class TenantRules(BaseModel):
    """Customer-specific business rules that the deterministic engines enforce (set per tenant through the policy layer, never hard-coded)."""
    allowed_vendors: list[str] = Field(default_factory=list)          # empty = every vendor
    max_lead_time_days: float | None = None
    max_order_quantity: int | None = None                              # per proposal line
    max_line_amount: float | None = None                               # per proposal line (price x quantity)
    max_batch_amount: float | None = None                              # whole procurement batch
    budget_per_category_monthly: dict[str, float] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCM_", env_nested_delimiter="__", extra="ignore")
    rules: TenantRules = TenantRules()
    forecast: ForecastCfg = ForecastCfg()
    risk: RiskCfg = RiskCfg()
    supplier: SupplierCfg = SupplierCfg()
    optimizer: OptimizerCfg = OptimizerCfg()
    approval: ApprovalCfg = ApprovalCfg()
    llm: LLMCfg = LLMCfg()
    obs: ObsCfg = ObsCfg()


@lru_cache(maxsize=1)
def _base_settings() -> Settings:
    path = os.environ.get("SCM_CONFIG_FILE")
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return Settings(**data)
    return Settings()


_OVERRIDE: contextvars.ContextVar[Settings | None] = contextvars.ContextVar("scm_settings_override", default=None)


def get_settings() -> Settings:
    """Platform settings, or the current tenant's effective settings when called inside `use_settings(...)` (see enterprise.tenancy.scope)."""
    override = _OVERRIDE.get()
    return override if override is not None else _base_settings()


@contextmanager
def use_settings(settings: Settings):
    token = _OVERRIDE.set(settings)
    try:
        yield settings
    finally:
        _OVERRIDE.reset(token)


def reset_settings_cache() -> None:
    _base_settings.cache_clear()
