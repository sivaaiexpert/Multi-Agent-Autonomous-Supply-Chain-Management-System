"""Case-study generator: turns the pre-registered benchmark results into a document, and refuses to say more than the numbers do.

    python -m enterprise.casestudy [--profile retailer] [--out docs/CASE_STUDY_SYNTHETIC_RETAILER.md]

Every figure is read from evaluation/reports/experiment.json (produced by `make evaluate`). The document is titled and labelled as a SYNTHETIC BENCHMARK,
never as a customer case study; sentences that would claim a customer outcome are not generated.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "evaluation" / "reports" / "experiment.json"
PROFILE_BLURB = {"retailer": "a retailer with many fast-moving SKUs and several suppliers", "manufacturer": "a manufacturer buying raw materials with longer lead times",
                 "distributor": "a distributor with a wide catalogue and thin margins", "seasonal": "a seasonal business with strong demand peaks"}
ARM_NAME = {"manual": "manual buyer (lowest sticker price, fixed reorder rule)", "rule_based": "rule-based (reliability filter, cheapest eligible)", "ai": "AI-assisted (default: service-first)",
            "ai_lean": "AI-assisted, lean (orders to forecast demand)"}


def money(x: float) -> str:
    return f"{x:,.0f}"


def generate(profile: str, report: Path = REPORT) -> str:
    if not report.exists():
        raise SystemExit(f"{report} not found: run `make evaluate` first")
    ex = json.loads(report.read_text(encoding="utf-8"))
    if profile not in ex["by_profile"]:
        raise SystemExit(f"profile must be one of {sorted(ex['by_profile'])}")
    arms = ex["by_profile"][profile]
    runs = [r for r in ex["runs"] if r["profile"] == profile]
    seeds = sorted(r["seed"] for r in runs)
    L = [f"# Synthetic benchmark: {profile}", "", "> **SYNTHETIC BENCHMARK - not a customer case study.** The data is generated, the suppliers are simulated from generated delivery histories, "
         "and every result is a simulation. Nothing below is a measured customer outcome.", "",
         "## Setup", "", f"- Business modelled: {PROFILE_BLURB.get(profile, profile)}.", f"- Runs: {len(runs)} seeds ({seeds}) at scale {ex['protocol']['scale']}, {runs[0]['skus']} SKUs, {runs[0]['suppliers']} suppliers, "
         f"{runs[0]['eval_days']}-day evaluation window.", f"- Protocol: pre-registered, sha256 `{ex['protocol_sha256']}` (file match: {ex['protocol_file_matches']}). Inclusion rule: {ex['protocol']['inclusion_rule']}.",
         "- Arms compared on identical demand and identical random supplier outcomes (common random numbers); no look-ahead.", ""]
    L += ["## Results (mean over the seeds)", "", "| Metric | " + " | ".join(ARM_NAME[a].split(" (")[0] for a in ex["protocol"]["arms"]) + " |", "|---|" + "---:|" * len(ex["protocol"]["arms"])]
    fmts = [("Gross profit", "gross_profit", money), ("Total cost", "total_cost", money), ("Cost per unit served", "cost_per_unit_served", lambda v: f"{v:.2f}"), ("Stock-out events", "stockout_events", lambda v: f"{v:.1f}"),
            ("Stock-out days", "stockout_days", lambda v: f"{v:.1f}"), ("Fill rate", "fill_rate", lambda v: f"{100 * v:.1f}%"), ("Average inventory value", "avg_inventory_value", money),
            ("Inventory turnover (annualised)", "turnover_annualized", lambda v: f"{v:.1f}"), ("Safety-stock adherence", "safety_stock_adherence", lambda v: f"{100 * v:.1f}%"),
            ("Manual touches (assumption-based)", "manual_touches", lambda v: f"{v:.0f}")]
    for label, key, f in fmts:
        L.append(f"| {label} | " + " | ".join(f(arms[a][key]) for a in ex["protocol"]["arms"]) + " |")
    L += ["", "Arms: " + "; ".join(f"**{ARM_NAME[a].split(' (')[0]}** = {ARM_NAME[a]}" for a in ex["protocol"]["arms"]) + ".", ""]
    L += ["## Across all profiles and seeds (paired differences, bootstrap 95% CI)", "", "| Comparison | Gross profit difference | 95% CI | Better in | Stock-out days difference | Better in |", "|---|---:|---|---:|---:|---:|"]
    for name, d in ex["paired_deltas"].items():
        b = ex["units_where_first_arm_is_better"][name]
        g, s = d["gross_profit"], d["stockout_days"]
        L.append(f"| {name.replace('_vs_', ' vs ')} | {money(g['mean'])} | {money(g['low'])} to {money(g['high'])} | {b['gross_profit']}/{ex['units']} | {s['mean']:.1f} | {b['stockout_days']}/{ex['units']} |")
    L += ["", "## What the numbers support", ""]
    lean, ai = ex["paired_deltas"]["ai_lean_vs_manual"], ex["paired_deltas"]["ai_vs_manual"]
    if lean["gross_profit"]["low"] > 0:
        L.append(f"- The lean AI policy earned more simulated gross profit than the manual policy in the pooled comparison (difference {money(lean['gross_profit']['mean'])}, 95% CI excludes zero), "
                 f"while having fewer stock-out days in {ex['units_where_first_arm_is_better']['ai_lean_vs_manual']['stockout_days']} of {ex['units']} runs.")
    else:
        L.append("- The pooled gross-profit comparison of the lean AI policy against the manual policy does NOT exclude zero.")
    if ai["gross_profit"]["high"] < 0:
        L.append(f"- The default (service-first) AI policy earned LESS simulated gross profit than the manual policy (difference {money(ai['gross_profit']['mean'])}); it buys protection against stock-outs with inventory.")
    L += ["- Results differ by profile and seed; consult the per-run data in `evaluation/reports/experiment.json` before drawing any conclusion for a specific business.", "",
          "## What the numbers do not support", "", "- They do not predict savings for any real company: the demand, suppliers and costs are synthetic and the assumptions are listed in the protocol.",
          "- They do not show that the AI components, rather than the optimisation and policy rules, caused the differences (the arms differ in policy, not only in the use of a model).",
          "- Manual effort figures are assumption-based (12 min per order line vs 5 min per reviewed batch), not measured.", "",
          "## Reproduce", "", "```", "make evaluate", "python -m enterprise.casestudy --profile " + profile, "```", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="retailer")
    ap.add_argument("--out", default=None)
    ap.add_argument("--report", default=str(REPORT))
    a = ap.parse_args(argv)
    text = generate(a.profile, Path(a.report))
    out = Path(a.out) if a.out else ROOT / "docs" / f"CASE_STUDY_SYNTHETIC_{a.profile.upper()}.md"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
