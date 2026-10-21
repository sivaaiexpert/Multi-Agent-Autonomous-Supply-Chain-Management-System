# Commercial readiness

An evidence-based statement of what this platform is today. Each capability has one label: **IMPLEMENTED** (code exists), **VALIDATED** (implemented and covered by automated tests or measurements that were run, linked below), **SIMULATED** (exercised only on synthetic data or mock systems), **ADAPTER-READY** (an interface exists, no connector), **FUTURE** (not built). A label never exceeds its evidence.

Evidence at the time of writing: **604 automated tests** (596 run green in the last full run, including the slow 128-case QA matrix; the 8 added afterwards for the tooling were run and pass), ruff and mypy clean, 114-scenario engine benchmark, 20-unit pre-registered business experiment, 12-scenario platform failure suite + 52-scenario engine fault matrix, load test at 10 / 50 / 100 concurrent workflows, 154-test security run (`make security-test`). Commands to reproduce each are in [README.md](../README.md).

## Capability matrix

| # | Capability | Status | Evidence / note |
|---|---|---|---|
| 1 | Domain model (tenant, user, role, SKU, supplier, orders, forecasts, risks, approvals, workflow runs, audit, metrics, integrations ...) with separate DB / API / UI models | VALIDATED | `enterprise/` + `scm/migrations.py` (v3), `tests/test_contracts.py`, `tests/test_tenancy.py` |
| 2 | Multi-tenancy: database per tenant + control plane, `Tenant_ID` stamped and filtered | VALIDATED | 52 tenancy tests incl. cross-tenant read/write/approval/import/webhook |
| 3 | Business Impact Engine (procurement, inventory, forecast accuracy, supplier, operations KPIs with provenance) | VALIDATED on synthetic data | `tests/test_business_impact.py`; no customer data |
| 4 | Baseline vs optimised comparison with documented methodology | VALIDATED (synthetic) | [BUSINESS_METRICS](product/BUSINESS_METRICS.md); negative savings reported as is |
| 5 | Deterministic what-if simulation (demand, stock, price, capacity, lead time, budget, risk tolerance) | VALIDATED | determinism tested, also over the API |
| 6 | ROI with explicit assumptions; undefined without a cost basis | VALIDATED (method) / SIMULATED (inputs) | [ROI_METHODOLOGY](product/ROI_METHODOLOGY.md) |
| 7 | Data import CSV / JSON / Excel with mapping, ID mapping, validation, quality report, preview, atomic commit | VALIDATED | 45 importer tests; row-level rollback after commit is FUTURE (restore from backup) |
| 8 | ERPAdapter abstraction + MockERPAdapter | VALIDATED (mock) | |
| 8b | Generic REST ERP adapter | SIMULATED | validated only against the bundled mock server |
| 8c | SAP / Oracle / Microsoft Dynamics / NetSuite connectors | **ADAPTER-READY** | interface only; no connector code, nothing validated |
| 9 | Versioned REST API + OpenAPI + captured examples | VALIDATED | `docs/openapi.json` (72 paths), [API_EXAMPLES](product/API_EXAMPLES.md) generated from real calls |
| 10 | Inbound webhooks: HMAC, replay window, idempotency, schema validation, event log, retry -> DEAD | VALIDATED (local) | integration + security tests |
| 11 | Outbound events: subscriptions, signed delivery, backoff, DEAD, replay, SSRF re-check | VALIDATED (local receiver) | |
| 12 | Mock enterprise environment with failure scenarios | IMPLEMENTED / VALIDATED | six scenarios, `mocks/server.py` |
| 13 | Demo mode and end-to-end demo (labelled DEMO / SYNTHETIC DATA) | VALIDATED | `make demo`, smoke test |
| 14 | Stateful onboarding derived from real data | VALIDATED | demo tenants can never go to production |
| 15 | Per-tenant business policies (validated, versioned, platform bounds) | VALIDATED | |
| 16 | Configurable approval workflow (tiers, multi-step, reject / edit / re-source / escalate / time-out, audited, segregation of duties) | VALIDATED | 15 approval tests |
| 17 | RBAC (6 roles) enforced server-side | VALIDATED | [RBAC](product/RBAC.md) + API tests |
| 18 | Secret management (encrypted at rest, startup validation, `.env.example`) | VALIDATED | no rotation tooling (FUTURE) |
| 19 | Per-tenant model / provider config under platform allow-list | VALIDATED | cannot disable safety controls |
| 20 | Cost control and attribution (LLM / compute / integration per tenant / workflow / model / agent) | VALIDATED | LLM cost is cloud-equivalent for local inference; no budgets that block spending |
| 21 | Reproducible business-value experiment (manual vs rule vs AI, pre-registered, hash-locked) | VALIDATED (method) / SIMULATED (data) | `evaluation/protocol.json`; results mixed, see below |
| 22 | Synthetic customer benchmarks (retailer, manufacturer, distributor, seasonal) | SIMULATED | 4 profiles x 5 seeds |
| 23 | Load tests (10 / 50 / 100 concurrent workflows) | VALIDATED on one machine | [LOAD_TEST_REPORT](LOAD_TEST_REPORT.md): 0 errors; ~1.6 workflows/s ceiling; **not production scale** |
| 24 | Controlled failure tests | VALIDATED | [FAILURE_TEST_REPORT](FAILURE_TEST_REPORT.md): 12/12 + 52/52 |
| 25 | Disaster recovery: backup, verified restore, workflow recovery, event replay, audit preservation | VALIDATED (small synthetic tenant) | restore ~66 ms measured; **RTO at real volumes and RPO not established** |
| 26 | Observability dashboards (system / AI / business / tenant) | IMPLEMENTED / VALIDATED (API) | no external metrics stack or alerting |
| 27 | Explanations generated only from stored data; decision trace | VALIDATED | |
| 28 | Downloadable reports (7 kinds; CSV / JSON / Markdown / HTML) | VALIDATED | CSV formula injection neutralised |
| 29 | Usage metering, plan quotas (billing-ready, no prices) | VALIDATED | no invoicing or payments (FUTURE) |
| 30 | SLA / operations documentation without unsupported claims | IMPLEMENTED | [OPERATIONS](product/OPERATIONS.md): states that no SLA is offered |
| 31 | Case-study generator from benchmark results | VALIDATED | refuses customer-style claims; tested |
| 32 | Product documentation set (11 documents) + architecture diagram | IMPLEMENTED | [docs/product](product/) |
| 33 | Security review with fixes and tests | VALIDATED (self-assessed) | [SECURITY](product/SECURITY.md); **no external pentest** |
| 34 | Privacy controls: classification, minimisation, retention, export, deletion | VALIDATED (technical controls) | not a compliance claim |
| 35 | `scripts/production_readiness_check` (PASS / WARNING / FAIL) | VALIDATED | currently reports 0 FAIL, warnings listed below |
| 36 | Real customer data, pilot, or reference deployment | **FUTURE** | none exists |
| 37 | Horizontal scale, managed database, HA, multi-region | **FUTURE** | SQLite single node |
| 38 | TLS, process supervision, container orchestration, log shipping | **FUTURE** (documented only) | Dockerfile / compose are development starting points |
| 39 | Per-user authentication for the console | FUTURE | console uses configured identity |
| 40 | External security assessment / certifications | **FUTURE** | |

## What the evidence says about value

From the pre-registered experiment (20 synthetic units, paired differences, bootstrap 95% CI; per-profile tables in `docs/CASE_STUDY_SYNTHETIC_*.md`):

- Lean AI policy vs manual buyer: **+36.8k simulated gross profit** on average (CI 4.5k to 74.6k), better in 14/20 units; fewer stock-out days in 20/20.
- Default (service-first) AI policy vs manual buyer: **-58.1k** gross profit (CI -109.8k to -13.9k), better in 7/20; fewer stock-out days in 20/20. It buys service with inventory.
- Lean AI vs rule-based: +128.2k (CI 80.7k to 182.3k), better in 17/20.

These are simulated results on generated data under stated assumptions. They suggest the lean policy is worth testing on a real customer's history; they are **not** savings, and they do not isolate the contribution of any language model.

## Honest assessment

| Question | Answer |
|---|---|
| Is it a credible technical foundation for a procurement-AI product? | **Yes.** Deterministic decision engines with a human gate, tenant isolation, RBAC, audit, integration surfaces, measurement tooling, and reproducible evidence exist and are tested. |
| Is it ready for a design-partner pilot? | **Yes, in sandbox / shadow mode**: import the partner's data, run back-tests, compare recommendations with what buyers actually ordered, no ERP write-back. |
| Is it ready for paying customers in production? | **No.** Missing: a real ERP connector validated against a real system; any customer data validation; managed database / HA / TLS / supervision; external penetration test; per-user console auth; operational processes (on-call, backup scheduling, support). |
| Can savings or ROI be quoted? | **No.** Only a method and simulated, synthetic results exist. A quote requires the customer's own data and baseline. |
| Biggest technical risks | (1) throughput: ~1.6 workflows/s and linear latency growth on one node; (2) SQLite per tenant for large tenant counts; (3) benchmark results are mixed by policy and depend on assumptions; (4) the real-ERP integration effort is unknown. |
| Biggest commercial risks | No customer evidence; value hinges on the lean policy's profit effect holding on real data; integration cost per customer. |

## Current `make production-check` warnings (this repository, development environment)

Not production mode; no `SCM_SECRET_KEY` in the environment; quota enforcement off; TLS cannot be verified; no control plane yet / no recent backup; Docker files are not a validated deployment; SQLite is single-node; ERP connectors are mock / generic only. Zero FAIL.

## Path to the next stage (suggested order)

1. Design-partner shadow pilot on real history (validates metrics, data mapping, buyer acceptance).
2. First real ERP adapter built and tested against the partner's sandbox.
3. Deployment hardening: TLS, managed storage, supervision, backups on a schedule, monitoring and alerting; load test on target infrastructure with LLM enabled.
4. External security assessment; dependency scanning; key rotation.
5. Per-user console authentication; billing integration if a commercial model requires it.
