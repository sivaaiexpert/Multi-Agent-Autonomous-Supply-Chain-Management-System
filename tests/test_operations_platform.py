"""Onboarding, usage / cost / plans, privacy controls, backup / restore / recovery."""
import json
import zipfile
import io
import sqlite3
from datetime import datetime, timedelta

import pytest

from conftest import make_tenant
from enterprise import controlplane, dr, importer as I, onboarding, privacy, tenancy, usage
from enterprise.approvals import ApprovalService
from enterprise.hooks import install_all
from scm import audit, db
from scm.security import Principal, Role
from scm.services import WorkflowService

T = "acme"


def csv(rows, header):
    return (header + "\n" + "\n".join(rows) + "\n").encode()


def import_all_required():
    for ent, content in (("products", csv(["A1,Alpha,Parts,20,100", "A2,Beta,Parts,10,60"], "sku_id,name,category,reorder_point,target_stock")),
                         ("suppliers", csv(["S1,Acme,a@x.example,US,92", "S2,Bolt,b@x.example,EU,85"], "supplier_id,name,email,region,reliability")),
                         ("quotes", csv(["S1,A1,9.5,6,10,500", "S2,A1,10,4,1,", "S1,A2,20,7,1,"], "supplier_id,sku_id,unit_price,lead_time_days,moq,capacity")),
                         ("inventory", csv(["A1,5", "A2,40"], "sku_id,quantity"))):
        pv = I.preview(ent, content, f"{ent}.csv", "admin")
        I.commit(pv["job_id"], "admin")


# ---------------------------------------------------------------------------------------------- onboarding
@pytest.fixture()
def fresh(plat):
    tenancy.provision("newco", "NewCo", "pilot", "SANDBOX")
    with tenancy.scope("newco"):
        yield plat


def step(st, name):
    return next(s for s in st["steps"] if s["step"] == name)


def test_onboarding_starts_with_everything_pending_and_names_the_next_step(fresh):
    st = onboarding.status()
    assert [s["status"] for s in st["steps"]] == ["PENDING"] * 9 and st["next_step"] == "create_organization" and not st["complete"]
    assert step(st, "import_data")["blocked"] and not step(st, "create_organization")["blocked"]


def test_steps_are_derived_from_real_data_not_from_claims(fresh):
    onboarding.create_organization("NewCo Ltd", "IN", "INR", "admin")
    assert step(onboarding.status(), "create_organization")["status"] == "DONE"
    fresh.create_user("newco", "root", "root@newco.example", "Root", Role.TENANT_ADMIN)
    assert step(onboarding.status(), "create_admin")["status"] == "DONE"
    assert step(onboarding.status(), "import_data")["status"] == "PENDING"
    import_all_required()
    st = onboarding.status()
    assert step(st, "import_data")["status"] == "DONE" and step(st, "validate_data")["status"] == "DONE" and step(st, "configure_suppliers")["status"] == "DONE"
    assert step(st, "configure_policies")["status"] == "PENDING"                         # defaults must be confirmed deliberately
    onboarding.confirm_policies("root")
    assert step(onboarding.status(), "configure_policies")["status"] == "DONE"


def test_data_quality_gate_blocks_bad_data_and_warns_about_gaps(fresh):
    assert not onboarding.data_quality_gate()["ok"]
    import_all_required()
    g = onboarding.data_quality_gate()
    assert g["ok"] and any("demand history" in w for w in g["warnings"]) and any("delivery history" in w for w in g["warnings"])
    with db.connect(write=True) as c:
        c.execute("UPDATE inventory SET Target_Stock=Reorder_Point WHERE Item_ID='A1'")
    assert any("target stock <= reorder point" in b for b in onboarding.data_quality_gate()["blocking"])


def test_review_requires_a_simulation_and_production_requires_every_step(fresh):
    with pytest.raises(onboarding.OnboardingError, match="simulation"):
        onboarding.acknowledge_review("root")
    with pytest.raises(onboarding.OnboardingError, match="incomplete steps"):
        onboarding.enable_production("root")


def test_full_onboarding_to_production_for_a_real_tenant(fresh):
    onboarding.create_organization("NewCo Ltd", "IN", "INR", "admin")
    fresh.create_user("newco", "root", "root@newco.example", "Root", Role.TENANT_ADMIN)
    import_all_required()
    onboarding.confirm_policies("root")
    res = onboarding.run_simulation("root")
    assert res["base"]["skus"] == 2
    assert step(onboarding.status(), "run_simulation")["status"] == "DONE"
    onboarding.acknowledge_review("root", "reviewed the base case")
    st = onboarding.status()
    assert st["ready_for_production"] and st["next_step"] == "enable_production"
    st = onboarding.enable_production("root")
    assert st["complete"] and st["mode"] == "PRODUCTION" and fresh.get_tenant("newco")["mode"] == "PRODUCTION"
    assert audit.events(event_type="TENANT_MODE_CHANGED") and audit.events(event_type="ONBOARDING_STEP")


def test_demo_tenants_can_never_be_switched_to_production(plat):
    make_tenant("demo-co", "retailer", 0.08, seed=5)
    with tenancy.scope("demo-co"):
        onboarding.create_organization("Demo", None, "USD", "a")
        plat.create_user("demo-co", "root", "root@demo.example", "R", Role.TENANT_ADMIN)
        for e in onboarding.REQUIRED_IMPORTS:                         # satisfy the data-derived steps the cheap way: the data is already there
            with db.connect(write=True) as c:
                c.execute("INSERT INTO import_jobs (Tenant_ID, Job_ID, Entity, Status, Report_JSON, Created_By, Created_At) VALUES ('demo-co',?,?,'COMMITTED','{}','x','t')", (f"j-{e}", e))
        onboarding.confirm_policies("root")
        onboarding.run_simulation("root")
        onboarding.acknowledge_review("root")
        with pytest.raises(onboarding.OnboardingError, match="synthetic"):
            onboarding.enable_production("root")


# ---------------------------------------------------------------------------------------------- usage, cost, plans
def one_run(tid=T):
    install_all()
    svc = WorkflowService()
    run, _ = svc.start(Principal("op", Role.OPERATOR, tid))
    svc.wait(run["workflow_id"], 120)
    return svc, run["workflow_id"]


def test_usage_report_meters_workflows_llm_and_quota_state(plat):
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        svc = WorkflowService(llm_config={"llm_enabled": True, "llm_emails": True, "fault_llm": "custom",
                                          "fault_payload": '{"summary": "Stock is low here.", "intro": "Please process the order.", "closing": "Thanks."}'})
        run, _ = svc.start(Principal("op", Role.OPERATOR, T))
        svc.wait(run["workflow_id"], 120)
        rep = usage.usage_report()
        assert rep["plan"] == "professional" and rep["usage"]["workflows_per_month"] == 1 and rep["usage"]["llm_tokens_per_month"] > 0
        assert rep["quotas"]["workflows_per_month"]["limit"] == 200 and not rep["quotas"]["workflows_per_month"]["exceeded"]
        assert "no prices" in rep["billing"] and rep["costs"]["compute_usd"] >= 0
        costs = usage.llm_costs()
        assert costs["total"]["calls"] > 0 and set(costs["by_agent"]) >= {"Analytics"} and run["workflow_id"] in costs["by_workflow"]
        wc = usage.workflow_cost(run["workflow_id"])
        assert wc["llm"]["calls"] > 0 and wc["compute"]["provenance"] == "ASSUMPTION_BASED" and wc["total_cloud_equivalent_usd"] > 0
        svc.shutdown()


def test_cost_figures_are_tenant_private(plat):
    make_tenant(T, "retailer", 0.08, seed=5)
    make_tenant("globex", "manufacturer", 0.2, seed=6)
    with tenancy.scope(T):
        svc, wf = one_run()
        assert usage.usage_report()["usage"]["workflows_per_month"] == 1
        svc.shutdown()
    with tenancy.scope("globex"):
        r = usage.usage_report()
        assert r["usage"]["workflows_per_month"] == 0 and usage.llm_costs()["total"]["calls"] == 0 and usage.compute_costs()["node_runs"] == 0


def test_compute_cost_is_an_explicit_assumption_and_configurable(plat, monkeypatch):
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        svc, wf = one_run()
        monkeypatch.setenv("SCM_COMPUTE_RATE_PER_CPU_HOUR", "3600")                      # $1 per cpu-second
        c = usage.compute_costs(wf)
        assert c["cost_usd"] == pytest.approx(c["cpu_seconds"], rel=1e-6) and c["assumed_rate_usd_per_cpu_hour"] == 3600
        svc.shutdown()


def test_quota_enforcement_is_off_by_default_and_hard_when_enabled(plat, monkeypatch):
    make_tenant(T, "retailer", 0.08, seed=5)
    controlplane.get().update_tenant(T, "root", plan="pilot")
    with tenancy.scope(T):
        with db.connect(write=True) as c:
            for i in range(25):
                c.execute("INSERT INTO workflow_runs (Workflow_ID, Status, Created_By, Created_At, Updated_At, Tenant_ID) VALUES (?,?,?,?,?,?)",
                          (f"w{i}", "COMPLETED", "x", datetime.now().isoformat(), datetime.now().isoformat(), T))
        usage.check_quota("workflows_per_month")                                          # advisory by default
        assert usage.usage_report()["quotas"]["workflows_per_month"]["exceeded"]
        monkeypatch.setenv("SCM_ENFORCE_QUOTAS", "1")
        with pytest.raises(usage.QuotaExceeded):
            usage.check_quota("workflows_per_month")


def test_enterprise_plan_has_no_caps_and_plans_define_no_prices():
    assert all(getattr(usage.PLANS["enterprise"], f) is None for f in ("workflows_per_month", "api_calls_per_day", "users"))
    assert not any(hasattr(p, "price") for p in usage.PLANS.values())
    assert usage.PLANS["pilot"].workflows_per_month < usage.PLANS["professional"].workflows_per_month


# ---------------------------------------------------------------------------------------------- privacy
def test_every_stored_table_is_classified():
    inv = {r["store"]: r for r in privacy.data_inventory()}
    assert inv["secrets"]["classification"] == "sensitive" and inv["control.api_keys"]["classification"] == "sensitive"
    assert inv["llm_calls"]["purpose"].count("NOT stored") == 1
    assert {r["classification"] for r in inv.values()} <= {"public", "internal", "confidential", "sensitive"}


def test_every_platform_table_is_either_classified_or_deliberately_exempt(plat):
    from scm.migrations import PLATFORM_TABLES
    tenancy.provision("a-co", "A")
    classified = set(privacy.CLASSIFICATION)
    exempt = {"tenant_policy", "tenant_policy_history", "tenant_models", "onboarding", "field_mappings", "id_mappings", "import_jobs", "organizations", "warehouses",
              "inventory_by_warehouse", "erp_dispatches", "experiments", "outbound_subscriptions"}
    assert set(PLATFORM_TABLES) <= classified | exempt


def test_export_contains_the_tenants_data_but_no_secrets_or_key_hashes(plat):
    make_tenant(T, "retailer", 0.08, seed=5)
    plat.create_user(T, "root", "root@acme.example", "Root", Role.TENANT_ADMIN)
    key, _ = plat.issue_key(T, "root")
    with tenancy.scope(T):
        from enterprise.integrations import registry
        registry.create("erp_mock", "m", {}, "hush-hush-secret", "root")
        blob = privacy.export_tenant("root")
        assert audit.events(event_type="DATA_EXPORTED")
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = set(z.namelist())
    assert {"MANIFEST.json", "inventory.json", "audit_events.json", "control_users.json"} <= names and "secrets.json" not in names
    everything = " ".join(z.read(n).decode() for n in names)
    assert "hush-hush-secret" not in everything and key not in everything and "key_hash" not in everything
    assert json.loads(z.read("MANIFEST.json"))["tables"]["inventory"] > 0


def test_retention_purges_only_operational_data(plat):
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        svc, wf = one_run()
        old = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
        with db.connect(write=True) as c:
            c.execute("UPDATE node_runs SET Started_At=?", (old,))
            c.execute("INSERT INTO llm_calls (Tenant_ID, Status, Latency_MS, Ts) VALUES (?, 'ok', 1, ?)", (T, old))
        before = {t: db.df(f"SELECT COUNT(*) n FROM {t}").n[0] for t in ("inventory", "purchase_orders", "audit_events", "decision_traces")}
        out = privacy.purge_expired("root")
        assert out["node_runs"] > 0 and out["llm_calls"] >= 1
        after = {t: db.df(f"SELECT COUNT(*) n FROM {t}").n[0] for t in before}
        assert after["inventory"] == before["inventory"] and after["purchase_orders"] == before["purchase_orders"] and after["decision_traces"] == before["decision_traces"]
        assert after["audit_events"] == before["audit_events"] + 1 and audit.verify_chain()["ok"]            # only the purge itself was added
        svc.shutdown()


def test_tenant_deletion_erases_data_revokes_access_and_keeps_the_lifecycle_record(plat):
    make_tenant(T, "retailer", 0.08, seed=5)
    plat.create_user(T, "root", "root@acme.example", "Root", Role.TENANT_ADMIN)
    key, _ = plat.issue_key(T, "root")
    assert plat.authenticate(key)
    path = tenancy.tenant_dir(T)
    assert path.exists()
    with pytest.raises(ValueError):
        privacy.delete_tenant(T, "root", "wrong-confirmation")
    res = privacy.delete_tenant(T, "root", T)
    assert res["deleted"] and not path.exists() and plat.authenticate(key) is None and plat.get_tenant(T)["status"] == "DELETED"
    assert [e["event"] for e in plat.platform_audit(T)][0] == "TENANT_DELETED"
    with pytest.raises(controlplane.ControlPlaneError):
        with tenancy.scope(T):
            pass


# ---------------------------------------------------------------------------------------------- backup / restore / recovery
def test_backup_and_restore_preserve_data_and_prove_the_audit_chain(plat, tmp_path):
    install_all()
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        svc, wf = one_run()
        ApprovalService(svc).act(Principal("boss", Role.APPROVER, T), wf, "approve", "ok")
        svc.wait(wf, 120)
        svc.shutdown()
        with db.connect() as c:
            want = dr._counts(c)
    b = dr.backup_tenant(T, tmp_path / "backups")
    assert b["audit"]["chain_ok"] and b["audit"]["events"] > 5 and set(b["files"]) >= {"engine.db"} and b["schema_version"] >= 3
    assert dr.verify_backup(b["path"])["ok"]
    with tenancy.scope(T):                                                                   # disaster: the tenant's database is destroyed
        pass
    engine = tenancy.tenant_paths(T)[0]
    for suffix in ("", "-wal", "-shm"):
        engine.with_name(engine.name + suffix).unlink(missing_ok=True)
    assert not engine.exists()
    r = dr.restore_tenant(b["path"], T)                                                      # no overwrite needed: the database is gone
    assert r["row_counts_match"] and r["audit_chain_ok"] and r["restore_seconds"] >= 0
    tenancy.forget_workspaces()
    with tenancy.scope(T):
        with db.connect() as c:
            got = dr._counts(c)
            assert {k: v for k, v in got.items() if k != "audit_events"} == {k: v for k, v in want.items() if k != "audit_events"}
            assert got["audit_events"] == want["audit_events"] + 1                            # + the BACKUP_RESTORED event itself
        assert audit.verify_chain()["ok"] and audit.events(event_type="BACKUP_RESTORED")
        assert audit.events(wf, "HUMAN_APPROVED")                                            # the decision survived the disaster


def test_a_corrupt_backup_is_rejected_and_live_data_is_untouched(plat, tmp_path):
    make_tenant(T, "retailer", 0.08, seed=5)
    b = dr.backup_tenant(T, tmp_path / "backups")
    with open(f"{b['path']}/engine.db", "ab") as f:
        f.write(b"corruption")
    v = dr.verify_backup(b["path"])
    assert not v["ok"] and v["corrupt_or_missing"] == ["engine.db"]
    with tenancy.scope(T):
        before = db.df("SELECT COUNT(*) n FROM inventory").n[0]
    with pytest.raises(dr.DRError, match="corrupt"):
        dr.restore_tenant(b["path"], T, overwrite=True)
    with tenancy.scope(T):
        assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == before


def test_a_tampered_audit_log_in_a_backup_fails_restore_verification(plat, tmp_path):
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        for i in range(4):
            audit.record("E", "wf", "u", {"i": i})
    b = dr.backup_tenant(T, tmp_path / "backups")
    conn = sqlite3.connect(f"{b['path']}/engine.db")                                        # an attacker edits the backup, drops the triggers and fixes the checksum
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit_events SET Payload='{\"i\": 99}' WHERE Event_ID=2")
    conn.commit()
    conn.close()
    manifest_path = __import__("pathlib").Path(b["path"]) / "MANIFEST.json"
    m = json.loads(manifest_path.read_text())
    m["files"]["engine.db"]["sha256"] = dr._sha(__import__("pathlib").Path(b["path"]) / "engine.db")
    manifest_path.write_text(json.dumps(m))
    with pytest.raises(dr.DRError, match="audit chain"):
        dr.restore_tenant(b["path"], T, overwrite=True)


def test_restore_refuses_to_overwrite_without_permission(plat, tmp_path):
    make_tenant(T, "retailer", 0.08, seed=5)
    b = dr.backup_tenant(T, tmp_path / "backups")
    with pytest.raises(dr.DRError, match="overwrite"):
        dr.restore_tenant(b["path"], T)


def test_control_plane_backup(plat, tmp_path):
    plat.create_tenant("x-co", "X")
    p = dr.backup_control(tmp_path)
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM tenants WHERE tenant_id='x-co'").fetchone()[0] == 1
    c.close()


def test_a_worker_crash_is_recovered_from_the_checkpoint_without_duplicate_orders(plat, tmp_path):
    """Simulate a process death in the middle of a run: the node raises SystemExit (not an Exception, so nothing cleans up), then a NEW service resumes it."""
    install_all()
    make_tenant(T, "retailer", 0.08, seed=5)
    ckpt = tmp_path / "ckpt.db"
    import scm.graph as g
    real = g.vendor_comm
    state = {"crash": True}

    def crashing(s, c):
        if state["crash"]:
            raise SystemExit("process killed")
        return real(s, c)
    with tenancy.scope(T):
        graph_node = g.vendor_comm
        g.vendor_comm = crashing
        try:
            svc1 = WorkflowService(ckpt)
            run, _ = svc1.start(Principal("op", Role.OPERATOR, T))
            wf = run["workflow_id"]
            try:
                svc1.futures[wf].result(timeout=120)
            except BaseException:
                pass
            assert svc1.get(wf)["status"] == "RUNNING"                                       # the row still says RUNNING: the process "died"
            svc1.shutdown()
        finally:
            g.vendor_comm = graph_node
        state["crash"] = False
        svc2 = WorkflowService(ckpt)                                                         # "restart"
        out = svc2.recover()
        assert out["resumed"] == [wf]
        done = svc2.wait(wf, 120)
        assert done["status"] == "AWAITING_APPROVAL"
        from scm import orders
        assert orders.duplicate_count(wf) == 0 and db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=?", (wf,)).n[0] == len(done["result"]["proposals"])
        assert svc2.recover() == {"resumed": [], "at_gate": [], "failed": []}                # idempotent
        svc2.shutdown()


def test_runs_without_a_surviving_checkpoint_are_marked_failed(plat, tmp_path):
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        with db.connect(write=True) as c:
            c.execute("INSERT INTO workflow_runs (Workflow_ID, Status, Created_By, Created_At, Updated_At, Tenant_ID) VALUES ('lost-1','RUNNING','x','t','t',?)", (T,))
        svc = WorkflowService(tmp_path / "empty-ckpt.db")
        assert svc.recover()["failed"] == ["lost-1"] and svc.get("lost-1")["status"] == "FAILED"
        svc.shutdown()


def test_event_replay_after_an_outage(plat):
    from enterprise import outbound
    install_all()
    make_tenant(T, "retailer", 0.08, seed=5)
    with tenancy.scope(T):
        outbound.subscribe("http://127.0.0.1:1/down", ["dispatch.completed"], "root")
        outbound.emit("dispatch.completed", {"n": 1}, "e1")
        for k in range(outbound.MAX_ATTEMPTS):
            outbound.deliver_due(datetime.now() + timedelta(days=k + 1))
        d = outbound.deliveries()[0]
        assert d["status"] == "DEAD"
        outbound.replay(d["delivery_id"], "root")
        assert outbound.deliveries()[0]["status"] == "PENDING" and outbound.deliveries()[0]["attempts"] == 0
