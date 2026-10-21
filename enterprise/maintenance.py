"""Periodic background work for every tenant: outbound webhook delivery, inbound webhook retries, ERP dispatch retries, approval escalation / timeout, API-usage flush.

`run_once()` is safe to call repeatedly from a scheduler (cron, the API's optional background thread, or a test). Each tenant is processed inside its own scope and
a failure in one tenant never stops the others.
"""
from __future__ import annotations

import threading
from datetime import datetime

from scm import obs

from . import approvals, controlplane, outbound, tenancy, webhooks
from .integrations import dispatch, registry


def run_once(now: datetime | None = None, service_factory=None) -> dict:
    out: dict = {"tenants": 0, "errors": []}
    controlplane.flush_api_usage()
    for tid in tenancy.tenants_with_workspaces():
        try:
            with tenancy.scope(tid):
                r: dict = {"outbound": outbound.deliver_due(now), "webhooks": webhooks.process_pending(now)}
                if service_factory is not None:
                    r["approvals"] = approvals.ApprovalService(service_factory(tid)).process_overdue(now)
                r["erp_retry"] = {}
                for integ in registry.list_all():
                    if integ["status"] == "ACTIVE" and integ["kind"] in ("erp_mock", "erp_rest"):
                        r["erp_retry"][integ["integration_id"]] = dispatch.retry_failed(integ["integration_id"])
                out[tid] = r
                out["tenants"] += 1
        except Exception as exc:                        # one tenant's problem must not stop the rest
            out["errors"].append({"tenant": tid, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            obs.log_event("maintenance failed for a tenant", level=40, tenant=tid, error=str(exc))
    return out


class Background:
    def __init__(self, interval_s: float = 30.0, service_factory=None):
        self.interval, self.service_factory = interval_s, service_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.interval):
                run_once(service_factory=self.service_factory)
        self._thread = threading.Thread(target=loop, daemon=True, name="maintenance")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
