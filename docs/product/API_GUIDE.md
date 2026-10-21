# API guide

Versioned REST API under `/api/v1`. Machine-readable contract: [../openapi.json](../openapi.json) (also served at `/openapi.json` and `/docs`). Real requests and responses, captured by executing them: [API_EXAMPLES.md](API_EXAMPLES.md) (regenerate with `python -m scripts.capture_api_examples`).

## Conventions

- **Authentication**: `X-API-Key: <key>` or `Authorization: Bearer <key>`. The tenant is the key's tenant; it can never be chosen by a header or parameter (such parameters are ignored / rejected).
- **Errors**: `{"error": {"code", "message", "request_id"}}` with `400/401/403/404/409/413/422/429/503/504`. `request_id` is also the `X-Request-ID` response header; quote it when reporting problems.
- **Pagination**: `limit` (1-200, default 50) and `offset`; list responses carry `items`, `total`, `limit`, `offset`.
- **Idempotency**: `POST /workflows` honours `Idempotency-Key`; replays return the original run.
- **Limits**: 1 MB JSON bodies (32 MB for imports), `Content-Length` required on writes (`411`), per-key token bucket (`429` + `Retry-After`), failed-authentication limiter per IP.
- **Long operations** are asynchronous: start returns `202` with a `workflow_id`; poll `GET /workflows/{id}`.
- **Provenance**: reports and impact responses include `provenance` and `data_provenance` fields.

## Resources

| Group | Endpoints | Minimum permission |
|---|---|---|
| Health | `GET /health`, `GET /ready` | none |
| Inventory / suppliers | `GET /inventory`, `GET /suppliers` | read |
| Forecasts | `GET /forecasts` | read |
| Risks | `GET /risks?level=` | read |
| Procurement | `GET /procurement/orders`, `GET /procurement/comparison`, `GET /workflows/{id}/trace`, `GET /workflows/{id}/explanation` | read / view_reports / view_trace |
| Workflows | `POST /workflows`, `GET /workflows`, `GET /workflows/{id}` | run_workflow / read |
| Approvals | `GET /approvals`, `GET /approvals/{wf}`, `POST /workflows/{id}/approve \| reject \| resource`, `POST /approvals/process-overdue` | approve / manage_policies |
| Metrics | `GET /metrics`, `GET /impact/{procurement,forecast,suppliers,operations}`, `POST /impact/snapshot`, `POST /impact/roi` | view_reports |
| Simulation | `POST /simulations/what-if`, `POST /simulations/backtest` | run_simulation |
| Imports | `POST /imports/preview`, `POST /imports/{job}/commit`, `GET /imports[/{job}]`, `POST /mappings/suggest`, `GET\|PUT /mappings/{entity}`, `PUT /mappings/ids/{sku\|supplier}` | import_data |
| Integrations | `GET /integrations[/adapters]`, `POST /integrations`, `DELETE /integrations/{id}`, `POST /integrations/{id}/sync/{entity}`, `GET /integrations/{id}/reconcile`, `POST /integrations/{id}/push/{wf}`, `GET /integrations/dispatches` | manage_integrations |
| Events | `GET\|POST /events/subscriptions`, `DELETE /events/subscriptions/{id}`, `GET /events/deliveries`, `POST /events/deliveries/{id}/replay` | manage_integrations |
| Webhooks | `POST /webhooks/inbound/{tenant}/{source}` (signature), `GET /webhooks/events` | signature / read |
| Reports | `GET /reports/{kind}?format=csv\|json\|md\|html&period=YYYY-MM` | view_reports (audit: read_audit) |
| Usage / dashboards | `GET /usage`, `GET /usage/workflows/{id}`, `GET /dashboards/{system,ai,business,tenant}` | view_usage / read |
| Administration | `/admin/users`, `/admin/keys`, `/admin/policy[/history]`, `/admin/model-config`, `/admin/onboarding/*`, `/admin/privacy/*`, `/admin/maintenance/run` | manage_users / manage_policies / configure |
| Platform | `/admin/tenants` (create, list, patch, delete) | manage_tenants (SUPER_ADMIN only) |

Report kinds: `monthly_procurement`, `supplier_performance`, `forecast_accuracy`, `inventory_risk`, `savings`, `ai_usage`, `audit`.

## Minimal session

```bash
KEY=scm_...                       # from POST /admin/users/{id}/keys
curl -H "X-API-Key: $KEY" "http://localhost:8000/api/v1/risks?level=CRITICAL"
curl -X POST -H "X-API-Key: $KEY" -H "Idempotency-Key: run-2026-10-02" http://localhost:8000/api/v1/workflows
curl -H "X-API-Key: $KEY" http://localhost:8000/api/v1/workflows/<id>        # AWAITING_APPROVAL
curl -X POST -H "X-API-Key: $APPROVER" -H "Content-Type: application/json" \
     -d '{"reason":"reviewed"}' http://localhost:8000/api/v1/workflows/<id>/approve
curl -H "X-API-Key: $KEY" "http://localhost:8000/api/v1/reports/savings?format=csv" -o savings.csv
```

## Compatibility

`/api/v1` is additive: new fields and endpoints may appear; existing fields are not removed or re-typed within v1. Breaking changes would go to `/api/v2`. This is a stated policy of this codebase, not yet a history: there are no external consumers to date.
