"""Strict schemas for every structured LLM output.

Contract: a model may only produce *advisory text*. Decision fields (vendor, quantity, price, approval) are not part of any schema; if a
model emits them anyway they are reported as `ignored_fields` and never read. Schemas validate; policy.py + the optimiser decide.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .security import sanitize_llm_text

DECISION_FIELDS = frozenset({"selected_vendor", "recommended_order_quantity", "unit_price_usd", "vendor", "supplier", "quantity", "qty",
                             "price", "unit_price", "total", "approve", "approved", "decision", "status", "system_telemetry_override"})


def _text(v: str, limit: int) -> str:
    """Oversized output is truncated (still useful), empty / tag-only output is rejected."""
    clean = sanitize_llm_text(v, limit)
    if len(clean) < 3:
        raise ValueError("text is empty after sanitisation")
    return clean


class _Advisory(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class DemandAnalysis(_Advisory):
    """Analytics agent: narrative over the deterministic forecast + risk numbers."""
    summary: str

    @field_validator("summary")
    @classmethod
    def _clean(cls, v: str) -> str:
        return _text(v, 600)


class RiskExplanation(_Advisory):
    """Optional per-SKU explanations (SKU -> sentence)."""
    insights: dict[str, str] = Field(default_factory=dict, max_length=500)

    @field_validator("insights")
    @classmethod
    def _clean(cls, v: dict[str, str]) -> dict[str, str]:
        return {str(k)[:64]: sanitize_llm_text(s, 300) for k, s in v.items() if isinstance(s, str) and s.strip()}


class ProcurementRecommendation(_Advisory):
    """Procurement agent: rationale for the optimiser's already-made decision."""
    summary: str
    notes: dict[str, str] = Field(default_factory=dict, max_length=500)

    @field_validator("summary")
    @classmethod
    def _clean(cls, v: str) -> str:
        return _text(v, 600)

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, v: dict[str, str]) -> dict[str, str]:
        return {str(k)[:64]: sanitize_llm_text(s, 300) for k, s in v.items() if isinstance(s, str) and s.strip()}


class VendorMessage(_Advisory):
    """Negotiation / vendor-communication copy: tone only, never terms."""
    intro: str
    closing: str = "Thank you for your continued partnership."

    @field_validator("intro")
    @classmethod
    def _clean_intro(cls, v: str) -> str:
        return _text(v, 700)

    @field_validator("closing")
    @classmethod
    def _clean_closing(cls, v: str) -> str:
        return sanitize_llm_text(v, 300) or "Thank you for your continued partnership."


SCHEMA_BY_AGENT = {"Analytics": DemandAnalysis, "Procurement": ProcurementRecommendation, "Vendor Comms": VendorMessage}


def validate_output(schema: type[BaseModel], data: dict) -> tuple[BaseModel | None, list[str], list[str]]:
    """Returns (model | None, problems, ignored_decision_fields)."""
    ignored = sorted(k for k in data if k in DECISION_FIELDS)
    try:
        return schema.model_validate(data), [], ignored
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:5], ignored
