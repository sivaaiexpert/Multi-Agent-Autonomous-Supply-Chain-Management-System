"""Production readiness check:  python -m scripts.production_readiness_check [--strict] [--json]   (`make production-check`)

Checks the configuration and the data of THIS deployment and the evidence files in the repository. It cannot certify a deployment as secure or compliant:
it reports PASS / WARNING / FAIL per item with the reason. Exit code 1 if anything FAILs (with --strict, also on WARNING).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SECRET_PATTERNS = [(re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"), (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
                   (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9/+_\-]{24,}['\"]"), "hard-coded credential"),
                   (re.compile(r"sk-[A-Za-z0-9]{32,}"), "API-style secret key")]
SKIP_DIRS = {".venv", "__pycache__", ".git", "data", "outbox", "node_modules", ".ruff_cache", ".mypy_cache"}


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, area: str, name: str, status: str, detail: str) -> None:
        self.items.append({"area": area, "check": name, "status": status, "detail": detail})


def check_config(r: Report) -> None:
    from enterprise import secrets_store
    for it in secrets_store.validate_startup():
        r.add("configuration", it["name"], {"PASS": "PASS", "WARN": "WARNING", "FAIL": "FAIL"}[it["status"]], it["detail"])
    env = os.environ
    prod = env.get("SCM_ENV", "development").lower() == "production"
    r.add("configuration", "SCM_ENV=production", "PASS" if prod else "WARNING", "production mode" if prod else "not production: auth/secret checks are advisory")
    lim = env.get("SCM_RATE_LIMIT_PER_MIN")
    r.add("configuration", "rate limit", "PASS", f"{lim}/min per key" if lim else "default 1200/min per key plus a burst bucket")
    enforced = env.get("SCM_ENFORCE_QUOTAS") == "1"
    r.add("configuration", "quota enforcement", "PASS" if enforced else "WARNING", "plan quotas are enforced (HTTP 429)" if enforced else "quotas are advisory only (SCM_ENFORCE_QUOTAS=1 to enforce)")
    r.add("configuration", "TLS", "WARNING", "the application serves plain HTTP: terminate TLS in a reverse proxy / load balancer (this check cannot verify it)")
    llm_on = env.get("SCM_LLM_ENABLED", "0") == "1"
    r.add("configuration", "LLM", "PASS", "LLM enabled: ensure the provider is on the platform allow-list" if llm_on else "LLM disabled: deterministic engines only")


def check_source(r: Report) -> None:
    hits = []
    for p in ROOT.rglob("*"):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts) or p.suffix not in {".py", ".json", ".yml", ".yaml", ".toml", ".md", ".env", ".txt", ".cfg"}:
            continue
        if p.name == ".env.example" or "tests" in p.parts or p.name == "production_readiness_check.py":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, label in SECRET_PATTERNS:
            if rx.search(text):
                hits.append(f"{p.relative_to(ROOT)} ({label})")
    r.add("secrets", "no secrets in source", "FAIL" if hits else "PASS", ("found: " + "; ".join(hits[:5])) if hits else "no key/credential patterns found in tracked source types")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".gitignore").exists() else ""
    ignored = bool(re.search(r"(?m)^\.env\s*$", gi))
    r.add("secrets", ".env git-ignored", "PASS" if ignored else "FAIL", ".env is in .gitignore" if ignored else ".env must be git-ignored")
    has_example = (ROOT / ".env.example").exists()
    r.add("secrets", ".env.example present", "PASS" if has_example else "FAIL", "template for required configuration" if has_example else "missing")
    has_env = (ROOT / ".env").exists()
    r.add("secrets", ".env not in repo tree", "WARNING" if has_env else "PASS", "a local .env exists: make sure it is not committed" if has_env else "no .env file in the tree")


def check_data(r: Report) -> None:
    from enterprise import controlplane, tenancy
    from scm import audit, db, migrations
    cp_path = controlplane.control_db_path()
    if not Path(cp_path).exists():
        r.add("data", "control plane", "WARNING", f"{cp_path} does not exist yet (nothing provisioned / the API has not started)")
        return
    cp = controlplane.get()
    try:
        ok = sqlite3.connect(cp_path).execute("PRAGMA integrity_check").fetchone()[0]
        r.add("data", "control-plane integrity", "PASS" if ok == "ok" else "FAIL", f"PRAGMA integrity_check: {ok}")
    except sqlite3.Error as exc:
        r.add("data", "control-plane integrity", "FAIL", str(exc))
    tenants = [t for t in cp.list_tenants() if t["status"] != "DELETED"]
    r.add("data", "tenants", "PASS" if tenants else "WARNING", f"{len(tenants)} tenant(s)" if tenants else "no tenants provisioned")
    demo = [t["tenant_id"] for t in tenants if t["is_demo"]]
    prod_env = os.environ.get("SCM_ENV", "development").lower() == "production"
    if demo:
        r.add("data", "demo tenants", "WARNING" if prod_env else "PASS", f"demo tenants present: {demo}" + (" (remove from a production deployment)" if prod_env else ""))
    keys = sum(len(cp.list_keys(t["tenant_id"])) for t in tenants)
    r.add("access", "API keys", "PASS" if keys or os.environ.get("SCM_API_KEYS") else "WARNING", f"{keys} control-plane key(s)" if keys else "no keys: protected endpoints refuse all requests")
    for t in tenants:
        tid = t["tenant_id"]
        try:
            with tenancy.scope(tid, enforce_active=False):
                path = tenancy.current().db_path
                if not Path(path).exists():
                    r.add("data", f"tenant {tid}", "WARNING", "no workspace database")
                    continue
                with db.connect() as c:
                    integ = c.execute("PRAGMA integrity_check").fetchone()[0]
                    ver = migrations.current_version(c)
                chain = audit.verify_chain()
            good = integ == "ok" and ver == migrations.LATEST and chain["ok"]
            r.add("data", f"tenant {tid}: integrity/schema/audit", "PASS" if good else "FAIL",
                  f"integrity={integ}, schema v{ver} (latest {migrations.LATEST}), audit chain ok={chain['ok']} over {chain['checked']} events")
        except Exception as exc:
            r.add("data", f"tenant {tid}", "FAIL", f"{type(exc).__name__}: {exc}")
    backups = list(Path(os.environ.get("SCM_BACKUP_DIR", str(controlplane.data_root() / "backups"))).glob("*/MANIFEST.json"))
    if backups:
        age_h = (time.time() - max(b.stat().st_mtime for b in backups)) / 3600
        r.add("recovery", "recent backup", "PASS" if age_h <= 26 else "WARNING", f"{len(backups)} backup(s); newest is {age_h:.1f} h old")
    else:
        r.add("recovery", "recent backup", "WARNING", "no backup found: schedule enterprise.dr.backup_tenant (see docs/product/OPERATIONS.md)")


def check_evidence(r: Report) -> None:
    def fresh(path: Path, what: str, max_days: int = 30) -> None:
        if not path.exists():
            r.add("evidence", what, "WARNING", f"{path.relative_to(ROOT)} missing: run the matching make target")
            return
        age = (time.time() - path.stat().st_mtime) / 86400
        r.add("evidence", what, "PASS" if age <= max_days else "WARNING", f"{path.relative_to(ROOT)} generated {age:.1f} days ago")
    rep = ROOT / "evaluation" / "reports"
    fresh(rep / "experiment.json", "benchmark experiment (make evaluate)")
    fresh(rep / "loadtest.json", "load test (make load-test)")
    fresh(rep / "failure_suite.json", "failure tests (make chaos-test)")
    try:
        fs = json.loads((rep / "failure_suite.json").read_text(encoding="utf-8"))
        r.add("evidence", "failure tests pass", "PASS" if fs["passed"] == fs["total"] else "FAIL", f"{fs['passed']}/{fs['total']} scenarios passed")
    except (OSError, ValueError, KeyError):
        pass
    try:
        lt = json.loads((rep / "loadtest.json").read_text(encoding="utf-8"))
        worst = max(x["error_rate"] for x in lt["results"])
        r.add("evidence", "load-test error rate", "PASS" if worst == 0 else "WARNING" if worst < 0.05 else "FAIL",
              f"worst error rate {worst:.1%} across {[x['concurrent_workflows'] for x in lt['results']]} concurrent workflows (single machine)")
    except (OSError, ValueError, KeyError):
        pass
    try:
        from enterprise import experiments
        same = experiments.protocol_file_matches()
        r.add("evidence", "experiment protocol unchanged", "PASS" if same else "FAIL", "protocol.json matches the hash used for the reported results" if same else "protocol.json changed after the results")
    except Exception as exc:
        r.add("evidence", "experiment protocol unchanged", "WARNING", str(exc))
    for doc in ("docs/COMMERCIAL_READINESS.md", "docs/product/LIMITATIONS.md", "docs/product/SECURITY.md", "docs/THREAT_MODEL.md"):
        ok = (ROOT / doc).exists()
        r.add("documentation", doc, "PASS" if ok else "WARNING", "present" if ok else "missing")


def check_deployment(r: Report) -> None:
    r.add("deployment", "container / orchestration", "WARNING", "Dockerfile and docker-compose.yml are a starting point only, not a validated production deployment (no HA, no managed DB, no TLS)")
    r.add("deployment", "database", "WARNING", "SQLite per tenant: single node, no horizontal scaling; multi-node production needs a managed database")
    r.add("deployment", "ERP integrations", "WARNING", "only the mock adapter and a generic REST adapter (validated against mocks) exist; real ERP connectors are ADAPTER-READY, not validated")


def run() -> list[dict]:
    r = Report()
    for fn in (check_config, check_source, check_data, check_evidence, check_deployment):
        try:
            fn(r)
        except Exception as exc:
            r.add(fn.__name__, "check crashed", "FAIL", f"{type(exc).__name__}: {exc}")
    return r.items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit non-zero on WARNING too")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    items = run()
    counts = {s: sum(i["status"] == s for i in items) for s in ("PASS", "WARNING", "FAIL")}
    if a.json:
        print(json.dumps({"counts": counts, "items": items}, indent=2))
    else:
        for i in items:
            print(f"[{i['status']:<7}] {i['area']:<14} {i['check']:<46} {i['detail']}")
        print(f"\nPASS {counts['PASS']}   WARNING {counts['WARNING']}   FAIL {counts['FAIL']}")
        overall = "FAIL" if counts["FAIL"] else "WARNING" if counts["WARNING"] else "PASS"
        print(f"OVERALL: {overall}  (a PASS here is not a certification: see docs/COMMERCIAL_READINESS.md)")
    return 1 if counts["FAIL"] or (a.strict and counts["WARNING"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
