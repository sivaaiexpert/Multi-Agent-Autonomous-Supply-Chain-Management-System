# Business metrics

Every metric is computed by code in `enterprise/impact.py` / `enterprise/simulation.py` from stored records and carries a **provenance**:

| Provenance | Meaning |
|---|---|
| OBSERVED | read directly from the tenant's records (orders, stock, deliveries) |
| BACKTEST | a forecast replayed on history using only earlier data at each origin |
| SIMULATED | a policy replayed through the simulator with modelled supplier outcomes |
| ASSUMPTION_BASED | depends on inputs the user supplies (e.g. minutes per order line) |

and a **data provenance**: `SYNTHETIC` (generated) or `CUSTOMER_DATA`. Savings are never labelled "AI savings" unless the baseline is what the customer would really have done.

## Procurement (`GET /impact/procurement?baseline=`)

Baseline cost vs optimised cost for the same quantities, so differences are attributable to supplier selection / allocation only. Baselines: `manual_buyer` (lowest sticker price among listed quotes), `average_quote` (mean listed quote), `last_paid` (price of the previous PO for the SKU). Reported: baseline cost, optimised cost, savings and %, units, average lead time (both), lines without a stored decision snapshot. A negative saving is reported as is. `GET /procurement/comparison` adds the **effective** baseline cost after the units the baseline policy fails to cover and constraint violations (MOQ, capacity, policy) it would incur.

## Inventory (`GET /metrics`, `POST /simulations/backtest`)

OBSERVED now: safety-stock adherence (share of SKUs at or above safety stock), excess inventory units and value (stock above target), SKUs at high / critical stock-out risk.
SIMULATED over a window per policy arm: stock-out events (a SKU transitions from in stock to out of stock), stock-out days, units short, fill rate, average inventory units / value, turnover (annualised cost of goods consumed / average inventory value), excess value, safety-stock adherence, late-order rate, average lead time, purchase / holding / stock-out cost, gross profit.

Simulator assumptions (`SimConfig`): holding cost 20% of inventory value per year, 25% gross margin lost on unmet demand, forecasts refit weekly, supplier delivery outcomes drawn from each supplier's empirical history (common random numbers across arms), physical MOQ and capacity effects, no look-ahead. Change them with request parameters; they are echoed in every result.

## Forecast accuracy (`GET /impact/forecast?horizon=&origins=`)

Rolling-origin back-test per SKU and category: **MAE**, **RMSE**, **WAPE** (sum of absolute errors / sum of actuals), **MAPE**, **bias** (sum of forecast - actual / sum of actuals), compared with a naive last-7-day-mean baseline. SKUs with less than 28 days of history are not evaluated (the number evaluated is reported), not guessed.

## Suppliers (`GET /impact/suppliers`)

From delivery history, per supplier: on-time delivery rate, average lead time and its variance, quality / defect rate, order-fulfilment rate, cancellation rate, historical price and price volatility, a 0-100 reliability score and the recent trend of each; with the number of observations behind each figure so thin evidence is visible.

## Operations (`GET /impact/operations`)

OBSERVED cycle times (time to recommendation, to approval, to dispatch, workflow completion: median / P90 / mean), manual interventions (approvals, rejections, re-sources, edits), human-approval and automation rates. The effort comparison is ASSUMPTION_BASED (default 12 minutes per order line without the platform vs 5 minutes per reviewed batch with it); `manual_minutes_per_line` is a parameter.

## Snapshots

`POST /impact/snapshot` stores the headline KPIs in `business_metrics` with their provenance and time so trends can be shown without recomputation.

## Benchmark and experiment

The platform's policies are compared against manual and rule-based procurement on four synthetic customer profiles (retailer, manufacturer, distributor, seasonal) x five seeds under a pre-registered, hash-locked protocol: [../../evaluation/protocol.json](../../evaluation/protocol.json), results [../../evaluation/reports/experiment.json](../../evaluation/reports/experiment.json), narrative per profile in `docs/CASE_STUDY_SYNTHETIC_*.md`. Method and caveats: [ROI_METHODOLOGY.md](ROI_METHODOLOGY.md).
