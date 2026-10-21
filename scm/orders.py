"""Purchase-order persistence with idempotency and all-or-nothing transactions.

* `create_pending_pos` creates every PO of a proposal batch in ONE transaction. Each line carries an idempotency key
  (workflow_id | item | vendor); replaying the same step (a retry, a double submit, a crash/restart) returns the existing PO instead of a duplicate.
* `finalize_orders` applies approve/reject (status change + stock receipt + audit row) in one transaction.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from typing import Iterable

from . import audit, db, obs


def idempotency_key(workflow_id: str, item_id: str, vendor_id: str, round_no: int = 0) -> str:
    return hashlib.sha256(f"{workflow_id}|{item_id}|{vendor_id}|{round_no}".encode()).hexdigest()[:40]


def create_pending_pos(lines: Iterable[dict], workflow_id: str, round_no: int = 0, actor: str = "agent:procurement") -> list[dict]:
    """lines: dicts with Item_ID, Vendor_ID, qty, unit_price, savings. Returns [{po_id, created(bool), ...line}] in input order.
    Raises (and rolls back EVERYTHING) if any line is invalid: a partially created batch can never exist."""
    out = []
    with db.connect(write=True) as c:
        for ln in lines:
            if not (isinstance(ln["qty"], int) and ln["qty"] > 0) or not ln["unit_price"] > 0:
                raise ValueError(f"invalid order line for {ln.get('Item_ID')}: qty={ln['qty']!r}, price={ln['unit_price']!r}")
            key = idempotency_key(workflow_id, ln["Item_ID"], ln["Vendor_ID"], round_no)
            row = c.execute("SELECT PO_ID FROM purchase_requests WHERE Idempotency_Key=?", (key,)).fetchone()
            if row:
                out.append({**ln, "po_id": row[0], "created": False})
                continue
            po = db._next_po_id(c)
            tenant = obs.context().get("tenant_id") or "default"
            c.execute("INSERT INTO purchase_orders (PO_ID, Item_ID, Vendor_ID, Quantity, Status, Timestamp, Unit_Price, Savings, Workflow_ID, "
                      "Idempotency_Key, Tenant_ID) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (po, ln["Item_ID"], ln["Vendor_ID"], ln["qty"], db.PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       ln["unit_price"], round(ln.get("savings", 0.0), 2), workflow_id, key, tenant))
            c.execute("INSERT INTO purchase_requests (Idempotency_Key, Workflow_ID, Item_ID, Vendor_ID, PO_ID, Created_At, Tenant_ID) VALUES (?,?,?,?,?,?,?)",
                      (key, workflow_id, ln["Item_ID"], ln["Vendor_ID"], po, datetime.now().isoformat(timespec="seconds"), tenant))
            audit.record(audit.EventType.PROCUREMENT_PROPOSED, workflow_id, actor,
                         {"po_id": po, "item_id": ln["Item_ID"], "vendor_id": ln["Vendor_ID"], "quantity": ln["qty"], "unit_price": ln["unit_price"]},
                         conn=c)
            out.append({**ln, "po_id": po, "created": True})
    return out


def finalize_orders(po_ids: list[str], status: str, receive_stock: bool, workflow_id: str | None, actor: str, event: audit.EventType,
                    payload: dict | None = None) -> None:
    """Status change + (optional) stock receipt + audit event in ONE transaction: all of it commits or none of it does."""
    with db.connect(write=True) as c:
        db.set_po_status(po_ids, status, receive_stock=receive_stock, conn=c)
        audit.record(event, workflow_id, actor, {"po_ids": po_ids, "status": status, **(payload or {})}, conn=c)


def duplicate_count(workflow_id: str) -> int:
    """Number of (item, vendor) pairs with more than one PO in a workflow: must always be 0."""
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM (SELECT Item_ID, Vendor_ID FROM purchase_orders WHERE Workflow_ID=? AND Status NOT IN "
                         "('REJECTED','ROLLED_BACK','SUPERSEDED') GROUP BY Item_ID, Vendor_ID HAVING COUNT(*)>1)", (workflow_id,)).fetchone()[0]


def existing_for(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT PO_ID FROM purchase_requests WHERE Idempotency_Key=?", (key,)).fetchone()
    return row[0] if row else None
