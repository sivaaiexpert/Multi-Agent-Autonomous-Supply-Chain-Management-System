# AI system design

## 1. Principle

> LLM → structured proposal → schema validation → deterministic policy → optimisation engine → risk validation → human approval → execution → audit.

The model is a *writer*, not a *decider*. Everything that carries a unit, a price or a quantity is computed by code that can be unit-tested, replayed and explained.

## 2. Demand forecasting (`scm/forecast.py`)

Candidates (all deterministic, no heavy ML stack):

| Model | Idea | Needs |
|---|---|---|
| moving average | mean of the last *w* days | 1 point |
| weighted moving average | linearly increasing weights | 2 |
| exponential smoothing | level with α | 3 |
| Holt trend (damped, φ=0.9) | level + damped trend | 3 |
| seasonal baseline | level × weekday index | 2 seasons (14 days) |

**Selection.** Hold out the last `min(7, n/4)` days, fit on the rest, score RMSE, keep the best, **refit on all history** for the production forecast. **Confidence** = `max(0, 1 − WAPE) × min(1, n/56)`, capped at 0.3 below the 14-day minimum. **Metrics**: MAE, RMSE, MAPE, WAPE. MAPE divides by the actual, so it is returned as `None` when there is no non-zero actual or when fewer than 60 % of the days are non-zero (intermittent demand); WAPE (`Σ|e| / Σ actual`) is the safe alternative. Inputs are sanitised (negatives, NaN, strings → 0). Forecasts are persisted (`forecasts`) and memoised on a content hash (pure function ⇒ a cached answer can never be stale).

## 3. Stock-out risk engine (`scm/risk.py`)

For lead time *L* (mean of listed lead times), forecast daily mean *d*, residual std *σ_d* and lead-time std *σ_LT* (from supplier delivery variance):

```
μ_L   = Σ forecast[0..L]                      (extended with d beyond the horizon)
σ_L²  = L·σ_d² + (d·σ_LT)²
position = stock + open orders (IN_TRANSIT only)
P(stock-out) = 1 − Φ((position − μ_L)/σ_L)      (step function when σ_L = 0)
safety_stock = ⌈z·σ_L⌉  (z = 1.65 ⇒ 95 %)       dynamic ROP = ⌈μ_L + safety⌉
order-up-to = max(target, μ_L + d·review_period + safety)     quantity = ⌈(order-up-to − position)/pack⌉·pack
```

Levels: **CRITICAL** if stock = 0 with demand, or P ≥ 0.90, or cover < L/2 while position < μ_L; **HIGH** P ≥ 0.60 or cover < L; **MEDIUM** P ≥ 0.25, position < dynamic ROP, or stock < static ROP; else **LOW**. Thresholds are configuration. Without demand history the engine falls back to the legacy ratio rule (`basis = static_rule`) so imported data keeps working.

*Note:* dispatch currently simulates instant receipt (approving adds stock immediately), so "open orders" only counts orders an ERP integration marks `IN_TRANSIT`.

## 4. Supplier intelligence (`scm/suppliers.py`)

Per supplier from `supplier_deliveries`: on-time rate, mean lead time, lead-time variance, quality / defect rate, historical price, price volatility (mean CV per SKU), fulfilment rate, cancellation rate. **Score** (0–100) = `100·(w_otd·OTD + w_fill·fill + w_q·quality + w_lt·1/(1+CV_lead) + w_c·(1−cancel))` with configurable weights (defaults 0.35/0.20/0.25/0.10/0.10), then **shrunk toward the vendor's static reliability** with prior strength 5: `score = (n·observed + 5·prior)/(n+5)`. Two deliveries cannot make a supplier look perfect or terrible. Cached for 60 s and invalidated by every new delivery.

## 5. Supplier optimisation (`scm/optimizer.py`)

```
effective unit cost  e_i = p_i · (1 + a·exposure_i + b·lead_i + c·(1 − rel_i·qual_i))      exposure_i = max(0, lead_i − days_of_cover)
minimise   Σ e_i·x_i + F·|{i : x_i > 0}|
subject to Σ x_i ≥ Q,  x_i ∈ {0} ∪ [MOQ_i, cap_i],  x_i ≤ s·T (large orders, ≥ 2 suppliers),  Σ p_i x_i ≤ budget,  eligibility floors
```

Quantities are whole packs. For a fixed supplier subset the problem is linear, so the optimum is *MOQ first, then cheapest effective cost up to the caps*. The subset is found by enumerating all non-empty subsets of the (≤ 8) cheapest eligible suppliers: **exact**, deterministic (ties → fewer suppliers → higher reliability → vendor id). `test_exhaustive_search_matches_brute_force_objective` verifies the optimum against an independent brute force; `test_randomised_plans_always_satisfy_every_constraint` checks 300 random instances. No MILP library is justified at this size; adding `scipy.optimize.milp` would only matter beyond ~15 suppliers per SKU. If anything raises, a deterministic cheapest-valid-quote plan is used and flagged.

**Honest trade-off.** Measured in `docs/EVALUATION_REPORT.md`: against a constraint-aware cheapest baseline the default 70 % share cap costs a purchase premium for a small reduction in the probability of losing most of an order. It is a *policy choice*; set `SCM_OPTIMIZER__MAX_SUPPLIER_SHARE=1.0` to disable it.

## 6. LLM layer

* **Router** (`llm_router.py`): `primary → secondary → fallback` from configuration; the model picked in the UI is always primary; `complex` tasks may use `complex_model_override`.
* **Policy per call**: one shared deadline (`timeout`) across retries and models; ≤ `retries` retries on *transient* errors only (connection/OS/timeout-class) with exponential backoff and deterministic jitter; a timeout is *not* retried on the same model (it already spent the budget); permanent errors go straight to the next model.
* **Circuit breaker** per model: N consecutive failures → open for a cooldown → one half-open probe.
* **Metrics**: `request_count, failure_count, timeout_count, fallback_count, retry_count, breaker_rejections, tokens, latency percentiles`, plus a persisted `llm_calls` row per call (status ok / repaired / invalid_output / timeout / error, latency, tokens, cost, retries, fallback).
* **Structured output**: `parse_json → required keys/types → pydantic schema (sanitises text, truncates oversize) → on failure regex salvage → re-validate → deterministic fallback with "System Warning: LLM Format Exception Handled"`. Repaired output goes through the *same* schema and, like every output, never reaches a decision.
* **Untrusted data**: `sanitize_untrusted` (NFKC-normalise, strip tags/control/bidi characters, neutralise injection phrases, drop template characters, length-limit) + `<untrusted_data>` wrapping + an explicit notice in every system prompt.
* **Cost**: tokens from Ollama counters (else chars/4); two figures are tracked: the real marginal cost (configurable, default $0 for local inference) and a *cloud-equivalent* price for the same tokens (default GPT-4o list price) so savings from local inference are visible.
* **TTFT** is not measured: calls are non-streaming.

## 7. Policy validator (`scm/policy.py`)

Blocks: duplicate lines, unknown SKU/vendor, no listing, non-positive / non-integer quantity, non-positive/absurd price, **price deviating > 25 % from the catalogue quote**, quantity > 3× target, MOQ violation, capacity exceeded, budget exceeded, negative lead time. Warns (and escalates the human review): optimiser fallback, low forecast confidence (< 30 %), lead time longer than stock cover, line value above the elevated / executive thresholds. Everything is re-derived from the database, never trusted from the proposal.

## 8. Idempotency and transactions

`purchase_requests.Idempotency_Key = sha256(workflow | round | item | vendor)` (UNIQUE) + `purchase_orders.Idempotency_Key` (partial unique index). `create_pending_pos` creates a whole proposal batch in one transaction, replays return the existing PO, an invalid line rolls the whole batch back. `finalize_orders` changes status, receives stock (guarded against double receipt) and writes the audit row in one transaction. API-level `Idempotency-Key` makes `POST /workflows` safe to retry. A 12-thread race test proves exactly one order is created.

## 9. Audit (`scm/audit.py`)

Append-only (SQLite triggers abort UPDATE/DELETE) and **hash-chained** (`hash = sha256(prev_hash ‖ event)`); `verify_chain()` pinpoints the first tampered row even if someone drops the triggers. Payloads are redacted. Business writes and their audit row share a transaction. Event types: `WORKFLOW_STARTED, FORECAST_GENERATED, RISK_DETECTED, SUPPLIER_SELECTED, POLICY_VIOLATION, PROCUREMENT_PROPOSED, HUMAN_APPROVAL_REQUESTED, HUMAN_APPROVED, HUMAN_REJECTED, HUMAN_RESOURCED, PROCUREMENT_EDITED, DISPATCH_STARTED, DISPATCH_COMPLETED, DISPATCH_FAILED, PROMPT_INJECTION_DETECTED, LLM_FALLBACK, ACCESS_DENIED, WORKFLOW_COMPLETED, WORKFLOW_FAILED`.

## 10. Events (`scm/events.py`)

`InventoryLow, DemandForecastUpdated, StockoutRiskDetected, ProcurementCreated, ApprovalRequested, ProcurementApproved, ProcurementRejected, DispatchCompleted` follow the **transactional outbox** pattern: persisted to `domain_events` first, then delivered to in-process subscribers (one failing subscriber never blocks another or the workflow). `EventBus.relay(Transport)` ships unpublished rows to Redis / RabbitMQ / Kafka / SQS by implementing `send(event)`; no domain code changes. No broker is bundled because nothing here needs one.
