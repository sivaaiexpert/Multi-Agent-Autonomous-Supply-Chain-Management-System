"""Production-readiness check, case-study generator, demo and failure suite: the tooling that backs the commercial claims."""
import json
from pathlib import Path

import pytest

from enterprise import casestudy
from scripts import production_readiness_check as chk

ROOT = Path(__file__).resolve().parent.parent


def test_readiness_check_fails_in_production_without_a_secret_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SCM_ENV", "production")
    monkeypatch.delenv("SCM_SECRET_KEY", raising=False)
    monkeypatch.setenv("SCM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCM_CONTROL_DB", str(tmp_path / "data" / "control.db"))
    items = chk.run()
    secret = [i for i in items if i["check"] == "SCM_SECRET_KEY"][0]
    assert secret["status"] == "FAIL"
    assert chk.main([]) == 1


def test_readiness_check_flags_wildcard_cors_and_never_prints_secret_values(monkeypatch, tmp_path, capsys):
    from enterprise import secrets_store
    key = secrets_store.generate_secret_key()
    monkeypatch.setenv("SCM_ENV", "production")
    monkeypatch.setenv("SCM_SECRET_KEY", key)
    monkeypatch.setenv("SCM_CORS_ORIGINS", "*")
    monkeypatch.setenv("SCM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCM_CONTROL_DB", str(tmp_path / "data" / "control.db"))
    chk.main([])
    out = capsys.readouterr().out
    assert key not in out
    assert any(i["check"] == "SCM_CORS_ORIGINS" and i["status"] == "FAIL" for i in chk.run())


def test_readiness_check_statuses_are_only_pass_warning_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("SCM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCM_CONTROL_DB", str(tmp_path / "data" / "control.db"))
    assert {i["status"] for i in chk.run()} <= {"PASS", "WARNING", "FAIL"}


def test_secret_scanner_finds_a_planted_credential(tmp_path, monkeypatch):
    planted = tmp_path / "leak.py"
    planted.write_text('API_KEY = "sk-' + "a" * 40 + '"\n')
    monkeypatch.setattr(chk, "ROOT", tmp_path)
    r = chk.Report()
    chk.check_source(r)
    assert [i for i in r.items if i["check"] == "no secrets in source"][0]["status"] == "FAIL"


def test_case_study_is_labelled_synthetic_and_makes_no_customer_claims(tmp_path):
    report = ROOT / "evaluation" / "reports" / "experiment.json"
    if not report.exists():
        pytest.skip("run `make evaluate` first")
    for profile in json.loads(report.read_text(encoding="utf-8"))["by_profile"]:
        text = casestudy.generate(profile, report)
        assert "SYNTHETIC BENCHMARK - not a customer case study" in text
        assert "protocol" in text.lower() and "95%" in text
        for forbidden in ("our customer", "customers saved", "guarantee", "proven savings"):
            assert forbidden not in text.lower()


def test_case_study_numbers_come_from_the_report(tmp_path):
    report = ROOT / "evaluation" / "reports" / "experiment.json"
    if not report.exists():
        pytest.skip("run `make evaluate` first")
    ex = json.loads(report.read_text(encoding="utf-8"))
    text = casestudy.generate("retailer", report)
    assert f"{ex['by_profile']['retailer']['manual']['gross_profit']:,.0f}" in text
    assert ex["protocol_sha256"] in text


@pytest.mark.slow
def test_failure_suite_passes_end_to_end(tmp_path):
    from evaluation import failure_suite
    rep = failure_suite.run_all()
    failed = [r["scenario"] for r in rep["results"] if not r["passed"]]
    assert not failed, failed


@pytest.mark.slow
def test_demo_runs_end_to_end_and_is_labelled(tmp_path):
    from enterprise import demo
    out = demo.Out()
    summary = demo.run(out, tmp_path / "demo", 0.06, 42, True)
    text = "\n".join(out.lines)
    assert "DEMO  /  SYNTHETIC DATA" in text and "not enabled" in text.lower()
    assert summary["workflow"]["status"] == "COMPLETED" and summary["workflow"]["erp_pushed"] > 0
