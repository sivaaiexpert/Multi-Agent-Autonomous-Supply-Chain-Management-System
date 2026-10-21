"""`make production`: start the API the way a production host would, but only after the readiness check has no FAIL.

    SCM_ENV=production SCM_SECRET_KEY=<fernet key> python -m scripts.run_production [--host 127.0.0.1] [--port 8000] [--workers 1]

This is a hardened LOCAL launcher, not a deployment: it does not provide TLS, process supervision, a managed database or high availability.
Put it behind a reverse proxy that terminates TLS and run it under a supervisor (systemd, a container orchestrator) - see docs/DEPLOYMENT.md.
SQLite per tenant is a single-node store, so the default is ONE worker process (the workflow engine keeps per-process state).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("SCM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SCM_PORT", "8000")))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WEB_CONCURRENCY", "1")))
    ap.add_argument("--skip-check", action="store_true", help="do not run the readiness check first (not recommended)")
    a = ap.parse_args(argv)
    os.environ["SCM_ENV"] = "production"
    from enterprise import secrets_store
    try:
        secrets_store.assert_startup_ok()
    except secrets_store.SecretError as exc:
        print(f"refusing to start: {exc}\nGenerate a key with:  python -c \"from enterprise.secrets_store import generate_secret_key as g; print(g())\"", file=sys.stderr)
        return 2
    if not a.skip_check:
        from scripts import production_readiness_check as chk
        items = chk.run()
        fails = [i for i in items if i["status"] == "FAIL"]
        for i in items:
            if i["status"] != "PASS":
                print(f"[{i['status']:<7}] {i['check']}: {i['detail']}")
        if fails:
            print(f"refusing to start: {len(fails)} readiness check(s) FAILED", file=sys.stderr)
            return 1
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("note: binding to a non-loopback address - put a TLS-terminating reverse proxy in front of this process")
    import uvicorn
    uvicorn.run("api.main:app", host=a.host, port=a.port, workers=a.workers, log_level="info", proxy_headers=True, forwarded_allow_ips=os.environ.get("SCM_FORWARDED_ALLOW_IPS", "127.0.0.1"),
                server_header=False, timeout_keep_alive=15, limit_concurrency=int(os.environ.get("SCM_LIMIT_CONCURRENCY", "200")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
