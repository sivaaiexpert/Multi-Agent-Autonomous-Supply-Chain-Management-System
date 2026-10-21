# Operations and service levels

What an operator can rely on, what is measured, and what is **not** promised. See also [../OPERATIONS.md](../OPERATIONS.md) (engine runbook) and [../DEPLOYMENT.md](../DEPLOYMENT.md).

## No SLA is offered here

This repository contains no uptime, latency or recovery commitment. The numbers below are **measurements from a single developer machine** (Windows, 8 logical CPUs, one uvicorn process, SQLite WAL, synthetic data, deterministic decision path) and are useful for comparing runs, not for promising service levels. A service level has to be derived from measurements on the target infrastructure, with the customer's data volumes and their LLM provider.

## Measured behaviour (reproducible)

**Load test** - `make load-test`, full report [../LOAD_TEST_REPORT.md](../LOAD_TEST_REPORT.md). A workflow = start, reach the human gate, approve, complete, over real HTTP; 4 tenants.

| Concurrent workflows | Errors | Throughput | End-to-end P50 / P95 / P99 | Queue wait P50 / P95 |
|---:|---:|---:|---|---|
| 10 | 0 | 0.75 wf/s | 13.2 s / 13.4 s / 13.4 s | 0.6 ms / 14 ms |
| 50 | 0 | 1.63 wf/s | 22.9 s / 30.2 s / 30.3 s | 12.7 s / 21.4 s |
| 100 | 0 | 1.56 wf/s | 44.0 s / 62.5 s / 62.8 s | 30.5 s / 53.2 s |

Reading: no errors and no lost workflows up to 100 simultaneous lifecycles, but throughput saturates at about 1.6 workflows/s on this machine and latency grows linearly with concurrency (the service uses a small fixed worker pool and SQLite serialises writers: no database lock errors occurred, connection-held time is dominated by writes). LLM latency was not measured: the decision path was deterministic. This is not a capacity claim for any production environment.

**Failure tests** - `make chaos-test`, report [../FAILURE_TEST_REPORT.md](../FAILURE_TEST_REPORT.md): 12 of 12 platform scenarios passed (LLM unavailable, database write-locked, ERP unavailable, ERP timeout, partial ERP dispatch, supplier withdrawn, duplicate webhook delivery, worker crash mid-run, corrupt import, inventory mismatch, customer webhook receiver down, disaster recovery) plus the 52-scenario engine fault matrix. Each records detection time, recovery time, integrity, duplicate prevention and final-state correctness.

**Disaster recovery** (measured, small synthetic tenant): backup then restore of one tenant database took about 66 ms to restore with row counts and audit chain verified; a corrupted backup was refused. **RTO for real data volumes and RPO are not established**: RPO equals the interval at which you schedule backups, and no scheduler is shipped.

## Running it

- Development: `make dev` (API, auto-reload) and `make ui` (console). Development keys and data only.
- Production-like local launch: `make production` (needs `SCM_SECRET_KEY`; refuses on a failed readiness check; one worker; loopback by default). It is not a deployment: add TLS termination, a process supervisor, log shipping and backups. See [../DEPLOYMENT.md](../DEPLOYMENT.md); the Dockerfile / compose file are development starting points, not validated production artifacts.
- Readiness: `GET /health` (liveness), `GET /ready` (database, migrations, key configuration), `make production-check` (configuration, secrets-in-source scan, per-tenant integrity / schema / audit-chain verification, backup age, evidence freshness).
- Background maintenance (`SCM_BACKGROUND=1`, interval `SCM_MAINTENANCE_INTERVAL`): webhook retries, outbound delivery, approval escalation / time-outs. Without it, call `POST /admin/maintenance/run`.

## Backup, restore and recovery

- **Backup**: `enterprise.dr.backup_tenant(tenant, dest)` uses SQLite's online backup API, writes a manifest (SHA-256 per file, row counts, schema version, audit tip hash); `backup_control(dest)` for the control plane.
- **Restore**: `enterprise.dr.restore_tenant(dir, tenant, overwrite=True)` verifies checksums, migrates, compares row counts, verifies the audit chain **before** replacing live data; failure leaves live data untouched.
- **Workflow recovery**: a restarted service calls `recover()` on start: runs left RUNNING resume from the last durable checkpoint; runs at the human gate stay waiting; unrecoverable runs are marked FAILED. Idempotent; no duplicate orders (tested).
- **Event replay**: DEAD outbound deliveries re-queue with `POST /events/deliveries/{id}/replay`; inbound events that failed processing are retried by the maintenance job with backoff and end as DEAD (visible in `GET /webhooks/events`); re-processing a DEAD inbound event is manual (the sender can re-send under a new event id).
- **Audit preservation**: the audit log is append-only and hash-chained; backups carry the chain tip and restores re-verify it.

## Observability

`GET /dashboards/system` (API request counts, latency percentiles and error rates per route group; the worker pool is threads inside the API process), `/ai` (LLM calls, tokens, latency, fallbacks, breaker state, cost), `/business` (KPIs with provenance), `/tenant` (usage vs plan, users, integrations). Structured JSON logs carry `request_id`, `trace_id`, `tenant_id`, `workflow_id`, node timings and token counts; no prompt text.

## Usage, cost and plans

`GET /usage` meters workflows, API calls, LLM tokens, storage, users and outbound deliveries per tenant and period, and attributes LLM (cloud-equivalent), compute and integration cost per workflow / model / agent. Plans `pilot`, `professional`, `enterprise` are **packaging concepts with quota limits, not prices**; quotas are advisory unless `SCM_ENFORCE_QUOTAS=1` (then HTTP 429). Metered data is "billing-ready" in that it is per-tenant, per-period and auditable; there is no invoicing or payment integration.

## Support boundaries (to agree with a customer, not promised here)

Severity definitions, response times, maintenance windows, data-residency and exit/export terms are contractual matters this repository does not define. The technical building blocks that would support them (request ids, audit trail, export, backup / restore, status endpoints) exist as described above.
