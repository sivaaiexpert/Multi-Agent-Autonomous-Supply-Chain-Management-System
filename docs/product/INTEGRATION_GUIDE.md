# Integration guide

Three integration surfaces, all tenant-scoped and audited.

| Surface | Direction | Auth | Status |
|---|---|---|---|
| REST API (`/api/v1`) | pull / push | API key | VALIDATED |
| Inbound webhooks | system -> platform | HMAC signature | VALIDATED |
| Outbound events | platform -> system | HMAC signature | VALIDATED (local receiver) |
| ERP adapter (`ERPAdapter`) | platform <-> ERP | adapter-specific | mock IMPLEMENTED; generic REST SIMULATED; SAP / Oracle / Dynamics / NetSuite ADAPTER-READY |

> **Honesty note.** No SAP, Oracle, Microsoft Dynamics or NetSuite connector exists or has been tested. `MockERPAdapter` is a deterministic in-repo stand-in with failure injection; `RestERPAdapter` speaks a generic JSON contract and has only been run against the bundled mock server (`mocks/server.py`). `GET /api/v1/integrations/adapters` returns this status table.

## ERP adapter interface

```python
class ERPAdapter(ABC):
    def get_inventory(self, since=None) -> list[ERPInventoryItem]
    def get_suppliers(self) -> list[ERPSupplier]
    def get_purchase_orders(self, since=None) -> list[ERPPurchaseOrder]
    def create_purchase_order(self, po: ERPPurchaseOrder, idempotency_key: str) -> ERPPurchaseOrder   # idempotent: same key, same order
    def get_delivery_status(self, po_number: str) -> ERPDeliveryStatus
```

Errors are typed and carry `retryable`: `ERPUnavailable`, `ERPTimeout` (retryable), `ERPValidationError`, `ERPConflict` (not). To connect a real ERP, implement the five methods (`enterprise/integrations/base.py`) and register the adapter kind; the sync, reconciliation, dispatch and retry machinery is shared.

**Pull (`POST /integrations/{id}/sync/{inventory|suppliers|purchase_orders}`)** goes through the same validated importer as a customer upload (quality report, atomic commit). `GET /integrations/{id}/reconcile` lists SKUs where ERP and platform stock differ and writes nothing.

**Push.** With `push_on_approve`, approved purchase orders are sent to the ERP after the human decision. Each PO has a durable dispatch record with an idempotency key, so retries and replays cannot create it twice; failures are recorded with the reason, retried (`POST /integrations/{id}/push/{workflow}`), and an ERP outage never undoes the local approval. `SANDBOX` tenants can push only to the mock ERP.

Run the mock environment: `python -m mocks.server` (ERP, supplier, inventory, procurement endpoints) with scenarios `happy_path`, `supplier_failure`, `inventory_mismatch`, `api_timeout`, `duplicate_request`, `partial_failure` (switch with `POST /_scenario {"name": ...}`).

## Inbound webhooks

`POST /api/v1/webhooks/inbound/{tenant_id}/{source}` - create the source first with `POST /integrations` `{"kind": "webhook_in", "config": {"source": "erp"}}`; the signing secret is returned once.

Headers: `X-SCM-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`, `X-SCM-Event-ID` (unique per event), `X-SCM-Event-Type`.

| Control | Behaviour |
|---|---|
| Authentication | HMAC over timestamp + raw body; constant-time compare |
| Replay protection | timestamp must be within 300 s; the event id is unique per (tenant, source) |
| Idempotency | same id + same payload -> `200 {"status":"duplicate"}`, applied once; same id + different payload -> `409` |
| Schema validation | strict pydantic model per event type; unknown fields rejected (`422`) |
| Oracle resistance | unknown tenant, unknown source and bad signature all return the same `401` |
| Size | 256 KB |
| Event log | every accepted event is stored (validated payload only) with status RECEIVED / PROCESSED / FAILED / DEAD; `GET /webhooks/events` |
| Retry | processing failures retry with exponential backoff (60 s x 2^n), 5 attempts, then DEAD |

Event types: `inventory.updated`, `purchase_order.created`, `purchase_order.updated`, `delivery.received`, `supplier.updated`. Examples with real responses: [API_EXAMPLES.md](API_EXAMPLES.md).

## Outbound events

`POST /events/subscriptions` `{"url": "https://...", "event_types": [...]}` returns a signing secret once. Event types: `stockout.risk.detected`, `procurement.recommendation.created`, `approval.requested`, `procurement.approved`, `procurement.rejected`, `purchase_order.created`, `dispatch.completed`.

Delivery is at-least-once with the same signature scheme and `X-SCM-Event-ID` (receivers should de-duplicate), exponential backoff from 30 s (cap 1 h), 6 attempts, then `DEAD` with the failure reason; `POST /events/deliveries/{id}/replay` re-queues. Status, attempts and last error are listed at `GET /events/deliveries`. The target URL is re-checked for SSRF on **every** attempt (DNS rebinding). Delivery runs in the maintenance loop (`SCM_BACKGROUND=1`) or `POST /admin/maintenance/run`.

Verify a signature on the receiving side with `scm.security.verify_signature(secret, body, header)` or re-implement the three lines above.

## Network safety

Integration and subscription URLs must be public HTTP(S): loopback, link-local (including cloud metadata), private ranges, non-HTTP schemes and credentials in URLs are refused. `SCM_ALLOW_PRIVATE_URLS=1` relaxes this for local development only and is flagged by the readiness check.
