# Synthetic benchmark: seasonal

> **SYNTHETIC BENCHMARK - not a customer case study.** The data is generated, the suppliers are simulated from generated delivery histories, and every result is a simulation. Nothing below is a measured customer outcome.

## Setup

- Business modelled: a seasonal business with strong demand peaks.
- Runs: 5 seeds ([101, 102, 103, 104, 105]) at scale 0.25, 20 SKUs, 3 suppliers, 60-day evaluation window.
- Protocol: pre-registered, sha256 `4bf614de4ccc0eab8b707c3a8d19a8f7725739c4a50cffd5f6c5b89b42ec9a39` (file match: True). Inclusion rule: every profile x seed is reported; no run is excluded or replaced after results are seen.
- Arms compared on identical demand and identical random supplier outcomes (common random numbers); no look-ahead.

## Results (mean over the seeds)

| Metric | manual buyer | rule-based | AI-assisted | AI-assisted, lean |
|---|---:|---:|---:|---:|
| Gross profit | 722,177 | 670,079 | 726,292 | 744,240 |
| Total cost | 1,979,558 | 2,095,137 | 2,193,902 | 2,192,529 |
| Cost per unit served | 55.94 | 57.73 | 57.83 | 57.64 |
| Stock-out events | 24.4 | 18.4 | 5.8 | 5.6 |
| Stock-out days | 124.8 | 94.8 | 33.4 | 23.2 |
| Fill rate | 86.4% | 89.6% | 96.1% | 97.0% |
| Average inventory value | 613,844 | 736,038 | 843,397 | 612,055 |
| Inventory turnover (annualised) | 20.3 | 17.0 | 16.0 | 21.9 |
| Safety-stock adherence | 87.6% | 90.7% | 96.8% | 97.7% |
| Manual touches (assumption-based) | 70 | 33 | 34 | 47 |

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
python -m enterprise.casestudy --profile seasonal
```
