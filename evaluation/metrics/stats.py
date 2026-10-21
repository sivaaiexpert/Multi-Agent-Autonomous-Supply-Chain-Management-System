"""Small, dependency-free statistics helpers for the evaluation reports."""
from __future__ import annotations

from statistics import mean
from typing import Iterable, Sequence


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100). None for an empty sample."""
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (k - lo))


def rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def safe_mean(values: Iterable[float]) -> float | None:
    xs = list(values)
    return mean(xs) if xs else None


def latency_summary(values: Sequence[float]) -> dict:
    return {"n": len(values), "p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95), "p99_ms": percentile(values, 99),
            "mean_ms": safe_mean(values), "max_ms": max(values) if values else None}


def precision_recall(tp: int, fp: int, fn: int) -> dict:
    return {"precision": rate(tp, tp + fp), "recall": rate(tp, tp + fn), "tp": tp, "fp": fp, "fn": fn}


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"
