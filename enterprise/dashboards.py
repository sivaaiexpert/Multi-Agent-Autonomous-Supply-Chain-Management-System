"""Dashboard read models: system, AI, business, tenant. Plain dicts so the API and the console render the same numbers."""
from __future__ import annotations

import threading
import time
from collections import deque

from scm import db, obs
from scm.llm_router import METRICS

from . import impact, usage


class ApiMetrics:
    """In-process request metrics (rate, errors, latency, availability) since the process started. Not persisted: use the control-plane `api_usage` rollups for history."""

    def __init__(self, maxlen: int = 20000):
        self.started = time.time()
        self._lock = threading.Lock()
        self._rows: deque[tuple[float, str, int, float, str]] = deque(maxlen=maxlen)

    def record(self, group: str, status: int, duration_ms: float, tenant: str) -> None:
        with self._lock:
            self._rows.append((time.time(), group, status, duration_ms, tenant))

    def summary(self, window_s: int = 300, tenant: str | None = None) -> dict:
        now = time.time()
        with self._lock:
            rows = [r for r in self._rows if now - r[0] <= window_s and (tenant is None or r[4] == tenant)]
            total_rows = [r for r in self._rows if tenant is None or r[4] == tenant]
        lat = [r[3] for r in rows]
        errs = sum(r[2] >= 500 for r in rows)
        all_errs = sum(r[2] >= 500 for r in total_rows)
        by_group: dict[str, dict] = {}
        for _, g, st, ms, _t in rows:
            b = by_group.setdefault(g, {"calls": 0, "errors_5xx": 0, "total_ms": 0.0})
            b["calls"] += 1
            b["errors_5xx"] += st >= 500
            b["total_ms"] += ms
        return {"window_s": window_s, "requests": len(rows), "requests_per_second": len(rows) / window_s, "error_rate_5xx": errs / len(rows) if rows else None,
                "latency_ms": {"p50": obs.percentile(lat, 50), "p95": obs.percentile(lat, 95), "p99": obs.percentile(lat, 99)},
                "availability_since_start": (1 - all_errs / len(total_rows)) if total_rows else None, "uptime_s": round(now - self.started, 1),
                "by_route_group": {k: {**v, "mean_ms": v["total_ms"] / v["calls"]} for k, v in by_group.items()},
                "note": "availability here means 'share of requests that did not fail with a 5xx' since this process started; it is not an externally measured uptime"}


API_METRICS = ApiMetrics()


def system() -> dict:
    return {"api": API_METRICS.summary(), "workers": {"note": "thread pool inside the API process"}}


def ai() -> dict:
    c = usage.llm_costs()
    with db.connect() as cn:
        statuses = {r[0]: r[1] for r in cn.execute("SELECT Status, COUNT(*) FROM llm_calls WHERE Tenant_ID=? GROUP BY Status", (obs.context().get("tenant_id") or "default",))}
    total = sum(statuses.values()) or 1
    return {"calls": c["total"]["calls"], "tokens_in": c["total"]["tokens_in"], "tokens_out": c["total"]["tokens_out"], "fallbacks": c["total"]["fallbacks"],
            "timeouts": statuses.get("timeout", 0), "structured_output_failures": statuses.get("invalid_output", 0), "repaired": statuses.get("repaired", 0),
            "errors": statuses.get("error", 0), "structured_output_validity": (statuses.get("ok", 0) + statuses.get("repaired", 0)) / total if statuses else None,
            "cloud_equivalent_cost_usd": c["total"]["cloud_equivalent_cost_usd"], "by_agent": c["by_agent"], "by_model": c["by_model"], "process_metrics": METRICS.snapshot()}


def business() -> dict:
    return {"metrics": impact.latest_metrics(), "procurement": {k: v for k, v in impact.procurement_kpis().items() if k != "lines_detail"},
            "operations": impact.operations_kpis(), "data_provenance": impact.data_provenance()}


def tenant() -> dict:
    u = usage.usage_report()
    with db.connect() as c:
        tid = obs.context().get("tenant_id") or "default"
        runs = {r[0]: r[1] for r in c.execute("SELECT Status, COUNT(*) FROM workflow_runs WHERE Tenant_ID=? GROUP BY Status", (tid,))}
        node_err = c.execute("SELECT COUNT(*), COALESCE(SUM(Status<>'success'),0) FROM node_runs WHERE Tenant_ID=?", (tid,)).fetchone()
    return {"usage": u, "workflows_by_status": runs, "node_errors": {"runs": node_err[0], "errors": node_err[1]}, "api": API_METRICS.summary(tenant=tid),
            "daily": usage.daily_series(), "business": {k: v for k, v in impact.operations_kpis().items() if k in ("human_approval_rate", "automation_rate", "manual_interventions")}}
