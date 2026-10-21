# ROI methodology

**Purpose.** Show how a prospective customer could estimate the value of the platform from *their own* data, with every assumption visible. It is not a promise of savings, and nothing here is a measured customer result.

## Definitions

```
annual ROI   = (annual benefit - annual cost) / annual cost          (undefined when annual cost = 0)
annual benefit = realisation factor x annualised( gross-profit difference between the platform arm and the baseline arm over a simulated window )
annual cost    = licence + infrastructure + integration & support  (all supplied by the user)  + measured LLM cost x workflows per year
payback months = annual cost / (annual benefit / 12)                (undefined when benefit <= 0)
```

The benefit is decomposed into four components that sum to the simulated gross-profit difference (exactly when revenue per unit is the same in both arms; the response includes a `decomposition_check` that reports any difference):

| Component | Formula | Provenance |
|---|---|---|
| Purchase effect | (baseline cost per unit served - platform cost per unit served) x platform units served | SIMULATED |
| Service effect | (platform units served - baseline units served) x (revenue per unit - baseline cost per unit) | SIMULATED |
| Inventory carrying cost | baseline holding cost - platform holding cost (negative if the platform holds more stock) | SIMULATED |
| Buyer time | (baseline decisions x manual minutes - platform review decisions x review minutes) / 60 x hourly cost | ASSUMPTION_BASED |

## Assumptions (all explicit, all changeable: `POST /impact/roi`)

| Assumption | Default | Notes |
|---|---|---|
| Window | 60 days | length of the replayed history; benefits are annualised from it |
| Realisation factor | 0.5 | share of the simulated benefit expected to materialise; a haircut for imperfect execution |
| Gross margin | 25% | margin lost on unmet demand; used by the simulator and the service effect |
| Holding cost | 20% of inventory value per year | simulator assumption |
| Manual minutes per order line | 12 | without the platform |
| Review minutes per decision | 5 | with the platform |
| Hourly labour cost | 30 | currency units |
| Licence / infrastructure / integration & support | **0** | **not invented**: the user supplies their own; ROI is reported as undefined when there is no cost basis |
| LLM cost per workflow | 0 | replaced by the metered figure; 0 for local inference |

## Baseline arms

`manual` (lowest sticker price, fixed reorder rule), `rule_based` (reliability floor, cheapest eligible), `ai` (forecast + optimiser, service-first) and `ai_lean` (orders to forecast demand). **Choose the baseline that matches what the customer actually does**; a weak baseline inflates the benefit. All arms see identical demand and identical random supplier outcomes (common random numbers), start from the same stock, and use only information available at each decision date.

## The benchmark experiment (pre-registered)

- 4 profiles x 5 seeds = 20 synthetic units; arms manual / rule_based / ai / ai_lean; metrics fixed in advance.
- The protocol file's SHA-256 is recorded with the results; `protocol_file_matches` shows it was not edited afterwards. **Every unit is reported; no run is excluded or replaced.**
- Reported as paired differences with a bootstrap 95% confidence interval (2000 resamples, seed 7) and the count of units where the first arm is better.

What the 20 units showed (see `docs/CASE_STUDY_SYNTHETIC_*.md`): the lean AI policy had higher mean simulated gross profit than the manual policy (CI above zero, better in 14/20 units) and fewer stock-out days in 20/20; the default service-first AI policy had **lower** gross profit than the manual policy (CI below zero, better in 7/20) because it buys service with inventory. Results differ by profile and seed. This is a statement about generated data and the stated assumptions, not about any company.

## What would make it real

1. Import the customer's own history (`CUSTOMER_DATA`) and run the back-test on it.
2. Replace the assumptions with the customer's (margin, holding cost, labour, price paid).
3. Run the platform in shadow mode (recommendations only) and compare with what buyers actually ordered; that comparison is OBSERVED, not simulated.

## Known limits

Synthetic data; simulated supplier outcomes drawn from empirical delivery history; budget constraints are not simulated; demand shocks outside the history are not modelled; the realisation factor is a judgement, not an estimate; labour effects are assumption-based.
