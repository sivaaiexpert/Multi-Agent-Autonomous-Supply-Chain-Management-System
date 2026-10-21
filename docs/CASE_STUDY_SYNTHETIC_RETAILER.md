# Synthetic benchmark: retailer

> **SYNTHETIC BENCHMARK - not a customer case study.** The data is generated, the suppliers are simulated from generated delivery histories, and every result is a simulation. Nothing below is a measured customer outcome.

## Setup

- Business modelled: a retailer with many fast-moving SKUs and several suppliers.
- Runs: 5 seeds ([101, 102, 103, 104, 105]) at scale 0.25, 30 SKUs, 4 suppliers, 60-day evaluation window.
- Protocol: pre-registered, sha256 `4bf614de4ccc0eab8b707c3a8d19a8f7725739c4a50cffd5f6c5b89b42ec9a39` (file match: True). Inclusion rule: every profile x seed is reported; no run is excluded or replaced after results are seen.
- Arms compared on identical demand and identical random supplier outcomes (common random numbers); no look-ahead.

## Results (mean over the seeds)

| Metric | manual buyer | rule-based | AI-assisted | AI-assisted, lean |
|---|---:|---:|---:|---:|
| Gross profit | 2,461,190 | 2,273,507 | 2,266,077 | 2,553,667 |
| Total cost | 6,384,355 | 6,836,191 | 6,665,389 | 6,813,220 |
| Cost per unit served | 62.42 | 64.14 | 63.98 | 62.53 |
| Stock-out events | 35.4 | 17.0 | 2.0 | 2.4 |
| Stock-out days | 128.8 | 53.6 | 73.4 | 3.4 |
| Fill rate | 90.8% | 96.2% | 93.3% | 99.9% |
| Average inventory value | 1,395,348 | 1,802,115 | 1,906,511 | 1,171,570 |
| Inventory turnover (annualised) | 28.6 | 22.9 | 21.0 | 36.5 |
| Safety-stock adherence | 91.9% | 96.4% | 95.9% | 99.7% |
| Manual touches (assumption-based) | 157 | 49 | 40 | 59 |

Arms: **manual buyer** = manual buyer (lowest sticker price, fixed reorder rule); **rule-based** = rule-based (reliability filter, cheapest eligible); **AI-assisted** = AI-assisted (default: service-first); **AI-assisted, lean** = AI-assisted, lean (orders to forecast demand).

## Across all profiles and seeds (paired differences, bootstrap 95% CI)

| Comparison | Gross profit difference | 95% CI | Better in | Stock-out days difference | Better in |
|---|---:|---|---:|---:|---:|
| ai vs manual | -58,100 | -109,794 to -13,944 | 7/20 | -79.5 | 20/20 |
| ai vs rule_based | 33,258 | -327 to 63,687 | 14/20 | -43.3 | 16/20 |
| ai_lean vs manual | 36,802 | 4,507 to 74,592 | 14/20 | -97.5 | 20/20 |
| ai_lean vs rule_based | 128,161 | 80,740 to 182,344 | 17/20 | -61.2 | 20/20 |

## What the numbers support

- The lean AI policy earned more simulated gross profit than the manual policy in the pooled comparison (difference 36,802, 95% CI excludes zero), while having fewer stock-out days in 20 of 20 runs.
- The default (service-first) AI policy earned LESS simulated gross profit than the manual policy (difference -58,100); it buys protection against stock-outs with inventory.
- Results differ by profile and seed; consult the per-run data in `evaluation/reports/experiment.json` before drawing any conclusion for a specific business.

## What the numbers do not support

- They do not predict savings for any real company: the demand, suppliers and costs are synthetic and the assumptions are listed in the protocol.
- They do not show that the AI components, rather than the optimisation and policy rules, caused the differences (the arms differ in policy, not only in the use of a model).
- Manual effort figures are assumption-based (12 min per order line vs 5 min per reviewed batch), not measured.

## Reproduce

```
make evaluate
python -m enterprise.casestudy --profile retailer
```
