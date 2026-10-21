"""Push approved purchase orders to the customer's ERP.

Runs AFTER the human approval and the local atomic commit, as its own step, so an ERP outage can never undo or duplicate an approved order:
  * one `erp_dispatches` row per (PO, integration) with a UNIQUE idempotency key; a PO already SENT is never sent again,
  * partial failure is normal: successful POs stay SENT, failed ones are recorded with the error and retried by `retry_failed` (bounded, then DEAD),
  * the adapter call itself is idempotent on the same key, so a crash between 'ERP accepted' and 'we recorded it' cannot create a duplicate,
  * SANDBOX tenants may push only to the in-process mock ERP; real ERP integrations require PRODUCTION mode.
"""
from __future__ import annotations

from datetime import datetime

from scm import audit, db, obs
from scm.events import BUS, EventName

from .. import tenancy
from . import registry
from .base import ERPError, ERPPurchaseOrder, ERPPurchaseOrderLine

MAX_ATTEMPTS = 5


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _allowed(integration: dict) -> tuple[bool, str]:
    mode = tenancy.current().mode
    if mode != "PRODUCTION" and integration["kind"] != "erp_mock":
        return False, "tenant is in SANDBOX mode: only the mock ERP may receive orders (enable production mode first)"
    return True, ""


def _record(po_id: str, iid: str, status: str, attempts: int, external_id: str | None, error: str | None) -> None:
    key = f"erp:{iid}:{po_id}"
    with db.connect(write=True) as c:
        c.execute("INSERT INTO erp_dispatches (Tenant_ID, PO_ID, Integration_ID, Status, Attempts, External_ID, Last_Error, Idempotency_Key, Updated_At) VALUES (?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(Tenant_ID, PO_ID, Integration_ID) DO UPDATE SET Status=excluded.Status, Attempts=excluded.Attempts, External_ID=COALESCE(excluded.External_ID, External_ID), "
                  "Last_Error=excluded.Last_Error, Updated_At=excluded.Updated_At", (_tenant(), po_id, iid, status, attempts, external_id, error, key, _now()))


def _attempt(adapter, po_row: dict, iid: str, attempts_before: int, actor: str) -> str:
    po = ERPPurchaseOrder(po_row["PO_ID"], po_row["Vendor_ID"], [ERPPurchaseOrderLine(po_row["Item_ID"], po_row["Quantity"], po_row["Unit_Price"])])
    try:
        created = adapter.create_purchase_order(po, f"erp:{iid}:{po_row['PO_ID']}")
    except ERPError as exc:
        n = attempts_before + 1
        status = "DEAD" if (not exc.retryable or n >= MAX_ATTEMPTS) else "FAILED"
        _record(po_row["PO_ID"], iid, status, n, None, f"{type(exc).__name__}: {str(exc)[:200]}")
        audit.safe_record(audit.EventType.ERP_PO_FAILED, po_row.get("Workflow_ID"), actor, {"po_id": po_row["PO_ID"], "integration": iid, "error": str(exc)[:200], "attempt": n})
        return status
    _record(po_row["PO_ID"], iid, "SENT", attempts_before + 1, created.external_id, None)
    audit.safe_record(audit.EventType.ERP_PO_CREATED, po_row.get("Workflow_ID"), actor, {"po_id": po_row["PO_ID"], "integration": iid, "external_id": created.external_id})
    BUS.publish(EventName.PURCHASE_ORDER_CREATED, {"po_id": po_row["PO_ID"], "external_id": created.external_id, "sku": po_row["Item_ID"], "vendor_id": po_row["Vendor_ID"],
                                                   "quantity": po_row["Quantity"]}, po_row.get("Workflow_ID"))
    return "SENT"


def push_workflow(workflow_id: str, integration_id: str, actor: str = "system:erp-dispatch") -> dict:
    integration = registry.get(integration_id)
    ok, why = _allowed(integration)
    if not ok:
        return {"status": "SKIPPED", "reason": why, "sent": 0, "failed": 0, "already_sent": 0}
    adapter = registry.adapter_for(integration_id)
    with db.connect() as c:
        pos = [dict(r) for r in c.execute("SELECT * FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT' ORDER BY PO_ID", (workflow_id,))]
        done = {r[0]: (r[1], r[2]) for r in c.execute("SELECT PO_ID, Status, Attempts FROM erp_dispatches WHERE Tenant_ID=? AND Integration_ID=?", (_tenant(), integration_id))}
    sent = failed = already = 0
    results = []
    for po in pos:
        st, attempts = done.get(po["PO_ID"], (None, 0))
        if st == "SENT":
            already += 1
            continue
        if st == "DEAD":
            continue
        res = _attempt(adapter, po, integration_id, attempts, actor)
        results.append({"po_id": po["PO_ID"], "status": res})
        sent += res == "SENT"
        failed += res != "SENT"
    return {"status": "COMPLETED" if not failed else "PARTIAL", "sent": sent, "failed": failed, "already_sent": already, "results": results}


def retry_failed(integration_id: str, actor: str = "system:erp-retry") -> dict:
    integration = registry.get(integration_id)
    ok, why = _allowed(integration)
    if not ok:
        return {"status": "SKIPPED", "reason": why}
    adapter = registry.adapter_for(integration_id)
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT d.PO_ID, d.Attempts, p.* FROM erp_dispatches d JOIN purchase_orders p ON p.PO_ID=d.PO_ID WHERE d.Tenant_ID=? AND d.Integration_ID=? "
                                           "AND d.Status='FAILED'", (_tenant(), integration_id))]
    out = {"retried": 0, "sent": 0, "still_failing": 0, "dead": 0}
    for r in rows:
        out["retried"] += 1
        res = _attempt(adapter, r, integration_id, r["Attempts"], actor)
        out["sent" if res == "SENT" else "dead" if res == "DEAD" else "still_failing"] += 1
    return out


def status(workflow_id: str | None = None) -> list[dict]:
    sql, params = "SELECT PO_ID, Integration_ID, Status, Attempts, External_ID, Last_Error, Updated_At FROM erp_dispatches WHERE Tenant_ID=?", [_tenant()]
    if workflow_id:
        sql += " AND PO_ID IN (SELECT PO_ID FROM purchase_orders WHERE Workflow_ID=?)"
        params.append(workflow_id)
    with db.connect() as c:
        rows = c.execute(sql + " ORDER BY Updated_At DESC", params).fetchall()
    return [dict(zip(("po_id", "integration_id", "status", "attempts", "external_id", "last_error", "updated_at"), r)) for r in rows]


def install_hooks() -> None:
    """After an approved workflow, push its orders through every active integration that has push_on_approve."""
    from scm import services

    def erp_push_on_done(svc, wf: str, summary: dict) -> None:
        if summary.get("graph_status") != "approved":
            return
        for integ in registry.list_all():
            if integ["status"] == "ACTIVE" and integ["config"].get("push_on_approve") and integ["kind"] in ("erp_mock", "erp_rest"):
                res = push_workflow(wf, integ["integration_id"])
                from .. import trace
                trace.update_execution(wf, {"erp": {integ["integration_id"]: {k: v for k, v in res.items() if k != "results"}}})
    erp_push_on_done.__name__ = "erp_push_on_done"
    if not any(getattr(h, "__name__", "") == "erp_push_on_done" for h in services.DONE_HOOKS):
        services.DONE_HOOKS.append(erp_push_on_done)
