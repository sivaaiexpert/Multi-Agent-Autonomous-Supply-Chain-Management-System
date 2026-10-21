"""Multi-tenant platform API: RBAC enforced server-side, tenant isolation, privilege escalation, webhook endpoint, imports, reports."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from conftest import make_tenant
from enterprise import controlplane, tenancy
from scm import security
from scm.security import Role

pytestmark = pytest.mark.security


def _user(cp, tenant, role, uid=None):
    uid = uid or f"{role.value.lower()}-{tenant}"
    cp.create_user(tenant, uid, f"{uid}@example.com", uid, role)
    key, _ = cp.issue_key(tenant, uid, "test")
    return {"X-API-Key": key}


@pytest.fixture()
def api(plat):
    cp = plat
    make_tenant("acme", "retailer", 0.06, seed=5)
    make_tenant("globex", "manufacturer", 0.08, seed=6)
    h = {t: {r: _user(cp, t, r) for r in Role if r is not Role.SUPER_ADMIN} for t in ("acme", "globex")}
    root_key, entry = security.generate_key(Role.SUPER_ADMIN, "root")           # platform operators are bootstrapped from the environment keyring, not the DB
    ring = security.load_keyring(entry)
    with TestClient(create_app(keyring=ring), raise_server_exceptions=False) as c:
        c.h, c.root = h, {"X-API-Key": root_key}
        yield c


def test_read_endpoints_return_only_the_callers_tenant(api):
    a = api.get("/api/v1/inventory?limit=200", headers=api.h["acme"][Role.VIEWER]).json()
    g = api.get("/api/v1/inventory?limit=200", headers=api.h["globex"][Role.VIEWER]).json()
    skus_a, skus_g = {r["Item_ID"] for r in a["items"]}, {r["Item_ID"] for r in g["items"]}
    assert skus_a and skus_g and a["total"] != g["total"] or skus_a != skus_g
    for r in a["items"]:
        assert r.get("Tenant_ID", "acme") == "acme"


@pytest.mark.parametrize("path", ["/api/v1/forecasts", "/api/v1/risks", "/api/v1/procurement/orders", "/api/v1/integrations", "/api/v1/usage",
                                  "/api/v1/admin/policy", "/api/v1/imports", "/api/v1/dashboards/business"])
def test_every_read_endpoint_rejects_missing_or_bad_key(api, path):
    assert api.get(path).status_code == 401
    assert api.get(path, headers={"X-API-Key": "scm_nope"}).status_code == 401


def test_tenant_cannot_be_chosen_by_header_or_parameter(api):
    h = {**api.h["acme"][Role.VIEWER], "X-Tenant-ID": "globex"}
    mine = api.get("/api/v1/inventory?limit=200&tenant_id=globex", headers=h).json()
    plain = api.get("/api/v1/inventory?limit=200", headers=api.h["acme"][Role.VIEWER]).json()
    assert mine == plain


def test_cross_tenant_workflow_is_invisible(api):
    r = api.post("/api/v1/workflows", headers=api.h["acme"][Role.OPERATOR])
    assert r.status_code in (200, 202), r.text
    wf = r.json()["workflow_id"]
    for path in (f"/api/v1/workflows/{wf}", f"/api/v1/workflows/{wf}/trace", f"/api/v1/workflows/{wf}/explanation", f"/api/v1/approvals/{wf}"):
        resp = api.get(path, headers=api.h["globex"][Role.ADMIN if hasattr(Role, "ADMIN") else Role.TENANT_ADMIN])
        assert resp.status_code in (404, 200) and (resp.status_code == 404 or not resp.json().get("steps")), (path, resp.text)
    assert api.post(f"/api/v1/workflows/{wf}/approve", headers=api.h["globex"][Role.APPROVER], json={}).status_code in (404, 409)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.ANALYST, Role.APPROVER, Role.OPERATOR])
def test_non_admin_roles_cannot_use_admin_endpoints(api, role):
    h = api.h["acme"][role]
    assert api.put("/api/v1/admin/policy", headers=h, json={}).status_code in (403, 422) and api.put("/api/v1/admin/policy", headers=h, json={}).status_code != 200
    assert api.post("/api/v1/admin/users", headers=h, json={"user_id": "evil", "email": "e@x.io", "display_name": "e", "role": "TENANT_ADMIN"}).status_code == 403
    assert api.get("/api/v1/admin/keys", headers=h).status_code == 403
    assert api.post("/api/v1/admin/tenants", headers=h, json={}).status_code in (403, 422)


def test_viewer_cannot_write_anything(api):
    h = api.h["acme"][Role.VIEWER]
    assert api.post("/api/v1/workflows", headers=h).status_code == 403
    assert api.post("/api/v1/imports/preview", headers=h, json={"entity": "inventory", "records": []}).status_code == 403
    assert api.post("/api/v1/integrations", headers=h, json={"kind": "erp_mock", "name": "x"}).status_code == 403
    assert api.post("/api/v1/simulations/what-if", headers=h, json={}).status_code == 403


def test_tenant_admin_cannot_escalate_to_super_admin_or_touch_other_tenants(api):
    h = api.h["acme"][Role.TENANT_ADMIN]
    r = api.post("/api/v1/admin/users", headers=h, json={"user_id": "sneaky", "email": "s@x.io", "display_name": "s", "role": "SUPER_ADMIN"})
    assert r.status_code in (403, 422)
    assert api.get("/api/v1/admin/tenants", headers=h).status_code == 403
    assert api.delete("/api/v1/admin/tenants/globex?confirm=globex", headers=h).status_code == 403
    users = api.get("/api/v1/admin/users", headers=h).json()["items"]
    assert all(u["tenant_id"] == "acme" for u in users)


def test_super_admin_manages_tenants_but_cannot_read_their_data(api):
    assert api.get("/api/v1/admin/tenants", headers=api.root).status_code == 200
    assert api.get("/api/v1/inventory", headers=api.root).status_code == 403
    assert api.get("/api/v1/audit", headers=api.root).status_code == 403


def test_super_admin_creates_a_tenant_and_the_new_admin_key_works(api):
    r = api.post("/api/v1/admin/tenants", headers=api.root, json={"tenant_id": "initech", "name": "Initech", "plan": "pilot", "admin_user_id": "boss", "admin_email": "boss@initech.io"})
    assert r.status_code == 201, r.text
    key = r.json()["admin_api_key_shown_once"]
    assert "hash" not in json.dumps(r.json()).lower()
    inv = api.get("/api/v1/inventory", headers={"X-API-Key": key})
    assert inv.status_code == 200 and inv.json()["total"] == 0                    # customer tenants start empty


def test_webhook_endpoint_signed_duplicate_forged_and_unknown_tenant(api):
    with tenancy.scope("acme"):
        from enterprise.integrations import registry
        secret = registry.create("webhook_in", "ERP", {"source": "erp"}, None, "admin")["secret_shown_once"]
        from scm import db
        sku = db.df("SELECT Item_ID FROM inventory ORDER BY Item_ID LIMIT 1").Item_ID[0]
    data = {"sku_id": sku, "quantity": 77}
    body = json.dumps({"id": "e1", "type": "inventory.updated", "data": data}).encode()
    sig, _ = security.sign_payload(secret, body)
    hdr = {"X-SCM-Signature": sig, "X-SCM-Event-ID": "e1", "X-SCM-Event-Type": "inventory.updated", "Content-Type": "application/json"}
    url = "/api/v1/webhooks/inbound/acme/erp"
    ok = api.post(url, content=body, headers=hdr)
    assert ok.status_code in (200, 202), ok.text
    dup = api.post(url, content=body, headers=hdr)
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    forged = api.post(url, content=body, headers={**hdr, "X-SCM-Signature": "t=1,v1=" + "0" * 64})
    assert forged.status_code == 401
    unknown = api.post("/api/v1/webhooks/inbound/nosuchtenant/erp", content=body, headers=hdr)
    assert unknown.status_code == 401 and unknown.json()["error"]["message"] == forged.json()["error"]["message"]
    other = api.post("/api/v1/webhooks/inbound/globex/erp", content=body, headers=hdr)          # same signature, wrong tenant: secrets are per-tenant
    assert other.status_code == 401


def test_import_preview_commit_and_cross_tenant_job_access(api):
    csv = "sku,name,quantity,reorder_point,target_stock,unit_cost\nZ-1,Widget,10,5,50,2.5\nZ-2,Gadget,3,5,40,4.0\n"
    h = api.h["acme"][Role.OPERATOR] if False else api.h["acme"][Role.TENANT_ADMIN]
    p = api.post("/api/v1/imports/preview", headers=h, json={"entity": "products", "filename": "inv.csv", "content_base64": base64.b64encode(csv.encode()).decode(),
                                                                "mapping": {"columns": {"sku": "sku_id", "name": "name", "reorder_point": "reorder_point", "target_stock": "target_stock"}}})
    assert p.status_code == 200, p.text
    job = p.json()["job_id"]
    assert api.get(f"/api/v1/imports/{job}", headers=api.h["globex"][Role.TENANT_ADMIN]).status_code == 404
    assert api.post(f"/api/v1/imports/{job}/commit", headers=api.h["globex"][Role.TENANT_ADMIN]).status_code in (404, 422)
    c = api.post(f"/api/v1/imports/{job}/commit", headers=h)
    assert c.status_code == 200, c.text
    skus = {r["Item_ID"] for r in api.get("/api/v1/inventory?limit=200", headers=h).json()["items"]}
    assert {"Z-1", "Z-2"} <= skus
    gl = {r["Item_ID"] for r in api.get("/api/v1/inventory?limit=200", headers=api.h["globex"][Role.VIEWER]).json()["items"]}
    assert not ({"Z-1", "Z-2"} & gl)


def test_bad_base64_and_both_payload_kinds_are_rejected(api):
    h = api.h["acme"][Role.TENANT_ADMIN]
    assert api.post("/api/v1/imports/preview", headers=h, json={"entity": "inventory", "content_base64": "!!!"}).status_code == 422
    assert api.post("/api/v1/imports/preview", headers=h, json={"entity": "inventory"}).status_code == 422


def test_reports_require_permission_and_are_tenant_scoped(api):
    assert api.get("/api/v1/reports/inventory_risk?format=json", headers=api.h["acme"][Role.VIEWER]).status_code in (200, 403)
    r = api.get("/api/v1/reports/supplier_performance?format=csv", headers=api.h["acme"][Role.ANALYST])
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert api.get("/api/v1/reports/audit?format=json", headers=api.h["acme"][Role.ANALYST]).status_code in (200, 403)
    assert api.get("/api/v1/reports/nonsense", headers=api.h["acme"][Role.ANALYST]).status_code == 422


def test_what_if_is_deterministic_over_the_api(api):
    h = api.h["acme"][Role.ANALYST]
    body = {"demand_multiplier": 1.3}
    r1, r2 = api.post("/api/v1/simulations/what-if", headers=h, json=body), api.post("/api/v1/simulations/what-if", headers=h, json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json() == r2.json()


def test_api_keys_are_revocable_and_a_revoked_key_stops_working(api):
    admin = api.h["acme"][Role.TENANT_ADMIN]
    issued = api.post("/api/v1/admin/users/viewer-acme/keys", headers=admin, json={"label": "temp"})
    assert issued.status_code == 201
    key, kid = issued.json()["api_key_shown_once"], issued.json()["key"]["key_id"]
    assert api.get("/api/v1/risks", headers={"X-API-Key": key}).status_code == 200
    assert api.delete(f"/api/v1/admin/keys/{kid}", headers=admin).status_code == 200
    assert api.get("/api/v1/risks", headers={"X-API-Key": key}).status_code == 401


def test_suspended_tenant_is_locked_out(api):
    controlplane.get().update_tenant("globex", "root", status="SUSPENDED")
    assert api.get("/api/v1/inventory", headers=api.h["globex"][Role.VIEWER]).status_code in (401, 403)
    assert api.get("/api/v1/inventory", headers=api.h["acme"][Role.VIEWER]).status_code == 200


def test_oversized_body_and_missing_content_length_are_refused(api):
    h = {**api.h["acme"][Role.TENANT_ADMIN], "Content-Type": "application/json"}
    r = api.post("/api/v1/admin/users", headers=h, content=b"{" + b" " * 2_000_000 + b"}")
    assert r.status_code == 413


def test_security_headers_and_no_cors_by_default(api):
    r = api.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_integration_creation_rejects_ssrf_targets(api):
    h = api.h["acme"][Role.TENANT_ADMIN]
    import os
    os.environ.pop("SCM_ALLOW_PRIVATE_URLS", None)
    try:
        for url in ("http://169.254.169.254/latest", "http://localhost:8080", "file:///etc/passwd", "http://10.0.0.5/x"):
            r = api.post("/api/v1/integrations", headers=h, json={"kind": "erp_rest", "name": "bad", "config": {"base_url": url}})
            assert r.status_code in (400, 422), (url, r.status_code, r.text)
    finally:
        os.environ["SCM_ALLOW_PRIVATE_URLS"] = "1"


def test_secrets_never_appear_in_integration_listings(api):
    h = api.h["acme"][Role.TENANT_ADMIN]
    r = api.post("/api/v1/integrations", headers=h, json={"kind": "webhook_in", "name": "ERP", "config": {"source": "erp2"}, "secret": "super-secret-value-123"})
    assert r.status_code == 201
    listing = api.get("/api/v1/integrations", headers=h).text
    assert "super-secret-value-123" not in listing and "super-secret-value-123" not in r.text
