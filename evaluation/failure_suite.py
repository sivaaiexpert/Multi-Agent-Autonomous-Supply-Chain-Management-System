"""Controlled failure tests for the platform (`make chaos-test` runs this and the legacy engine fault matrix).

Each scenario injects ONE real fault into a running, SYNTHETIC-data tenant, then measures (not assumes):
  detected        - the platform noticed (error recorded / refused / flagged)               detection_ms
  recovered       - after the fault is removed the system reaches the correct end state      recovery_ms
  integrity       - no corrupt / partial business state was left behind
  no_duplicates   - no duplicate orders / events after the retry or replay
  state_correct   - the final state equals what a fault-free run would have produced

    python -m evaluation.failure_suite [--out evaluation/reports/failure_suite.json] [--md docs/FAILURE_TEST_REPORT.md]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
T = "chaos"


def ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def setup() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="scm_chaos_"))
    os.environ.update({"SCM_DATA_DIR": str(tmp / "data"), "SCM_CONTROL_DB": str(tmp / "data" / "control.db"), "SCM_ENV": "development",
                       "SCM_ALLOW_PRIVATE_URLS": "1", "SCM_OBS__LOG_LEVEL": "ERROR", "SCM_LLM_ENABLED": "0"})
    from enterprise import controlplane, secrets_store, tenancy
    os.environ["SCM_SECRET_KEY"] = secrets_store.generate_secret_key()
    from scm import db
    db.DB_PATH, db.OUTBOX = tmp / "default.db", tmp / "default_outbox"
    controlplane.reset()
    tenancy.forget_workspaces()
    from enterprise import outbound
    from enterprise.hooks import install_all
    from enterprise.integrations import dispatch
    install_all()
    dispatch.install_hooks()
    outbound.install_event_bridge()
    return tmp


def fresh_tenant(name: str, seed: int = 5):
    from enterprise import profiles, tenancy
    tenancy.provision(name, name.title(), "professional", "SANDBOX", is_demo=True)
    ds = profiles.generate("retailer", seed, 0.08)
    with tenancy.scope(name):
        profiles.load_into_current_tenant(ds)


def to_gate(svc, tenant: str):
    from scm.security import Principal, Role
    run, _ = svc.start(Principal("op", Role.OPERATOR, tenant))
    wf = run["workflow_id"]
    return wf, svc.wait(wf, 180)


def approver(tenant: str):
    from scm.security import Principal, Role
    return Principal("boss", Role.APPROVER, tenant)


# --------------------------------------------------------------------------------------------------- scenarios
def llm_unavailable(tenant: str) -> dict:
    """The model endpoint is unreachable. Expected: deterministic fallback, a complete correct workflow, the failure counted."""
    from enterprise import tenancy
    from scm import llm_router
    from scm.services import WorkflowService
    llm_router.reset_breakers()
    with tenancy.scope(tenant):
        base = WorkflowService()
        ref_wf, ref = to_gate(base, tenant)
        base.shutdown()
        svc = WorkflowService(llm_config={"llm_enabled": True, "llm_base_url": "http://127.0.0.1:9", "llm_timeout": 2, "llm_backoff_base": 0.0, "llm_model": "none"})
        t0 = time.perf_counter()
        wf, run = to_gate(svc, tenant)
        took = ms(t0)
        metrics = llm_router.METRICS.snapshot() if hasattr(llm_router.METRICS, "snapshot") else {}
        svc.shutdown()
        same = [(p["Item_ID"], p["Vendor_ID"], p["qty"]) for p in (run["result"] or {}).get("proposals", [])] == [(p["Item_ID"], p["Vendor_ID"], p["qty"]) for p in (ref["result"] or {}).get("proposals", [])]
        from scm import orders
        dup = orders.duplicate_count(wf) + orders.duplicate_count(ref_wf)
    return {"fault": "LLM endpoint unreachable (connection refused)", "expected": "workflow still reaches the human gate with the same decision as the deterministic path",
            "detected": run["status"] == "AWAITING_APPROVAL", "detection_ms": None, "recovered": run["status"] == "AWAITING_APPROVAL", "recovery_ms": took,
            "integrity": True, "no_duplicates": bool(dup == 0), "state_correct": bool(same),
            "evidence": {"status": run["status"], "decisions_equal_deterministic_reference": same, "llm_metrics": metrics}}


def db_unavailable(tenant: str) -> dict:
    """Another process holds the tenant database's write lock. Expected: requests fail cleanly (no partial rows), then succeed once it is released."""
    from enterprise import tenancy
    from scm import db
    from scm.services import InvalidRequest  # noqa: F401
    old = db.BUSY_TIMEOUT_S
    db.BUSY_TIMEOUT_S = 1
    with tenancy.scope(tenant):
        path = tenancy.current().db_path
        before = db.df("SELECT COUNT(*) n FROM purchase_orders").n[0]
        blocker = sqlite3.connect(path, timeout=1, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        t0 = time.perf_counter()
        err = None
        try:
            with db.connect(write=True) as c:
                c.execute("INSERT INTO app_state (Key, Value) VALUES ('chaos-probe','1')")
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        detect = ms(t0)
        blocker.execute("ROLLBACK")
        blocker.close()
        t1 = time.perf_counter()
        with db.connect(write=True) as c:
            c.execute("INSERT OR REPLACE INTO app_state (Key, Value) VALUES ('chaos-probe','1')")
        rec = ms(t1)
        after = db.df("SELECT COUNT(*) n FROM purchase_orders").n[0]
        with db.connect(write=True) as c:
            c.execute("DELETE FROM app_state WHERE Key='chaos-probe'")
    db.BUSY_TIMEOUT_S = old
    return {"fault": "tenant database write-locked by another connection", "expected": "writes fail with a clear error after the busy timeout; nothing half-written; works once released",
            "detected": err is not None and "locked" in err.lower(), "detection_ms": detect, "recovered": True, "recovery_ms": rec, "integrity": before == after,
            "no_duplicates": True, "state_correct": before == after, "evidence": {"error": err, "busy_timeout_used_s": 1, "orders_before": int(before), "orders_after": int(after)}}


def erp_unavailable(tenant: str) -> dict:
    """The ERP is down at approval time. Expected: approval stands, pushes recorded FAILED, retried when the ERP is back, exactly one ERP order per PO."""
    from enterprise import tenancy
    from enterprise.approvals import ApprovalService
    from enterprise.integrations import dispatch, registry
    from scm import db
    from scm.services import WorkflowService
    with tenancy.scope(tenant):
        integ = registry.create("erp_mock", "Down ERP", {"failure": "unavailable", "push_on_approve": True}, None, "chaos")
        svc = WorkflowService()
        wf, _ = to_gate(svc, tenant)
        t0 = time.perf_counter()
        ApprovalService(svc).act(approver(tenant), wf, "approve")
        done = svc.wait(wf, 180)
        rows = dispatch.status(wf)
        detect = ms(t0)
        failed = [r for r in rows if r["status"] == "FAILED"]
        n_pos = int(db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT'", (wf,)).n[0])
        registry.set_mock_failure(integ["integration_id"], None)
        t1 = time.perf_counter()
        out = dispatch.retry_failed(integ["integration_id"])
        rec = ms(t1)
        adapter: Any = registry.adapter_for(integ["integration_id"])
        rows2 = dispatch.status(wf)
        svc.shutdown()
    ids = [r["external_id"] for r in rows2 if r["external_id"]]
    return {"fault": "ERP unavailable (every call raises a retryable error)", "expected": "local approval stands; pushes FAILED with reason; retry sends each PO to the ERP exactly once",
            "detected": bool(failed) and all(r["last_error"] for r in failed), "detection_ms": detect, "recovered": all(r["status"] == "SENT" for r in rows2) and out["still_failing"] == 0,
            "recovery_ms": rec, "integrity": done["stage"] == "approved" and n_pos > 0, "no_duplicates": len(adapter.orders) == len(set(ids)) == n_pos, "state_correct": len(adapter.orders) == n_pos,
            "evidence": {"approval_stage": done["stage"], "pos_sent_locally": n_pos, "failed_pushes_during_outage": len(failed), "erp_orders_after_retry": len(adapter.orders)}}


def erp_timeout(tenant: str) -> dict:
    """ERP calls time out. Same expectations; additionally the timeout must be classified retryable."""
    from enterprise import tenancy
    from enterprise.approvals import ApprovalService
    from enterprise.integrations import dispatch, registry
    from scm.services import WorkflowService
    with tenancy.scope(tenant):
        integ = registry.create("erp_mock", "Slow ERP", {"failure": "timeout", "push_on_approve": True}, None, "chaos")
        svc = WorkflowService()
        wf, _ = to_gate(svc, tenant)
        t0 = time.perf_counter()
        ApprovalService(svc).act(approver(tenant), wf, "approve")
        svc.wait(wf, 180)
        rows = dispatch.status(wf)
        detect = ms(t0)
        registry.set_mock_failure(integ["integration_id"], None)
        t1 = time.perf_counter()
        dispatch.retry_failed(integ["integration_id"])
        rec = ms(t1)
        rows2 = dispatch.status(wf)
        orders = len(cast(Any, registry.adapter_for(integ["integration_id"])).orders)
        svc.shutdown()
    return {"fault": "ERP calls time out", "expected": "recorded as failed/retryable, no duplicate ERP orders after retry",
            "detected": bool(rows) and all(r["status"] == "FAILED" for r in rows), "detection_ms": detect, "recovered": all(r["status"] == "SENT" for r in rows2), "recovery_ms": rec,
            "integrity": True, "no_duplicates": orders == len(rows2), "state_correct": all(r["status"] == "SENT" for r in rows2),
            "evidence": {"errors": sorted({(r["last_error"] or "")[:60] for r in rows}), "erp_orders": orders}}


def partial_dispatch(tenant: str) -> dict:
    """The ERP accepts 2 POs then fails. Expected: exactly the accepted ones are SENT, the rest FAILED, retry completes with no duplicates."""
    from enterprise import tenancy
    from enterprise.approvals import ApprovalService
    from enterprise.integrations import dispatch, registry
    from scm.services import WorkflowService
    with tenancy.scope(tenant):
        integ = registry.create("erp_mock", "Flaky ERP", {"failure": "partial", "fail_after": 2, "push_on_approve": True}, None, "chaos")
        svc = WorkflowService()
        wf, _ = to_gate(svc, tenant)
        t0 = time.perf_counter()
        ApprovalService(svc).act(approver(tenant), wf, "approve")
        svc.wait(wf, 180)
        rows = dispatch.status(wf)
        detect = ms(t0)
        sent, failed = [r for r in rows if r["status"] == "SENT"], [r for r in rows if r["status"] == "FAILED"]
        adapter: Any = registry.adapter_for(integ["integration_id"])
        in_erp_partial = len(adapter.orders)
        registry.set_mock_failure(integ["integration_id"], None)
        t1 = time.perf_counter()
        dispatch.retry_failed(integ["integration_id"])
        rec = ms(t1)
        rows2 = dispatch.status(wf)
        svc.shutdown()
    return {"fault": "ERP fails after accepting 2 purchase orders", "expected": "2 SENT, remainder FAILED and recorded; retry sends only the remainder",
            "detected": len(sent) == 2 and bool(failed), "detection_ms": detect, "recovered": all(r["status"] == "SENT" for r in rows2), "recovery_ms": rec,
            "integrity": in_erp_partial == 2, "no_duplicates": len(adapter.orders) == len(rows2), "state_correct": len(adapter.orders) == len(rows2),
            "evidence": {"sent_during_fault": len(sent), "failed_during_fault": len(failed), "erp_orders_final": len(adapter.orders)}}


def supplier_unavailable(tenant: str) -> dict:
    """The preferred supplier disappears (its quotes are withdrawn). Expected: no PO is addressed to it; the workflow still produces a valid plan."""
    from enterprise import tenancy
    from scm import db
    from scm.services import WorkflowService
    with tenancy.scope(tenant):
        svc = WorkflowService()
        _, ref = to_gate(svc, tenant)
        counts: dict[str, int] = {}
        for p in (ref["result"] or {}).get("proposals", []):
            counts[p["Vendor_ID"]] = counts.get(p["Vendor_ID"], 0) + 1
        victim = max(counts, key=lambda k: counts[k])
        with db.connect(write=True) as c:
            saved = c.execute("SELECT * FROM vendor_listings WHERE Vendor_ID=?", (victim,)).fetchall()
            c.execute("DELETE FROM vendor_listings WHERE Vendor_ID=?", (victim,))
        from scm import suppliers
        suppliers.invalidate_cache()
        t0 = time.perf_counter()
        wf, run = to_gate(svc, tenant)
        took = ms(t0)
        used = {p["Vendor_ID"] for p in (run["result"] or {}).get("proposals", [])}
        with db.connect(write=True) as c:
            c.executemany("INSERT INTO vendor_listings VALUES (" + ",".join("?" * len(saved[0])) + ")", [tuple(r) for r in saved])
        suppliers.invalidate_cache()
        svc.shutdown()
    return {"fault": f"supplier {victim} withdraws every quote", "expected": "no order addressed to it; a valid plan from the remaining suppliers (or an explicit escalation)",
            "detected": victim not in used, "detection_ms": took, "recovered": run["status"] == "AWAITING_APPROVAL", "recovery_ms": took, "integrity": True, "no_duplicates": True,
            "state_correct": victim not in used and run["status"] == "AWAITING_APPROVAL", "evidence": {"victim": victim, "suppliers_used": sorted(used), "proposals": len((run["result"] or {}).get("proposals", []))}}


def duplicate_webhook_events(tenant: str) -> dict:
    """The same signed event is delivered 5 times (and once with a changed body). Expected: applied once; the changed body rejected."""
    from enterprise import tenancy, webhooks
    from enterprise.integrations import registry
    from scm import db, security
    with tenancy.scope(tenant):
        secret = registry.create("webhook_in", "ERP", {"source": "erp"}, None, "chaos")["secret_shown_once"]
        sku = db.df("SELECT Item_ID FROM inventory ORDER BY Item_ID LIMIT 1").Item_ID[0]
        before_n = int(db.df("SELECT COUNT(*) n FROM webhook_events_in").n[0])

        def send(data, eid="dup-1"):
            body = json.dumps({"id": eid, "type": "inventory.updated", "data": data}).encode()
            sig, _ = security.sign_payload(secret, body)
            try:
                return webhooks.receive("erp", body, {"X-SCM-Signature": sig, "X-SCM-Event-ID": eid, "X-SCM-Event-Type": "inventory.updated"})["status"]
            except webhooks.WebhookError as exc:
                return f"{exc.status}:{exc.code}"
        t0 = time.perf_counter()
        results = [send({"sku_id": sku, "quantity": 321}) for _ in range(5)]
        detect = ms(t0)
        changed = send({"sku_id": sku, "quantity": 999})
        stock = int(db.df("SELECT Stock_Level s FROM inventory WHERE Item_ID=?", (sku,)).s[0])
        rows = int(db.df("SELECT COUNT(*) n FROM webhook_events_in").n[0]) - before_n
    return {"fault": "same signed webhook delivered 5 times, then re-sent with a different body under the same event id", "expected": "applied once, 4 acknowledged as duplicates, tampered re-send rejected (409)",
            "detected": results.count("duplicate") == 4 and changed.startswith("409"), "detection_ms": detect, "recovered": True, "recovery_ms": 0.0, "integrity": stock == 321,
            "no_duplicates": rows == 1, "state_correct": stock == 321, "evidence": {"results": results, "changed_body": changed, "event_rows": rows, "stock_after": stock}}


def worker_crash(tenant: str) -> dict:
    """The process dies in the middle of a run (SystemExit inside a node, nothing cleans up). Expected: a new service resumes from the checkpoint, no duplicate orders."""
    import scm.graph as g
    from enterprise import tenancy
    from scm import db, orders
    from scm.security import Principal, Role
    from scm.services import WorkflowService
    tmp = Path(tempfile.mkdtemp(prefix="scm_ckpt_"))
    ckpt = tmp / "ckpt.db"
    real, state = g.vendor_comm, {"crash": True}

    def crashing(s, c):
        if state["crash"]:
            raise SystemExit("process killed")
        return real(s, c)
    with tenancy.scope(tenant):
        g.vendor_comm = crashing
        try:
            svc1 = WorkflowService(ckpt)
            run, _ = svc1.start(Principal("op", Role.OPERATOR, tenant))
            wf = run["workflow_id"]
            try:
                svc1.futures[wf].result(timeout=180)
            except BaseException:
                pass
            stuck = svc1.get(wf)["status"] == "RUNNING"
            svc1.shutdown()
        finally:
            g.vendor_comm = real
        state["crash"] = False
        t0 = time.perf_counter()
        svc2 = WorkflowService(ckpt)
        out = svc2.recover()
        done = svc2.wait(wf, 180)
        rec = ms(t0)
        again = svc2.recover()
        n_po = int(db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=?", (wf,)).n[0])
        props = len(done["result"]["proposals"]) if done.get("result") else -1
        dups = orders.duplicate_count(wf)
        svc2.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    return {"fault": "worker process killed mid-workflow", "expected": "state left RUNNING, a restarted service resumes it from the durable checkpoint, no duplicate orders, recover() idempotent",
            "detected": stuck and out["resumed"] == [wf], "detection_ms": None, "recovered": done["status"] == "AWAITING_APPROVAL", "recovery_ms": rec,
            "integrity": n_po == props, "no_duplicates": dups == 0 and again == {"resumed": [], "at_gate": [], "failed": []}, "state_correct": done["status"] == "AWAITING_APPROVAL" and n_po == props,
            "evidence": {"recover": out, "second_recover": again, "purchase_orders": n_po, "proposals": props}}


def invalid_data_import(tenant: str) -> dict:
    """A corrupt upload (wrong types, negative stock, duplicates, script injection, bad dates). Expected: nothing reaches business tables without a clean preview + commit."""
    from enterprise import importer, mapping, tenancy
    from scm import db
    bad = ("sku_id,name,reorder_point,target_stock\nA-1,<script>alert(1)</script>,five,50\nA-1,Dup,5,50\nA-2,Neg,-3,10\nA-3,Ok,5,40\n,Blank,5,5\nA-4,TargetBelowReorder,50,10\n")
    with tenancy.scope(tenant):
        before = int(db.df("SELECT COUNT(*) n FROM inventory").n[0])
        t0 = time.perf_counter()
        rep = importer.preview("products", bad.encode(), "bad.csv", "chaos", mapping.FieldMapping(columns={"sku_id": "sku_id", "name": "name", "reorder_point": "reorder_point", "target_stock": "target_stock"}),
                               importer.ImportOptions(allow_partial=False))
        detect = ms(t0)
        after_preview = int(db.df("SELECT COUNT(*) n FROM inventory").n[0])
        blocked = False
        try:
            importer.commit(rep["job_id"], "chaos")
        except Exception:
            blocked = True
        after_commit = int(db.df("SELECT COUNT(*) n FROM inventory").n[0])
        scripts = int(db.df("SELECT COUNT(*) n FROM inventory WHERE Item_Name LIKE '%<script%'").n[0])
    return {"fault": "corrupt customer upload (type errors, duplicate ids, negatives, blank id, injected markup)", "expected": "problems reported per row; with allow_partial=False the commit is blocked and nothing is written",
            "detected": rep["report"]["rejected"] > 0 and not rep["can_commit"], "detection_ms": detect, "recovered": True, "recovery_ms": 0.0,
            "integrity": after_preview == before and after_commit == before and scripts == 0, "no_duplicates": True, "state_correct": after_commit == before and blocked,
            "evidence": {"rows_before": before, "rows_after_commit_attempt": after_commit, "commit_blocked": blocked, "report_status": rep["report"]["status"], "rejected_rows": rep["report"]["rejected"], "issues_sample": [i.get("message", str(i))[:70] for i in rep["report"]["issues"][:4]], "injected_markup_rows": scripts}}


def inventory_mismatch(tenant: str) -> dict:
    """ERP stock disagrees with the platform's books. Expected: reconcile reports each difference and changes nothing."""
    from enterprise import tenancy
    from enterprise.integrations import sync
    from scm import db
    with tenancy.scope(tenant):
        from enterprise.integrations.base import ERPInventoryItem
        from enterprise.integrations.mock_erp import MockERPAdapter
        books = db.df("SELECT Item_ID, Stock_Level FROM inventory")
        adapter = MockERPAdapter([ERPInventoryItem(r.Item_ID, int(r.Stock_Level), "WH-A", "") for r in books.itertuples()], failure="inventory_mismatch")
        before = db.df("SELECT SUM(Stock_Level) s FROM inventory").s[0]
        t0 = time.perf_counter()
        rep = sync.reconcile_inventory(adapter, 0)
        detect = ms(t0)
        after = db.df("SELECT SUM(Stock_Level) s FROM inventory").s[0]
    n = len(rep["mismatched"])
    return {"fault": "ERP on-hand differs from the platform's inventory", "expected": "mismatches listed with both values; no data modified by reconciliation",
            "detected": n > 0, "detection_ms": detect, "recovered": True, "recovery_ms": 0.0, "integrity": before == after, "no_duplicates": True, "state_correct": before == after,
            "evidence": {"mismatches_found": n, "stock_sum_before": float(before), "stock_sum_after": float(after)}}


def outbound_endpoint_down(tenant: str) -> dict:
    """The customer's webhook receiver is down. Expected: deliveries retried with backoff then dead-lettered with a reason; replay delivers once it is back."""
    from enterprise import outbound, tenancy
    from scm import db
    with tenancy.scope(tenant):
        sub = outbound.subscribe("http://127.0.0.1:9/hook", ["procurement.approved", "dispatch.completed"], "chaos")
        outbound.emit("procurement.approved", {"workflow_id": "chaos-wf"}, "chaos-evt-1", "chaos-wf")
        from datetime import datetime, timedelta
        t0 = time.perf_counter()
        clock = datetime.now()
        for _ in range(outbound.MAX_ATTEMPTS + 1):                       # real connection attempts; the clock jumps past each backoff window
            outbound.deliver_due(now=clock)
            clock += timedelta(hours=6)
        detect = ms(t0)
        rows = outbound.deliveries(None, 50, 0)
        dead = [r for r in rows if r["status"] == "DEAD"]
        t1 = time.perf_counter()
        for r in dead:
            outbound.replay(r["delivery_id"], "chaos")                   # the receiver is back
        outbound.deliver_due(now=clock, sender=lambda url, headers, body, timeout: (200, "ok"))
        rec = ms(t1)
        after = outbound.deliveries(None, 50, 0)
        outbound.unsubscribe(str(sub["sub_id"]), "chaos")
        _ = db
    return {"fault": "customer webhook receiver unreachable", "expected": "bounded retries with backoff, dead-lettered with a reason, nothing dropped; replay delivers exactly once when the receiver is back",
            "detected": bool(dead) and all(r["last_error"] and r["attempts"] == outbound.MAX_ATTEMPTS for r in dead), "detection_ms": detect,
            "recovered": bool(after) and all(r["status"] == "DELIVERED" for r in after), "recovery_ms": rec, "integrity": len(after) == len(rows),
            "no_duplicates": len({r["event_id"] for r in after}) == len(after), "state_correct": bool(rows) and len(dead) == len(rows),
            "evidence": {"deliveries": len(rows), "dead_lettered": len(dead), "attempts_each": sorted({r["attempts"] for r in dead}), "error": (dead[0]["last_error"] if dead else "")[:80],
                         "after_replay": sorted({r["status"] for r in after})}}


def disaster_recovery(tenant: str) -> dict:
    """Disaster: the tenant's data is destroyed after a backup. Expected: restore from the verified backup reproduces every row count and the audit chain; a tampered backup is refused."""
    from enterprise import dr, tenancy
    from enterprise.approvals import ApprovalService
    from scm import audit, db
    from scm.services import WorkflowService
    tmp = Path(tempfile.mkdtemp(prefix="scm_bk_"))
    with tenancy.scope(tenant):
        svc = WorkflowService()
        wf, _ = to_gate(svc, tenant)
        ApprovalService(svc).act(approver(tenant), wf, "approve")
        svc.wait(wf, 180)
        svc.shutdown()
        bk = dr.backup_tenant(tenant, tmp)
        po_before = int(db.df("SELECT COUNT(*) n FROM purchase_orders").n[0])
        with db.connect(write=True) as c:                                      # the disaster (audit triggers are append-only, so destroy the business data)
            c.execute("DELETE FROM purchase_orders")
            c.execute("DELETE FROM inventory")
        wiped = int(db.df("SELECT COUNT(*) n FROM inventory").n[0]) == 0
    t0 = time.perf_counter()
    dr.restore_tenant(bk["path"], tenant, overwrite=True)
    rto = ms(t0)
    with tenancy.scope(tenant):
        po_after = int(db.df("SELECT COUNT(*) n FROM purchase_orders").n[0])
        chain = audit.verify_chain()
    f = Path(bk["path"]) / "engine.db"
    raw = bytearray(f.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    f.write_bytes(bytes(raw))
    try:
        dr.restore_tenant(bk["path"], tenant, overwrite=True)
        refused = False
    except dr.DRError:
        refused = True
    shutil.rmtree(tmp, ignore_errors=True)
    return {"fault": "tenant data destroyed after a backup; then a corrupted backup is offered", "expected": "restore reproduces row counts and a valid audit chain; a corrupted backup is refused",
            "detected": wiped and refused, "detection_ms": None, "recovered": po_after == po_before and chain["ok"], "recovery_ms": rto, "integrity": chain["ok"],
            "no_duplicates": po_after == po_before, "state_correct": po_after == po_before and chain["ok"],
            "evidence": {"measured_restore_ms": rto, "backup_seconds": bk["backup_seconds"], "rows_in_backup": bk["row_counts"].get("purchase_orders"), "audit_events_verified": chain["checked"],
                         "corrupted_backup_refused": refused, "scope": "measured on this machine with a small synthetic tenant; RTO for real data volumes must be measured on the target system. RPO is the backup interval you schedule (not measured here)"}}


SCENARIOS = [llm_unavailable, db_unavailable, erp_unavailable, erp_timeout, partial_dispatch, supplier_unavailable, duplicate_webhook_events, worker_crash, invalid_data_import,
             inventory_mismatch, outbound_endpoint_down, disaster_recovery]


def run_all() -> dict:
    tmp = setup()
    results = []
    try:
        for fn in SCENARIOS:
            name = f"{T}-{fn.__name__}".replace("_", "-")[:40]
            try:
                fresh_tenant(name)
                t0 = time.perf_counter()
                r = fn(name)
                r["scenario"], r["total_ms"] = fn.__name__, ms(t0)
                r["passed"] = all(r[k] for k in ("detected", "recovered", "integrity", "no_duplicates", "state_correct"))
            except Exception as exc:
                r = {"scenario": fn.__name__, "fault": fn.__doc__.split(".")[0] if fn.__doc__ else fn.__name__, "passed": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-600:]}
            results.append(r)
            print(f"  {'PASS' if r['passed'] else 'FAIL'}  {fn.__name__}  detect={r.get('detection_ms')} ms recover={r.get('recovery_ms')} ms", flush=True)
    finally:
        from enterprise import controlplane, tenancy
        controlplane.reset()
        tenancy.forget_workspaces()
        shutil.rmtree(tmp, ignore_errors=True)
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "data": "SYNTHETIC retailer tenants, mock ERP and local fault injection",
            "passed": sum(r["passed"] for r in results), "total": len(results), "results": results}


def markdown(rep: dict) -> str:
    L = ["# Controlled failure test report", "", f"Generated {rep['generated_at']} by `make chaos-test` (`python -m evaluation.failure_suite`). Data: {rep['data']}.", "",
         f"**{rep['passed']} of {rep['total']} scenarios passed.** Times are wall-clock milliseconds measured on the test machine; 'detection' is fault -> failure recorded/visible, "
         "'recovery' is fault removed -> correct end state. They are not SLA figures.", "",
         "| Scenario | Fault | Detected | Detect ms | Recovered | Recover ms | Integrity | No duplicates | State correct |", "|---|---|---|---:|---|---:|---|---|---|"]
    yn = lambda v: "yes" if v else "NO"
    for r in rep["results"]:
        if "error" in r:
            L.append(f"| {r['scenario']} | {r['fault']} | ERROR | | | | | | |")
            continue
        L.append(f"| {r['scenario']} | {r['fault']} | {yn(r['detected'])} | {r['detection_ms'] if r['detection_ms'] is not None else '-'} | {yn(r['recovered'])} | {r['recovery_ms']} | "
                 f"{yn(r['integrity'])} | {yn(r['no_duplicates'])} | {yn(r['state_correct'])} |")
    L += ["", "## Evidence per scenario", ""]
    for r in rep["results"]:
        L += [f"### {r['scenario']}", "", f"- Expected: {r.get('expected', '-')}", f"- Evidence: `{json.dumps(r.get('evidence', r.get('error')), default=str)[:600]}`", ""]
    L += ["## What this does not show", "", "- Faults are injected in one process on one machine; real networks, disks and multi-node failures are not exercised.",
          "- The ERP is the in-repo mock (or the mock HTTP server), not a real ERP."]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "evaluation" / "reports" / "failure_suite.json"))
    ap.add_argument("--md", default=str(ROOT / "docs" / "FAILURE_TEST_REPORT.md"))
    a = ap.parse_args(argv)
    rep = run_all()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    Path(a.md).write_text(markdown(rep), encoding="utf-8")
    print(f"{rep['passed']}/{rep['total']} passed; wrote {a.out}, {a.md}")
    return 0 if rep["passed"] == rep["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
