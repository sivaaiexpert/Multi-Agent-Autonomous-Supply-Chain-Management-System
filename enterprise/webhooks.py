"""Inbound webhooks from external systems (ERP, WMS, supplier portals).

Per request, in this order:  authenticate (HMAC-SHA256 signature over '<timestamp>.<body>')  ->  replay protection (timestamp window)  ->  schema validation
->  idempotency (unique event id per tenant + source; a repeat is acknowledged, not re-applied; the same id with a different body is a CONFLICT)
->  durable event log  ->  processing  ->  on failure: stored, retried with exponential backoff, then dead-lettered.
Unknown tenant / source / signature all look identical to the caller (HTTP 401) so the endpoint cannot be used to discover tenants.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scm import audit, db, obs, suppliers
from scm.security import verify_signature

from .integrations import registry

EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,100}$")
MAX_BODY = 256 * 1024
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 60.0


class WebhookError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


_ID = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-/ :#]{0,63}$")


class InventoryUpdated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_id: str = _ID
    quantity: int = Field(ge=0, le=10**9)
    warehouse_id: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


class PurchaseOrderCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    po_id: str = _ID
    sku_id: str = _ID
    supplier_id: str = _ID
    quantity: int = Field(gt=0, le=10**9)
    unit_price: float = Field(gt=0, lt=10**9)
    status: str = Field("IN_TRANSIT", pattern="^(PENDING_APPROVAL|SENT|IN_TRANSIT|RECEIVED|REJECTED|CANCELLED)$")
    order_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PurchaseOrderUpdated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    po_id: str = _ID
    status: str = Field(pattern="^(SENT|IN_TRANSIT|RECEIVED|REJECTED|CANCELLED)$")


class DeliveryReceived(BaseModel):
    model_config = ConfigDict(extra="forbid")
    po_id: str = _ID
    qty_received: int = Field(ge=0, le=10**9)
    qty_defective: int = Field(0, ge=0, le=10**9)
    actual_days: int | None = Field(None, ge=0, le=1000)


class SupplierUpdated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str = _ID
    name: str | None = Field(None, min_length=1, max_length=120)
    email: str | None = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    region: str | None = Field(None, max_length=40)
    reliability: float | None = Field(None, ge=0, le=100)


MODELS: dict[str, type[BaseModel]] = {"inventory.updated": InventoryUpdated, "purchase_order.created": PurchaseOrderCreated, "purchase_order.updated": PurchaseOrderUpdated,
                                      "delivery.received": DeliveryReceived, "supplier.updated": SupplierUpdated}


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _now() -> datetime:
    return datetime.now()


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ handlers (all writes are one transaction each)
def _h_inventory(d: InventoryUpdated) -> None:
    with db.connect(write=True) as c:
        if c.execute("SELECT 1 FROM inventory WHERE Item_ID=?", (d.sku_id,)).fetchone() is None:
            raise LookupError(f"unknown SKU {d.sku_id!r} (import products first)")
        if d.warehouse_id:
            c.execute("INSERT OR IGNORE INTO warehouses (Tenant_ID, Warehouse_ID, Name) VALUES (?,?,?)", (_tenant(), d.warehouse_id, d.warehouse_id))
            c.execute("INSERT INTO inventory_by_warehouse (Tenant_ID, Item_ID, Warehouse_ID, Quantity) VALUES (?,?,?,?) ON CONFLICT(Tenant_ID, Item_ID, Warehouse_ID) "
                      "DO UPDATE SET Quantity=excluded.Quantity", (_tenant(), d.sku_id, d.warehouse_id, d.quantity))
            c.execute("UPDATE inventory SET Stock_Level=(SELECT COALESCE(SUM(Quantity),0) FROM inventory_by_warehouse WHERE Item_ID=?) WHERE Item_ID=?", (d.sku_id, d.sku_id))
        else:
            c.execute("UPDATE inventory SET Stock_Level=? WHERE Item_ID=?", (d.quantity, d.sku_id))


def _h_po_created(d: PurchaseOrderCreated) -> None:
    with db.connect(write=True) as c:
        if c.execute("SELECT 1 FROM inventory WHERE Item_ID=?", (d.sku_id,)).fetchone() is None:
            raise LookupError(f"unknown SKU {d.sku_id!r}")
        if c.execute("SELECT 1 FROM vendors WHERE Vendor_ID=?", (d.supplier_id,)).fetchone() is None:
            raise LookupError(f"unknown supplier {d.supplier_id!r}")
        c.execute("INSERT OR IGNORE INTO purchase_orders (PO_ID, Item_ID, Vendor_ID, Quantity, Status, Timestamp, Unit_Price, Savings, Tenant_ID) VALUES (?,?,?,?,?,?,?,0,?)",
                  (d.po_id, d.sku_id, d.supplier_id, d.quantity, d.status, d.order_date + " 00:00:00", d.unit_price, _tenant()))


def _h_po_updated(d: PurchaseOrderUpdated) -> None:
    with db.connect(write=True) as c:
        cur = c.execute("UPDATE purchase_orders SET Status=? WHERE PO_ID=?", (d.status, d.po_id))
        if cur.rowcount == 0:
            raise LookupError(f"unknown purchase order {d.po_id!r}")


def _h_delivery(d: DeliveryReceived) -> None:
    with db.connect(write=True) as c:
        po = c.execute("SELECT Item_ID, Vendor_ID, Quantity, Unit_Price, Timestamp, Workflow_ID, Status FROM purchase_orders WHERE PO_ID=?", (d.po_id,)).fetchone()
        if po is None:
            raise LookupError(f"unknown purchase order {d.po_id!r}")
        if po["Status"] == "RECEIVED":
            return                                                      # already applied (a second delivery notice for the same PO is a no-op)
        lead = c.execute("SELECT Lead_Time_Days FROM vendor_listings WHERE Vendor_ID=? AND Item_ID=?", (po["Vendor_ID"], po["Item_ID"])).fetchone()
        actual = d.actual_days if d.actual_days is not None else (lead[0] if lead else 0)
        c.execute("INSERT INTO supplier_deliveries (Vendor_ID, Item_ID, Ordered_At, Promised_Days, Actual_Days, Qty_Ordered, Qty_Received, Qty_Defective, Unit_Price, Status) "
                  "VALUES (?,?,?,?,?,?,?,?,?,'DELIVERED')", (po["Vendor_ID"], po["Item_ID"], po["Timestamp"][:10], lead[0] if lead else actual, actual, po["Quantity"],
                                                             d.qty_received, min(d.qty_defective, d.qty_received), po["Unit_Price"]))
        c.execute("UPDATE purchase_orders SET Status='RECEIVED' WHERE PO_ID=?", (d.po_id,))
        if po["Workflow_ID"] is None:                                   # externally created PO: stock arrives now. Platform POs were already received at approval (simulated receipt)
            c.execute("UPDATE inventory SET Stock_Level = Stock_Level + ? WHERE Item_ID=?", (d.qty_received - d.qty_defective, po["Item_ID"]))
    suppliers.invalidate_cache()


def _h_supplier(d: SupplierUpdated) -> None:
    with db.connect(write=True) as c:
        row = c.execute("SELECT Vendor_Name, Contact_Email, Reliability, Region FROM vendors WHERE Vendor_ID=?", (d.supplier_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO vendors (Vendor_ID, Vendor_Name, Base_Price, Contact_Email, Reliability, Region) VALUES (?,?,1.0,?,?,?)",
                      (d.supplier_id, d.name or d.supplier_id, d.email or "orders@unknown.example", d.reliability if d.reliability is not None else 90.0, d.region or "GLOBAL"))
        else:
            c.execute("UPDATE vendors SET Vendor_Name=?, Contact_Email=?, Reliability=?, Region=? WHERE Vendor_ID=?",
                      (d.name or row[0], d.email or row[1], d.reliability if d.reliability is not None else row[2], d.region or row[3], d.supplier_id))
    suppliers.invalidate_cache()


HANDLERS: dict[str, Callable] = {"inventory.updated": _h_inventory, "purchase_order.created": _h_po_created, "purchase_order.updated": _h_po_updated,
                                 "delivery.received": _h_delivery, "supplier.updated": _h_supplier}


# ------------------------------------------------------------------------------------------------ receive
def _unauthorised() -> WebhookError:
    return WebhookError(401, "unauthorized", "signature verification failed")


def receive(source: str, body: bytes, headers: Mapping[str, str], now: float | None = None) -> dict:
    """Call inside `tenancy.scope(tenant)`. Returns {status, event_id}; raises WebhookError (the API maps it to the HTTP status)."""
    if len(body) > MAX_BODY:
        raise WebhookError(413, "too_large", f"body larger than {MAX_BODY} bytes")
    h = {k.lower(): v for k, v in headers.items()}
    found = registry.webhook_secret_for_source(source)
    if found is None:
        raise _unauthorised()                                           # unknown source and bad signature are indistinguishable
    secret, _integration = found
    if not verify_signature(secret, body, h.get("x-scm-signature"), 300, now):
        audit.safe_record(audit.EventType.WEBHOOK_REJECTED, None, f"webhook:{source}", {"reason": "bad signature or outside the replay window", "event_id": h.get("x-scm-event-id")})
        raise _unauthorised()
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookError(400, "bad_json", "body is not valid JSON") from exc
    etype = h.get("x-scm-event-type") or payload.get("type")
    eid = h.get("x-scm-event-id") or payload.get("id")
    data = payload.get("data", payload if "data" not in payload else {})
    if etype not in MODELS:
        raise WebhookError(422, "unknown_event", f"event type must be one of {sorted(MODELS)}")
    if not isinstance(eid, str) or not EVENT_ID_RE.match(eid):
        raise WebhookError(422, "bad_event_id", "a unique event id (1-100 safe characters) is required")
    try:
        model = MODELS[etype].model_validate(data)
    except ValidationError as exc:
        audit.safe_record(audit.EventType.WEBHOOK_REJECTED, None, f"webhook:{source}", {"reason": "schema validation failed", "event_id": eid, "event_type": etype})
        raise WebhookError(422, "invalid_payload", "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from exc
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    stored = json.dumps({"data": model.model_dump(), "sha256": digest}, sort_keys=True)
    with db.connect(write=True) as c:
        row = c.execute("SELECT Payload_JSON, Status FROM webhook_events_in WHERE Tenant_ID=? AND Source=? AND Event_ID=?", (_tenant(), source, eid)).fetchone()
        if row is not None:
            if json.loads(row[0]).get("sha256") != digest:
                raise WebhookError(409, "event_id_reuse", "this event id was already used with a different payload")
            return {"status": "duplicate", "event_id": eid}             # idempotent: acknowledged, NOT applied twice
        c.execute("INSERT INTO webhook_events_in (Tenant_ID, Source, Event_ID, Event_Type, Received_At, Status, Attempts, Payload_JSON) VALUES (?,?,?,?,?,'RECEIVED',0,?)",
                  (_tenant(), source, eid, etype, _iso(_now()), stored))
        audit.record(audit.EventType.WEBHOOK_RECEIVED, None, f"webhook:{source}", {"event_id": eid, "event_type": etype}, conn=c)
    return {"status": _process(source, eid, etype, model, 0), "event_id": eid}


def _process(source: str, eid: str, etype: str, model: BaseModel, attempts: int) -> str:
    try:
        HANDLERS[etype](model)
    except Exception as exc:
        n = attempts + 1
        dead = n >= MAX_ATTEMPTS
        with db.connect(write=True) as c:
            c.execute("UPDATE webhook_events_in SET Status=?, Attempts=?, Last_Error=?, Next_Attempt_At=? WHERE Tenant_ID=? AND Source=? AND Event_ID=?",
                      ("DEAD" if dead else "FAILED", n, f"{type(exc).__name__}: {str(exc)[:200]}",
                       None if dead else _iso(_now() + timedelta(seconds=BACKOFF_BASE_S * 2 ** (n - 1))), _tenant(), source, eid))
        return "dead_lettered" if dead else "accepted_for_retry"
    with db.connect(write=True) as c:
        c.execute("UPDATE webhook_events_in SET Status='PROCESSED', Attempts=?, Last_Error=NULL, Next_Attempt_At=NULL, Processed_At=? WHERE Tenant_ID=? AND Source=? AND Event_ID=?",
                  (attempts + 1, _iso(_now()), _tenant(), source, eid))
    return "processed"


def process_pending(now: datetime | None = None, limit: int = 100) -> dict:
    """Retry stored events whose backoff has elapsed (run periodically). Safe to call repeatedly."""
    now = now or _now()
    with db.connect() as c:
        rows = c.execute("SELECT Source, Event_ID, Event_Type, Payload_JSON, Attempts FROM webhook_events_in WHERE Tenant_ID=? AND Status='FAILED' AND Next_Attempt_At<=? "
                         "ORDER BY Received_At LIMIT ?", (_tenant(), _iso(now), limit)).fetchall()
    out = {"processed": 0, "failed": 0, "dead": 0}
    for source, eid, etype, payload, attempts in rows:
        res = _process(source, eid, etype, MODELS[etype].model_validate(json.loads(payload)["data"]), attempts)
        out["processed" if res == "processed" else "dead" if res == "dead_lettered" else "failed"] += 1
    return out


def events(status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    sql, params = "SELECT Source, Event_ID, Event_Type, Received_At, Status, Attempts, Last_Error, Next_Attempt_At, Processed_At FROM webhook_events_in WHERE Tenant_ID=?", [_tenant()]
    if status:
        sql, params = sql + " AND Status=?", [*params, status]
    with db.connect() as c:
        rows = c.execute(sql + " ORDER BY Received_At DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    return [dict(zip(("source", "event_id", "event_type", "received_at", "status", "attempts", "last_error", "next_attempt_at", "processed_at"), r)) for r in rows]
