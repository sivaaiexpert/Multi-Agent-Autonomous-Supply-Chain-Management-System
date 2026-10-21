"""Configurable outbound events (webhooks) with signed delivery, bounded retries with exponential backoff, dead-lettering and replay.

    engine event (EventBus)  ->  external event name  ->  one queued delivery per matching subscription  ->  signed POST  ->  DELIVERED | retry | DEAD

Every delivery row records status, attempts, last error, response code and timestamps. Receivers verify `X-SCM-Signature` (HMAC-SHA256 over '<t>.<body>')
and can de-duplicate on `X-SCM-Event-ID`: delivery is AT-LEAST-ONCE by design. URLs are SSRF-checked at subscription time AND again before every attempt.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Callable

from scm import audit, db, obs
from scm.events import BUS, EventName
from scm.security import UnsafeUrl, assert_public_url, sign_payload

from . import secrets_store

EVENT_TYPES = ("stockout.risk.detected", "procurement.recommendation.created", "approval.requested", "procurement.approved", "procurement.rejected",
               "purchase_order.created", "dispatch.completed")
INTERNAL_TO_EXTERNAL = {EventName.STOCKOUT_RISK_DETECTED: "stockout.risk.detected", EventName.PROCUREMENT_CREATED: "procurement.recommendation.created",
                        EventName.APPROVAL_REQUESTED: "approval.requested", EventName.PROCUREMENT_APPROVED: "procurement.approved",
                        EventName.PROCUREMENT_REJECTED: "procurement.rejected", EventName.PURCHASE_ORDER_CREATED: "purchase_order.created",
                        EventName.DISPATCH_COMPLETED: "dispatch.completed"}
MAX_ATTEMPTS = 6
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 3600.0
TIMEOUT_S = 5.0

Sender = Callable[[str, dict, bytes, float], tuple[int, str]]


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _now() -> datetime:
    return datetime.now()


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def backoff_seconds(attempt: int) -> float:
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))


def subscribe(url: str, event_types: list[str], actor: str, secret: str | None = None) -> dict:
    if not event_types or any(e not in EVENT_TYPES for e in event_types):
        raise ValueError(f"event_types must be a non-empty subset of {list(EVENT_TYPES)}")
    assert_public_url(url)
    shown = None
    if not secret:
        secret = shown = "whsec_" + secrets.token_urlsafe(32)
    sid_secret = secrets_store.put_secret(secret)
    sub_id = "sub_" + obs.new_id()[:12]
    with db.connect(write=True) as c:
        c.execute("INSERT INTO outbound_subscriptions (Tenant_ID, Sub_ID, Url, Event_Types_JSON, Secret_ID, Active, Created_At) VALUES (?,?,?,?,?,1,?)",
                  (_tenant(), sub_id, url, json.dumps(sorted(set(event_types))), sid_secret, _iso(_now())))
        audit.record(audit.EventType.INTEGRATION_CONFIGURED, None, actor, {"outbound_subscription": sub_id, "url": url, "events": sorted(set(event_types))}, conn=c)
    rec = {"sub_id": sub_id, "url": url, "event_types": sorted(set(event_types)), "active": True}
    if shown:
        rec["secret_shown_once"] = shown
    return rec


def list_subscriptions() -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT Sub_ID, Url, Event_Types_JSON, Active, Created_At FROM outbound_subscriptions WHERE Tenant_ID=? ORDER BY Created_At", (_tenant(),)).fetchall()
    return [{"sub_id": r[0], "url": r[1], "event_types": json.loads(r[2]), "active": bool(r[3]), "created_at": r[4]} for r in rows]


def unsubscribe(sub_id: str, actor: str) -> None:
    with db.connect(write=True) as c:
        row = c.execute("SELECT Secret_ID FROM outbound_subscriptions WHERE Tenant_ID=? AND Sub_ID=?", (_tenant(), sub_id)).fetchone()
        if row is None:
            raise ValueError("subscription not found")
        c.execute("UPDATE outbound_subscriptions SET Active=0 WHERE Tenant_ID=? AND Sub_ID=?", (_tenant(), sub_id))
        audit.record(audit.EventType.INTEGRATION_CONFIGURED, None, actor, {"outbound_subscription": sub_id, "active": False}, conn=c)


def emit(event_type: str, data: dict, event_uid: str | None = None, workflow_id: str | None = None) -> int:
    """Queue the event for every active matching subscription (idempotent per subscription + event id). Returns how many deliveries were queued."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    uid = event_uid or obs.new_id()
    envelope = {"id": uid, "type": event_type, "tenant_id": _tenant(), "created_at": _iso(_now()), "workflow_id": workflow_id or obs.context().get("workflow_id"),
                "data": obs.redact(data)}
    body = json.dumps(envelope, default=str, sort_keys=True)
    queued = 0
    with db.connect(write=True) as c:
        subs = c.execute("SELECT Sub_ID, Event_Types_JSON FROM outbound_subscriptions WHERE Tenant_ID=? AND Active=1", (_tenant(),)).fetchall()
        for sub_id, types in subs:
            if event_type not in json.loads(types):
                continue
            cur = c.execute("INSERT OR IGNORE INTO outbound_deliveries (Tenant_ID, Sub_ID, Event_Type, Event_UID, Payload_JSON, Status, Attempts, Created_At, Next_Attempt_At) "
                            "VALUES (?,?,?,?,?,'PENDING',0,?,?)", (_tenant(), sub_id, event_type, uid, body, _iso(_now()), _iso(_now())))
            queued += cur.rowcount
    return queued


def _default_sender(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"


def deliver_due(now: datetime | None = None, limit: int = 50, sender: Sender | None = None) -> dict:
    """Attempt every delivery that is due. Failures are retried with exponential backoff up to MAX_ATTEMPTS, then dead-lettered (replayable)."""
    now = now or _now()
    send = sender or _default_sender
    out = {"delivered": 0, "failed": 0, "dead": 0}
    with db.connect() as c:
        due = c.execute("SELECT d.Delivery_ID, d.Sub_ID, d.Event_Type, d.Event_UID, d.Payload_JSON, d.Attempts, s.Url, s.Secret_ID FROM outbound_deliveries d "
                        "JOIN outbound_subscriptions s ON s.Tenant_ID=d.Tenant_ID AND s.Sub_ID=d.Sub_ID WHERE d.Tenant_ID=? AND d.Status IN ('PENDING','FAILED') "
                        "AND d.Next_Attempt_At <= ? AND s.Active=1 ORDER BY d.Delivery_ID LIMIT ?", (_tenant(), _iso(now), limit)).fetchall()
    for did, _sub_id, etype, uid, payload, attempts, url, secret_id in due:
        err, code = "", None
        try:
            assert_public_url(url)                                   # re-resolved on every attempt (DNS rebinding)
            body = payload.encode("utf-8")
            sig, _ = sign_payload(secrets_store.get_secret(secret_id), body)
            headers = {"Content-Type": "application/json", "X-SCM-Signature": sig, "X-SCM-Event-ID": uid, "X-SCM-Event-Type": etype, "X-SCM-Delivery-ID": str(did),
                       "User-Agent": "scm-platform-webhook/1"}
            code, text = send(url, headers, body, TIMEOUT_S)
            if 200 <= code < 300:
                with db.connect(write=True) as c:
                    c.execute("UPDATE outbound_deliveries SET Status='DELIVERED', Attempts=?, Delivered_At=?, Response_Code=?, Last_Error=NULL WHERE Delivery_ID=?",
                              (attempts + 1, _iso(now), code, did))
                out["delivered"] += 1
                continue
            err = text or f"HTTP {code}"
        except UnsafeUrl as exc:
            err = f"blocked: {exc}"
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:160]}"
        n = attempts + 1
        dead = n >= MAX_ATTEMPTS
        with db.connect(write=True) as c:
            c.execute("UPDATE outbound_deliveries SET Status=?, Attempts=?, Last_Error=?, Response_Code=?, Next_Attempt_At=? WHERE Delivery_ID=?",
                      ("DEAD" if dead else "FAILED", n, err[:300], code, None if dead else _iso(now + timedelta(seconds=backoff_seconds(n))), did))
        out["dead" if dead else "failed"] += 1
    return out


def deliveries(status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    sql, params = "SELECT Delivery_ID, Sub_ID, Event_Type, Event_UID, Status, Attempts, Last_Error, Created_At, Next_Attempt_At, Delivered_At, Response_Code FROM outbound_deliveries WHERE Tenant_ID=?", [_tenant()]
    if status:
        sql, params = sql + " AND Status=?", [*params, status]
    with db.connect() as c:
        rows = c.execute(sql + " ORDER BY Delivery_ID DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    keys = ("delivery_id", "sub_id", "event_type", "event_id", "status", "attempts", "last_error", "created_at", "next_attempt_at", "delivered_at", "response_code")
    return [dict(zip(keys, r)) for r in rows]


def replay(delivery_id: int, actor: str) -> None:
    """Event replay: put a FAILED / DEAD delivery back in the queue (attempt counter reset)."""
    with db.connect(write=True) as c:
        cur = c.execute("UPDATE outbound_deliveries SET Status='PENDING', Attempts=0, Next_Attempt_At=?, Last_Error=NULL WHERE Tenant_ID=? AND Delivery_ID=? AND Status IN ('FAILED','DEAD')",
                        (_iso(_now()), _tenant(), delivery_id))
        if cur.rowcount == 0:
            raise ValueError("delivery not found or not replayable")
        audit.record(audit.EventType.INTEGRATION_CONFIGURED, None, actor, {"replayed_delivery": delivery_id}, conn=c)


def install_event_bridge() -> None:
    """Forward the engine's internal events to subscriptions (registered once; handlers run inside the publishing tenant's scope)."""
    for internal, external in INTERNAL_TO_EXTERNAL.items():
        def handler(ev, _ext=external):
            try:
                emit(_ext, ev.payload, ev.event_uid, ev.workflow_id)
            except Exception as exc:
                obs.log_event("outbound bridge failed", level=40, event=_ext, error=str(exc))
        handler.__name__ = f"outbound_bridge_{external.replace('.', '_')}"
        if not any(getattr(h, "__name__", "") == handler.__name__ for h in BUS._handlers.get(internal, [])):
            BUS.subscribe(internal, handler)
