import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scm.settings import ForecastCfg, OptimizerCfg, Settings, get_settings, reset_settings_cache

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_defaults_are_sane_and_documented_in_one_place():
    s = get_settings()
    assert s.optimizer.max_supplier_share == 0.7 and s.llm.retries == 2 and s.approval.executive_review_total == 1_000_000
    assert abs(s.supplier.w_on_time + s.supplier.w_fulfillment + s.supplier.w_quality + s.supplier.w_lead_time_stability + s.supplier.w_cancellation - 1.0) < 1e-9


def test_environment_variables_override_nested_values(monkeypatch):
    monkeypatch.setenv("SCM_OPTIMIZER__MAX_SUPPLIER_SHARE", "0.55")
    monkeypatch.setenv("SCM_LLM__RETRIES", "0")
    monkeypatch.setenv("SCM_APPROVAL__EXECUTIVE_REVIEW_TOTAL", "500")
    s = Settings()
    assert s.optimizer.max_supplier_share == 0.55 and s.llm.retries == 0 and s.approval.executive_review_total == 500


def test_json_config_file_is_loaded(monkeypatch, tmp_path):
    f = tmp_path / "engine.json"
    f.write_text(json.dumps({"optimizer": {"max_supplier_share": 0.5}, "llm": {"primary_model": "custom-model"}}), encoding="utf-8")
    monkeypatch.setenv("SCM_CONFIG_FILE", str(f))
    s = get_settings()
    assert s.optimizer.max_supplier_share == 0.5 and s.llm.primary_model == "custom-model"


def test_the_shipped_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("SCM_CONFIG_FILE", str(ROOT / "config" / "engine.example.json"))
    assert get_settings().optimizer.diversify_min_qty == 250


@pytest.mark.parametrize("cls,kw", [(ForecastCfg, {"horizon_days": 0}), (ForecastCfg, {"es_alpha": 1.5}), (OptimizerCfg, {"max_supplier_share": 0}),
                                    (OptimizerCfg, {"max_supplier_share": 1.5})])
def test_invalid_configuration_is_rejected(cls, kw):
    with pytest.raises(ValidationError):
        cls(**kw)


def test_env_example_lists_every_documented_variable():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("SCM_DB_PATH", "SCM_API_KEYS", "SCM_UI_ROLE", "SCM_LLM__PRIMARY_MODEL", "SCM_OPTIMIZER__MAX_SUPPLIER_SHARE", "SCM_CHECKPOINT_DB"):
        assert var in text
    assert "=sk-" not in text and "password" not in text.lower()
