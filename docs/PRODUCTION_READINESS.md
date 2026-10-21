# Production readiness

Status vocabulary: **Implemented** (works and is tested) · **Partially implemented** (works, with a stated gap) · **Not implemented**. No scores are given on purpose.

Verification run for this revision: `pytest` 286 tests green (incl. the 128-case QA matrix), coverage 91 % overall (policy 99 %, risk 98 %, graph 98 %, optimiser 97 %, suppliers 100 %, orders 94 %), `ruff` and `mypy` clean, 114/114 evaluation scenarios, 52/52 chaos scenarios, and a **real-server smoke test** (uvicorn + curl: fresh DB, key auth, idempotent create/replay, approve, audit-chain verify).

| Category | Status | Detail |
|---|---|---|
| **Architecture** | Partially implemented | Layered, documented, engines separated from the LLM layer, events behind an abstraction. Gaps: some graph nodes still call `db.*` helpers directly rather than going through service/tool objects; domain / DB / DTO model separation is partial (dataclasses + pydantic at the edges, but legacy `db.py` still mixes queries and a little logic). |
| **Reliability** | Implemented | Timeouts, bounded retries, backoff, breakers, model fallback, deterministic fallbacks, contained node crashes, idempotency (API / order / approval), transactional PO creation and dispatch, durable checkpoints for the API. Single-node SQLite is a scale limit, not a correctness gap. |
| **Security** | Partially implemented | Hashed API keys, RBAC enforced at API/service/console, validation, injection defence, redaction, tamper-evident audit. Gaps: console auth is an env-var role; no rate limiting, key expiry/rotation, SSRF allow-list for webhooks, or dependency scanning (see `THREAT_MODEL.md`). |
| **Observability** | Implemented | Trace/workflow/run ids, per-node spans, LLM call rows, structured JSON logs with redaction, `/metrics` with P50/P95/P99. Gaps within this: no OpenTelemetry/Prometheus exporter, no alerting. |
| **Testing** | Implemented | 286 tests across unit, property, integration, workflow, API, adversarial, contract, UI (AppTest) and the legacy matrix; CI configuration is **not** included (`make check` is the contract). |
| **Performance** | Partially implemented | Measured (P50/P95/P99 per node and per workflow) and documented; deterministic part ≈ 0.5 s. DB-layer optimisations were tried and measured as ineffective, so only the batched queries / pragmas were kept. No load test, no concurrency benchmark beyond the 12-thread idempotency race test. |
| **Cost** | Implemented | Tokens, latency and cost per call/workflow persisted; cloud-equivalent vs local cost; configurable model routing. Cost figures for the scripted benchmark are estimates (chars/4); real counters appear in the live sample. |
| **Data integrity** | Implemented | CHECK/FK/UNIQUE constraints on new tables, versioned reversible migrations, indexes, append-only audit, idempotency keys, atomic orders. Legacy tables lack FKs (decision log #7). |
| **AI evaluation** | Partially implemented | Real framework + report; live LLM sample is small (3 workflows) and groundedness is a SKU-id check, not semantic; stock-out and savings studies are synthetic; TTFT not measured. |
| **Deployment** | Partially implemented | Dockerfile, compose, `.env.example`, health/readiness, migrations CLI. **Container build was not run here** (no Docker); no CI/CD pipeline, no IaC, no TLS termination (use a proxy). |
| **Documentation** | Implemented | README, architecture, AI design, threat model, evaluation (+ generated report), operations, deployment, decision log. |

## Requirement-by-requirement notes (from the brief)

* **Forecasting, risk engine, supplier intelligence, optimiser, policy validator, idempotency, transactions, audit, events, RBAC, API, worker, migrations, config, evaluation, chaos tests, benchmarks** – Implemented.
* **Multi-agent separation (Demand / Risk / Supplier Intel / Optimisation / Policy / Comms)** – Implemented as sequential supervisor-routed nodes, not parallel fan-out (LangGraph state merging for parallel branches was judged not worth the added failure modes); approval gate is after email drafting by design (decision log #2).
* **Structured outputs** – `DemandAnalysis`, `RiskExplanation`, `ProcurementRecommendation`, `VendorMessage`. A separate `NegotiationProposal` was intentionally **not** built: negotiation terms would be a decision channel (decision log #11).
* **Fallback provider** – model-level fallback only; the only provider is Ollama (the provider interface is `invoke(model, remaining)`, so adding another is local to `llm.py`).
* **Caching** – supplier metrics (TTL + invalidation on new facts) and forecasts (content-hash memo). LLM responses are deliberately **not** cached.
* **Repository hygiene** – `.gitignore`/`.dockerignore` added; `.venv/`, caches, local DBs and outbox are ignored. The directory is not under git, so nothing needed `git rm`; `.venv` was left in place as this checkout's interpreter.

## Known limitations (summary)
SQLite single-writer · console is not a hardened multi-user app · pattern-based injection filter (architecture, not the filter, is the real defence) · synthetic evaluation data · small live-LLM sample · Docker untested here · `no_vendor` ends a run rather than escalating when every supplier fails the floors.
