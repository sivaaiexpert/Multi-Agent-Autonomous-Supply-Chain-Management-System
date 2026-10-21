"""Shared API context: authentication (control-plane keys first, legacy env keys second), tenant scoping, per-tenant workflow services, rate limits."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

from fastapi import HTTPException, Request

from enterprise import controlplane, tenancy
from scm import audit
from scm.security import DEFAULT_TENANT, Principal, authenticate, load_keyring
from scm.services import WorkflowService

from .ratelimit import RateLimiter


class AppContext:
    def __init__(self, service: WorkflowService | None = None, keyring: dict[str, Principal] | None = None):
        self.default_service = service
        self._keyring = keyring
        self._services: dict[str, WorkflowService] = {}
        self._lock = threading.RLock()
        per_min = int(os.environ.get("SCM_RATE_LIMIT_PER_MIN", "1200"))
        self.limiter = RateLimiter(per_min, burst=max(20, per_min // 6))
        self.auth_failures = RateLimiter(int(os.environ.get("SCM_AUTH_FAILURES_PER_MIN", "30")))

    # ------------------------------------------------------------------ authentication
    def keyring(self) -> dict[str, Principal]:
        if self._keyring is None:
            self._keyring = load_keyring()
        return self._keyring

    def authenticate(self, key: str | None) -> Principal | None:
        if not key:
            return None
        try:
            who = controlplane.get().authenticate(key)
        except Exception:
            who = None
        return who or authenticate(key, self.keyring())

    def principal(self, request: Request) -> Principal:
        auth = request.headers.get("authorization")
        key = request.headers.get("x-api-key") or (auth[7:] if auth and auth.lower().startswith("bearer ") else None)
        ip = request.client.host if request.client else "?"
        who = self.authenticate(key)
        if who is None:
            ok, wait = self.auth_failures.allow(f"ip:{ip}")
            if not ok:
                raise HTTPException(status_code=429, detail="too many failed authentication attempts", headers={"Retry-After": str(int(wait) + 1)})
            try:
                controlplane.get().audit("platform", "anonymous", "ACCESS_DENIED", {"path": request.url.path, "reason": "missing or invalid API key", "ip": ip})
            except Exception:
                pass
            raise HTTPException(status_code=401, detail="missing or invalid API key")
        ok, wait = self.limiter.allow(f"key:{who.tenant_id}:{who.actor}")
        if not ok:
            raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": str(int(wait) + 1)})
        request.state.principal = who
        request.state.actor = who.actor
        request.state.tenant = who.tenant_id
        return who

    # ------------------------------------------------------------------ tenancy
    @contextmanager
    def scoped(self, who: Principal):
        """Run the body as the principal's tenant. The tenant comes ONLY from the authenticated principal, never from a header or a parameter."""
        with tenancy.scope(who.tenant_id):
            yield

    def service(self, tenant_id: str) -> WorkflowService:
        """The tenant's workflow service (call inside its scope). Durable (SQLite checkpoints) except for an explicitly injected default-tenant service."""
        with self._lock:
            if tenant_id == DEFAULT_TENANT and self.default_service is not None:
                return self.default_service
            if tenant_id not in self._services:
                ctx = tenancy.current()
                ctx.checkpoints.parent.mkdir(parents=True, exist_ok=True)
                svc = WorkflowService(ctx.checkpoints, llm_config={"llm_enabled": os.environ.get("SCM_LLM_ENABLED", "0") == "1",
                                                                  "llm_emails": os.environ.get("SCM_LLM_EMAILS", "0") == "1"})
                svc.recover()                                      # resume anything the previous process left half-way
                self._services[tenant_id] = svc
            return self._services[tenant_id]

    def deny(self, request: Request, reason: str) -> None:
        who = getattr(request.state, "principal", None)
        if who is None:
            return
        try:
            with tenancy.scope(who.tenant_id, enforce_active=False):
                audit.safe_record(audit.EventType.ACCESS_DENIED, None, who.actor, {"path": request.url.path, "reason": reason, "role": who.role.value})
        except Exception:
            pass

    def shutdown(self) -> None:
        for s in self._services.values():
            s.shutdown()
