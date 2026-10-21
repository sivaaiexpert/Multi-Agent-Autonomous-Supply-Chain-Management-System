"""Test fixtures. Safety first: the whole session points SCM_DB_PATH at a temp directory BEFORE any scm module is imported, so no test can
ever touch the real supply_chain.db."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="scm_tests_"))
os.environ["SCM_DB_PATH"] = str(_SESSION_DIR / "session.db")
os.environ["SCM_OUTBOX"] = str(_SESSION_DIR / "outbox")
os.environ["SCM_OBS__LOG_LEVEL"] = "ERROR"
os.environ["SCM_DATA_DIR"] = str(_SESSION_DIR / "data")
os.environ["SCM_CONTROL_DB"] = str(_SESSION_DIR / "data" / "control.db")
os.environ.setdefault("SCM_SECRET_KEY", "J7tW1mUQeXn1c0wH0o6c1mI9p7vPq0dQ2m3Y1m2n3o4=")
os.environ["SCM_ALLOW_PRIVATE_URLS"] = "1"
os.environ.pop("SCM_API_KEYS", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from scm import db, llm_router, seed, suppliers  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A brand-new seeded SQLite database (legacy demo data only: no demand history, like the QA matrix)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "OUTBOX", tmp_path / "outbox")
    suppliers.invalidate_cache()
    llm_router.reset_breakers()
    llm_router.METRICS.reset()
    db.init_db()
    return tmp_path


@pytest.fixture()
def demo_db(fresh_db):
    """Same, plus synthetic demand history, supplier deliveries and MOQ / capacity terms."""
    seed.ensure_demo_data()
    return fresh_db


def run_graph(graph, thread: str = "t1", llm: bool = False, **cfg_extra):
    """Run a graph to completion or to the human gate. Returns (config, final snapshot)."""
    from scm.graph import initial_state
    cfg = {"configurable": {"thread_id": thread, "llm_enabled": llm, "llm_model": "test", "llm_base_url": "", "llm_timeout": 5,
                            "llm_backoff_base": 0.0, **cfg_extra}}
    for _ in graph.stream(initial_state(), cfg, stream_mode="updates"):
        pass
    return cfg, graph.get_state(cfg)


def resume(graph, cfg, decision: str, **extra):
    graph.update_state(cfg, {"decision": decision, "approver": "tester", **extra}, as_node="supervisor")
    for _ in graph.stream(None, cfg, stream_mode="updates"):
        pass
    return graph.get_state(cfg)


# ---------------------------------------------------------------- multi-tenant platform fixtures
@pytest.fixture()
def plat(tmp_path, monkeypatch):
    """An isolated platform: own data dir, control plane, platform secret key and default-tenant database."""
    from enterprise import controlplane, secrets_store, tenancy
    monkeypatch.setenv("SCM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCM_CONTROL_DB", str(tmp_path / "data" / "control.db"))
    monkeypatch.setenv("SCM_SECRET_KEY", secrets_store.generate_secret_key())
    monkeypatch.setenv("SCM_ENV", "development")
    monkeypatch.setenv("SCM_ALLOW_PRIVATE_URLS", "1")           # tests talk to local mock servers
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "default.db")
    monkeypatch.setattr(db, "OUTBOX", tmp_path / "default_outbox")
    controlplane.reset()
    tenancy.forget_workspaces()
    suppliers.invalidate_cache()
    llm_router.reset_breakers()
    yield controlplane.get()
    controlplane.reset()
    tenancy.forget_workspaces()


def make_tenant(tenant_id: str = "acme", profile: str = "retailer", scale: float = 0.08, seed: int = 1, mode: str = "SANDBOX", policy=None):
    """Provision a tenant and fill it with SYNTHETIC data of the given profile. Returns the Dataset."""
    from enterprise import policies, profiles, tenancy
    tenancy.provision(tenant_id, tenant_id.title(), "professional", mode, is_demo=True)
    ds = profiles.generate(profile, seed, scale)
    with tenancy.scope(tenant_id):
        profiles.load_into_current_tenant(ds)
        if policy is not None:
            policies.save_policy(policy, "setup")
    return ds
