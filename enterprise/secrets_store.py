"""Secret handling.

* Platform secret: `SCM_SECRET_KEY` (a Fernet key) encrypts tenant secrets at rest (webhook signing secrets, ERP credentials, hosted-LLM keys).
  It is read from the environment / a secret manager, never from source code. In `SCM_ENV=production` its absence is a startup FAILURE; in
  development a key is generated into `<data>/.dev_secret_key` (git-ignored, owner-only) with a loud warning.
* Tenant secrets live in the tenant database, encrypted. They are never returned by any API (only a reference id) and never logged.
* API keys are not secrets we can recover: only their SHA-256 hash is stored.
"""
from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from scm import db, obs
from scm.obs import log_event

from .controlplane import data_root


class SecretError(Exception):
    pass


def is_production() -> bool:
    return os.environ.get("SCM_ENV", "development").lower() == "production"


def generate_secret_key() -> str:
    return Fernet.generate_key().decode()


def _platform_key() -> bytes:
    raw = os.environ.get("SCM_SECRET_KEY", "").strip()
    if raw:
        try:
            Fernet(raw.encode())
        except Exception as exc:
            raise SecretError("SCM_SECRET_KEY is not a valid Fernet key (generate one with: python -m enterprise.admin gen-secret-key)") from exc
        return raw.encode()
    if is_production():
        raise SecretError("SCM_SECRET_KEY is required when SCM_ENV=production")
    path = data_root() / ".dev_secret_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generate_secret_key(), encoding="utf-8")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        log_event("SCM_SECRET_KEY not set: generated a DEVELOPMENT key (never use this outside development)", level=30, path=str(path))
    return path.read_text(encoding="utf-8").strip().encode()


def _fernet() -> Fernet:
    return Fernet(_platform_key())


def put_secret(value: str, secret_id: str | None = None) -> str:
    """Encrypt and store a tenant secret; returns its id (a reference, not the value)."""
    if not value:
        raise SecretError("secret value must not be empty")
    sid = secret_id or "sec_" + obs.new_id()[:16]
    token = _fernet().encrypt(value.encode("utf-8")).decode()
    tid = obs.context().get("tenant_id") or "default"
    with db.connect(write=True) as c:
        c.execute("INSERT INTO secrets (Tenant_ID, Secret_ID, Ciphertext, Created_At) VALUES (?,?,?,?) ON CONFLICT(Tenant_ID, Secret_ID) DO UPDATE SET "
                  "Ciphertext=excluded.Ciphertext, Created_At=excluded.Created_At", (tid, sid, token, datetime.now().isoformat(timespec="seconds")))
    return sid


def get_secret(secret_id: str) -> str:
    tid = obs.context().get("tenant_id") or "default"
    with db.connect() as c:
        row = c.execute("SELECT Ciphertext FROM secrets WHERE Tenant_ID=? AND Secret_ID=?", (tid, secret_id)).fetchone()
    if row is None:
        raise SecretError("unknown secret")
    try:
        return _fernet().decrypt(row[0].encode()).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError("secret cannot be decrypted with the current platform key") from exc


def delete_secret(secret_id: str) -> None:
    tid = obs.context().get("tenant_id") or "default"
    with db.connect(write=True) as c:
        c.execute("DELETE FROM secrets WHERE Tenant_ID=? AND Secret_ID=?", (tid, secret_id))


# ------------------------------------------------------------------ startup validation
def validate_startup(env: dict | None = None) -> list[dict]:
    """Returns [{name, status: PASS|WARN|FAIL, detail}] for the secrets / configuration the process needs. Never prints a secret value."""
    e = env if env is not None else os.environ
    out: list[dict] = []
    prod = e.get("SCM_ENV", "development").lower() == "production"

    def add(name: str, status: str, detail: str) -> None:
        out.append({"name": name, "status": status, "detail": detail})
    key = (e.get("SCM_SECRET_KEY") or "").strip()
    if key:
        try:
            Fernet(key.encode())
            add("SCM_SECRET_KEY", "PASS", "present and a valid Fernet key")
        except Exception:
            add("SCM_SECRET_KEY", "FAIL", "set but not a valid Fernet key")
    else:
        add("SCM_SECRET_KEY", "FAIL" if prod else "WARN", "missing (required in production; a development key is generated otherwise)")
    add("SCM_ENV", "PASS" if prod else "WARN", "production" if prod else "development: some checks are advisory")
    keys_env = (e.get("SCM_API_KEYS") or "").strip()
    add("SCM_API_KEYS", "PASS" if keys_env else "WARN", "legacy env keys configured" if keys_env else "none (control-plane keys are used)")
    if prod and e.get("SCM_ALLOW_PRIVATE_URLS", "0") == "1":
        add("SCM_ALLOW_PRIVATE_URLS", "WARN", "private-network webhook/ERP targets are allowed (SSRF guard relaxed)")
    if prod and e.get("SCM_CORS_ORIGINS", "") == "*":
        add("SCM_CORS_ORIGINS", "FAIL", "wildcard CORS is not allowed in production")
    return out


def assert_startup_ok() -> None:
    failed = [r for r in validate_startup() if r["status"] == "FAIL"]
    if failed:
        raise SecretError("startup validation failed: " + "; ".join(f"{r['name']}: {r['detail']}" for r in failed))


def dev_key_path() -> Path:
    return data_root() / ".dev_secret_key"
