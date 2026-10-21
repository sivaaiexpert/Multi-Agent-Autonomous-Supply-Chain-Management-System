# Operations runbook

## Processes
| Process | Command | Notes |
|---|---|---|
| API | `uvicorn api.main:app --port 8000` | initialises/migrates the DB on start; workflows run in a 4-thread worker pool |
| Console | `streamlit run app.py` | seeds demo data on first start (idempotent) |
| Migrations | `python -m scm.migrations status \| up \| down <version>` | `up` is idempotent; `down 1` removes the v2 tables (⚠ deletes audit/forecast data) |
| Keys | `python -m scm.security gen-key --role APPROVER --actor alice` | prints the key once + the `SCM_API_KEYS` entry (hash) |

## Health
* `GET /health` – liveness. `GET /ready` – 200 only when the DB is reachable **and** migrations are at the latest version; also reports whether any API key is configured.
* `GET /api/v1/metrics` – workflow counts/success rate, LLM totals (calls, tokens, cloud-equivalent cost, fallbacks, retries), per-node P50/P95/P99, process-level LLM metrics.
* `GET /api/v1/audit?verify=true` – verifies the whole hash chain. Run it on a schedule; `ok:false` means the log was modified outside the application.

## Logs
JSON lines on stderr: `timestamp, level, message, trace_id, workflow_id, run_id, agent, node, duration_ms, status, error, model, tokens_in/out, estimated_cost_usd`. Secrets are redacted by key and by value pattern. `SCM_OBS__LOG_LEVEL=WARNING` quietens routine node logs.

## Common situations
| Symptom | Likely cause | Action |
|---|---|---|
| Runs end `no_vendor` | no listings, or every supplier below the quality/reliability floor | check `supplier_deliveries`/`Reliability`; lower `SCM_SUPPLIER__MIN_*` only deliberately |
| Runs end `policy_blocked` | lines violated policy (see audit `POLICY_VIOLATION`) | read the violation message; fix data (price/MOQ/capacity) |
| Many `LLM format exception` warnings | model returns invalid JSON | try a larger model (`SCM_LLM__PRIMARY_MODEL`), check Ollama; runs still complete via fallbacks |
| LLM calls skipped | breaker open after repeated failures | wait for `breaker_cooldown_seconds`; check Ollama |
| `database is locked` | another writer holding the lock > 20 s | SQLite is single-writer; avoid long external transactions |
| Workflow stuck `RUNNING` after a crash | worker died mid-run | its checkpoint is durable; start a new workflow (the old pending orders are superseded) |
| `/ready` = `migrations: behind` | DB older than the code | `python -m scm.migrations up` |

## Backups
SQLite WAL: copy the `.db` **and** `-wal`/`-shm` files with the app stopped, or use `sqlite3 .backup`. The checkpoint DB is disposable only if no workflow is awaiting approval.

## Tuning
All business thresholds/weights/timeouts are in `scm/settings.py`, overridable by `SCM_*` environment variables or `SCM_CONFIG_FILE` (see `config/engine.example.json`). Restart to apply.

## Performance notes
A workflow's deterministic work takes ≈ 0.5 s on the demo data (about 60 % of it is SQLite connection overhead on Windows; attempts to remove it were measured and rejected, see decision log #17). A real run is dominated by model calls (~18 s each on a CPU-only `llama3.2`). To cut P95: use a smaller/faster model, keep `llm_emails` off (templates), or run the API with `SCM_LLM_ENABLED=0` (deterministic engines only).
