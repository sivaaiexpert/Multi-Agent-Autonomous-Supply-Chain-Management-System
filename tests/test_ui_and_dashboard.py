"""Frontend tests (Streamlit AppTest: the real app script executed headlessly) + the dashboard read models + the legacy QA matrix."""
import threading
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from scm import audit, dashboard, db, orders, qa, seed
from scm.security import Role

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PAGES = ["views/home.py", "views/ops.py", "views/provision.py", "views/executive.py", "views/impact.py"]


@pytest.fixture(scope="module")
def app():
    at = AppTest.from_file(APP, default_timeout=180).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_without_exceptions(app, page):
    app.switch_page(page).run()
    assert not app.exception, [e.value for e in app.exception]
    assert not app.error, [e.value for e in app.error]


def test_executive_page_shows_kpis_and_five_tabs(app):
    app.switch_page("views/executive.py").run()
    assert len(app.tabs) == 5 and len(app.dataframe) >= 3


def test_run_pipeline_then_approve_through_the_ui(app):
    app.switch_page("views/ops.py").run()
    toggles = {t.key: t for t in app.toggle}
    if "cfg_llm" in toggles:
        toggles["cfg_llm"].set_value(False)
    app.run()
    next(b for b in app.button if b.key == "runbtn").click().run(timeout=180)
    assert not app.exception, [e.value for e in app.exception]
    keys = [b.key for b in app.button]
    assert {"approve", "deny", "resource"} <= set(keys), keys
    app.text_input(key="sb_reason").set_value("approved from the UI test").run()
    next(b for b in app.button if b.key == "approve").click().run(timeout=180)
    assert not app.exception, [e.value for e in app.exception]
    wf = app.session_state["thread_id"]
    ev = audit.events(wf, "HUMAN_APPROVED")
    assert ev and ev[0]["payload"]["reason"] == "approved from the UI test" and ev[0]["actor"]


def test_state_survives_page_switches(app):
    app.switch_page("views/ops.py").run()
    thread = app.session_state["thread_id"]
    for page in ("views/provision.py", "views/executive.py", "views/home.py", "views/ops.py"):
        app.switch_page(page).run()
        assert not app.exception
    assert app.session_state["thread_id"] == thread


def test_viewer_role_cannot_approve_in_the_console(monkeypatch):
    db.init_db(reset=True)                      # earlier tests restocked everything; start from the seeded low-stock data
    seed.ensure_demo_data()
    monkeypatch.setenv("SCM_UI_ROLE", "VIEWER")
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.switch_page("views/ops.py").run()
    if "cfg_llm" in [t.key for t in at.toggle]:
        at.toggle(key="cfg_llm").set_value(False)
    at.run()
    next(b for b in at.button if b.key == "runbtn").click().run(timeout=180)
    next(b for b in at.button if b.key == "approve").click().run(timeout=180)
    assert any("not allowed" in e.value for e in at.error), [e.value for e in at.error]
    assert not audit.events(at.session_state["thread_id"], "HUMAN_APPROVED")
    assert audit.events(at.session_state["thread_id"], "ACCESS_DENIED")


# ---------------------------------------------------------------- dashboard read models
def test_executive_summary_shape_and_ranges(demo_db):
    s = dashboard.executive_summary()
    assert 0 <= s["inventory_health"] <= 1 and s["skus"] == 14 and sum(s["risk_levels"].values()) == 14
    assert {"orders", "value"} == set(s["open_procurement"]) and s["audit"]["chain"]["ok"] and 0 <= s["supplier_reliability"] <= 100


def test_inventory_risk_uses_forecasts_when_history_exists(demo_db):
    rows = {r["Item_ID"]: r for r in dashboard.inventory_risk()}
    assert all(r["basis"] == "forecast" for r in rows.values())
    assert rows["SKU-1005"]["risk_level"] == "CRITICAL" and rows["SKU-1011"]["risk_level"] in ("LOW", "MEDIUM")


def test_inventory_risk_falls_back_to_static_rules_without_history(fresh_db):
    assert all(r["basis"] == "static_rule" for r in dashboard.inventory_risk())


def test_workflow_timeline_lists_nodes(demo_db):
    from conftest import run_graph
    from scm.graph import build_graph
    cfg, _ = run_graph(build_graph(), "tl-1")
    nodes = [r["Node"] for r in dashboard.workflow_timeline("tl-1")]
    assert "demand" in nodes and "procurement" in nodes and dashboard.workflow_timeline(None) == []


# ---------------------------------------------------------------- concurrency
def test_concurrent_identical_requests_create_exactly_one_order(fresh_db):
    line = [{"Item_ID": "SKU-1001", "Vendor_ID": "V01", "qty": 50, "unit_price": 60.0, "savings": 0.0}]
    results, errors = [], []

    def worker():
        try:
            results.append(orders.create_pending_pos(line, "race-wf")[0])
        except Exception as exc:                     # pragma: no cover - would be a failure below
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len({r["po_id"] for r in results}) == 1 and sum(r["created"] for r in results) == 1
    assert db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID='race-wf'").n[0] == 1


# ---------------------------------------------------------------- the original QA matrix (scenario x LLM fault x node crash)
@pytest.mark.slow
def test_legacy_qa_matrix_passes_completely():
    results = qa.run_suite()
    failed = [(r["DB scenario"], r["LLM fault"], r["Node crash"], r["Detail"]) for r in results if r["Result"] != "PASS"]
    assert len(results) >= 128 and not failed, failed


def test_roles_enum_matches_the_spec():
    assert {r.value for r in Role} == {"SUPER_ADMIN", "TENANT_ADMIN", "ADMIN", "OPERATOR", "APPROVER", "ANALYST", "VIEWER"}


def test_impact_page_labels_provenance_and_survives_backtest_and_whatif_clicks(app):
    app.switch_page("views/impact.py").run()
    assert "SYNTHETIC" in " ".join(m.value for m in app.markdown) or "CUSTOMER DATA" in " ".join(m.value for m in app.markdown)
    [b for b in app.button if b.key == "imp_bt"][0].click().run()
    assert not app.exception, [e.value for e in app.exception]
    [b for b in app.button if b.key == "wi_go"][0].click().run()
    assert not app.exception, [e.value for e in app.exception]
