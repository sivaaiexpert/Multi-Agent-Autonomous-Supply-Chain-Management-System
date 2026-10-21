"""Reproducible load test: N concurrent complete workflow lifecycles over real HTTP against a real uvicorn server.

    python -m evaluation.loadtest [--levels 10 50 100] [--tenants 1] [--out evaluation/reports/loadtest.json] [--md docs/LOAD_TEST_REPORT.md]

What one "workflow" is: POST /workflows -> poll until it waits at the human gate -> approve -> poll until COMPLETED.
Everything runs on this machine with SYNTHETIC data and a deterministic (no-LLM) decision path, so the numbers describe THIS machine's
single process with SQLite. They are not production-scale numbers. Measured: throughput, per-request and end-to-end latency (P50/P95/P99),
error rate, queue wait (submit -> a worker picks it up), DB time share (connection-held time / wall time) and LLM latency (only when SCM_LLM_ENABLED=1).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


def summarise(xs: list[float]) -> dict:
    return {"n": len(xs), "mean_ms": round(statistics.fmean(xs), 2) if xs else None, "p50_ms": pct(xs, 50), "p95_ms": pct(xs, 95), "p99_ms": pct(xs, 99), "max_ms": round(max(xs), 2) if xs else None}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Instruments:
    """Wrap the engine's seams to measure queue wait and DB connection-held time without changing behaviour."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.submit: dict[str, float] = {}
        self.queue_ms: list[float] = []
        self.db_held_s = 0.0
        self.db_write_held_s = 0.0
        self.db_conns = 0
        self.busy_errors = 0

    def install(self) -> None:
        from scm import db
        from scm.services import WorkflowService
        inst = self
        orig_submit, orig_run, orig_connect = WorkflowService._submit, WorkflowService._run, db.connect

        def submit(svc, wf, *a, **k):
            with inst.lock:
                inst.submit[wf] = time.perf_counter()
            return orig_submit(svc, wf, *a, **k)

        def run(svc, wf, *a, **k):
            t0 = inst.submit.get(wf)
            if t0 is not None:
                with inst.lock:
                    inst.queue_ms.append((time.perf_counter() - t0) * 1000)
            return orig_run(svc, wf, *a, **k)

        from contextlib import contextmanager

        @contextmanager
        def connect(*a, **k):
            t = time.perf_counter()
            try:
                with orig_connect(*a, **k) as c:
                    yield c
            except Exception as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    with inst.lock:
                        inst.busy_errors += 1
                raise
            finally:
                held = time.perf_counter() - t
                with inst.lock:
                    inst.db_held_s += held
                    inst.db_conns += 1
                    if k.get("write") or (a and a[0] is True):
                        inst.db_write_held_s += held

        WorkflowService._submit, WorkflowService._run, db.connect = submit, run, connect  # type: ignore[method-assign,assignment]
        self._orig = (orig_submit, orig_run, orig_connect)

    def reset(self) -> None:
        with self.lock:
            self.submit.clear(); self.queue_ms.clear()
            self.db_held_s = self.db_write_held_s = 0.0
            self.db_conns = self.busy_errors = 0


def one_lifecycle(base: str, op_key: str, ap_key: str, timeout: float) -> dict:
    import httpx
    out: dict[str, Any] = {"ok": False, "outcome": None, "lat": {}, "error": None}
    t_all = time.perf_counter()
    try:
        with httpx.Client(base_url=base, timeout=timeout) as c:
            t = time.perf_counter()
            r = c.post("/api/v1/workflows", headers={"X-API-Key": op_key})
            out["lat"]["start_ms"] = (time.perf_counter() - t) * 1000
            if r.status_code not in (200, 202):
                out["error"] = f"start:{r.status_code}"
                return out
            wf = r.json()["workflow_id"]
            t_gate = time.perf_counter()
            status = None
            while time.perf_counter() - t_all < timeout:
                t = time.perf_counter()
                g = c.get(f"/api/v1/workflows/{wf}", headers={"X-API-Key": op_key})
                out["lat"].setdefault("poll_ms", []).append((time.perf_counter() - t) * 1000)
                if g.status_code != 200:
                    out["error"] = f"poll:{g.status_code}"
                    return out
                status = g.json()["status"]
                if status in ("AWAITING_APPROVAL", "COMPLETED", "FAILED", "REJECTED"):
                    break
                time.sleep(0.05)
            out["to_gate_ms"] = (time.perf_counter() - t_gate) * 1000
            if status == "AWAITING_APPROVAL":
                t = time.perf_counter()
                a = c.post(f"/api/v1/workflows/{wf}/approve", headers={"X-API-Key": ap_key}, json={"reason": "load test"})
                out["lat"]["approve_ms"] = (time.perf_counter() - t) * 1000
                if a.status_code not in (200, 202):
                    out["error"] = f"approve:{a.status_code}"
                    return out
                while time.perf_counter() - t_all < timeout:
                    g = c.get(f"/api/v1/workflows/{wf}", headers={"X-API-Key": op_key})
                    status = g.json()["status"]
                    if status in ("COMPLETED", "FAILED", "REJECTED"):
                        break
                    time.sleep(0.05)
            out["outcome"] = status
            out["ok"] = status in ("COMPLETED", "AWAITING_APPROVAL")           # a clean policy-driven end is not an error
            if not out["ok"]:
                out["error"] = f"status:{status}"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:160]
    out["e2e_ms"] = (time.perf_counter() - t_all) * 1000
    return out


def run(levels: list[int], tenants: int, timeout: float, scale: float) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="scm_load_"))
    os.environ.update({"SCM_DATA_DIR": str(tmp / "data"), "SCM_CONTROL_DB": str(tmp / "data" / "control.db"), "SCM_ENV": "development",
                       "SCM_RATE_LIMIT_PER_MIN": "1000000", "SCM_ALLOW_PRIVATE_URLS": "1", "SCM_MAINTENANCE": "0", "SCM_OBS__LOG_LEVEL": "ERROR"})
    os.environ.setdefault("SCM_LLM_ENABLED", "0")
    from enterprise import controlplane, profiles, secrets_store, tenancy
    os.environ["SCM_SECRET_KEY"] = secrets_store.generate_secret_key()
    from scm import db
    db.DB_PATH, db.OUTBOX = tmp / "default.db", tmp / "default_outbox"
    controlplane.reset()
    from scm.security import Role
    cp = controlplane.get()
    keys = []
    for i in range(tenants):
        tid = f"load{i}"
        tenancy.provision(tid, f"Load {i}", "enterprise", "SANDBOX", is_demo=True)
        ds = profiles.generate("retailer", 10 + i, scale)
        with tenancy.scope(tid):
            profiles.load_into_current_tenant(ds)
        for role, uid in ((Role.OPERATOR, f"op{i}"), (Role.APPROVER, f"ap{i}")):
            cp.create_user(tid, uid, f"{uid}@example.com", uid, role)
        keys.append((cp.issue_key(tid, f"op{i}", "load")[0], cp.issue_key(tid, f"ap{i}", "load")[0]))
    inst = Instruments()
    inst.install()
    import uvicorn
    from api.main import create_app
    app = create_app(keyring={})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", workers=1))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    base = f"http://127.0.0.1:{port}"
    results = []
    try:
        for n in levels:
            inst.reset()
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(one_lifecycle, base, *keys[i % tenants], timeout) for i in range(n)]
                rs = [f.result() for f in futs]
            wall = time.perf_counter() - t0
            ok = [r for r in rs if r["ok"]]
            errs = [r for r in rs if not r["ok"]]
            outcomes: dict[str, int] = {}
            for r in rs:
                outcomes[str(r["outcome"])] = outcomes.get(str(r["outcome"]), 0) + 1
            reqs = sum(1 + len(r["lat"].get("poll_ms", [])) + ("approve_ms" in r["lat"]) for r in rs)
            results.append({
                "concurrent_workflows": n, "tenants": tenants, "wall_s": round(wall, 2),
                "completed": sum(r["outcome"] == "COMPLETED" for r in rs), "errors": len(errs), "error_rate": round(len(errs) / n, 4),
                "outcomes": outcomes, "error_samples": sorted({r["error"] for r in errs if r["error"]})[:5],
                "throughput_workflows_per_s": round(len(ok) / wall, 2), "http_requests": reqs, "http_requests_per_s": round(reqs / wall, 1),
                "latency": {"workflow_end_to_end": summarise([r["e2e_ms"] for r in ok]), "time_to_human_gate": summarise([r["to_gate_ms"] for r in ok if "to_gate_ms" in r]),
                            "start_request": summarise([r["lat"]["start_ms"] for r in rs if "start_ms" in r["lat"]]),
                            "poll_request": summarise([x for r in rs for x in r["lat"].get("poll_ms", [])]),
                            "approve_request": summarise([r["lat"]["approve_ms"] for r in rs if "approve_ms" in r["lat"]])},
                "queue_wait": summarise(list(inst.queue_ms)),
                "database": {"connections": inst.db_conns, "connection_held_s": round(inst.db_held_s, 2), "write_held_s": round(inst.db_write_held_s, 2),
                             "db_time_share_of_wall_pct": round(100 * inst.db_held_s / wall, 1), "lock_errors": inst.busy_errors,
                             "note": "share can exceed 100% because many connections overlap; it is connection-held time, not CPU"},
                "llm": {"enabled": os.environ.get("SCM_LLM_ENABLED") == "1", "latency": "not measured: deterministic decision path (no model calls)" if os.environ.get("SCM_LLM_ENABLED") != "1" else "see /api/v1/dashboards/ai"},
            })
            print(f"  {n:>4} concurrent: {results[-1]['throughput_workflows_per_s']} wf/s, e2e P95 {results[-1]['latency']['workflow_end_to_end']['p95_ms']} ms, "
                  f"errors {len(errs)}/{n}", flush=True)
    finally:
        server.should_exit = True
        th.join(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)
    return {"environment": {"python": platform.python_version(), "os": platform.platform(), "cpu_count": os.cpu_count(), "server": "uvicorn, 1 process, SQLite (WAL), local loopback",
                            "data": "SYNTHETIC retailer profile", "scale": scale, "tenants": tenants,
                            "scope": "Single-machine load test of one process. NOT a production-scale or capacity claim."},
            "results": results, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def markdown(rep: dict) -> str:
    env = rep["environment"]
    L = ["# Load test report", "", f"Generated {rep['generated_at']}. Reproduce with `make load-test` (`python -m evaluation.loadtest`).", "",
         "**Scope.** " + env["scope"], "",
         f"- Environment: Python {env['python']}, {env['os']}, {env['cpu_count']} logical CPUs; {env['server']}",
         f"- Data: {env['data']} (scale {env['scale']}), {env['tenants']} tenant(s); decisions made by the deterministic path (no LLM calls)",
         "- A *workflow* is a complete lifecycle: start, wait at the human gate, approve, reach COMPLETED - all over real HTTP.", "",
         "| Concurrent workflows | Wall s | Completed | Errors | Error rate | Throughput wf/s | E2E P50 / P95 / P99 ms | Queue wait P50 / P95 ms | DB lock errors |",
         "|---:|---:|---:|---:|---:|---:|---|---|---:|"]
    for r in rep["results"]:
        e, q = r["latency"]["workflow_end_to_end"], r["queue_wait"]
        L.append(f"| {r['concurrent_workflows']} | {r['wall_s']} | {r['completed']} | {r['errors']} | {r['error_rate']:.1%} | {r['throughput_workflows_per_s']} | "
                 f"{e['p50_ms']} / {e['p95_ms']} / {e['p99_ms']} | {q['p50_ms']} / {q['p95_ms']} | {r['database']['lock_errors']} |")
    L += ["", "## Detail", ""]
    for r in rep["results"]:
        L += [f"### {r['concurrent_workflows']} concurrent", "", f"- Outcomes: {r['outcomes']}", f"- Error samples: {r['error_samples'] or 'none'}",
              f"- HTTP: {r['http_requests']} requests, {r['http_requests_per_s']} req/s",
              f"- Start request P50/P95/P99: {r['latency']['start_request']['p50_ms']} / {r['latency']['start_request']['p95_ms']} / {r['latency']['start_request']['p99_ms']} ms",
              f"- Poll request P50/P95/P99: {r['latency']['poll_request']['p50_ms']} / {r['latency']['poll_request']['p95_ms']} / {r['latency']['poll_request']['p99_ms']} ms",
              f"- Time to human gate P50/P95: {r['latency']['time_to_human_gate']['p50_ms']} / {r['latency']['time_to_human_gate']['p95_ms']} ms",
              f"- Database: {r['database']['connections']} connections, held {r['database']['connection_held_s']} s (writes {r['database']['write_held_s']} s) = {r['database']['db_time_share_of_wall_pct']}% of wall time. {r['database']['note']}",
              f"- LLM latency: {r['llm']['latency']}", ""]
    L += ["## How to read this", "", "- Queue wait is the time between a workflow being submitted and a worker thread starting it (the service has a small fixed worker pool, so it grows with concurrency).",
          "- Numbers depend on the machine and on what else is running; compare runs on the same machine only.",
          "- This does not establish how many tenants or workflows a production deployment can serve: that requires a test on the target infrastructure."]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", type=int, nargs="+", default=[10, 50, 100])
    ap.add_argument("--tenants", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--scale", type=float, default=0.06)
    ap.add_argument("--out", default=str(ROOT / "evaluation" / "reports" / "loadtest.json"))
    ap.add_argument("--md", default=str(ROOT / "docs" / "LOAD_TEST_REPORT.md"))
    a = ap.parse_args(argv)
    print(f"load test: levels {a.levels}, {a.tenants} tenant(s)")
    rep = run(a.levels, a.tenants, a.timeout, a.scale)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    Path(a.md).write_text(markdown(rep), encoding="utf-8")
    print(f"wrote {a.out} and {a.md}")
    return 1 if any(r["error_rate"] > 0.05 for r in rep["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
