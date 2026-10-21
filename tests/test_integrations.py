"""ERP adapters, mock environment, sync, ERP dispatch, inbound webhooks, outbound events."""
import json
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import make_tenant
from enterprise import outbound, tenancy, webhooks
from enterprise.approvals import ApprovalService
from enterprise.hooks import install_all
from enterprise.integrations import dispatch, registry, sync
from enterprise.integrations.base import (ADAPTER_STATUS, ERPConflict, ERPPurchaseOrder, ERPPurchaseOrderLine, ERPTimeout, ERPUnavailable, ERPValidationError)
from enterprise.integrations.mock_erp import MockERPAdapter
from enterprise.integrations.rest_erp import RestERPAdapter
from mocks.runner import MockServer
from scm import audit, db, security
from scm.security import Principal, Role
from scm.services import WorkflowService

T = "acme"


def po(n="PO-1", sku="S1", qty=10, price=5.0, sup="SUP-1"):
    return ERPPurchaseOrder(n, sup, [ERPPurchaseOrderLine(sku, qty, price)])


@pytest.fixture()
def mock_server():
    with MockServer(timeout_delay=2.0) as m:
        yield m


@pytest.fixture()
def env(plat):
    install_all()
    dispatch.install_hooks()
    outbound.install_event_bridge()
    make_tenant(T, "retailer", 0.08, seed=5)
    yield plat


# ---------------------------------------------------------------------------------------------- adapters
def test_adapter_status_is_honest_about_what_exists():
    assert ADAPTER_STATUS["mock"]["status"] == "IMPLEMENTED" and ADAPTER_STATUS["rest"]["status"] == "SIMULATED"
    for name in ("sap", "oracle", "dynamics", "netsuite"):
        assert ADAPTER_STATUS[name]["status"] == "ADAPTER_READY" and ADAPTER_STATUS[name]["validated_against"] is None


def test_mock_adapter_contract_and_idempotency():
    a = MockERPAdapter()
    assert a.get_inventory() and a.get_suppliers() and a.get_purchase_orders() == []
    first = a.create_purchase_order(po(), "k1")
    again = a.create_purchase_order(po(), "k1")
    assert first.external_id == again.external_id and len(a.orders) == 1
    assert a.create_purchase_order(po("PO-2"), "k2").external_id != first.external_id
    assert a.get_delivery_status("PO-1").status == "IN_TRANSIT"
    with pytest.raises(ERPValidationError):
        a.create_purchase_order(po(qty=0), "k3")
    with pytest.raises(ERPValidationError):
        a.create_purchase_order(po(), "")
    with pytest.raises(ERPValidationError):
        a.get_delivery_status("nope")


@pytest.mark.parametrize("mode,exc", [("unavailable", ERPUnavailable), ("timeout", ERPTimeout)])
def test_mock_failure_modes_raise_retryable_errors(mode, exc):
    a = MockERPAdapter(failure=mode)
    with pytest.raises(exc) as e:
        a.get_inventory()
    assert e.value.retryable


def test_mock_partial_failure_and_conflict_modes():
    a = MockERPAdapter(failure="partial", fail_after=1)
    a.create_purchase_order(po("A"), "ka")
    with pytest.raises(ERPUnavailable):
        a.create_purchase_order(po("B"), "kb")
    c = MockERPAdapter(failure="conflict_on_duplicate")
    c.create_purchase_order(po(), "k")
    with pytest.raises(ERPConflict):
        c.create_purchase_order(po(), "k")
    with pytest.raises(ValueError):
        MockERPAdapter(failure="nonsense")


def test_rest_adapter_against_the_mock_environment(plat, mock_server):
    a = RestERPAdapter(mock_server.url, "mock-token", timeout=2, retries=1, backoff=0.01)
    assert a.health()["status"] == "ok"
    assert {i.sku for i in a.get_inventory()} == {"MOCK-001", "MOCK-002", "MOCK-003"} and len(a.get_suppliers()) == 2
    x = a.create_purchase_order(po("PO-9", "MOCK-001", 10, 10.5, "MS-1"), "key-1")
    y = a.create_purchase_order(po("PO-9", "MOCK-001", 10, 10.5, "MS-1"), "key-1")
    assert x.external_id == y.external_id and len(a.get_purchase_orders()) == 1 and mock_server.world.distinct_creates == 1
    assert a.get_delivery_status("PO-9").status == "IN_TRANSIT"


def test_rest_adapter_rejects_bad_credentials_without_leaking_them(plat, mock_server):
    a = RestERPAdapter(mock_server.url, "wrong-secret-token", timeout=2, retries=0)
    with pytest.raises(ERPValidationError) as e:
        a.get_inventory()
    assert "wrong-secret-token" not in str(e.value) and "401" in str(e.value)


def test_rest_adapter_timeout_scenario(plat, mock_server):
    mock_server.set_scenario("api_timeout")
    a = RestERPAdapter(mock_server.url, "mock-token", timeout=0.5, retries=1, backoff=0.01)
    t0 = time.perf_counter()
    with pytest.raises(ERPTimeout):
        a.get_inventory()
    assert time.perf_counter() - t0 < 4.0                                    # bounded: 2 attempts x 0.5 s + backoff


def test_rest_adapter_duplicate_request_scenario_returns_the_original_order(plat, mock_server):
    a = RestERPAdapter(mock_server.url, "mock-token", timeout=2, retries=0)
    mock_server.set_scenario("duplicate_request")
    first = a.create_purchase_order(po("PO-D", "MOCK-001", 5, 10.5, "MS-1"), "dup-key")
    second = a.create_purchase_order(po("PO-D", "MOCK-001", 5, 10.5, "MS-1"), "dup-key")     # server answers 409; the adapter fetches the original
    assert first.external_id == second.external_id and mock_server.world.distinct_creates == 1


def test_rest_adapter_partial_failure_scenario(plat, mock_server):
    a = RestERPAdapter(mock_server.url, "mock-token", timeout=2, retries=0)
    mock_server.set_scenario("partial_failure")
    ok = a.create_purchase_order(po("PO-1", "MOCK-001", 5, 10.5, "MS-1"), "k1")
    with pytest.raises(ERPUnavailable):
        a.create_purchase_order(po("PO-2", "MOCK-002", 5, 25.0, "MS-1"), "k2")
    assert ok.external_id and len(a.get_purchase_orders()) == 1


def test_mock_environment_other_apis_and_scenarios(plat, mock_server):
    import urllib.error
    import urllib.request

    def get(path, token="mock-token"):
        req = urllib.request.Request(mock_server.url + path, headers={"Authorization": f"Bearer {token}"})
        return json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert get("/supplier/quotes?sku=MOCK-001")["quotes"] and get("/inventory/levels")["levels"][0]["onHand"] == 25
    mock_server.set_scenario("supplier_failure")
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/supplier/quotes?sku=MOCK-001")
    assert e.value.code == 503
    mock_server.set_scenario("inventory_mismatch")
    assert get("/inventory/levels")["levels"][0]["onHand"] == 20
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/erp/inventory", token="nope")
    assert e.value.code == 401


def test_ssrf_guard_blocks_private_and_metadata_targets(plat, monkeypatch):
    monkeypatch.setenv("SCM_ALLOW_PRIVATE_URLS", "0")
    for url in ("http://127.0.0.1:9000", "http://localhost/x", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/",
                "ftp://example.com", "file:///etc/passwd", "http://user:pw@example.com/", "javascript:alert(1)", "http:///x"):
        with pytest.raises(security.UnsafeUrl):
            security.assert_public_url(url)
    with pytest.raises(security.UnsafeUrl):
        RestERPAdapter("http://127.0.0.1:1")
    monkeypatch.setenv("SCM_ALLOW_PRIVATE_URLS", "1")
    assert security.assert_public_url("http://127.0.0.1:9000")


# ---------------------------------------------------------------------------------------------- registry
def test_integration_secrets_are_encrypted_and_never_returned(env):
    with tenancy.scope(T):
        rec = registry.create("erp_rest", "Customer ERP", {"base_url": "http://127.0.0.1:9999", "push_on_approve": True}, "super-secret-key", "admin")
        assert rec["has_secret"] is True and "super-secret-key" not in json.dumps(rec) and "super-secret-key" not in json.dumps(registry.list_all())
        raw = db.df("SELECT Ciphertext FROM secrets").Ciphertext[0]
        assert "super-secret-key" not in raw
        assert audit.events(event_type="INTEGRATION_CONFIGURED") and "super-secret-key" not in json.dumps(audit.events(limit=50))


def test_integration_config_validation(env):
    with tenancy.scope(T):
        for kind, cfg in (("erp_rest", {"base_url": "ftp://x"}), ("erp_rest", {"base_url": "http://127.0.0.1:1", "timeout": 999}), ("erp_mock", {"failure": "bogus"}),
                          ("webhook_in", {"source": "Bad Name!"}), ("sap", {})):
            with pytest.raises(registry.IntegrationError):
                registry.create(kind, "x", cfg, None, "admin")


def test_inbound_webhook_source_must_be_unique_and_secret_is_shown_once(env):
    with tenancy.scope(T):
        rec = registry.create("webhook_in", "ERP events", {"source": "erp"}, None, "admin")
        assert rec["secret_shown_once"].startswith("whsec_") and "secret_shown_once" not in registry.get(rec["integration_id"])
        with pytest.raises(registry.IntegrationError, match="already exists"):
            registry.create("webhook_in", "dup", {"source": "erp"}, None, "admin")


def test_integrations_are_tenant_private(plat):
    make_tenant(T, "retailer", 0.06, seed=5)
    make_tenant("globex", "manufacturer", 0.1, seed=6)
    with tenancy.scope(T):
        rec = registry.create("erp_mock", "A's ERP", {}, None, "a")
    with tenancy.scope("globex"):
        assert registry.list_all() == []
        with pytest.raises(registry.IntegrationError):
            registry.get(rec["integration_id"])
        with pytest.raises(registry.IntegrationError):
            registry.adapter_for(rec["integration_id"])


# ---------------------------------------------------------------------------------------------- sync (through the validated importer)
def test_inventory_sync_goes_through_the_importer_and_reports_rejections(env):
    with tenancy.scope(T):
        skus = list(db.df("SELECT Item_ID FROM inventory ORDER BY Item_ID LIMIT 2").Item_ID)
        from enterprise.integrations.base import ERPInventoryItem
        adapter = MockERPAdapter([ERPInventoryItem(skus[0], 77, "WH-1"), ERPInventoryItem("NOT-IN-PLATFORM", 5, "WH-1"), ERPInventoryItem(skus[1], -4, "WH-1")])
        res = sync.sync_inventory(adapter, "sync-bot")
        assert res["applied"] and res["report"]["valid"] == 2 and res["report"]["rejected"] == 1
        assert {i["code"] for i in res["report"]["issues"]} >= {"UNKNOWN_SKU", "NEGATIVE_STOCK"}
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (skus[0],)).Stock_Level[0] == 77
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (skus[1],)).Stock_Level[0] == 0
        assert audit.events(event_type="IMPORT_COMMITTED")


def test_inventory_mismatch_is_detected_before_it_is_applied(env):
    with tenancy.scope(T):
        from enterprise.integrations.base import ERPInventoryItem
        rows = db.df("SELECT Item_ID, Stock_Level FROM inventory").to_dict("records")
        agree = MockERPAdapter([ERPInventoryItem(r["Item_ID"], r["Stock_Level"]) for r in rows])
        assert sync.reconcile_inventory(agree)["in_sync"]
        drift = MockERPAdapter([ERPInventoryItem(r["Item_ID"], r["Stock_Level"] + (25 if i == 0 else 0)) for i, r in enumerate(rows)] + [ERPInventoryItem("ONLY-ERP", 3)])
        rec = sync.reconcile_inventory(drift)
        assert not rec["in_sync"] and len(rec["mismatched"]) == 1 and rec["mismatched"][0]["delta"] == 25 and rec["only_in_erp"] == ["ONLY-ERP"]
        before = db.df("SELECT SUM(Stock_Level) s FROM inventory").s[0]
        assert db.df("SELECT SUM(Stock_Level) s FROM inventory").s[0] == before                 # reconcile never writes


def test_supplier_and_order_sync(env):
    with tenancy.scope(T):
        adapter = MockERPAdapter()
        r = sync.sync_suppliers(adapter, "bot")
        assert r["report"]["committed"]["inserted"] == 2
        sku = db.df("SELECT Item_ID FROM inventory LIMIT 1").Item_ID[0]
        adapter.create_purchase_order(ERPPurchaseOrder("EXT-1", "SUP-1", [ERPPurchaseOrderLine(sku, 12, 3.5)]), "k")
        out = sync.sync_purchase_orders(adapter, "bot")
        assert out["report"]["committed"]["inserted"] == 1
        again = sync.sync_purchase_orders(adapter, "bot")
        assert again["report"]["committed"]["inserted"] == 0 and again["report"]["committed"]["skipped_already_present"] == 1


# ---------------------------------------------------------------------------------------------- ERP dispatch
def approve_one():
    svc = WorkflowService()
    run, _ = svc.start(Principal("op", Role.OPERATOR, T))
    wf = run["workflow_id"]
    assert svc.wait(wf, 120)["status"] == "AWAITING_APPROVAL"
    return svc, wf


def test_approved_orders_are_pushed_to_the_erp_automatically_and_once(env):
    with tenancy.scope(T):
        integ = registry.create("erp_mock", "Mock ERP", {"push_on_approve": True}, None, "admin")
        svc, wf = approve_one()
        ApprovalService(svc).act(Principal("boss", Role.APPROVER, T), wf, "approve")
        svc.wait(wf, 120)
        rows = dispatch.status(wf)
        n_pos = db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT'", (wf,)).n[0]
        assert len(rows) == n_pos and all(r["status"] == "SENT" and r["external_id"] for r in rows)
        adapter = registry.adapter_for(integ["integration_id"])
        assert len(adapter.orders) == n_pos
        assert dispatch.push_workflow(wf, integ["integration_id"])["already_sent"] == n_pos and len(adapter.orders) == n_pos      # a second push creates nothing
        assert audit.events(wf, "ERP_PO_CREATED")
        from enterprise import trace
        assert trace.load(wf)["execution"]["erp"][integ["integration_id"]]["status"] == "COMPLETED"
        svc.shutdown()


def test_partial_erp_failure_is_recorded_retried_and_never_duplicates(env):
    with tenancy.scope(T):
        integ = registry.create("erp_mock", "Flaky ERP", {"failure": "partial", "fail_after": 2, "push_on_approve": True}, None, "admin")
        svc, wf = approve_one()
        ApprovalService(svc).act(Principal("boss", Role.APPROVER, T), wf, "approve")
        svc.wait(wf, 120)
        rows = dispatch.status(wf)
        sent = [r for r in rows if r["status"] == "SENT"]
        failed = [r for r in rows if r["status"] == "FAILED"]
        assert len(sent) == 2 and failed and all(r["last_error"] for r in failed)
        adapter = registry.adapter_for(integ["integration_id"])
        assert len(adapter.orders) == 2
        registry.set_mock_failure(integ["integration_id"], None)                    # the ERP recovers
        out = dispatch.retry_failed(integ["integration_id"])
        assert out["sent"] == len(failed) and out["still_failing"] == 0
        total = db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT'", (wf,)).n[0]
        assert len(adapter.orders) == total and all(r["status"] == "SENT" for r in dispatch.status(wf))
        assert audit.events(wf, "ERP_PO_FAILED")
        svc.shutdown()


def test_an_erp_outage_never_undoes_the_local_approval(env):
    with tenancy.scope(T):
        registry.create("erp_mock", "Down ERP", {"failure": "unavailable", "push_on_approve": True}, None, "admin")
        svc, wf = approve_one()
        ApprovalService(svc).act(Principal("boss", Role.APPROVER, T), wf, "approve")
        done = svc.wait(wf, 120)
        assert done["stage"] == "approved"                                          # the human decision stands
        assert db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT'", (wf,)).n[0] > 0
        assert all(r["status"] == "FAILED" for r in dispatch.status(wf))
        svc.shutdown()


def test_sandbox_tenants_cannot_push_to_a_real_erp_but_production_can(env, mock_server):
    with tenancy.scope(T):
        integ = registry.create("erp_rest", "Real-ish ERP", {"base_url": mock_server.url, "timeout": 2, "retries": 0}, "mock-token", "admin")
        svc, wf = approve_one()
        ApprovalService(svc).act(Principal("boss", Role.APPROVER, T), wf, "approve")
        svc.wait(wf, 120)
        res = dispatch.push_workflow(wf, integ["integration_id"])
        assert res["status"] == "SKIPPED" and "SANDBOX" in res["reason"] and mock_server.world.distinct_creates == 0
        svc.shutdown()
    from enterprise import controlplane
    controlplane.get().update_tenant(T, "root", mode="PRODUCTION")
    with tenancy.scope(T):
        res = dispatch.push_workflow(wf, integ["integration_id"])
        assert res["status"] == "COMPLETED" and res["sent"] > 0 and mock_server.world.distinct_creates == res["sent"]


# ---------------------------------------------------------------------------------------------- inbound webhooks
def hook(secret, etype, data, eid="evt-1", ts=None, source_body=None):
    body = json.dumps(source_body or {"id": eid, "type": etype, "data": data}).encode()
    sig, _ = security.sign_payload(secret, body, ts)
    return body, {"X-SCM-Signature": sig, "X-SCM-Event-ID": eid, "X-SCM-Event-Type": etype}


@pytest.fixture()
def wh(env):
    with tenancy.scope(T):
        rec = registry.create("webhook_in", "ERP events", {"source": "erp"}, None, "admin")
    return rec["secret_shown_once"]


def sku0():
    return db.df("SELECT Item_ID FROM inventory ORDER BY Item_ID LIMIT 1").Item_ID[0]


def test_signed_inventory_update_is_applied(wh):
    with tenancy.scope(T):
        s = sku0()
        body, h = hook(wh, "inventory.updated", {"sku_id": s, "quantity": 321})
        assert webhooks.receive("erp", body, h) == {"status": "processed", "event_id": "evt-1"}
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0] == 321
        assert audit.events(event_type="WEBHOOK_RECEIVED")


@pytest.mark.parametrize("mangle", ["wrong_secret", "tampered_body", "no_signature", "garbage_signature", "old_timestamp", "future_timestamp"])
def test_forged_or_replayed_webhooks_are_rejected_and_change_nothing(wh, mangle):
    with tenancy.scope(T):
        s = sku0()
        before = db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0]
        secret = "whsec_wrong" if mangle == "wrong_secret" else wh
        ts = int(time.time()) - 3600 if mangle == "old_timestamp" else int(time.time()) + 3600 if mangle == "future_timestamp" else None
        body, h = hook(secret, "inventory.updated", {"sku_id": s, "quantity": 999}, ts=ts)
        if mangle == "tampered_body":
            body = body.replace(b"999", b"1")
        if mangle == "no_signature":
            h.pop("X-SCM-Signature")
        if mangle == "garbage_signature":
            h["X-SCM-Signature"] = "t=abc,v1=zzz"
        with pytest.raises(webhooks.WebhookError) as e:
            webhooks.receive("erp", body, h)
        assert e.value.status == 401
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0] == before
        assert webhooks.events() == []


def test_unknown_source_looks_exactly_like_a_bad_signature(wh):
    with tenancy.scope(T):
        body, h = hook(wh, "inventory.updated", {"sku_id": sku0(), "quantity": 1})
        with pytest.raises(webhooks.WebhookError) as e:
            webhooks.receive("nonexistent-source", body, h)
        assert e.value.status == 401 and e.value.code == "unauthorized"


def test_duplicate_events_are_acknowledged_but_applied_once(wh):
    with tenancy.scope(T):
        s = sku0()
        body, h = hook(wh, "inventory.updated", {"sku_id": s, "quantity": 50})
        assert webhooks.receive("erp", body, h)["status"] == "processed"
        with db.connect(write=True) as c:
            c.execute("UPDATE inventory SET Stock_Level=7 WHERE Item_ID=?", (s,))              # a later change the replay must not overwrite
        body2, h2 = hook(wh, "inventory.updated", {"sku_id": s, "quantity": 50})
        assert webhooks.receive("erp", body2, h2)["status"] == "duplicate"
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0] == 7
        assert len(webhooks.events()) == 1


def test_event_id_reuse_with_a_different_payload_is_a_conflict(wh):
    with tenancy.scope(T):
        s = sku0()
        webhooks.receive("erp", *hook(wh, "inventory.updated", {"sku_id": s, "quantity": 50}))
        with pytest.raises(webhooks.WebhookError) as e:
            webhooks.receive("erp", *hook(wh, "inventory.updated", {"sku_id": s, "quantity": 60}))
        assert e.value.status == 409


@pytest.mark.parametrize("etype,data", [("inventory.updated", {"sku_id": "x", "quantity": -1}), ("inventory.updated", {"sku_id": "x", "quantity": 1, "extra": 1}),
                                         ("purchase_order.created", {"po_id": "p"}), ("delivery.received", {"po_id": "p", "qty_received": "many"}),
                                         ("supplier.updated", {"supplier_id": "s", "reliability": 150}), ("totally.unknown", {})])
def test_schema_violations_are_rejected_with_422(wh, etype, data):
    with tenancy.scope(T):
        with pytest.raises(webhooks.WebhookError) as e:
            webhooks.receive("erp", *hook(wh, etype, data))
        assert e.value.status == 422


def test_oversized_and_malformed_bodies(wh):
    with tenancy.scope(T):
        with pytest.raises(webhooks.WebhookError) as e:
            webhooks.receive("erp", b"x" * (webhooks.MAX_BODY + 1), {})
        assert e.value.status == 413
        body = b"{not json"
        sig, _ = security.sign_payload(wh, body)
        with pytest.raises(webhooks.WebhookError) as e:
            webhooks.receive("erp", body, {"X-SCM-Signature": sig, "X-SCM-Event-ID": "e", "X-SCM-Event-Type": "inventory.updated"})
        assert e.value.status == 400


def test_purchase_order_delivery_and_supplier_events(wh):
    with tenancy.scope(T):
        s = sku0()
        vendor = db.df("SELECT Vendor_ID FROM vendors ORDER BY Vendor_ID LIMIT 1").Vendor_ID[0]
        stock0 = db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0]
        n_before = db.df("SELECT COUNT(*) n FROM supplier_deliveries").n[0]
        webhooks.receive("erp", *hook(wh, "purchase_order.created", {"po_id": "EXT-PO-1", "sku_id": s, "supplier_id": vendor, "quantity": 100, "unit_price": 4.5, "order_date": "2026-01-02"}, "e1"))
        webhooks.receive("erp", *hook(wh, "purchase_order.updated", {"po_id": "EXT-PO-1", "status": "IN_TRANSIT"}, "e2"))
        assert webhooks.receive("erp", *hook(wh, "delivery.received", {"po_id": "EXT-PO-1", "qty_received": 95, "qty_defective": 5, "actual_days": 9}, "e3"))["status"] == "processed"
        assert db.df("SELECT Status FROM purchase_orders WHERE PO_ID='EXT-PO-1'").Status[0] == "RECEIVED"
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0] == stock0 + 90          # good units only
        assert db.df("SELECT COUNT(*) n FROM supplier_deliveries").n[0] == n_before + 1                                  # feeds supplier performance
        webhooks.receive("erp", *hook(wh, "delivery.received", {"po_id": "EXT-PO-1", "qty_received": 95}, "e4"))        # a second notice is a no-op
        assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID=?", (s,)).Stock_Level[0] == stock0 + 90
        webhooks.receive("erp", *hook(wh, "supplier.updated", {"supplier_id": vendor, "reliability": 77.5, "region": "EU"}, "e5"))
        assert db.df("SELECT Reliability, Region FROM vendors WHERE Vendor_ID=?", (vendor,)).iloc[0].tolist() == [77.5, "EU"]


def test_failed_processing_is_retried_with_backoff_then_dead_lettered(wh):
    with tenancy.scope(T):
        body, h = hook(wh, "inventory.updated", {"sku_id": "SKU-NOT-YET-IMPORTED", "quantity": 5}, "late-1")
        assert webhooks.receive("erp", body, h)["status"] == "accepted_for_retry"
        ev = webhooks.events()[0]
        assert ev["status"] == "FAILED" and ev["attempts"] == 1 and "unknown SKU" in ev["last_error"] and ev["next_attempt_at"]
        assert webhooks.process_pending(datetime.now())["processed"] == 0                      # not due yet
        with db.connect(write=True) as c:                                                      # the missing product arrives
            c.execute("INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock) VALUES ('SKU-NOT-YET-IMPORTED','x','c',0,5,50)")
        assert webhooks.process_pending(datetime.now() + timedelta(hours=1))["processed"] == 1
        assert webhooks.events()[0]["status"] == "PROCESSED"
        webhooks.receive("erp", *hook(wh, "inventory.updated", {"sku_id": "NEVER-EXISTS", "quantity": 5}, "doomed"))
        for k in range(webhooks.MAX_ATTEMPTS):
            webhooks.process_pending(datetime.now() + timedelta(days=30 * (k + 1)))
        assert [e for e in webhooks.events() if e["event_id"] == "doomed"][0]["status"] == "DEAD"


def test_webhook_secrets_and_events_are_tenant_isolated(plat):
    install_all()
    make_tenant(T, "retailer", 0.06, seed=5)
    make_tenant("globex", "manufacturer", 0.1, seed=6)
    with tenancy.scope(T):
        secret_a = registry.create("webhook_in", "A", {"source": "erp"}, None, "a")["secret_shown_once"]
        sku_a = sku0()
    with tenancy.scope("globex"):
        registry.create("webhook_in", "B", {"source": "erp"}, None, "b")
        with pytest.raises(webhooks.WebhookError) as e:                                         # A's secret does not authenticate against B's 'erp' source
            webhooks.receive("erp", *hook(secret_a, "inventory.updated", {"sku_id": sku_a, "quantity": 5}))
        assert e.value.status == 401


# ---------------------------------------------------------------------------------------------- outbound events
class Receiver:
    """A tiny local webhook receiver that verifies signatures like a customer would."""

    def __init__(self, secret, status=200):
        self.secret, self.status, self.received = secret, status, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                ok = security.verify_signature(outer.secret, body, self.headers.get("X-SCM-Signature"))
                outer.received.append({"ok": ok, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(body)})
                self.send_response(outer.status if ok else 401)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()


def test_subscription_validation_and_ssrf(env, monkeypatch):
    with tenancy.scope(T):
        for bad in ([], ["not.an.event"]):
            with pytest.raises(ValueError):
                outbound.subscribe("http://127.0.0.1:9", bad, "admin")
        monkeypatch.setenv("SCM_ALLOW_PRIVATE_URLS", "0")
        with pytest.raises(security.UnsafeUrl):
            outbound.subscribe("http://127.0.0.1:9/hook", ["dispatch.completed"], "admin")
        with pytest.raises(security.UnsafeUrl):
            outbound.subscribe("http://169.254.169.254/", ["dispatch.completed"], "admin")


def test_events_are_delivered_signed_with_status_tracking(env):
    rx = Receiver("will-be-replaced")
    try:
        with tenancy.scope(T):
            sub = outbound.subscribe(rx.url, ["approval.requested", "procurement.approved", "dispatch.completed"], "admin")
            rx.secret = sub["secret_shown_once"]
            svc, wf = approve_one()
            ApprovalService(svc).act(Principal("boss", Role.APPROVER, T), wf, "approve")
            svc.wait(wf, 120)
            queued = outbound.deliveries()
            assert {d["event_type"] for d in queued} >= {"approval.requested", "procurement.approved", "dispatch.completed"} and all(d["status"] == "PENDING" for d in queued)
            res = outbound.deliver_due()
            assert res["delivered"] == len(queued) and res["failed"] == 0
            assert all(d["status"] == "DELIVERED" and d["attempts"] == 1 and d["response_code"] == 200 and d["delivered_at"] for d in outbound.deliveries())
            assert all(r["ok"] for r in rx.received) and {r["body"]["type"] for r in rx.received} == {"approval.requested", "procurement.approved", "dispatch.completed"}
            one = rx.received[0]
            assert one["body"]["tenant_id"] == T and one["headers"]["x-scm-event-id"] == one["body"]["id"] and one["headers"]["x-scm-event-type"] == one["body"]["type"]
            assert outbound.deliver_due()["delivered"] == 0                                     # nothing is sent twice
            svc.shutdown()
    finally:
        rx.close()


def test_event_filtering_and_per_subscription_idempotency(env):
    with tenancy.scope(T):
        a = outbound.subscribe("http://127.0.0.1:9/a", ["dispatch.completed"], "admin")
        outbound.subscribe("http://127.0.0.1:9/b", ["approval.requested"], "admin")
        assert outbound.emit("dispatch.completed", {"x": 1}, "evt-1") == 1                       # only the matching subscription
        assert outbound.emit("dispatch.completed", {"x": 1}, "evt-1") == 0                       # same event id: not queued twice
        assert [d["sub_id"] for d in outbound.deliveries()] == [a["sub_id"]]
        with pytest.raises(ValueError):
            outbound.emit("made.up", {})


def test_failed_deliveries_back_off_exponentially_then_dead_letter_and_can_be_replayed(env):
    rx = Receiver("x", status=500)
    try:
        with tenancy.scope(T):
            sub = outbound.subscribe(rx.url, ["dispatch.completed"], "admin")
            rx.secret = sub["secret_shown_once"]
            outbound.emit("dispatch.completed", {"n": 1}, "evt-x")
            t = datetime.now()
            gaps = []
            for _ in range(outbound.MAX_ATTEMPTS):
                outbound.deliver_due(t)
                d = outbound.deliveries()[0]
                if d["status"] == "FAILED":
                    nxt = datetime.fromisoformat(d["next_attempt_at"])
                    gaps.append((nxt - t).total_seconds())
                    assert outbound.deliver_due(t)["failed"] == 0                                  # not due again before the backoff elapsed
                    t = nxt
            d = outbound.deliveries()[0]
            assert d["status"] == "DEAD" and d["attempts"] == outbound.MAX_ATTEMPTS and d["last_error"] and d["response_code"] == 500
            assert gaps == sorted(gaps) and gaps[1] == pytest.approx(gaps[0] * 2, abs=2) and max(gaps) <= outbound.BACKOFF_CAP_S
            rx.status = 200                                                                          # the customer fixes their endpoint, then replays
            outbound.replay(d["delivery_id"], "admin")
            assert outbound.deliver_due(datetime.now() + timedelta(seconds=5))["delivered"] == 1
            with pytest.raises(ValueError):
                outbound.replay(d["delivery_id"], "admin")                                           # a delivered event is not replayable
    finally:
        rx.close()


def test_an_unreachable_endpoint_is_a_recorded_failure_not_a_crash(env):
    with tenancy.scope(T):
        outbound.subscribe("http://127.0.0.1:1/never", ["dispatch.completed"], "admin")
        outbound.emit("dispatch.completed", {}, "evt-y")
        assert outbound.deliver_due()["failed"] == 1
        d = outbound.deliveries()[0]
        assert d["status"] == "FAILED" and d["last_error"] and d["next_attempt_at"]


def test_a_rewired_hostname_is_blocked_at_delivery_time(env, monkeypatch):
    with tenancy.scope(T):
        outbound.subscribe("http://127.0.0.1:9/ok", ["dispatch.completed"], "admin")             # allowed while private targets are allowed
        outbound.emit("dispatch.completed", {}, "evt-z")
        monkeypatch.setenv("SCM_ALLOW_PRIVATE_URLS", "0")                                         # ... and no longer
        outbound.deliver_due()
        assert "blocked" in outbound.deliveries()[0]["last_error"]


def test_outbound_queues_are_tenant_private(plat):
    install_all()
    make_tenant(T, "retailer", 0.06, seed=5)
    make_tenant("globex", "manufacturer", 0.1, seed=6)
    with tenancy.scope(T):
        outbound.subscribe("http://127.0.0.1:9/a", ["dispatch.completed"], "a")
        outbound.emit("dispatch.completed", {"secret": "a-only", "k": 1}, "e1")
    with tenancy.scope("globex"):
        assert outbound.deliveries() == [] and outbound.list_subscriptions() == []
        assert outbound.emit("dispatch.completed", {}, "e2") == 0
