import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from scm import audit, db
from scm.security import Role, generate_key, load_keyring
from scm.services import WorkflowService

KEYS, ENTRIES = {}, []
for role in Role:
    key, entry = generate_key(role, f"{role.value.lower()}-user")
    KEYS[role] = key
    ENTRIES.append(entry)
RING = load_keyring(",".join(ENTRIES))


@pytest.fixture()
def client(fresh_db):
    svc = WorkflowService()
    app = create_app(svc, RING)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.svc = svc
        yield c
    svc.shutdown()


def H(role, **extra):
    return {"X-API-Key": KEYS[role], **extra}


def run_to_gate(client, key=None):
    headers = H(Role.OPERATOR, **({"Idempotency-Key": key} if key else {}))
    r = client.post("/api/v1/workflows", headers=headers)
    assert r.status_code in (200, 202), r.text
    wf = r.json()["workflow_id"]
    client.svc.wait(wf)
    return wf


def test_health_is_public_and_ready_reports_dependencies(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok" and r.json()["checks"]["migrations"] == "ok"


def test_protected_endpoints_require_a_valid_key(client):
    for path in ("/api/v1/inventory", "/api/v1/suppliers", "/api/v1/metrics", "/api/v1/audit", "/api/v1/workflows"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "scm_wrong"}).status_code == 401
    assert client.post("/api/v1/workflows").status_code == 401


def test_bearer_token_is_accepted(client):
    assert client.get("/api/v1/inventory", headers={"Authorization": f"Bearer {KEYS[Role.VIEWER]}"}).status_code == 200


def test_unauthenticated_attempts_are_audited(client):
    client.get("/api/v1/inventory")
    from enterprise import controlplane
    assert any(e["actor"] == "anonymous" and e["event"] == "ACCESS_DENIED" for e in controlplane.get().platform_audit(limit=20))


def test_error_format_is_consistent_and_request_id_is_returned(client):
    r = client.get("/api/v1/inventory", headers={"X-Request-ID": "req-123"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "http_error" and r.headers["X-Request-ID"] == "req-123"
    assert client.get("/nope").json()["error"]["code"] == "http_error"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_viewer_is_read_only(client):
    assert client.get("/api/v1/inventory", headers=H(Role.VIEWER)).status_code == 200
    assert client.post("/api/v1/workflows", headers=H(Role.VIEWER)).status_code == 403
    assert client.post("/api/v1/workflows/x/approve", headers=H(Role.VIEWER), json={}).status_code == 403


def test_operator_can_run_but_not_approve(client):
    wf = run_to_gate(client)
    assert client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.OPERATOR), json={}).status_code == 403


def test_approver_can_approve_but_not_start(client):
    assert client.post("/api/v1/workflows", headers=H(Role.APPROVER)).status_code == 403


def test_full_flow_start_poll_approve(client, fresh_db):
    wf = run_to_gate(client)
    run = client.get(f"/api/v1/workflows/{wf}", headers=H(Role.VIEWER)).json()
    assert run["status"] == "AWAITING_APPROVAL" and run["result"]["awaiting_approval"] and run["result"]["proposals"]
    assert run["trace_id"] and run["created_by"] == "operator-user"
    r = client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={"reason": "ok"})
    assert r.status_code == 202
    done = client.svc.wait(wf)
    assert done["status"] == "COMPLETED" and done["stage"] == "approved"
    approved = audit.events(wf, "HUMAN_APPROVED")[0]
    assert approved["actor"] == "approver-user" and approved["payload"]["reason"].startswith("ok")


def test_repeated_approval_is_idempotent(client):
    wf = run_to_gate(client)
    client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={})
    client.svc.wait(wf)
    stock = db.df("SELECT SUM(Stock_Level) s FROM inventory").s[0]
    again = client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={})
    assert again.status_code == 202 and again.json()["status"] == "COMPLETED"
    assert db.df("SELECT SUM(Stock_Level) s FROM inventory").s[0] == stock          # stock never received twice


def test_cannot_decide_a_workflow_that_is_not_waiting(client):
    r = client.post("/api/v1/workflows", headers=H(Role.OPERATOR))
    wf = r.json()["workflow_id"]
    client.svc.wait(wf)
    client.post(f"/api/v1/workflows/{wf}/reject", headers=H(Role.APPROVER), json={})
    client.svc.wait(wf)
    assert client.post(f"/api/v1/workflows/{wf}/resource", headers=H(Role.APPROVER), json={}).status_code in (202, 409)


def test_reject_and_resource(client):
    wf = run_to_gate(client)
    client.post(f"/api/v1/workflows/{wf}/resource", headers=H(Role.APPROVER), json={"reason": "different vendor"})
    run = client.svc.wait(wf)
    assert run["status"] == "AWAITING_APPROVAL" and run["result"]["rounds"] == 1
    client.post(f"/api/v1/workflows/{wf}/reject", headers=H(Role.APPROVER), json={})
    assert client.svc.wait(wf)["stage"] == "rejected"


def test_idempotency_key_never_creates_a_second_workflow(client):
    a = client.post("/api/v1/workflows", headers=H(Role.OPERATOR, **{"Idempotency-Key": "req-1"}))
    client.svc.wait(a.json()["workflow_id"])
    b = client.post("/api/v1/workflows", headers=H(Role.OPERATOR, **{"Idempotency-Key": "req-1"}))
    assert a.status_code == 202 and b.status_code == 200 and a.json()["workflow_id"] == b.json()["workflow_id"]
    assert db.df("SELECT COUNT(*) n FROM workflow_runs WHERE Idempotency_Key='req-1'").n[0] == 1
    c = client.post("/api/v1/workflows", headers=H(Role.OPERATOR, **{"Idempotency-Key": "req-2"}))
    assert c.json()["workflow_id"] != a.json()["workflow_id"]


def test_malformed_idempotency_key_is_rejected(client):
    r = client.post("/api/v1/workflows", headers=H(Role.OPERATOR, **{"Idempotency-Key": "bad key!; DROP TABLE"}))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_unknown_and_malformed_workflow_ids(client):
    assert client.get("/api/v1/workflows/does-not-exist", headers=H(Role.VIEWER)).status_code == 404
    assert client.get("/api/v1/workflows/" + "x" * 300, headers=H(Role.VIEWER)).status_code == 422
    assert client.get("/api/v1/workflows/a%27%20OR%201=1--", headers=H(Role.VIEWER)).status_code in (404, 422)


def test_sql_injection_attempts_in_filters_are_harmless(client):
    r = client.get("/api/v1/audit", headers=H(Role.VIEWER), params={"workflow_id": "x' OR '1'='1"})
    assert r.status_code == 422
    r = client.get("/api/v1/audit", headers=H(Role.VIEWER), params={"event_type": "X'; DROP TABLE audit_events;--"})
    assert r.status_code == 422
    assert db.df("SELECT COUNT(*) n FROM sqlite_master WHERE name='audit_events'").n[0] == 1


def test_pagination_bounds(client):
    h = H(Role.VIEWER)
    assert client.get("/api/v1/inventory?limit=0", headers=h).status_code == 422
    assert client.get("/api/v1/inventory?limit=100000", headers=h).status_code == 422
    assert client.get("/api/v1/inventory?offset=-1", headers=h).status_code == 422
    body = client.get("/api/v1/inventory?limit=5&offset=10", headers=h).json()
    assert body["total"] == 14 and len(body["items"]) == 4 and body["limit"] == 5 and body["offset"] == 10
    assert client.get("/api/v1/inventory?low_only=true", headers=h).json()["total"] == db.kpis()["low"]


def test_suppliers_endpoint_exposes_metrics(client):
    items = client.get("/api/v1/suppliers", headers=H(Role.VIEWER)).json()["items"]
    assert len(items) == 6 and {"reliability_score", "observations", "source"} <= set(items[0]["metrics"])


def test_metrics_endpoint_reports_runs_nodes_and_llm(client):
    run_to_gate(client)
    m = client.get("/api/v1/metrics", headers=H(Role.VIEWER)).json()
    assert m["workflows"]["by_status"]["AWAITING_APPROVAL"] == 1 and m["workflows"]["success_rate"] == 1.0
    assert any(n["node"] == "procurement" and n["p95_ms"] is not None for n in m["nodes"])
    assert {"calls", "tokens_in", "cloud_equivalent_cost_usd", "fallbacks", "process"} <= set(m["llm"])


def test_audit_endpoint_filters_and_verifies_the_chain(client):
    wf = run_to_gate(client)
    body = client.get("/api/v1/audit", headers=H(Role.VIEWER), params={"workflow_id": wf, "verify": "true"}).json()
    assert body["total"] >= 5 and body["chain"]["ok"] and all(e["workflow_id"] == wf for e in body["items"])


def test_unknown_fields_in_request_bodies_are_rejected(client):
    wf = run_to_gate(client)
    r = client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={"reason": "x", "is_admin": True})
    assert r.status_code == 422


def test_edit_quantity_is_validated_by_policy_and_audited(client):
    wf = run_to_gate(client)
    po = client.get(f"/api/v1/workflows/{wf}", headers=H(Role.VIEWER)).json()["result"]["proposals"][0]
    bad = client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={"edits": {po["po_id"]: {"quantity": 9_999_999}}})
    assert bad.status_code == 422 and "policy" in bad.json()["error"]["message"]
    assert client.get(f"/api/v1/workflows/{wf}", headers=H(Role.VIEWER)).json()["status"] == "AWAITING_APPROVAL"       # still waiting
    ok_qty = po["qty"] + 5
    good = client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={"edits": {po["po_id"]: {"quantity": ok_qty}}, "reason": "bump"})
    assert good.status_code == 202
    client.svc.wait(wf)
    assert db.df("SELECT Quantity FROM purchase_orders WHERE PO_ID=?", (po["po_id"],)).Quantity[0] == ok_qty
    ev = [e for e in audit.events(wf, "PROCUREMENT_EDITED") if e["payload"]["field"] == "quantity"]
    assert len(ev) == 1 and ev[0]["payload"]["previous_value"] == po["qty"] and ev[0]["payload"]["new_value"] == ok_qty and ev[0]["actor"] == "approver-user"
    assert any(e["payload"]["field"] == "email_body" for e in audit.events(wf, "PROCUREMENT_EDITED"))      # the regenerated email is audited too


def test_edit_with_invalid_quantity_or_po_is_rejected(client):
    wf = run_to_gate(client)
    po = client.get(f"/api/v1/workflows/{wf}", headers=H(Role.VIEWER)).json()["result"]["proposals"][0]["po_id"]
    for edits in ({po: {"quantity": -5}}, {po: {"quantity": 0}}, {"PO-NOPE": {"quantity": 5}}):
        assert client.post(f"/api/v1/workflows/{wf}/approve", headers=H(Role.APPROVER), json={"edits": edits}).status_code == 422


def test_unhandled_errors_do_not_leak_internals(fresh_db, monkeypatch):
    svc = WorkflowService()
    app = create_app(svc, RING)
    monkeypatch.setattr(svc, "list", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret internal detail /etc/passwd")))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/workflows", headers=H(Role.VIEWER))
    assert r.status_code == 500 and "secret internal detail" not in r.text and r.json()["error"]["code"] == "internal_error"


def test_no_keys_configured_means_everything_protected_is_refused(fresh_db):
    app = create_app(WorkflowService(), {})
    with TestClient(app) as c:
        assert c.get("/api/v1/inventory", headers={"X-API-Key": "anything"}).status_code == 401
        assert c.get("/ready").json()["checks"]["api_keys"].startswith("none")
        assert c.get("/health").status_code == 200


def test_durable_checkpoints_survive_a_service_restart(fresh_db, tmp_path):
    ckpt = tmp_path / "ckpt.db"
    svc1 = WorkflowService(ckpt)
    run, _ = svc1.start(__import__("scm.security", fromlist=["Principal"]).Principal("op", Role.OPERATOR))
    wf = run["workflow_id"]
    svc1.wait(wf)
    svc1.shutdown()
    svc2 = WorkflowService(ckpt)                                  # "restart": new process memory, same checkpoint file
    approver = __import__("scm.security", fromlist=["Principal"]).Principal("boss", Role.APPROVER)
    svc2.decide(approver, wf, "approve", "after restart")
    assert svc2.wait(wf)["stage"] == "approved"
    svc2.shutdown()
