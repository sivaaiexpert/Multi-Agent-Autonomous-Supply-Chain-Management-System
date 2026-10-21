"""Configurable, per-tenant business policy and model configuration.

Customer-specific rules are DATA (a validated, versioned JSON document in the tenant's database), never code. The policy is translated into
the engine's `Settings` (floors, caps, thresholds) and `TenantRules` (allowed suppliers, lead-time / quantity / amount / budget limits) and applied
for the duration of `tenancy.scope(...)`, so the deterministic engines (optimiser, policy validator) enforce it for every workflow.

Platform guard-rails: a tenant can tighten or relax values only inside platform bounds, and nothing here can switch off the policy validator, the
human gate, authorisation or audit (those have no field to switch).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scm import audit, db, obs
from scm.settings import LLMCfg, Settings, TenantRules

# ---- platform bounds (a tenant cannot go outside these)
MIN_RELIABILITY_FLOOR = 30.0
MIN_QUALITY_FLOOR = 0.50
MAX_PRICE_DEVIATION_CAP = 0.50
MIN_SHARE_FLOOR = 0.30
MAX_LLM_TIMEOUT = 120
MAX_LLM_RETRIES = 3


class ApprovalTier(BaseModel):
    """Orders up to `max_amount` need one approval per entry of `steps`; each entry is the minimum approval LEVEL of that step
    (1 = operator, 2 = approver / manager, 3 = tenant admin / senior). `max_amount=None` is the open-ended last tier."""
    model_config = ConfigDict(extra="forbid")
    max_amount: float | None = Field(None, gt=0)
    steps: list[int] = Field(default_factory=lambda: [2], min_length=1, max_length=3)

    @field_validator("steps")
    @classmethod
    def _levels(cls, v: list[int]) -> list[int]:
        if any(x not in (1, 2, 3) for x in v):
            raise ValueError("approval levels must be 1, 2 or 3")
        return v


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tiers: list[ApprovalTier] = Field(default_factory=lambda: [ApprovalTier()], min_length=1, max_length=6)
    step_timeout_hours: float = Field(24.0, gt=0, le=24 * 30)
    total_timeout_hours: float = Field(72.0, gt=0, le=24 * 60)
    on_timeout: Literal["escalate", "reject"] = "escalate"
    distinct_approvers: bool = True                    # segregation of duties: one person cannot satisfy two steps

    @model_validator(mode="after")
    def _shape(self) -> ApprovalPolicy:
        bounded = [t.max_amount for t in self.tiers[:-1]]
        if any(b is None for b in bounded) or bounded != sorted(bounded):          # type: ignore[type-var]
            raise ValueError("tiers must be ordered by increasing max_amount; only the last tier may be open-ended")
        if self.tiers[-1].max_amount is not None:
            raise ValueError("the last tier must have max_amount=null so that every order amount is covered")
        return self

    def chain_for(self, amount: float) -> list[int]:
        for t in self.tiers:
            if t.max_amount is None or amount <= t.max_amount:
                return list(t.steps)
        return list(self.tiers[-1].steps)


class TenantPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    min_supplier_reliability: float | None = Field(None, ge=MIN_RELIABILITY_FLOOR, le=100)
    min_quality_rate: float | None = Field(None, ge=MIN_QUALITY_FLOOR, le=1)
    max_lead_time_days: float | None = Field(None, gt=0, le=365)
    max_order_quantity: int | None = Field(None, gt=0)
    max_line_amount: float | None = Field(None, gt=0)
    max_batch_amount: float | None = Field(None, gt=0)
    allowed_suppliers: list[str] = Field(default_factory=list, max_length=500)
    budget_per_category_monthly: dict[str, float] = Field(default_factory=dict)
    max_price_deviation: float | None = Field(None, gt=0, le=MAX_PRICE_DEVIATION_CAP)
    max_supplier_share: float | None = Field(None, ge=MIN_SHARE_FLOOR, le=1)
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    approval: ApprovalPolicy = ApprovalPolicy()

    @field_validator("allowed_suppliers")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        import re
        if any(not re.match(r"^[A-Za-z0-9._-]{1,64}$", x) for x in v):
            raise ValueError("supplier ids may only contain letters, digits, '.', '_' and '-'")
        return v

    @field_validator("budget_per_category_monthly")
    @classmethod
    def _budgets(cls, v: dict[str, float]) -> dict[str, float]:
        if any(x <= 0 for x in v.values()):
            raise ValueError("budgets must be positive")
        return v


class ModelConfig(BaseModel):
    """Which approved model/provider a tenant uses. Only model names and bounded call parameters exist: there is no field that could
    disable a safety control, because the LLM has no authority to begin with."""
    model_config = ConfigDict(extra="forbid")
    provider: str = "ollama"
    primary_model: str | None = Field(None, max_length=80)
    secondary_model: str | None = Field(None, max_length=80)
    fallback_model: str | None = Field(None, max_length=80)
    timeout_seconds: int | None = Field(None, ge=1, le=MAX_LLM_TIMEOUT)
    retries: int | None = Field(None, ge=0, le=MAX_LLM_RETRIES)

    @model_validator(mode="after")
    def _platform_allowlist(self) -> ModelConfig:
        providers = {p.strip() for p in os.environ.get("SCM_PLATFORM_ALLOWED_PROVIDERS", "ollama").split(",") if p.strip()}
        if self.provider not in providers:
            raise ValueError(f"provider {self.provider!r} is not allowed on this platform (allowed: {sorted(providers)})")
        allowed = {m.strip() for m in os.environ.get("SCM_PLATFORM_ALLOWED_MODELS", "").split(",") if m.strip()}
        if allowed:
            for m in (self.primary_model, self.secondary_model, self.fallback_model):
                if m and m not in allowed:
                    raise ValueError(f"model {m!r} is not on the platform allow-list")
        return self


RISK_PROFILES = {   # risk tolerance -> (service-level z, P(critical), P(high), P(medium))
    "low": (2.33, 0.80, 0.50, 0.15),
    "medium": (1.65, 0.90, 0.60, 0.25),
    "high": (1.28, 0.95, 0.70, 0.35),
}


def effective_settings(base: Settings, policy: TenantPolicy | None = None, model: ModelConfig | None = None) -> Settings:
    """The engine settings in force for a tenant: platform defaults + the tenant's validated overrides."""
    s = base.model_copy(deep=True)
    if policy is not None:
        if policy.min_supplier_reliability is not None:
            s.supplier.min_reliability_score = policy.min_supplier_reliability
        if policy.min_quality_rate is not None:
            s.supplier.min_quality_rate = policy.min_quality_rate
        if policy.max_price_deviation is not None:
            s.approval.max_price_deviation = policy.max_price_deviation
        if policy.max_supplier_share is not None:
            s.optimizer.max_supplier_share = policy.max_supplier_share
        z, pc, ph, pm = RISK_PROFILES[policy.risk_tolerance]
        s.forecast.service_level_z = z
        s.risk.prob_critical, s.risk.prob_high, s.risk.prob_medium = pc, ph, pm
        s.rules = TenantRules(allowed_vendors=list(policy.allowed_suppliers), max_lead_time_days=policy.max_lead_time_days,
                              max_order_quantity=policy.max_order_quantity, max_line_amount=policy.max_line_amount,
                              max_batch_amount=policy.max_batch_amount, budget_per_category_monthly=dict(policy.budget_per_category_monthly))
    if model is not None:
        llm: LLMCfg = s.llm
        if model.primary_model:
            llm.primary_model = model.primary_model
        if model.secondary_model is not None:
            llm.secondary_model = model.secondary_model
        if model.fallback_model is not None:
            llm.fallback_model = model.fallback_model
        if model.timeout_seconds:
            llm.timeout_seconds = model.timeout_seconds
        if model.retries is not None:
            llm.retries = model.retries
    return s


# ------------------------------------------------------------------ persistence (tenant database)
def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def load_policy() -> TenantPolicy:
    with db.connect() as c:
        try:
            row = c.execute("SELECT Policy_JSON FROM tenant_policy WHERE Tenant_ID=?", (_tenant(),)).fetchone()
        except Exception:                      # tenant database not yet migrated
            return TenantPolicy()
    return TenantPolicy.model_validate_json(row[0]) if row else TenantPolicy()


def policy_version() -> int:
    with db.connect() as c:
        row = c.execute("SELECT Version FROM tenant_policy WHERE Tenant_ID=?", (_tenant(),)).fetchone()
    return int(row[0]) if row else 0


def save_policy(policy: TenantPolicy, actor: str) -> int:
    """Versioned save; the previous version stays in `tenant_policy_history`. Audited."""
    body, now, tid = policy.model_dump_json(), datetime.now().isoformat(timespec="seconds"), _tenant()
    with db.connect(write=True) as c:
        row = c.execute("SELECT Version FROM tenant_policy WHERE Tenant_ID=?", (tid,)).fetchone()
        version = (int(row[0]) if row else 0) + 1
        c.execute("INSERT INTO tenant_policy (Tenant_ID, Version, Policy_JSON, Updated_By, Updated_At) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(Tenant_ID) DO UPDATE SET Version=excluded.Version, Policy_JSON=excluded.Policy_JSON, Updated_By=excluded.Updated_By, "
                  "Updated_At=excluded.Updated_At", (tid, version, body, actor, now))
        c.execute("INSERT INTO tenant_policy_history (Tenant_ID, Version, Policy_JSON, Updated_By, Updated_At) VALUES (?,?,?,?,?)", (tid, version, body, actor, now))
        audit.record("POLICY_UPDATED", None, actor, {"version": version, "policy": json.loads(body)}, conn=c)
    return version


def policy_history(limit: int = 20) -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT Version, Policy_JSON, Updated_By, Updated_At FROM tenant_policy_history WHERE Tenant_ID=? ORDER BY Version DESC LIMIT ?",
                         (_tenant(), limit)).fetchall()
    return [{"version": r[0], "policy": json.loads(r[1]), "updated_by": r[2], "updated_at": r[3]} for r in rows]


def load_model_config() -> ModelConfig:
    with db.connect() as c:
        try:
            row = c.execute("SELECT Config_JSON FROM tenant_models WHERE Tenant_ID=?", (_tenant(),)).fetchone()
        except Exception:
            return ModelConfig()
    return ModelConfig.model_validate_json(row[0]) if row else ModelConfig()


def save_model_config(cfg: ModelConfig, actor: str) -> None:
    with db.connect(write=True) as c:
        c.execute("INSERT INTO tenant_models (Tenant_ID, Config_JSON, Updated_At) VALUES (?,?,?) ON CONFLICT(Tenant_ID) DO UPDATE SET "
                  "Config_JSON=excluded.Config_JSON, Updated_At=excluded.Updated_At", (_tenant(), cfg.model_dump_json(), datetime.now().isoformat(timespec="seconds")))
        audit.record("MODEL_CONFIG_UPDATED", None, actor, cfg.model_dump(), conn=c)
