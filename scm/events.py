"""Domain events behind a broker-agnostic abstraction.

Publishing is two-step on purpose (transactional-outbox pattern): every event is first persisted to `domain_events` (so nothing is lost if
a consumer is down), then handed to in-process subscribers. A relay for Redis / RabbitMQ / Kafka / SQS only has to implement
`Transport.send(event)` and call `EventBus.relay()`; no domain code changes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Protocol

from . import db, obs


class EventName(str, Enum):
    INVENTORY_LOW = "InventoryLow"
    DEMAND_FORECAST_UPDATED = "DemandForecastUpdated"
    STOCKOUT_RISK_DETECTED = "StockoutRiskDetected"
    PROCUREMENT_CREATED = "ProcurementCreated"
    APPROVAL_REQUESTED = "ApprovalRequested"
    PROCUREMENT_APPROVED = "ProcurementApproved"
    PROCUREMENT_REJECTED = "ProcurementRejected"
    DISPATCH_COMPLETED = "DispatchCompleted"
    PURCHASE_ORDER_CREATED = "PurchaseOrderCreated"


@dataclass(frozen=True)
class Event:
    type: EventName
    payload: dict
    workflow_id: str | None = None
    event_uid: str = field(default_factory=obs.new_id)
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))


Handler = Callable[[Event], None]


class Transport(Protocol):
    def send(self, event: Event) -> None: ...


class EventBus:
    def __init__(self, persist: bool = True):
        self._handlers: dict[EventName, list[Handler]] = {}
        self.persist = persist
        self.delivery_errors: list[str] = []

    def subscribe(self, name: EventName, handler: Handler) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def publish(self, name: EventName, payload: dict | None = None, workflow_id: str | None = None) -> Event:
        ev = Event(name, obs.redact(payload or {}), workflow_id or obs.context().get("workflow_id"))
        if self.persist:
            try:
                with db.connect(write=True) as c:
                    c.execute("INSERT INTO domain_events (Event_UID, Type, Workflow_ID, Payload, Ts, Tenant_ID) VALUES (?,?,?,?,?,?)",
                              (ev.event_uid, ev.type.value, ev.workflow_id, json.dumps(ev.payload, default=str), ev.ts, obs.context().get("tenant_id") or "default"))
            except Exception as exc:  # the outbox write failing must not take the workflow down; it is surfaced
                self.delivery_errors.append(f"persist {ev.type.value}: {exc}")
        for h in self._handlers.get(name, []):
            try:
                h(ev)
            except Exception as exc:   # one bad subscriber never blocks the others or the workflow
                self.delivery_errors.append(f"{h.__name__}: {exc}")
                obs.log_event("event handler failed", level=40, event=ev.type.value, error=str(exc))
        return ev

    def relay(self, transport: Transport, batch: int = 100) -> int:
        """Ship unpublished outbox rows to an external broker; rows are marked published only after `send` succeeds."""
        sent = 0
        with db.connect(write=True) as c:
            rows = c.execute("SELECT Event_ID, Event_UID, Type, Workflow_ID, Payload, Ts FROM domain_events WHERE Published=0 "
                             "ORDER BY Event_ID LIMIT ?", (batch,)).fetchall()
            for r in rows:
                transport.send(Event(EventName(r[2]), json.loads(r[4]), r[3], r[1], r[5]))
                c.execute("UPDATE domain_events SET Published=1 WHERE Event_ID=?", (r[0],))
                sent += 1
        return sent


BUS = EventBus()


def pending_outbox() -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM domain_events WHERE Published=0").fetchone()[0]
