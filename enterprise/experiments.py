"""Reproducible business-value experiment: manual baseline vs rule-based baseline vs AI-assisted optimisation.

PRE-REGISTERED PROTOCOL. `PROTOCOL` (and `evaluation/protocol.json`, which must match it) fixes before any protocol run: which customer profiles, which seeds, the
catalogue scale, the policy arms, the comparisons, the simulation assumptions, the metrics and the inclusion rule ("every profile x seed is reported; nothing
is dropped after the results are seen"). Its SHA-256 is printed in every report. The seeds below were NOT used while the simulator or the policies were
being developed (exploratory runs used seed 11).

    python -m enterprise.experiments run --out evaluation/reports/experiment.json

Every result is SIMULATED on SYNTHETIC data. It validates the machinery and shows the trade-offs; it is not evidence of what any real customer would gain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from statistics import mean

from scm.forecast import DemandForecaster, MovingAverage, error_metrics

from . import profiles as P
from . import simulation as S

PROTOCOL: dict = {
    "version": 1,
    "title": "Manual vs rule-based vs AI-assisted procurement on synthetic customer profiles",
    "profiles": ["retailer", "manufacturer", "distributor", "seasonal"],
    "seeds": [101, 102, 103, 104, 105],
    "scale": 0.25,
    "arms": ["manual", "rule_based", "ai", "ai_lean"],
    "comparisons": [["ai", "manual"], ["ai", "rule_based"], ["ai_lean", "manual"], ["ai_lean", "rule_based"]],
    "simulation": {"eval_days": "profile default (60)", "refit_every": 7, "pack": 5, "holding_rate_annual": 0.20, "gross_margin": 0.25, "min_reliability_rule_based": 60.0},
    "metrics": ["gross_profit", "total_cost", "cost_per_unit_served", "stockout_events", "stockout_days", "fill_rate", "avg_inventory_value", "turnover_annualized",
                "avg_lead_time_days", "late_order_rate", "manual_touches", "excess_value_avg", "safety_stock_adherence"],
    "bootstrap": {"resamples": 2000, "seed": 7, "ci": 0.95},
    "inclusion_rule": "every profile x seed is reported; no run is excluded or replaced after results are seen",
    "known_limitations": ["synthetic data", "simulated supplier outcomes drawn from each supplier's empirical delivery history", "budget constraints are not simulated",
                          "manual effort is assumption-based (12 min per order line vs 5 min per reviewed batch)"],
}


def protocol_hash(p: dict | None = None) -> str:
    return hashlib.sha256(json.dumps(p or PROTOCOL, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def protocol_file() -> Path:
    return Path(__file__).resolve().parent.parent / "evaluation" / "protocol.json"


def write_protocol_file() -> Path:
    p = protocol_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**PROTOCOL, "sha256": protocol_hash()}, indent=2), encoding="utf-8")
    return p


def protocol_file_matches() -> bool:
    try:
        d = json.loads(protocol_file().read_text(encoding="utf-8"))
    except OSError:
        return False
    sha = d.pop("sha256", None)
    return d == json.loads(json.dumps(PROTOCOL)) and sha == protocol_hash()


def bootstrap_ci(values: list[float], resamples: int = 2000, seed: int = 7, ci: float = 0.95) -> dict:
    """Percentile bootstrap of the mean over independent units (profile x seed)."""
    if not values:
        return {"mean": None, "low": None, "high": None, "n": 0}
    rng = random.Random(seed)
    means = sorted(mean(rng.choice(values) for _ in values) for _ in range(resamples))
    lo, hi = means[int((1 - ci) / 2 * resamples)], means[min(resamples - 1, int((1 + ci) / 2 * resamples))]
    return {"mean": mean(values), "low": lo, "high": hi, "n": len(values), "all_positive": lo > 0, "all_negative": hi < 0}


def forecast_check(ds: P.Dataset, horizon: int = 14) -> dict:
    """Out-of-sample forecast accuracy of the platform's forecaster vs a naive baseline on the dataset's hidden future."""
    f, naive = DemandForecaster(), MovingAverage(7)
    a: list[float] = []
    p: list[float] = []
    n: list[float] = []
    for sku, hist in ds.history.items():
        fut = ds.truth["future_demand"][sku][:horizon]
        a += fut
        p += f.forecast(sku, hist, horizon).forecast
        n += naive.predict(hist, horizon)
    m, mn = error_metrics(a, p), error_metrics(a, n)
    bias = sum(x - y for x, y in zip(p, a)) / sum(a) if sum(a) else None
    return {"engine": {**m, "bias": bias}, "naive_last7_mean": mn, "wape_improvement_vs_naive": (1 - m["wape"] / mn["wape"]) if m["wape"] and mn["wape"] else None}


def run_experiment(protocol: dict | None = None, progress: bool = False) -> dict:
    pr = protocol or PROTOCOL
    runs = []
    t0 = time.perf_counter()
    for key in pr["profiles"]:
        for seed in pr["seeds"]:
            ds = P.generate(key, seed, pr["scale"])
            inp = S.from_dataset(ds)
            res = S.run_all_arms(inp, tuple(pr["arms"]))
            runs.append({"profile": key, "seed": seed, "skus": len(ds.skus), "suppliers": len(ds.suppliers), "eval_days": inp.config.eval_days,
                         "arms": {a: r["summary"].to_dict() for a, r in res.items()}, "forecast": forecast_check(ds)})
            if progress:
                print(f"  {key:13} seed {seed}: {len(ds.skus)} SKUs done", flush=True)
    deltas: dict = {}
    for ai, base in pr["comparisons"]:
        for metric in pr["metrics"]:
            per_unit = [(r["arms"][ai][metric] or 0) - (r["arms"][base][metric] or 0) for r in runs if r["arms"][ai].get(metric) is not None and r["arms"][base].get(metric) is not None]
            deltas.setdefault(f"{ai}_vs_{base}", {})[metric] = bootstrap_ci(per_unit, pr["bootstrap"]["resamples"], pr["bootstrap"]["seed"], pr["bootstrap"]["ci"])
    by_profile: dict = {}
    for key in pr["profiles"]:
        rs = [r for r in runs if r["profile"] == key]
        by_profile[key] = {arm: {m: mean((r["arms"][arm][m] or 0) for r in rs) for m in pr["metrics"]} for arm in pr["arms"]}
    wins = {f"{ai}_vs_{b}": {m: sum(((r["arms"][ai][m] or 0) > (r["arms"][b][m] or 0)) for r in runs) for m in ("gross_profit", "fill_rate")}
            | {m: sum(((r["arms"][ai][m] or 0) < (r["arms"][b][m] or 0)) for r in runs) for m in ("total_cost", "stockout_days", "avg_inventory_value", "cost_per_unit_served")}
            for ai, b in pr["comparisons"]}
    return {"protocol": pr, "protocol_sha256": protocol_hash(pr), "protocol_file_matches": protocol_file_matches(), "provenance": "SIMULATED", "data_provenance": P.DATA_PROVENANCE,
            "label": P.DEMO_LABEL, "units": len(runs), "duration_s": round(time.perf_counter() - t0, 1), "runs": runs, "by_profile": by_profile, "paired_deltas": deltas,
            "units_where_first_arm_is_better": wins}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="enterprise.experiments")
    ap.add_argument("command", choices=["run", "protocol"])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "evaluation" / "reports" / "experiment.json"))
    a = ap.parse_args(argv)
    if a.command == "protocol":
        print(write_protocol_file(), protocol_hash())
        return 0
    res = run_experiment(progress=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(f"protocol sha256 {res['protocol_sha256']}  (file matches: {res['protocol_file_matches']})  units: {res['units']}  -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
