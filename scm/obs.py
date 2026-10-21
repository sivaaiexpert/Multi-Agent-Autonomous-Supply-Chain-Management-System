"""Observability: correlation ids, structured JSON logging with secret redaction, node spans persisted to SQLite, LLM cost accounting."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from statistics import quantiles

from . import db
from .settings import get_settings

_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("scm_ctx", default=None)

SECRET_KEYS = re.compile(r"(api[_-]?key|secret|password|passwd|authorization|credential|bearer|(^|_)token$)", re.I)
SECRET_VALUES = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._\-]{8,}|sk-[A-Za-z0-9]{16,}|scm_[A-Za-z0-9]{20,})")
REDACTED = "***REDACTED***"


def new_id() -> str:
    return uuid.uuid4().hex


def bind(**fields) -> contextvars.Token:
    """Attach correlation fields (trace_id, workflow_id, run_id, agent...) to everything logged on this task/thread."""
    return _ctx.set({**(_ctx.get() or {}), **{k: v for k, v in fields.items() if v is not None}})


def unbind(token: contextvars.Token) -> None:
    _ctx.reset(token)


def context() -> dict:
    return dict(_ctx.get() or {})


def redact(obj, _depth: int = 0):
    """Recursively mask secret-looking keys and token-looking values (applied to every log line and audit payload)."""
    if _depth > 8:
        return "…"
    if isinstance(obj, dict):
        return {k: (REDACTED if SECRET_KEYS.search(str(k)) else redact(v, _depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return SECRET_VALUES.sub(REDACTED, obj)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"timestamp": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"), "level": record.levelname,
                "logger": record.name, "message": record.getMessage(), **(_ctx.get() or {}), **getattr(record, "fields", {})}
        if record.exc_info:
            base["error"] = str(record.exc_info[1])
        return json.dumps(redact(base), default=str, ensure_ascii=False)


_configured = False


def logger() -> logging.Logger:
    global _configured
    log = logging.getLogger("scm")
    if not _configured:
        cfg = get_settings().obs
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter() if cfg.log_json else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.handlers[:] = [handler]
        log.setLevel(cfg.log_level.upper())
        log.propagate = False
        _configured = True
    return log


def log_event(message: str, level: int = logging.INFO, **fields) -> None:
    logger().log(level, message, extra={"fields": fields})


# ------------------------------------------------------------------ cost
def estimate_cost(tokens_in: int, tokens_out: int, model: str = "") -> dict:
    """Two numbers on purpose: what this call actually cost (local inference ~ $0) and what the same tokens would cost on a cloud model."""
    cfg = get_settings().llm
    cloud = tokens_in / 1e6 * cfg.price_in_per_m + tokens_out / 1e6 * cfg.price_out_per_m
    local = (tokens_in + tokens_out) / 1e6 * cfg.local_cost_per_m
    return {"actual_usd": round(local, 8), "cloud_equivalent_usd": round(cloud, 8)}


# ------------------------------------------------------------------ spans
class Span:
    def __init__(self, agent: str, node: str):
        self.agent, self.node, self.status = agent, node, "success"
        self.error: str | None = None
        self.model: str | None = None
        self.tokens_in, self.tokens_out, self.cost = 0, 0, 0.0
        self.t0, self.started = time.perf_counter(), datetime.now().isoformat(timespec="milliseconds")
        self.span_id = new_id()

    def add_llm(self, model: str | None, tokens_in: int, tokens_out: int) -> None:
        self.model = model or self.model
        self.tokens_in += int(tokens_in or 0)
        self.tokens_out += int(tokens_out or 0)
        self.cost = estimate_cost(self.tokens_in, self.tokens_out, self.model or "")["cloud_equivalent_usd"]

    def fail(self, exc: BaseException | str) -> None:
        self.status, self.error = "error", str(exc)[:500]


@contextmanager
def span(agent: str, node: str):
    """Time a graph node: emits a structured log line and (when enabled) one `node_runs` row. Never raises because of telemetry."""
    sp = Span(agent, node)
    token = bind(agent=agent, node=node)
    try:
        yield sp
    except BaseException as exc:
        if sp.status == "success" and type(exc).__name__ != "GraphInterrupt":
            sp.fail(exc)
        raise
    finally:
        duration = (time.perf_counter() - sp.t0) * 1000
        ctx = context()
        log_event("node finished", agent=agent, node=node, duration_ms=round(duration, 1), status=sp.status, error=sp.error,
                  model=sp.model, tokens_in=sp.tokens_in, tokens_out=sp.tokens_out, estimated_cost_usd=sp.cost)
        if get_settings().obs.persist_spans:
            try:
                with db.connect(write=True) as c:
                    c.execute("INSERT INTO node_runs (Span_ID, Trace_ID, Workflow_ID, Agent, Node, Started_At, Duration_MS, Status, Error, Model, "
                              "Tokens_In, Tokens_Out, Cost_USD, Tenant_ID) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (sp.span_id, ctx.get("trace_id"), ctx.get("workflow_id"), agent, node, sp.started, round(duration, 2), sp.status,
                               sp.error, sp.model, sp.tokens_in, sp.tokens_out, sp.cost, ctx.get("tenant_id") or "default"))
            except Exception as exc:  # telemetry must never break the workflow
                logger().warning("span persistence failed: %s", exc)
        unbind(token)


def record_llm_call(agent: str, model: str, status: str, latency_ms: float, tokens_in: int, tokens_out: int, retries: int = 0,
                    fallback: bool = False) -> None:
    ctx = context()
    cost = estimate_cost(tokens_in, tokens_out, model)["cloud_equivalent_usd"]
    try:
        with db.connect(write=True) as c:
            c.execute("INSERT INTO llm_calls (Trace_ID, Workflow_ID, Agent, Model, Status, Latency_MS, Tokens_In, Tokens_Out, Cost_USD, Retries, "
                      "Fallback, Ts, Tenant_ID) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ctx.get("trace_id"), ctx.get("workflow_id"), agent, model, status, round(latency_ms, 1), tokens_in, tokens_out, cost,
                       retries, int(fallback), datetime.now().isoformat(timespec="milliseconds"), ctx.get("tenant_id") or "default"))
    except Exception as exc:
        logger().warning("llm call persistence failed: %s", exc)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    qs = quantiles(sorted(values), n=100, method="inclusive")
    return float(qs[min(98, max(0, int(round(p)) - 1))])


def node_latency_summary(workflow_id: str | None = None) -> list[dict]:
    sql = "SELECT Agent, Node, Duration_MS, Status FROM node_runs"
    params: tuple = ()
    if workflow_id:
        sql, params = sql + " WHERE Workflow_ID=?", (workflow_id,)
    d = db.df(sql, params)
    out = []
    for (agent, node), g in d.groupby(["Agent", "Node"]):
        v = g.Duration_MS.tolist()
        out.append({"agent": agent, "node": node, "runs": len(v), "errors": int((g.Status != "success").sum()),
                    "p50_ms": percentile(v, 50), "p95_ms": percentile(v, 95), "p99_ms": percentile(v, 99)})
    return out
