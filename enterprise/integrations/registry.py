"""Per-tenant integration registry. Secrets are encrypted (`enterprise.secrets_store`) and never returned; only a reference id is stored in the row."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

from scm import audit, db, obs
from scm.security import assert_public_url

from .. import secrets_store
from .base import ADAPTER_STATUS, ERPAdapter
from .mock_erp import MockERPAdapter
from .rest_erp import RestERPAdapter

KINDS = ("erp_mock", "erp_rest", "webhook_in")
_MOCKS: dict[tuple[str, str], MockERPAdapter] = {}      # in-process mock ERP state, per (tenant, integration)
_LOCK = threading.Lock()


class IntegrationError(Exception):
    pass


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _validate(kind: str, cfg: dict) -> dict:
    if kind not in KINDS:
        raise IntegrationError(f"kind must be one of {KINDS}")
    out: dict = {"push_on_approve": bool(cfg.get("push_on_approve", False))}
    if kind == "erp_rest":
        url = str(cfg.get("base_url", ""))
        try:
            assert_public_url(url)
        except ValueError as exc:
            raise IntegrationError(f"base_url rejected: {exc}") from exc
        t, r = float(cfg.get("timeout", 5)), int(cfg.get("retries", 2))
        if not (0.5 <= t <= 30) or not (0 <= r <= 3):
            raise IntegrationError("timeout must be 0.5-30 s and retries 0-3")
        out.update(base_url=url.rstrip("/"), timeout=t, retries=r)
    elif kind == "erp_mock":
        if cfg.get("failure") not in (None, "unavailable", "timeout", "partial", "inventory_mismatch", "conflict_on_duplicate"):
            raise IntegrationError("unknown mock failure mode")
        out["failure"] = cfg.get("failure")
        out["fail_after"] = int(cfg.get("fail_after", 1))
    else:                                                    # webhook_in
        src = str(cfg.get("source", ""))
        if not re.match(r"^[a-z0-9][a-z0-9_-]{1,39}$", src):
            raise IntegrationError("source must be 2-40 chars of a-z, 0-9, '_' and '-'")
        out["source"] = src
    return out


def create(kind: str, name: str, config: dict, secret: str | None, actor: str) -> dict:
    """Returns the integration record; for `webhook_in` without a given secret one is generated and returned ONCE in `secret_shown_once`."""
    if not name.strip() or len(name) > 120:
        raise IntegrationError("name must be 1-120 characters")
    cfg = _validate(kind, config)
    shown = None
    if kind == "webhook_in" and not secret:
        import secrets as _s
        secret = shown = "whsec_" + _s.token_urlsafe(32)
    sid = secrets_store.put_secret(secret) if secret else None
    iid = "int_" + obs.new_id()[:12]
    with db.connect(write=True) as c:
        if kind == "webhook_in" and c.execute("SELECT 1 FROM integrations WHERE Tenant_ID=? AND Kind='webhook_in' AND Config_JSON LIKE ?", (_tenant(), f'%"source": "{cfg["source"]}"%')).fetchone():
            raise IntegrationError(f"a webhook source named {cfg['source']!r} already exists")
        c.execute("INSERT INTO integrations (Tenant_ID, Integration_ID, Kind, Name, Config_JSON, Secret_ID, Status, Created_At) VALUES (?,?,?,?,?,?,?,?)",
                  (_tenant(), iid, kind, name.strip(), json.dumps(cfg), sid, "ACTIVE", datetime.now().isoformat(timespec="seconds")))
        audit.record(audit.EventType.INTEGRATION_CONFIGURED, None, actor, {"integration_id": iid, "kind": kind, "name": name, "config": cfg, "has_secret": bool(sid)}, conn=c)
    rec = get(iid)
    if shown:
        rec["secret_shown_once"] = shown
    return rec


def _row(r) -> dict:
    return {"integration_id": r[0], "kind": r[1], "name": r[2], "config": json.loads(r[3]), "has_secret": bool(r[4]), "status": r[5], "created_at": r[6],
            "adapter_status": ADAPTER_STATUS.get({"erp_mock": "mock", "erp_rest": "rest"}.get(r[1], ""), {}).get("status")}


def get(integration_id: str) -> dict:
    with db.connect() as c:
        r = c.execute("SELECT Integration_ID, Kind, Name, Config_JSON, Secret_ID, Status, Created_At FROM integrations WHERE Tenant_ID=? AND Integration_ID=?",
                      (_tenant(), integration_id)).fetchone()
    if r is None:
        raise IntegrationError("integration not found")
    return _row(r)


def list_all() -> list[dict]:
    with db.connect() as c:
        return [_row(r) for r in c.execute("SELECT Integration_ID, Kind, Name, Config_JSON, Secret_ID, Status, Created_At FROM integrations WHERE Tenant_ID=? ORDER BY Created_At",
                                           (_tenant(),))]


def set_status(integration_id: str, status: str, actor: str) -> None:
    if status not in ("ACTIVE", "DISABLED"):
        raise IntegrationError("status must be ACTIVE or DISABLED")
    get(integration_id)
    with db.connect(write=True) as c:
        c.execute("UPDATE integrations SET Status=? WHERE Tenant_ID=? AND Integration_ID=?", (status, _tenant(), integration_id))
        audit.record(audit.EventType.INTEGRATION_CONFIGURED, None, actor, {"integration_id": integration_id, "status": status}, conn=c)


def delete(integration_id: str, actor: str) -> None:
    with db.connect() as c:
        r = c.execute("SELECT Secret_ID FROM integrations WHERE Tenant_ID=? AND Integration_ID=?", (_tenant(), integration_id)).fetchone()
    if r is None:
        raise IntegrationError("integration not found")
    with db.connect(write=True) as c:
        c.execute("DELETE FROM integrations WHERE Tenant_ID=? AND Integration_ID=?", (_tenant(), integration_id))
        audit.record(audit.EventType.INTEGRATION_CONFIGURED, None, actor, {"integration_id": integration_id, "deleted": True}, conn=c)
    if r[0]:
        secrets_store.delete_secret(r[0])
    with _LOCK:
        _MOCKS.pop((_tenant(), integration_id), None)


def _secret_id(integration_id: str) -> str | None:
    with db.connect() as c:
        r = c.execute("SELECT Secret_ID FROM integrations WHERE Tenant_ID=? AND Integration_ID=?", (_tenant(), integration_id)).fetchone()
    return r[0] if r else None


def adapter_for(integration_id: str) -> ERPAdapter:
    rec = get(integration_id)
    if rec["status"] != "ACTIVE":
        raise IntegrationError("integration is disabled")
    cfg = rec["config"]
    if rec["kind"] == "erp_mock":
        key = (_tenant(), integration_id)
        with _LOCK:
            if key not in _MOCKS:
                _MOCKS[key] = MockERPAdapter(failure=cfg.get("failure"), fail_after=cfg.get("fail_after", 1))
            return _MOCKS[key]
    if rec["kind"] == "erp_rest":
        sid = _secret_id(integration_id)
        return RestERPAdapter(cfg["base_url"], secrets_store.get_secret(sid) if sid else "", cfg["timeout"], cfg["retries"])
    raise IntegrationError(f"integration kind {rec['kind']!r} has no ERP adapter")


def webhook_secret_for_source(source: str) -> tuple[str, dict] | None:
    """(secret, integration record) of the ACTIVE inbound-webhook integration with this source name, or None."""
    with db.connect() as c:
        rows = c.execute("SELECT Integration_ID, Kind, Name, Config_JSON, Secret_ID, Status, Created_At FROM integrations WHERE Tenant_ID=? AND Kind='webhook_in' AND Status='ACTIVE'",
                         (_tenant(),)).fetchall()
    for r in rows:
        if json.loads(r[3]).get("source") == source and r[4]:
            return secrets_store.get_secret(r[4]), _row(r)
    return None


def set_mock_failure(integration_id: str, failure: str | None) -> None:
    """Switch an in-process mock ERP into / out of a failure mode (demo and tests)."""
    adapter = adapter_for(integration_id)
    if isinstance(adapter, MockERPAdapter):
        adapter.failure = failure
