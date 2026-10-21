"""Usage metering, cost accounting and plan quotas (billing-READY, not billing: no prices are defined and no payment system exists).

Dimensions metered per tenant: workflows, LLM tokens (in/out) and cost, API calls (control-plane counters), integration calls (ERP pushes, webhook deliveries),
storage (bytes on disk) and users. Plans (pilot / professional / enterprise) are named quota sets: product concepts, not hard-coded pricing.

Cost model, stated explicitly:
  * LLM cost       measured per call (`llm_calls`): the cloud-equivalent price of the tokens AND the actual marginal cost (default $0 for local inference)
  * compute cost   ASSUMPTION: node execution seconds x `compute_rate_per_cpu_hour` (default 0.05 USD; override with SCM_COMPUTE_RATE_PER_CPU_HOUR)
  * integration    ASSUMPTION: calls x `integration_cost_per_call` (default 0: no basis for a number)
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from scm import db, obs

from . import controlplane, tenancy


@dataclass(frozen=True)
class Plan:
    name: str
    workflows_per_month: int | None
    api_calls_per_day: int | None
    llm_tokens_per_month: int | None
    storage_mb: int | None
    users: int | None
    outbound_deliveries_per_month: int | None


PLANS = {"pilot": Plan("pilot", 20, 2_000, 500_000, 500, 5, 2_000), "professional": Plan("professional", 200, 50_000, 5_000_000, 5_000, 25, 50_000),
         "enterprise": Plan("enterprise", None, None, None, None, None, None)}          # None = not capped by the plan


class QuotaExceeded(Exception):
    pass


def compute_rate() -> float:
    return float(os.environ.get("SCM_COMPUTE_RATE_PER_CPU_HOUR", "0.05"))


def integration_rate() -> float:
    return float(os.environ.get("SCM_INTEGRATION_COST_PER_CALL", "0"))


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _month_start() -> str:
    return datetime.now().strftime("%Y-%m-01")


def record_usage(dimension: str, quantity: float, cost_usd: float = 0.0, ref: str | None = None) -> None:
    with db.connect(write=True) as c:
        c.execute("INSERT INTO usage_events (Tenant_ID, Ts, Dimension, Quantity, Cost_USD, Ref) VALUES (?,?,?,?,?,?)",
                  (_tenant(), datetime.now().isoformat(timespec="seconds"), dimension, quantity, cost_usd, ref))


def llm_costs(since: str | None = None) -> dict:
    """LLM cost broken down by model, agent, workflow and day (measured, from `llm_calls`)."""
    since = since or "0000"
    out: dict = {"total": {}, "by_model": {}, "by_agent": {}, "by_workflow": {}, "by_day": {}}
    with db.connect() as c:
        rows = c.execute("SELECT Model, Agent, Workflow_ID, substr(Ts,1,10), COUNT(*), SUM(Tokens_In), SUM(Tokens_Out), SUM(Cost_USD), SUM(Fallback), SUM(Status IN ('error','timeout','invalid_output')) "
                         "FROM llm_calls WHERE Tenant_ID=? AND Ts>=? GROUP BY Model, Agent, Workflow_ID, substr(Ts,1,10)", (_tenant(), since)).fetchall()
    def add(bucket: dict, key, r) -> None:
        b = bucket.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cloud_equivalent_cost_usd": 0.0, "fallbacks": 0, "failures": 0})
        b["calls"] += r[4]; b["tokens_in"] += r[5] or 0; b["tokens_out"] += r[6] or 0; b["cloud_equivalent_cost_usd"] += r[7] or 0.0; b["fallbacks"] += r[8] or 0; b["failures"] += r[9] or 0
    for r in rows:
        add(out["by_model"], r[0] or "?", r)
        add(out["by_agent"], r[1] or "?", r)
        add(out["by_workflow"], r[2] or "-", r)
        add(out["by_day"], r[3], r)
        add(out["total"], "all", r)
    out["total"] = out["total"].get("all", {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cloud_equivalent_cost_usd": 0.0, "fallbacks": 0, "failures": 0})
    out["actual_marginal_cost_usd"] = 0.0
    out["note"] = "cloud_equivalent prices the same tokens at the configured reference rate; the actual marginal cost of local inference is 0 unless llm.local_cost_per_m is set"
    return out


def compute_costs(workflow_id: str | None = None, since: str | None = None) -> dict:
    sql, params = "SELECT COALESCE(SUM(Duration_MS),0), COUNT(*) FROM node_runs WHERE Tenant_ID=?", [_tenant()]
    if workflow_id:
        sql, params = sql + " AND Workflow_ID=?", [*params, workflow_id]
    if since:
        sql, params = sql + " AND Started_At>=?", [*params, since]
    with db.connect() as c:
        ms, n = c.execute(sql, params).fetchone()
    hours = ms / 3_600_000
    return {"node_runs": n, "cpu_seconds": ms / 1000, "cost_usd": hours * compute_rate(), "assumed_rate_usd_per_cpu_hour": compute_rate(), "provenance": "ASSUMPTION_BASED"}


def integration_costs(workflow_id: str | None = None, since: str | None = None) -> dict:
    with db.connect() as c:
        erp = c.execute("SELECT COUNT(*) FROM erp_dispatches WHERE Tenant_ID=? AND Attempts>0" + (" AND PO_ID IN (SELECT PO_ID FROM purchase_orders WHERE Workflow_ID=?)" if workflow_id else ""),
                        (_tenant(), workflow_id) if workflow_id else (_tenant(),)).fetchone()[0]
        hooks = 0 if workflow_id else c.execute("SELECT COALESCE(SUM(Attempts),0) FROM outbound_deliveries WHERE Tenant_ID=? AND Created_At>=?", (_tenant(), since or "0000")).fetchone()[0]
    calls = erp + hooks
    return {"erp_calls": erp, "webhook_delivery_attempts": hooks, "cost_usd": calls * integration_rate(), "assumed_cost_per_call_usd": integration_rate(), "provenance": "ASSUMPTION_BASED"}


def workflow_cost(workflow_id: str) -> dict:
    llm = llm_costs()["by_workflow"].get(workflow_id, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cloud_equivalent_cost_usd": 0.0})
    comp, integ = compute_costs(workflow_id), integration_costs(workflow_id)
    return {"workflow_id": workflow_id, "llm": llm, "compute": comp, "integration": integ,
            "total_cloud_equivalent_usd": llm["cloud_equivalent_cost_usd"] + comp["cost_usd"] + integ["cost_usd"]}


def storage_bytes() -> int:
    try:
        d = tenancy.current().db_path.parent
    except tenancy.NoTenantScope:
        return 0
    return sum(f.stat().st_size for f in Path(d).rglob("*") if f.is_file()) if d.exists() else 0


def usage_report(since: str | None = None) -> dict:
    since = since or _month_start()
    tid = _tenant()
    with db.connect() as c:
        wf = c.execute("SELECT COUNT(*), COALESCE(SUM(Status='FAILED'),0) FROM workflow_runs WHERE Tenant_ID=? AND Created_At>=?", (tid, since)).fetchone()
        deliveries = c.execute("SELECT COUNT(*) FROM outbound_deliveries WHERE Tenant_ID=? AND Created_At>=?", (tid, since)).fetchone()[0]
    llm = llm_costs(since)
    controlplane.flush_api_usage()
    try:
        users = len(controlplane.get().list_users(tid))
        plan_name = controlplane.get().get_tenant(tid)["plan"]
        api = controlplane.get().api_usage(tid, 31)
    except controlplane.ControlPlaneError:
        users, plan_name, api = 0, "enterprise", []
    today = datetime.now().strftime("%Y-%m-%d")
    api_today = sum(r["calls"] for r in api if r["day"] == today)
    plan = PLANS[plan_name]
    used = {"workflows_per_month": wf[0], "api_calls_per_day": api_today, "llm_tokens_per_month": llm["total"]["tokens_in"] + llm["total"]["tokens_out"],
            "storage_mb": round(storage_bytes() / 1_048_576, 2), "users": users, "outbound_deliveries_per_month": deliveries}
    lim = asdict(plan)
    quotas = {k: {"used": used[k], "limit": lim[k], "pct": (used[k] / lim[k]) if lim[k] else None, "exceeded": bool(lim[k] and used[k] > lim[k])} for k in used}
    return {"tenant_id": tid, "plan": plan_name, "period_start": since, "usage": used, "quotas": quotas, "workflows_failed": wf[1], "llm": llm["total"],
            "costs": {"llm_cloud_equivalent_usd": llm["total"]["cloud_equivalent_cost_usd"], "compute_usd": compute_costs(since=since)["cost_usd"],
                      "integration_usd": integration_costs(since=since)["cost_usd"]},
            "api_calls_30d": sum(r["calls"] for r in api), "api_errors_30d": sum(r["errors"] for r in api),
            "billing": "usage is metered per tenant and plan; no prices are defined and no payment system exists"}


def check_quota(dimension: str) -> None:
    """Raises QuotaExceeded when SCM_ENFORCE_QUOTAS=1 and the tenant's plan limit for `dimension` is used up. Off by default (advisory)."""
    if os.environ.get("SCM_ENFORCE_QUOTAS", "0") != "1":
        return
    q = usage_report()["quotas"].get(dimension)
    if q and q["limit"] is not None and q["used"] >= q["limit"]:
        raise QuotaExceeded(f"plan limit reached for {dimension}: {q['used']}/{q['limit']}")


def daily_series(days: int = 14) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.connect() as c:
        wf = {r[0]: r[1] for r in c.execute("SELECT substr(Created_At,1,10), COUNT(*) FROM workflow_runs WHERE Tenant_ID=? AND Created_At>=? GROUP BY 1", (_tenant(), since))}
    llm = llm_costs(since)["by_day"]
    return [{"day": d, "workflows": wf.get(d, 0), "llm_calls": llm.get(d, {}).get("calls", 0), "llm_cost_usd": llm.get(d, {}).get("cloud_equivalent_cost_usd", 0.0)}
            for d in sorted(set(wf) | set(llm))]
