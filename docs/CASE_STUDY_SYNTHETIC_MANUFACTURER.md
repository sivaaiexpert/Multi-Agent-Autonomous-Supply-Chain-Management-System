# Synthetic benchmark: manufacturer

> **SYNTHETIC BENCHMARK - not a customer case study.** The data is generated, the suppliers are simulated from generated delivery histories, and every result is a simulation. Nothing below is a measured customer outcome.

## Setup

- Business modelled: a manufacturer buying raw materials with longer lead times.
- Runs: 5 seeds ([101, 102, 103, 104, 105]) at scale 0.25, 15 SKUs, 3 suppliers, 60-day evaluation window.
- Protocol: pre-registered, sha256 `4bf614de4ccc0eab8b707c3a8d19a8f7725739c4a50cffd5f6c5b89b42ec9a39` (file match: True). Inclusion rule: every profile x seed is reported; no run is excluded or replaced after results are seen.
- Arms compared on identical demand and identical random supplier outcomes (common random numbers); no look-ahead.

## Results (mean over the seeds)

| Metric | manual buyer | rule-based | AI-assisted | AI-assisted, lean |
|---|---:|---:|---:|---:|
| Gross profit | 1,434,909 | 1,383,114 | 1,398,289 | 1,433,816 |
| Total cost | 3,969,591 | 4,109,576 | 4,326,611 | 4,321,170 |
| Cost per unit served | 183.24 | 187.49 | 190.65 | 190.00 |
| Stock-out events | 10.8 | 9.8 | 7.6 | 8.0 |
| Stock-out days | 127.8 | 117.2 | 70.4 | 67.4 |
| Fill rate | 86.3% | 87.6% | 92.7% | 93.2% |
| Average inventory value | 1,312,305 | 1,390,501 | 1,521,683 | 1,432,751 |
| Inventory turnover (annualised) | 17.9 | 17.3 | 16.7 | 17.9 |
| Safety-stock adherence | 84.8% | 86.0% | 91.4% | 91.6% |
| Manual touches (assumption-based) | 42 | 26 | 37 | 44 |

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
python -m enterprise.casestudy --profile manufacturer
```
