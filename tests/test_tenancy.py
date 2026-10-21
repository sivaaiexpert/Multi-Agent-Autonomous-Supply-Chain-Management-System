"""Multi-tenancy: isolation, scoping, control plane, policies, secrets."""
import threading

import pytest

from enterprise import policies, secrets_store, tenancy
from enterprise.controlplane import ControlPlaneError, ConflictError, NotFoundError
from enterprise.policies import ApprovalPolicy, ApprovalTier, ModelConfig, TenantPolicy
from pydantic import ValidationError
from scm import audit, db
from scm.optimizer import SupplierOption, SupplierOptimizer
from scm.security import Principal, Role
from scm.settings import get_settings


def two_tenants(cp):
    tenancy.provision("tenant-a", "Tenant A", "professional", "SANDBOX")
    tenancy.provision("tenant-b", "Tenant B", "pilot", "SANDBOX")


def put_item(sku, name="Thing", stock=5, rop=20, target=100):
    with db.connect(write=True) as c:
        c.execute("INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock) VALUES (?,?,?,?,?,?)", (sku, name, "General", stock, rop, target))


# ---------------------------------------------------------------- isolation
def test_each_tenant_has_its_own_empty_database(plat):
    two_tenants(plat)
    pa, pb = tenancy.tenant_paths("tenant-a")[0], tenancy.tenant_paths("tenant-b")[0]
    assert pa != pb and pa.exists() and pb.exists()
    with tenancy.scope("tenant-a"):
        assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 0               # customer tenants are NOT seeded with demo data


def test_tenant_a_data_is_invisible_to_tenant_b(plat):
    two_tenants(plat)
    with tenancy.scope("tenant-a"):
        put_item("SKU-A1")
        audit.record("X", "wf-a", "alice", {"secret": "a-only"})
    with tenancy.scope("tenant-b"):
        assert db.df("SELECT * FROM inventory").empty
        assert audit.events() == [] and audit.count() == 0
        with pytest.raises(Exception):
            db.df("SELECT * FROM inventory WHERE Item_ID='SKU-A1'").iloc[0]
    with tenancy.scope("tenant-a"):
        assert list(db.df("SELECT Item_ID FROM inventory").Item_ID) == ["SKU-A1"]


def test_same_sku_in_two_tenants_does_not_collide(plat):
    two_tenants(plat)
    for t, name in (("tenant-a", "Alpha widget"), ("tenant-b", "Beta widget")):
        with tenancy.scope(t):
            put_item("SKU-1", name)
    with tenancy.scope("tenant-a"):
        assert db.df("SELECT Item_Name FROM inventory").Item_Name[0] == "Alpha widget"
    with tenancy.scope("tenant-b"):
        assert db.df("SELECT Item_Name FROM inventory").Item_Name[0] == "Beta widget"


def test_audit_rows_carry_the_tenant_and_chain_per_tenant(plat):
    two_tenants(plat)
    for t in ("tenant-a", "tenant-b"):
        with tenancy.scope(t):
            for i in range(3):
                audit.record("E", "wf", "u", {"i": i})
            assert audit.verify_chain()["ok"]
            assert set(db.df("SELECT Tenant_ID FROM audit_events").Tenant_ID) == {t}


def test_row_level_tenant_filter_protects_a_shared_database(plat):
    """Defence in depth: even if two tenants shared one file, workflow lookups filter on Tenant_ID."""
    from scm.services import NotFound, WorkflowService
    two_tenants(plat)
    with tenancy.scope("tenant-a"):
        with db.connect(write=True) as c:
            c.execute("INSERT INTO workflow_runs (Workflow_ID, Status, Created_By, Created_At, Updated_At, Tenant_ID) VALUES ('wf-1','QUEUED','x','t','t','tenant-b')")
        svc = WorkflowService()
        with pytest.raises(NotFound):
            svc.get("wf-1")                                  # the row belongs to another tenant id: invisible
        svc.shutdown()


def test_unscoped_access_is_refused(plat):
    with pytest.raises(tenancy.NoTenantScope):
        tenancy.current()


def test_scope_restores_previous_state(plat):
    two_tenants(plat)
    before = db.current_path()
    with tenancy.scope("tenant-a") as ctx:
        assert db.current_path() == ctx.db_path and tenancy.current_id() == "tenant-a"
        with tenancy.scope("tenant-b"):
            assert tenancy.current_id() == "tenant-b"
        assert tenancy.current_id() == "tenant-a"
    assert db.current_path() == before
    with pytest.raises(tenancy.NoTenantScope):
        tenancy.current()


def test_worker_threads_keep_the_tenant_when_propagated(plat):
    two_tenants(plat)
    seen = {}
    with tenancy.scope("tenant-a"):
        put_item("SKU-A1")

        def work():
            seen["rows"] = list(db.df("SELECT Item_ID FROM inventory").Item_ID)
            from scm import obs
            seen["tenant"] = obs.context().get("tenant_id")
        t = threading.Thread(target=db.propagate(work))
        t.start()
        t.join()
    assert seen == {"rows": ["SKU-A1"], "tenant": "tenant-a"}


def test_concurrent_scopes_do_not_leak_between_threads(plat):
    two_tenants(plat)
    for t in ("tenant-a", "tenant-b"):
        with tenancy.scope(t):
            put_item(f"SKU-{t[-1].upper()}")
    results, errors = {}, []

    def run(t):
        try:
            for _ in range(30):
                with tenancy.scope(t):
                    results.setdefault(t, set()).update(db.df("SELECT Item_ID FROM inventory").Item_ID)
        except Exception as exc:   # pragma: no cover
            errors.append(exc)
    ts = [threading.Thread(target=run, args=(t,)) for t in ("tenant-a", "tenant-b") * 3]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert not errors and results == {"tenant-a": {"SKU-A"}, "tenant-b": {"SKU-B"}}


@pytest.mark.parametrize("bad", ["", "A", "UPPER", "a b", "../x", "a/../b", "x" * 41, "-ab", "ab-", "platform", "api", "a.b", "tenant\\x"])
def test_invalid_tenant_ids_are_rejected(plat, bad):
    with pytest.raises(ControlPlaneError):
        plat.create_tenant(bad, "x")
    with pytest.raises(ControlPlaneError):
        tenancy.tenant_dir(bad)


def test_path_traversal_through_tenant_id_is_impossible(plat):
    with pytest.raises(ControlPlaneError):
        tenancy.tenant_paths("../../etc")


def test_unknown_and_suspended_tenants_cannot_be_scoped(plat):
    with pytest.raises(NotFoundError):
        with tenancy.scope("ghost"):
            pass
    tenancy.provision("tenant-a", "A")
    plat.update_tenant("tenant-a", "root", status="SUSPENDED")
    with pytest.raises(ControlPlaneError, match="suspended"):
        with tenancy.scope("tenant-a"):
            pass


def test_duplicate_tenant_is_a_conflict(plat):
    tenancy.provision("tenant-a", "A")
    with pytest.raises(ConflictError):
        plat.create_tenant("tenant-a", "again")


def test_default_tenant_maps_to_the_legacy_database_and_is_seeded(plat):
    with tenancy.scope("default") as ctx:
        assert ctx.db_path == db.DB_PATH
        assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 14


# ---------------------------------------------------------------- control plane: users and keys
def test_keys_are_hashed_authenticate_and_are_bound_to_one_tenant(plat):
    two_tenants(plat)
    plat.create_user("tenant-a", "alice", "alice@a.example", "Alice", Role.APPROVER)
    key, rec = plat.issue_key("tenant-a", "alice")
    assert plat.authenticate(key) == Principal("alice", Role.APPROVER, "tenant-a")
    with plat.connect() as c:
        stored = [r[0] for r in c.execute("SELECT key_hash FROM api_keys")]
    assert key not in stored and all(len(h) == 64 for h in stored)
    assert plat.authenticate(key + "x") is None and plat.authenticate("") is None and plat.authenticate(None) is None


def test_revoked_expired_disabled_and_suspended_credentials_stop_working(plat):
    two_tenants(plat)
    plat.create_user("tenant-a", "alice", "alice@a.example", "Alice", Role.OPERATOR)
    k1, r1 = plat.issue_key("tenant-a", "alice")
    plat.revoke_key("tenant-a", r1["key_id"], "admin")
    assert plat.authenticate(k1) is None
    k2, _ = plat.issue_key("tenant-a", "alice", ttl_days=-1)                       # already expired
    assert plat.authenticate(k2) is None
    k3, _ = plat.issue_key("tenant-a", "alice")
    assert plat.authenticate(k3) is not None
    plat.set_user_status("tenant-a", "alice", "DISABLED", "admin")
    assert plat.authenticate(k3) is None
    plat.set_user_status("tenant-a", "alice", "ACTIVE", "admin")
    k4, _ = plat.issue_key("tenant-a", "alice")
    plat.update_tenant("tenant-a", "root", status="SUSPENDED")
    assert plat.authenticate(k4) is None


def test_privilege_escalation_through_user_management_is_blocked(plat):
    two_tenants(plat)
    plat.create_user("tenant-a", "root", "root@a.example", "Root", Role.TENANT_ADMIN)
    plat.create_user("tenant-a", "op", "op@a.example", "Op", Role.OPERATOR)
    admin, op = Principal("root", Role.TENANT_ADMIN, "tenant-a"), Principal("op", Role.OPERATOR, "tenant-a")
    plat.create_user("tenant-a", "v1", "v1@a.example", "V", Role.VIEWER, admin)                       # allowed
    with pytest.raises(ControlPlaneError):
        plat.create_user("tenant-a", "x1", "x1@a.example", "X", Role.SUPER_ADMIN, admin)             # nobody creates a platform role this way
    with pytest.raises(ControlPlaneError):
        plat.create_user("tenant-a", "x2", "x2@a.example", "X", Role.TENANT_ADMIN, op)               # an operator cannot create an admin
    with pytest.raises(ControlPlaneError):
        plat.create_user("tenant-b", "x3", "x3@b.example", "X", Role.VIEWER, admin)                  # a tenant admin cannot create users in ANOTHER tenant
    with pytest.raises(ControlPlaneError):
        plat.issue_key("tenant-a", "root", issued_by=op)                                             # an operator cannot mint an admin key


def test_platform_audit_is_append_only(plat):
    plat.create_tenant("tenant-a", "A")
    assert plat.platform_audit("tenant-a")[0]["event"] == "TENANT_CREATED"
    with plat.connect(write=True) as c, pytest.raises(Exception, match="append-only"):
        c.execute("DELETE FROM platform_audit")


def test_every_control_plane_table_is_tenant_scoped(plat):
    with plat.connect() as c:
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
            assert "tenant_id" in cols, t


def test_every_platform_table_in_a_tenant_database_carries_tenant_id(plat):
    from scm.migrations import PLATFORM_TABLES, TENANT_STAMPED
    tenancy.provision("tenant-a", "A")
    with tenancy.scope("tenant-a"), db.connect() as c:
        for t in (*PLATFORM_TABLES, *TENANT_STAMPED):
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
            assert "Tenant_ID" in cols, t


# ---------------------------------------------------------------- policy and model configuration
def test_policy_is_versioned_audited_and_applied_only_inside_the_tenant(plat):
    two_tenants(plat)
    base = get_settings().supplier.min_reliability_score
    with tenancy.scope("tenant-a"):
        v = policies.save_policy(TenantPolicy(min_supplier_reliability=85, max_lead_time_days=10, allowed_suppliers=["V01", "V02"]), "root")
        assert v == 1
        assert policies.save_policy(TenantPolicy(min_supplier_reliability=88), "root") == 2
        assert [h["version"] for h in policies.policy_history()] == [2, 1]
        assert audit.events(event_type="POLICY_UPDATED")
    with tenancy.scope("tenant-a"):
        s = get_settings()
        assert s.supplier.min_reliability_score == 88 and s.rules.allowed_vendors == []
    with tenancy.scope("tenant-b"):
        assert get_settings().supplier.min_reliability_score == base          # B is unaffected by A's policy
    assert get_settings().supplier.min_reliability_score == base              # and so is the platform default


@pytest.mark.parametrize("bad", [{"min_supplier_reliability": 5}, {"min_quality_rate": 0.1}, {"max_price_deviation": 0.99}, {"max_supplier_share": 0.05},
                                 {"max_order_quantity": 0}, {"allowed_suppliers": ["a b"]}, {"unknown_field": 1}, {"risk_tolerance": "reckless"},
                                 {"budget_per_category_monthly": {"x": -5}}])
def test_policy_values_outside_platform_bounds_are_rejected(bad):
    with pytest.raises(ValidationError):
        TenantPolicy(**bad)


def test_approval_tiers_must_cover_every_amount():
    with pytest.raises(ValidationError):
        ApprovalPolicy(tiers=[ApprovalTier(max_amount=1000, steps=[1])])                  # no open-ended last tier
    with pytest.raises(ValidationError):
        ApprovalPolicy(tiers=[ApprovalTier(max_amount=5000, steps=[1]), ApprovalTier(max_amount=1000, steps=[2]), ApprovalTier(steps=[3])])
    with pytest.raises(ValidationError):
        ApprovalTier(steps=[4])
    with pytest.raises(ValidationError):
        ApprovalTier(steps=[])
    p = ApprovalPolicy(tiers=[ApprovalTier(max_amount=50_000, steps=[1]), ApprovalTier(max_amount=500_000, steps=[2]), ApprovalTier(steps=[2, 3])])
    assert p.chain_for(10) == [1] and p.chain_for(50_000) == [1] and p.chain_for(50_001) == [2] and p.chain_for(10_000_000) == [2, 3]


def test_a_tenant_cannot_disable_safety_controls_through_model_config():
    for forbidden in ({"policy_enabled": False}, {"require_approval": False}, {"skip_auth": True}, {"disable_validation": True}):
        with pytest.raises(ValidationError):
            ModelConfig(**forbidden)


def test_model_config_respects_the_platform_allow_list(monkeypatch):
    monkeypatch.setenv("SCM_PLATFORM_ALLOWED_MODELS", "llama3.2,mistral")
    ModelConfig(primary_model="mistral")
    with pytest.raises(ValidationError):
        ModelConfig(primary_model="gpt-unlisted")
    with pytest.raises(ValidationError):
        ModelConfig(provider="somecloud")
    with pytest.raises(ValidationError):
        ModelConfig(timeout_seconds=9999)


def test_tenants_can_use_different_models(plat):
    two_tenants(plat)
    with tenancy.scope("tenant-a"):
        policies.save_model_config(ModelConfig(primary_model="llama3.2:1b", retries=0), "root")
    with tenancy.scope("tenant-a"):
        assert get_settings().llm.primary_model == "llama3.2:1b" and get_settings().llm.retries == 0
    with tenancy.scope("tenant-b"):
        assert get_settings().llm.primary_model == "llama3.2"


def test_risk_tolerance_changes_the_service_level(plat):
    tenancy.provision("tenant-a", "A")
    with tenancy.scope("tenant-a"):
        policies.save_policy(TenantPolicy(risk_tolerance="low"), "root")
    with tenancy.scope("tenant-a"):
        assert get_settings().forecast.service_level_z == 2.33 and get_settings().risk.prob_high == 0.50


def S(vid, price, lead=5, rel=95):
    return SupplierOption(vid, f"Vendor {vid}", price, lead, rel, 0.99, 1, None, "GLOBAL")


def test_tenant_rules_constrain_the_optimizer(plat):
    tenancy.provision("tenant-a", "A")
    with tenancy.scope("tenant-a"):
        policies.save_policy(TenantPolicy(allowed_suppliers=["V2"], max_lead_time_days=10), "root")
    with tenancy.scope("tenant-a"):
        plan = SupplierOptimizer().optimize(50, [S("V1", 5.0), S("V2", 9.0), S("V3", 4.0, lead=30)])
        assert plan.allocation == {"V2": 50}
        assert "approved-supplier" in plan.excluded["V1"] and "approved-supplier" in plan.excluded["V3"]
    plan = SupplierOptimizer().optimize(50, [S("V1", 5.0), S("V2", 9.0)])        # outside the scope the platform defaults apply
    assert plan.allocation == {"V1": 50}


# ---------------------------------------------------------------- secrets
def test_secrets_are_encrypted_at_rest_and_tenant_isolated(plat):
    two_tenants(plat)
    with tenancy.scope("tenant-a"):
        sid = secrets_store.put_secret("super-secret-erp-password")
        assert secrets_store.get_secret(sid) == "super-secret-erp-password"
        raw = db.df("SELECT Ciphertext FROM secrets").Ciphertext[0]
        assert "super-secret" not in raw
    with tenancy.scope("tenant-b"):
        with pytest.raises(secrets_store.SecretError):
            secrets_store.get_secret(sid)                                              # another tenant cannot read it, even knowing the id


def test_secret_cannot_be_read_with_a_different_platform_key(plat, monkeypatch):
    tenancy.provision("tenant-a", "A")
    with tenancy.scope("tenant-a"):
        sid = secrets_store.put_secret("value")
        monkeypatch.setenv("SCM_SECRET_KEY", secrets_store.generate_secret_key())
        with pytest.raises(secrets_store.SecretError):
            secrets_store.get_secret(sid)


def test_startup_validation_reports_missing_and_invalid_secrets():
    ok = secrets_store.validate_startup({"SCM_ENV": "production", "SCM_SECRET_KEY": secrets_store.generate_secret_key()})
    assert not [r for r in ok if r["status"] == "FAIL"]
    missing = secrets_store.validate_startup({"SCM_ENV": "production"})
    assert [r for r in missing if r["name"] == "SCM_SECRET_KEY" and r["status"] == "FAIL"]
    bad = secrets_store.validate_startup({"SCM_ENV": "production", "SCM_SECRET_KEY": "not-a-key"})
    assert [r for r in bad if r["name"] == "SCM_SECRET_KEY" and r["status"] == "FAIL"]
    dev = secrets_store.validate_startup({"SCM_ENV": "development"})
    assert not [r for r in dev if r["status"] == "FAIL"]
    wildcard = secrets_store.validate_startup({"SCM_ENV": "production", "SCM_SECRET_KEY": secrets_store.generate_secret_key(), "SCM_CORS_ORIGINS": "*"})
    assert [r for r in wildcard if r["name"] == "SCM_CORS_ORIGINS" and r["status"] == "FAIL"]


def test_production_refuses_to_start_without_the_platform_key(monkeypatch):
    monkeypatch.setenv("SCM_ENV", "production")
    monkeypatch.delenv("SCM_SECRET_KEY", raising=False)
    with pytest.raises(secrets_store.SecretError):
        secrets_store.assert_startup_ok()
    with pytest.raises(secrets_store.SecretError):
        secrets_store.put_secret("x")
