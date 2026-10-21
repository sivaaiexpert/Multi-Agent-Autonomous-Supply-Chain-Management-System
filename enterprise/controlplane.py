"""Control plane: who the customers are and how requests are routed to them.

It deliberately holds ONLY identity and routing data: tenants, users, API keys (hashed), a platform audit trail and per-day API usage rollups.
All business data (inventory, orders, policies, integrations, ...) lives in the tenant's own database, so a bug in a business query can never
cross a tenant boundary. Every table here carries `tenant_id`.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from scm import obs
from scm.security import (DEFAULT_TENANT, PLATFORM_TENANT, Principal, Role, assignable_roles, generate_key, hash_key)

TENANT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED = {PLATFORM_TENANT, "admin", "api", "system", "null", "none"}
PLANS = ("pilot", "professional", "enterprise")
MODES = ("SANDBOX", "PRODUCTION")


class ControlPlaneError(Exception):
    pass


class NotFoundError(ControlPlaneError):
    pass


class ConflictError(ControlPlaneError):
    pass


def data_root() -> Path:
    from scm import db
    return Path(os.environ.get("SCM_DATA_DIR") or (db.ROOT / "data"))


def control_db_path() -> Path:
    return Path(os.environ.get("SCM_CONTROL_DB") or data_root() / "control.db")


def valid_tenant_id(tid: object) -> bool:
    return isinstance(tid, str) and bool(TENANT_ID_RE.match(tid)) and (tid not in RESERVED or tid == DEFAULT_TENANT) and ".." not in tid


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT NOT NULL CHECK (plan IN ('pilot','professional','enterprise')),
    mode TEXT NOT NULL CHECK (mode IN ('SANDBOX','PRODUCTION')), status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED','DELETED')),
    is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, retention_days INTEGER NOT NULL DEFAULT 365
);
CREATE TABLE IF NOT EXISTS users (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id), user_id TEXT NOT NULL, email TEXT NOT NULL, display_name TEXT NOT NULL,
    role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id), user_id TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
    label TEXT, created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_keys_tenant ON api_keys (tenant_id);
CREATE TABLE IF NOT EXISTS platform_audit (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS platform_audit_no_update BEFORE UPDATE ON platform_audit BEGIN SELECT RAISE(ABORT, 'platform_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS platform_audit_no_delete BEFORE DELETE ON platform_audit BEGIN SELECT RAISE(ABORT, 'platform_audit is append-only'); END;
CREATE TABLE IF NOT EXISTS api_usage (
    tenant_id TEXT NOT NULL, day TEXT NOT NULL, route_group TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0, PRIMARY KEY (tenant_id, day, route_group)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ControlPlane:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else control_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect(write=True) as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self, write: bool = False):
        if write:
            self._lock.acquire()
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            if conn is not None:
                conn.close()
            if write:
                self._lock.release()

    # ------------------------------------------------------------------ tenants
    def audit(self, tenant_id: str, actor: str, event: str, payload: dict | None = None, conn: sqlite3.Connection | None = None) -> None:
        body = json.dumps(obs.redact(payload or {}), default=str, sort_keys=True)
        row = (tenant_id, _now(), actor, event, body)
        sql = "INSERT INTO platform_audit (tenant_id, ts, actor, event, payload) VALUES (?,?,?,?,?)"
        if conn is not None:
            conn.execute(sql, row)
        else:
            with self.connect(write=True) as c:
                c.execute(sql, row)

    def create_tenant(self, tenant_id: str, name: str, plan: str = "pilot", mode: str = "SANDBOX", is_demo: bool = False, actor: str = "system",
                      retention_days: int = 365) -> dict:
        if not valid_tenant_id(tenant_id):
            raise ControlPlaneError("tenant id must be 2-40 chars of a-z, 0-9 and '-' (not reserved)")
        if plan not in PLANS or mode not in MODES:
            raise ControlPlaneError(f"plan must be one of {PLANS}, mode one of {MODES}")
        if not name.strip() or len(name) > 120:
            raise ControlPlaneError("name must be 1-120 characters")
        try:
            with self.connect(write=True) as c:
                c.execute("INSERT INTO tenants (tenant_id, name, plan, mode, status, is_demo, created_at, retention_days) VALUES (?,?,?,?,?,?,?,?)",
                          (tenant_id, name.strip(), plan, mode, "ACTIVE", int(is_demo), _now(), int(retention_days)))
                self.audit(tenant_id, actor, "TENANT_CREATED", {"plan": plan, "mode": mode, "demo": is_demo}, c)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"tenant {tenant_id!r} already exists") from exc
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> dict:
        with self.connect() as c:
            r = c.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"tenant {tenant_id!r} not found")
        return dict(r)

    def ensure_default_tenant(self) -> dict:
        try:
            return self.get_tenant(DEFAULT_TENANT)
        except NotFoundError:
            return self.create_tenant(DEFAULT_TENANT, "Default tenant (legacy single-tenant data)", "enterprise", "PRODUCTION")

    def list_tenants(self, include_deleted: bool = False) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM tenants" + ("" if include_deleted else " WHERE status<>'DELETED'") + " ORDER BY tenant_id").fetchall()
        return [dict(r) for r in rows]

    def update_tenant(self, tenant_id: str, actor: str, **fields) -> dict:
        allowed = {"name", "plan", "mode", "status", "retention_days"}
        bad = set(fields) - allowed
        if bad:
            raise ControlPlaneError(f"cannot update {sorted(bad)}")
        if "plan" in fields and fields["plan"] not in PLANS or "mode" in fields and fields["mode"] not in MODES or \
                "status" in fields and fields["status"] not in ("ACTIVE", "SUSPENDED", "DELETED"):
            raise ControlPlaneError("invalid plan / mode / status")
        self.get_tenant(tenant_id)
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.connect(write=True) as c:
            c.execute(f"UPDATE tenants SET {sets} WHERE tenant_id=?", (*fields.values(), tenant_id))
            self.audit(tenant_id, actor, "TENANT_UPDATED", fields, c)
        return self.get_tenant(tenant_id)

    # ------------------------------------------------------------------ users and keys
    def create_user(self, tenant_id: str, user_id: str, email: str, display_name: str, role: Role, created_by: Principal | None = None) -> dict:
        self.get_tenant(tenant_id)
        if not re.match(r"^[A-Za-z0-9._@-]{2,80}$", user_id) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 200:
            raise ControlPlaneError("invalid user id or email")
        if created_by is not None:
            granted = assignable_roles(created_by.role)
            if role not in granted or (created_by.tenant_id != tenant_id and created_by.role != Role.SUPER_ADMIN):
                raise ControlPlaneError(f"role {created_by.role.value} may not create a {role.value} in tenant {tenant_id!r}")
        elif role == Role.SUPER_ADMIN and tenant_id != PLATFORM_TENANT:
            raise ControlPlaneError("SUPER_ADMIN users belong to the platform tenant only")
        try:
            with self.connect(write=True) as c:
                c.execute("INSERT INTO users (tenant_id, user_id, email, display_name, role, status, created_at) VALUES (?,?,?,?,?,?,?)",
                          (tenant_id, user_id, email, display_name.strip()[:120], role.value, "ACTIVE", _now()))
                self.audit(tenant_id, created_by.actor if created_by else "system", "USER_CREATED", {"user_id": user_id, "role": role.value}, c)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"user {user_id!r} already exists in tenant {tenant_id!r}") from exc
        return self.get_user(tenant_id, user_id)

    def get_user(self, tenant_id: str, user_id: str) -> dict:
        with self.connect() as c:
            r = c.execute("SELECT * FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)).fetchone()
        if r is None:
            raise NotFoundError("user not found")
        return dict(r)

    def list_users(self, tenant_id: str) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM users WHERE tenant_id=? ORDER BY user_id", (tenant_id,))]

    def set_user_status(self, tenant_id: str, user_id: str, status: str, actor: str) -> None:
        self.get_user(tenant_id, user_id)
        if status not in ("ACTIVE", "DISABLED"):
            raise ControlPlaneError("status must be ACTIVE or DISABLED")
        with self.connect(write=True) as c:
            c.execute("UPDATE users SET status=? WHERE tenant_id=? AND user_id=?", (status, tenant_id, user_id))
            if status == "DISABLED":                      # a disabled user's keys stop working immediately
                c.execute("UPDATE api_keys SET revoked_at=? WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL", (_now(), tenant_id, user_id))
            self.audit(tenant_id, actor, "USER_STATUS", {"user_id": user_id, "status": status}, c)

    def issue_key(self, tenant_id: str, user_id: str, label: str = "", ttl_days: int | None = None, issued_by: Principal | None = None) -> tuple[str, dict]:
        """Returns (plaintext key shown ONCE, key record). Only the SHA-256 hash is stored."""
        user = self.get_user(tenant_id, user_id)
        role = Role(user["role"])
        if user["status"] != "ACTIVE":
            raise ControlPlaneError("user is disabled")
        if issued_by is not None and issued_by.role != Role.SUPER_ADMIN and (issued_by.tenant_id != tenant_id or role not in assignable_roles(issued_by.role)
                                                                             and issued_by.actor != user_id):
            raise ControlPlaneError("you may not issue keys for that user")
        key, _entry = generate_key(role, user_id, tenant_id)
        key_id = obs.new_id()[:16]
        expires = (datetime.now() + timedelta(days=ttl_days)).isoformat(timespec="seconds") if ttl_days else None
        with self.connect(write=True) as c:
            c.execute("INSERT INTO api_keys (key_id, tenant_id, user_id, key_hash, role, label, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                      (key_id, tenant_id, user_id, hash_key(key), role.value, label[:80], _now(), expires))
            self.audit(tenant_id, issued_by.actor if issued_by else "system", "API_KEY_ISSUED", {"key_id": key_id, "user_id": user_id, "role": role.value, "expires_at": expires}, c)
        return key, {"key_id": key_id, "tenant_id": tenant_id, "user_id": user_id, "role": role.value, "expires_at": expires, "label": label}

    def revoke_key(self, tenant_id: str, key_id: str, actor: str) -> None:
        with self.connect(write=True) as c:
            cur = c.execute("UPDATE api_keys SET revoked_at=? WHERE tenant_id=? AND key_id=? AND revoked_at IS NULL", (_now(), tenant_id, key_id))
            if cur.rowcount == 0:
                raise NotFoundError("key not found or already revoked")
            self.audit(tenant_id, actor, "API_KEY_REVOKED", {"key_id": key_id}, c)

    def has_active_keys(self) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1").fetchone() is not None

    def list_keys(self, tenant_id: str) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT key_id, tenant_id, user_id, role, label, created_at, expires_at, revoked_at, last_used_at FROM api_keys "
                                               "WHERE tenant_id=? ORDER BY created_at", (tenant_id,))]

    def authenticate(self, key: str | None) -> Principal | None:
        """Constant-time lookup by hash; rejects revoked / expired keys, disabled users and suspended tenants."""
        if not key:
            return None
        digest = hash_key(key)
        with self.connect() as c:
            r = c.execute("SELECT k.key_hash, k.tenant_id, k.user_id, k.role, k.expires_at, k.revoked_at, u.status AS ustatus, t.status AS tstatus "
                          "FROM api_keys k JOIN users u ON u.tenant_id=k.tenant_id AND u.user_id=k.user_id JOIN tenants t ON t.tenant_id=k.tenant_id "
                          "WHERE k.key_hash=?", (digest,)).fetchone()
        if r is None or not hmac.compare_digest(r["key_hash"], digest):
            return None
        if r["revoked_at"] or r["ustatus"] != "ACTIVE" or r["tstatus"] != "ACTIVE":
            return None
        if r["expires_at"] and r["expires_at"] < _now():
            return None
        return Principal(r["user_id"], Role(r["role"]), r["tenant_id"])

    # ------------------------------------------------------------------ usage rollups (billing-ready counters)
    def record_api_call(self, tenant_id: str, route_group: str, duration_ms: float, error: bool) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        with self.connect(write=True) as c:
            c.execute("INSERT INTO api_usage (tenant_id, day, route_group, calls, errors, total_ms) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(tenant_id, day, route_group) DO UPDATE SET calls=calls+1, errors=errors+excluded.errors, total_ms=total_ms+excluded.total_ms",
                      (tenant_id, day, route_group, 1, int(error), duration_ms))

    def api_usage(self, tenant_id: str | None = None, days: int = 30) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql, params = "SELECT * FROM api_usage WHERE day>=?", [since]
        if tenant_id:
            sql, params = sql + " AND tenant_id=?", [since, tenant_id]
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY day, tenant_id, route_group", params)]

    def platform_audit(self, tenant_id: str | None = None, limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM platform_audit", []
        if tenant_id:
            sql, params = sql + " WHERE tenant_id=?", [tenant_id]
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY event_id DESC LIMIT ?", (*params, limit))]


_API_BUFFER: dict[tuple[str, str, str], list[float]] = {}
_API_BUFFER_LOCK = threading.Lock()


def buffer_api_call(tenant_id: str, route_group: str, duration_ms: float, error: bool) -> None:
    """Count API calls in memory and write them in batches: one database write per request would itself become the bottleneck."""
    key = (tenant_id, datetime.now().strftime("%Y-%m-%d"), route_group)
    with _API_BUFFER_LOCK:
        b = _API_BUFFER.setdefault(key, [0, 0, 0.0])
        b[0] += 1
        b[1] += int(error)
        b[2] += duration_ms
        n = len(_API_BUFFER)
    if n >= 500:
        flush_api_usage()


def flush_api_usage() -> int:
    with _API_BUFFER_LOCK:
        items = list(_API_BUFFER.items())
        _API_BUFFER.clear()
    if not items:
        return 0
    cp = get()
    with cp.connect(write=True) as c:
        for (tid, day, grp), (calls, errs, ms) in items:
            c.execute("INSERT INTO api_usage (tenant_id, day, route_group, calls, errors, total_ms) VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id, day, route_group) DO UPDATE SET "
                      "calls=calls+excluded.calls, errors=errors+excluded.errors, total_ms=total_ms+excluded.total_ms", (tid, day, grp, int(calls), int(errs), ms))
    return len(items)


_CP: dict[str, ControlPlane] = {}
_CP_LOCK = threading.Lock()


def get() -> ControlPlane:
    path = str(control_db_path())
    with _CP_LOCK:
        if path not in _CP:
            _CP[path] = ControlPlane(path)
        return _CP[path]


def reset() -> None:
    with _CP_LOCK:
        _CP.clear()
