"""Immutable, tamper-evident audit log.

Rows are append-only (SQLite triggers abort UPDATE/DELETE) and hash-chained: hash = sha256(prev_hash | canonical event). `verify_chain()`
detects any out-of-band modification. Payloads are redacted before storage, so secrets never reach the log.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from enum import Enum

from . import db, obs

GENESIS = "0" * 64


class EventType(str, Enum):
    WORKFLOW_STARTED = "WORKFLOW_STARTED"
    FORECAST_GENERATED = "FORECAST_GENERATED"
    RISK_DETECTED = "RISK_DETECTED"
    SUPPLIER_SELECTED = "SUPPLIER_SELECTED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    PROCUREMENT_PROPOSED = "PROCUREMENT_PROPOSED"
    HUMAN_APPROVAL_REQUESTED = "HUMAN_APPROVAL_REQUESTED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    HUMAN_RESOURCED = "HUMAN_RESOURCED"
    PROCUREMENT_EDITED = "PROCUREMENT_EDITED"
    DISPATCH_STARTED = "DISPATCH_STARTED"
    DISPATCH_COMPLETED = "DISPATCH_COMPLETED"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    PROMPT_INJECTION_DETECTED = "PROMPT_INJECTION_DETECTED"
    LLM_FALLBACK = "LLM_FALLBACK"
    ACCESS_DENIED = "ACCESS_DENIED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    POLICY_UPDATED = "POLICY_UPDATED"
    MODEL_CONFIG_UPDATED = "MODEL_CONFIG_UPDATED"
    APPROVAL_STEP_APPROVED = "APPROVAL_STEP_APPROVED"
    APPROVAL_ESCALATED = "APPROVAL_ESCALATED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_CHAIN_CREATED = "APPROVAL_CHAIN_CREATED"
    IMPORT_PREVIEWED = "IMPORT_PREVIEWED"
    IMPORT_COMMITTED = "IMPORT_COMMITTED"
    IMPORT_FAILED = "IMPORT_FAILED"
    MAPPING_UPDATED = "MAPPING_UPDATED"
    INTEGRATION_CONFIGURED = "INTEGRATION_CONFIGURED"
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED"
    ERP_PO_CREATED = "ERP_PO_CREATED"
    ERP_PO_FAILED = "ERP_PO_FAILED"
    ONBOARDING_STEP = "ONBOARDING_STEP"
    TENANT_MODE_CHANGED = "TENANT_MODE_CHANGED"
    DATA_EXPORTED = "DATA_EXPORTED"
    DATA_PURGED = "DATA_PURGED"
    BACKUP_CREATED = "BACKUP_CREATED"
    BACKUP_RESTORED = "BACKUP_RESTORED"


def _digest(prev: str, uid: str, ts: str, actor: str, etype: str, wf: str | None, payload: str, tenant: str = "default") -> str:
    fields = [prev, uid, ts, actor, etype, wf, payload]
    if tenant != "default":                     # rows of the legacy single tenant keep their original digest
        fields.append(tenant)
    body = json.dumps(fields, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def record(event_type: EventType | str, workflow_id: str | None = None, actor: str = "system", payload: dict | None = None,
           conn: sqlite3.Connection | None = None) -> str:
    """Append one event. Pass `conn` to make the audit row part of the caller's transaction (it commits or rolls back with the business write)."""
    etype = event_type.value if isinstance(event_type, EventType) else str(event_type)
    ws = workflow_id or obs.context().get("workflow_id")
    body = json.dumps(obs.redact(payload or {}), default=str, sort_keys=True, ensure_ascii=False)
    uid, ts = obs.new_id(), datetime.now().isoformat(timespec="milliseconds")
    tenant = obs.context().get("tenant_id") or "default"

    def _write(c: sqlite3.Connection) -> None:
        row = c.execute("SELECT Hash FROM audit_events ORDER BY Event_ID DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        c.execute("INSERT INTO audit_events (Event_UID, Ts, Actor, Event_Type, Workflow_ID, Payload, Prev_Hash, Hash, Tenant_ID) VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, ts, actor, etype, ws, body, prev, _digest(prev, uid, ts, actor, etype, ws, body, tenant), tenant))

    if conn is not None:
        _write(conn)
    else:
        with db.connect(write=True) as c:
            _write(c)
    return uid


def safe_record(*args, **kwargs) -> str | None:
    """Audit from places where a logging failure must not abort the business flow (the failure itself is logged loudly)."""
    try:
        return record(*args, **kwargs)
    except Exception as exc:
        obs.log_event("audit write failed", level=40, error=str(exc))
        return None


def events(workflow_id: str | None = None, event_type: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    where, params = [], []
    if workflow_id:
        where.append("Workflow_ID=?"); params.append(workflow_id)
    if event_type:
        where.append("Event_Type=?"); params.append(event_type)
    tenant = obs.context().get("tenant_id")
    if tenant:
        where.append("Tenant_ID=?"); params.append(tenant)
    sql = ("SELECT Event_ID, Event_UID, Ts, Actor, Event_Type, Workflow_ID, Payload FROM audit_events" +
           (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY Event_ID DESC LIMIT ? OFFSET ?")
    with db.connect() as c:
        rows = c.execute(sql, (*params, int(limit), int(offset))).fetchall()
    return [{"event_id": r[0], "event_uid": r[1], "timestamp": r[2], "actor": r[3], "event_type": r[4], "workflow_id": r[5],
             "payload": json.loads(r[6])} for r in rows]


def count(workflow_id: str | None = None) -> int:
    tenant = obs.context().get("tenant_id")
    where, params = [], []
    if workflow_id:
        where.append("Workflow_ID=?"); params.append(workflow_id)
    if tenant:
        where.append("Tenant_ID=?"); params.append(tenant)
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM audit_events" + (" WHERE " + " AND ".join(where) if where else ""), params).fetchone()[0]


def verify_chain() -> dict:
    """Recompute every hash. Returns {ok, checked, first_bad_event_id}."""
    prev, checked = GENESIS, 0
    with db.connect() as c:
        for r in c.execute("SELECT Event_ID, Event_UID, Ts, Actor, Event_Type, Workflow_ID, Payload, Prev_Hash, Hash, Tenant_ID FROM audit_events ORDER BY Event_ID"):
            expect = _digest(prev, r[1], r[2], r[3], r[4], r[5], r[6], r[9])
            if r[7] != prev or r[8] != expect:
                return {"ok": False, "checked": checked, "first_bad_event_id": r[0]}
            prev, checked = r[8], checked + 1
    return {"ok": True, "checked": checked, "first_bad_event_id": None}
