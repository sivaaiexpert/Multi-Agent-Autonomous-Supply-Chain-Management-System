# Product overview

An AI-assisted procurement and inventory-operations platform. It watches inventory and demand, forecasts, scores stock-out risk, selects and splits orders across suppliers under business rules, routes the result through a configurable human approval chain, and records every decision with the data behind it. It is multi-tenant and integrates with customer systems through a versioned REST API, signed webhooks and an ERP adapter interface.

> **Status labels used across these docs** - **IMPLEMENTED**: code exists. **VALIDATED**: IMPLEMENTED and covered by automated tests or measurements that were run (see the linked reports). **SIMULATED**: exercised only against synthetic data or mock systems. **ADAPTER-READY**: an extension point exists, no connector. **FUTURE**: not built. The authoritative per-capability table is in [COMMERCIAL_READINESS.md](../COMMERCIAL_READINESS.md).

## What it does

| Capability | What happens | Status |
|---|---|---|
| Demand forecasting | Per-SKU forecasts with intervals from history; accuracy back-tested (MAE, RMSE, WAPE, MAPE, bias) | VALIDATED (synthetic) |
| Inventory risk | Deterministic stock-out probability, days of cover, recommended quantity | VALIDATED |
| Supplier intelligence | Reliability, on-time, quality and lead-time statistics from delivery history | VALIDATED |
| Procurement optimisation | Cost + lead-time + risk allocation across suppliers with MOQ, capacity, diversification, budget and policy rules | VALIDATED |
| Human approval | Tiered, multi-step chains by order value; reject / edit / re-source / escalate / time-out; segregation of duties; all audited | VALIDATED |
| Decision trace and explanations | Inputs -> forecast -> risk -> candidates -> optimisation -> policy -> approval -> execution; plain-language reasons generated only from stored data | VALIDATED |
| Business impact | Baseline vs optimised procurement cost, inventory KPIs, forecast accuracy, supplier KPIs, cycle times; what-if simulation; ROI with documented assumptions | VALIDATED (synthetic) / SIMULATED |
| Data import | CSV / JSON / Excel for products, inventory, suppliers, quotes, demand, purchase orders, supplier performance; mapping, validation, quality report, preview, atomic commit | VALIDATED |
| Integration | Versioned REST API + OpenAPI, signed inbound webhooks, signed outbound events with retries, ERP adapter interface (mock + generic REST) | VALIDATED against mocks; real ERPs ADAPTER-READY |
| Multi-tenancy and access control | Database per tenant, six roles enforced on the server | VALIDATED |
| Operations | Usage metering, cost attribution, dashboards, backup / restore, crash recovery, retention, export, deletion | VALIDATED (single machine) |

## What it is not (today)

- Not validated against any customer's data or any real ERP. All benchmark results are on synthetic data. See [LIMITATIONS.md](LIMITATIONS.md).
- Not a hosted, horizontally scalable service: storage is SQLite per tenant on one node.
- Not a compliance certification. Privacy and security features are technical controls that support compliance requirements ([SECURITY.md](SECURITY.md)).

## How the pieces fit

See the architecture diagram in [../ARCHITECTURE.md](../ARCHITECTURE.md). In short: clients (console, API consumers, ERPs) -> FastAPI (auth, rate limits, tenant scope) -> platform modules (approvals, importer, impact, simulation, integrations, usage, privacy, DR) -> LangGraph workflow engine (supervisor, demand, risk, supplier, optimiser, policy, communications, human gate, dispatch) -> per-tenant SQLite databases plus a control-plane database for identity and routing.

## Where to go next

[CUSTOMER_ONBOARDING.md](CUSTOMER_ONBOARDING.md) · [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) · [API_GUIDE.md](API_GUIDE.md) · [BUSINESS_METRICS.md](BUSINESS_METRICS.md) · [ROI_METHODOLOGY.md](ROI_METHODOLOGY.md) · [RBAC.md](RBAC.md) · [SECURITY.md](SECURITY.md) · [OPERATIONS.md](OPERATIONS.md) · [DEMO_GUIDE.md](DEMO_GUIDE.md) · [LIMITATIONS.md](LIMITATIONS.md)
