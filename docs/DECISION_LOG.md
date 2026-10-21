# Decision log

Format: decision · alternatives · why · consequence.

| # | Decision | Alternatives considered | Why | Consequence / trade-off |
|---|---|---|---|---|
| 1 | Keep LangGraph, SQLite and the existing 5-node graph; **extend** with 3 nodes (`demand`, `supplier_intel`, `policy_check`) | Rewrite as microservices; replace SQLite with Postgres | Preserve working behaviour (128-case QA matrix kept green); single-node SQLite is adequate for this scale | Single-writer SQLite; documented as a scale limit |
| 2 | Emails are drafted **before** the human gate | Gate before vendor communication (brief's sketch) | Reviewers edit the email and need the exact ERP payload; nothing is sent before approval, so safety is identical | Draft POs exist in `PENDING_APPROVAL` while waiting |
| 3 | Exact subset-enumeration optimiser, no solver library | `scipy.optimize.milp`, OR-Tools | Costs are linear for a fixed supplier set; ≤ 8 suppliers ⇒ ≤ 256 subsets; zero new dependency; verified against brute force | Does not scale past ~15 suppliers per SKU (then use MILP) |
| 4 | Default 70 % per-supplier share cap for orders ≥ 250 units | No cap | Resilience to a supplier failing outright | Measured purchase premium for a small resilience gain (see report); configurable, can be disabled |
| 5 | When **no** supplier passes the quality/reliability floors the run ends `no_vendor` | Relax floors automatically | Silent relaxation would defeat the policy | A real stock-out can persist until a human intervenes; visible in the UI/API |
| 6 | Hand-rolled versioned migrations (`scm/migrations.py`) | Alembic + SQLAlchemy | SQLite-only project with raw SQL; an ORM layer would be disproportionate | Fewer features (no autogenerate); every migration has `up` **and** `down` |
| 7 | Foreign keys only on the **new** tables | Rebuild legacy tables with FKs | Rebuilding live tables is risky; the legacy QA matrix deliberately injects orphan rows | Legacy tables rely on application checks + the policy validator |
| 8 | Demand history is **not** seeded in `db.init_db()`; `seed.ensure_demo_data()` does it for the apps | Seed always | The QA matrix relies on a history-free DB (static rules) | Imported data without history uses static rules until history is provided |
| 9 | Audit log: triggers + hash chain | Triggers only; external log service | Triggers stop accidents, the chain detects deliberate tampering by someone with raw DB access | Verification is O(n) (`verify=true`) |
| 10 | API keys as SHA-256 hashes in `SCM_API_KEYS` | JWT/OIDC | No identity provider in scope; keys are simple to rotate | No key expiry; use an OIDC gateway in production |
| 11 | LLM schemas contain **no decision fields**; decision-like keys are ignored + audited | Validate model-suggested prices against policy | Removing the channel is stronger than guarding it | Model "negotiation" proposals are not implemented (a `NegotiationProposal` schema was deliberately not added) |
| 12 | Timeouts are **not** retried on the same model | Retry everything | A timeout already consumed the deadline; retrying multiplies user-visible latency | A slow-but-alive model is not retried; the next model / fallback is |
| 13 | A shared deadline per LLM call across retries and models | Per-attempt timeout | Bounds worst-case latency to the configured timeout | Fallback models only help for fast failures |
| 14 | Console auth = role from `SCM_UI_ROLE` (default ADMIN) | Full login for Streamlit | The console is a local operator tool; the API carries real authentication | Do not expose the console to untrusted networks without a gateway |
| 15 | No Kafka/Redis/RabbitMQ | Add a broker | Nothing needs one; the outbox + `Transport` protocol keeps the door open | In-process delivery only |
| 16 | Predictive alerts ON by default (`risk.predictive_alerts`) | Off | Core goal of the upgrade | More alerts than the static rule; each carries its reasoning |
| 17 | `synchronous=NORMAL` + journal mode set once per path; **keeper connection rejected** | Persistent connection pool | A/B (n=20 each) showed no measurable gain from the keeper (552 vs 547 ms p50) | The deterministic part of a run stays ~0.55 s; LLM latency dominates real runs |
| 18 | Evaluation uses a scripted LLM for the 114 scenarios and a separate small live sample | Live model everywhere | Reproducible, offline, fast; live behaviour measured separately | Live sample is small (indicative only) |
| 19 | `.venv` is git-ignored, not deleted | Delete it | It is the working interpreter of this checkout; the directory was never under version control | Anyone adding git must keep `.gitignore` |

## Defects found by the new test/evaluation work (and fixed)

* `DemandForecaster` treated `horizon_days=0` as "use default" (caught by a unit test).
* Optimiser read the **global** share cap instead of its own config, and forced ≥ 2 suppliers even with the cap at 100 % (caught by comparing the two evaluation columns; regression tests added).
* Supervisor crash was not audited as `WORKFLOW_FAILED` (caught by the chaos scenario `crash-supervisor`); now audited where it happens.
* Worker threads silently fell back to the **live** database during parallel email drafting (caught by the QA matrix after DB writes were added); fixed with `db.propagate`.
* Entity-encoded `&lt;script&gt;` survived CSV name sanitisation as text (caught by `test_sanitisation_removes_every_markup_variant`).
* `ruff --fix` removed imports that views relied on through `from ui.core import *` (caught by the UI approval test).
* A fresh database was not migrated by the API (caught by a real-server smoke test, not by `TestClient`).
