# Demo guide

Everything in a demo is labelled **DEMO / SYNTHETIC DATA**: the data is generated, the ERP is the in-repo mock, and the demo tenant can never be switched to production. Present it as an illustration of the product's behaviour, not as customer evidence.

## One command

```bash
make demo        # python -m enterprise.demo      (about a minute; uses a temporary data directory)
```

It runs the whole lifecycle once, printing live computed values (transcript saved to `evaluation/reports/demo_run.txt`):

1. **Tenant and data** - a demo tenant loaded with a synthetic retailer profile; data-quality gate.
2. **Onboarding** - organisation, policies, simulation, review; production stays off.
3. **Inventory risk today** - SKUs at high / critical stock-out risk (OBSERVED).
4. **Before vs after** - manual buyer vs rule-based vs AI vs AI-lean replayed on the same history (SIMULATED), then the current decision vs a lowest-sticker-price buyer (OBSERVED) - negative savings are shown as they are.
   **4c. The 20 pre-registered benchmark runs** - the aggregate with confidence intervals, so a single seed is not mistaken for the typical outcome.
5. **What-if** - demand +30%.
6. **Workflow -> tiered approval -> mock ERP** - a real LangGraph run, the approval chain (configurable tiers; the demo policy uses one APPROVER step; multi-step chains enforce different approvers), orders pushed to the mock ERP idempotently.
7. **Decision trace and explanations** - reasons generated from stored data only.
8. **ROI** - simulated benefit with *illustrative placeholder* cost assumptions (clearly marked; replace with the prospect's).
9. **Usage and reports** - metered usage and seven downloadable reports (CSV).

Options: `--scale 0.25` (bigger dataset), `--seed N`, `--keep` (keep the data directory), `--quick`.

## Interactive version

```bash
make dev     # API on :8000   (docs at /docs)
make ui      # console on :8501 - Operations Centre, Provisioning, Executive, Business Impact
```

The **Business Impact** page shows KPIs with provenance, the back-test, what-if sliders, ROI with editable assumptions, usage / cost, downloadable reports and the decision trace. The console runs against the default tenant's local database (seeded demo data); the multi-tenant API is what customers integrate with.

For a prospect-facing walk-through of the API, regenerate real captured calls with `python -m scripts.capture_api_examples` ([API_EXAMPLES.md](API_EXAMPLES.md)).

## Suggested narrative (10 minutes)

1. *Problem*: buyers choose suppliers by sticker price and fixed reorder points. Show step 4b: what that policy leaves uncovered and which constraints it violates.
2. *Control*: show the policy, the approval chain and that nothing is ordered without a human decision (step 6).
3. *Trust*: open the decision trace; every reason is traceable to data (step 7). Show the audit trail.
4. *Evidence*: step 4 and 4c. Say plainly: simulated, synthetic, mixed by arm; the claim to test on their data is the lean policy's stock-out reduction and its profit effect, not a savings number.
5. *Integration*: the API guide, signed webhooks, the ERP adapter (mock today; their ERP is the first integration project).
6. *Next*: import their data (preview + quality report), run the same back-test on it, then shadow mode.

## Things not to say

- That the mock ERP is SAP / Oracle / Dynamics / NetSuite, or that a connector exists.
- That synthetic savings are customer savings, or that the benchmark proves the AI (rather than the optimiser and the policy) causes the effect.
- That the load test shows production scale, or that the Docker files are a production deployment.
