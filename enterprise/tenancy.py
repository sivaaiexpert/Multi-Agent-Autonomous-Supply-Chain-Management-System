"""Tenant scoping: one database per tenant + tenant-aware context for everything that runs inside it.

    with tenancy.scope("acme"):            # thread-local DB override + observability context + the tenant's effective settings
        ...engine / repositories / audit / events ...

* A tenant's data lives in `<data>/tenants/<tenant_id>/engine.db` (the legacy single-tenant database stays the `default` tenant).
* Inside a scope, `obs.context()['tenant_id']` stamps every audit row, span, LLM call, event, forecast and order, and the tenant's policy / model
  configuration is applied to the engine settings.
* Code that needs the tenant (`current()`) raises `NoTenantScope` outside a scope, so an unscoped query cannot silently run against the wrong data.
* Worker threads must be started through `db.propagate(fn)` (copies this context); the workflow service already does.
"""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scm import db, obs
from scm.security import DEFAULT_TENANT, resolve_within
from scm.settings import _base_settings, use_settings

from . import controlplane
from .controlplane import ControlPlaneError, NotFoundError, data_root, valid_tenant_id
from .policies import ModelConfig, TenantPolicy, effective_settings, load_model_config, load_policy


class NoTenantScope(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantCtx:
    tenant_id: str
    db_path: Path
    outbox: Path
    checkpoints: Path
    mode: str
    plan: str
    policy: TenantPolicy
    models: ModelConfig


_CURRENT: contextvars.ContextVar[TenantCtx | None] = contextvars.ContextVar("scm_tenant", default=None)
_READY: set[str] = set()
_READY_LOCK = threading.Lock()


def tenant_dir(tenant_id: str) -> Path:
    if not valid_tenant_id(tenant_id):
        raise ControlPlaneError(f"invalid tenant id {tenant_id!r}")
    return resolve_within(data_root() / "tenants", tenant_id)


def tenant_paths(tenant_id: str) -> tuple[Path, Path, Path]:
    """(database, outbox, checkpoint database). The legacy `default` tenant keeps using the original database / outbox."""
    if tenant_id == DEFAULT_TENANT:
        import os
        ckpt = Path(os.environ.get("SCM_CHECKPOINT_DB") or data_root() / "checkpoints.db")
        return Path(db.DB_PATH), Path(db.OUTBOX), ckpt
    d = tenant_dir(tenant_id)
    return d / "engine.db", d / "outbox", d / "checkpoints.db"


def ensure_workspace(tenant_id: str) -> Path:
    """Create + migrate the tenant database (idempotent). Customer tenants start EMPTY: no demo data is ever seeded into them."""
    db_path, outbox, _ = tenant_paths(tenant_id)
    key = str(db_path)
    with _READY_LOCK:
        if key in _READY and db_path.exists():
            return db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        with db.use_path(db_path, outbox):
            db.init_db(seed_demo=(tenant_id == DEFAULT_TENANT))
        _READY.add(key)
    return db_path


def forget_workspaces() -> None:
    with _READY_LOCK:
        _READY.clear()


def _tenant_record(tenant_id: str) -> dict:
    cp = controlplane.get()
    if tenant_id == DEFAULT_TENANT:
        return cp.ensure_default_tenant()
    return cp.get_tenant(tenant_id)


@contextmanager
def scope(tenant_id: str, *, enforce_active: bool = True):
    """Run the body as `tenant_id`. Raises NotFoundError for an unknown tenant and ControlPlaneError for a suspended one."""
    rec = _tenant_record(tenant_id)
    if enforce_active and rec["status"] != "ACTIVE":
        raise ControlPlaneError(f"tenant {tenant_id!r} is {rec['status'].lower()}")
    db_path = ensure_workspace(tenant_id)
    _, outbox, ckpt = tenant_paths(tenant_id)
    token_ctx = obs.bind(tenant_id=tenant_id)
    with db.use_path(db_path, outbox):
        policy, models = load_policy(), load_model_config()
        ctx = TenantCtx(tenant_id, db_path, outbox, ckpt, rec["mode"], rec["plan"], policy, models)
        token = _CURRENT.set(ctx)
        try:
            with use_settings(effective_settings(_base_settings(), policy, models)):
                yield ctx
        finally:
            _CURRENT.reset(token)
            obs.unbind(token_ctx)


def current() -> TenantCtx:
    ctx = _CURRENT.get()
    if ctx is None:
        raise NoTenantScope("this operation needs a tenant scope (use `with tenancy.scope(tenant_id):`)")
    return ctx


def current_id() -> str:
    return current().tenant_id


def provision(tenant_id: str, name: str, plan: str = "pilot", mode: str = "SANDBOX", is_demo: bool = False, actor: str = "system") -> dict:
    """Create the tenant record and its empty, migrated workspace."""
    rec = controlplane.get().create_tenant(tenant_id, name, plan, mode, is_demo, actor)
    ensure_workspace(tenant_id)
    return rec


def tenants_with_workspaces() -> list[str]:
    out = []
    for t in controlplane.get().list_tenants():
        try:
            if tenant_paths(t["tenant_id"])[0].exists():
                out.append(t["tenant_id"])
        except ControlPlaneError:
            continue
    return out


__all__ = ["NoTenantScope", "NotFoundError", "TenantCtx", "current", "current_id", "ensure_workspace", "provision", "scope", "tenant_dir", "tenant_paths"]
