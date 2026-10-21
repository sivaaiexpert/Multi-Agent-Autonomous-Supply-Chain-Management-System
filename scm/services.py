"""Workflow service: the single entry point used by the HTTP API (and usable by any other front end).

Responsibilities: RBAC checks, idempotent workflow creation, running the LangGraph in a background worker pool (the API never blocks on a
slow model), durable checkpoints (SQLite), human decisions (approve / reject / re-source / edit) with policy re-validation, run bookkeeping.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import audit, db, obs, policy
from .graph import HITL_NODE, build_graph, compose_body, initial_state
from .security import Perm, PermissionDenied, Principal, valid_id

ACTIVE = ("QUEUED", "RUNNING", "RESUMING")

# Extension points used by the enterprise layer (approval chains, ERP dispatch, outbound events). They live here so that `scm` never imports
# `enterprise`. A hook receives (service, workflow_id, summary) and must never raise into the workflow: failures are logged.
GATE_HOOKS: list = []        # called when a workflow pauses at the human gate
DONE_HOOKS: list = []        # called when a workflow reaches a terminal state (approved / rejected / healthy / ...)


def _run_hooks(hooks: list, service, wf: str, summary: dict) -> None:
    for h in list(hooks):
        try:
            h(service, wf, summary)
        except Exception as exc:
            obs.log_event("workflow hook failed", level=40, hook=getattr(h, "__name__", "hook"), workflow=wf, error=str(exc))


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class InvalidRequest(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WorkflowService:
    def __init__(self, checkpoint_path: Path | str | None = None, workers: int = 4, llm_config: dict | None = None):
        self.llm_config = {"llm_enabled": False, "llm_model": "llama3.2", "llm_base_url": "http://localhost:11434", "llm_timeout": 60,
                           "llm_emails": False, **(llm_config or {})}
        self._lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf")
        self.futures: dict[str, Future] = {}
        if checkpoint_path:
            from langgraph.checkpoint.sqlite import SqliteSaver
            conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
            self.graph = build_graph(SqliteSaver(conn))
            self.durable = True
        else:
            self.graph = build_graph()
            self.durable = False

    # ------------------------------------------------------------------ helpers
    def _cfg(self, wf: str, actor: str, trace_id: str | None = None) -> dict:
        return {"configurable": {"thread_id": wf, "workflow_id": wf, "actor": actor, "trace_id": trace_id or obs.new_id(), **self.llm_config}}

    def _update_run(self, wf: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields) + ", Updated_At=?"
        with db.connect(write=True) as c:
            c.execute(f"UPDATE workflow_runs SET {sets} WHERE Workflow_ID=?", (*fields.values(), _now(), wf))

    def _row(self, wf: str) -> dict:
        if not valid_id(wf):
            raise InvalidRequest("malformed workflow id")
        tenant = obs.context().get("tenant_id")
        with db.connect() as c:
            if tenant:          # tenant isolation also at the row level (defence in depth on top of one database per tenant)
                r = c.execute("SELECT * FROM workflow_runs WHERE Workflow_ID=? AND Tenant_ID=?", (wf, tenant)).fetchone()
            else:
                r = c.execute("SELECT * FROM workflow_runs WHERE Workflow_ID=?", (wf,)).fetchone()
        if r is None:
            raise NotFound(f"workflow {wf} not found")
        return dict(r)

    def _public(self, row: dict) -> dict:
        out = {"workflow_id": row["Workflow_ID"], "status": row["Status"], "stage": row["Stage"], "created_by": row["Created_By"],
               "created_at": row["Created_At"], "updated_at": row["Updated_At"], "trace_id": row["Trace_ID"], "error": row["Error"],
               "idempotency_key": row["Idempotency_Key"]}
        out["result"] = json.loads(row["Result_JSON"]) if row["Result_JSON"] else None
        return out

    def _summarise(self, wf: str) -> dict:
        snap = self.graph.get_state({"configurable": {"thread_id": wf}})
        v = snap.values or {}
        paused = bool(snap.next and snap.next[0] == HITL_NODE)
        return {"graph_status": v.get("status"), "awaiting_approval": paused, "alerts": len(v.get("alerts", [])), "policy": v.get("policy", {}),
                "proposals": [{k: p.get(k) for k in ("po_id", "Item_ID", "Vendor_ID", "Vendor_Name", "qty", "unit_price", "total", "lead_time", "risk_level",
                                                    "days_until_stockout", "forecast_confidence", "savings", "share", "supplier_score", "why", "policy_flags")}
                              for p in v.get("proposals", [])],
                "drafts": [{"vendor_id": d["Vendor_ID"], "po_ids": d["po_ids"], "total": d["total"], "to": d["to"]} for d in v.get("drafts", [])],
                "outcome": v.get("outcome", {}), "errors": [{"node": e["node"], "type": e["type"], "message": e["message"]} for e in v.get("errors", [])],
                "trace": [{k: e[k] for k in ("ts", "agent", "step", "message", "level", "source")} for e in v.get("trace", [])][-60:],
                "rounds": v.get("rounds", 0)}

    def _finish(self, wf: str) -> None:
        try:
            summary = self._summarise(wf)
            g = summary["graph_status"]
            status = ("AWAITING_APPROVAL" if summary["awaiting_approval"] else "FAILED" if g == "error" else
                      "COMPLETED" if g in ("approved", "healthy", "rejected", "no_vendor", "policy_blocked") else "FAILED")
            self._update_run(wf, Status=status, Stage="awaiting_approval" if summary["awaiting_approval"] else g, Finished_At=_now(),
                             Result_JSON=json.dumps(summary, default=str),
                             Error=(summary["errors"][0]["message"] if summary["errors"] else None))
            _run_hooks(GATE_HOOKS if summary["awaiting_approval"] else DONE_HOOKS, self, wf, summary)
        except Exception as exc:
            self._update_run(wf, Status="FAILED", Error=f"{type(exc).__name__}: {exc}"[:500])

    def _run(self, wf: str, payload, actor: str, trace_id: str | None) -> None:
        try:
            self._update_run(wf, Status="RUNNING", Started_At=_now())
            token = obs.bind(workflow_id=wf, trace_id=trace_id)
            try:
                for _ in self.graph.stream(payload, self._cfg(wf, actor, trace_id), stream_mode="updates"):
                    pass
            finally:
                obs.unbind(token)
        except Exception as exc:  # the graph's own nodes are guarded; this protects against infrastructure failures
            self._update_run(wf, Status="FAILED", Error=f"{type(exc).__name__}: {exc}"[:500])
            audit.safe_record(audit.EventType.WORKFLOW_FAILED, wf, "system", {"error": str(exc)[:300]})
            return
        self._finish(wf)

    def _submit(self, wf: str, payload, actor: str, trace_id: str | None) -> None:
        fut = self.pool.submit(db.propagate(self._run), wf, payload, actor, trace_id)
        with self._lock:
            self.futures[wf] = fut

    def wait(self, wf: str, timeout: float = 60.0) -> dict:
        fut = self.futures.get(wf)
        if fut is not None:
            fut.result(timeout=timeout)
        return self.get(wf)

    # ------------------------------------------------------------------ public API
    def start(self, who: Principal, idempotency_key: str | None = None) -> tuple[dict, bool]:
        """Returns (run, created). The same idempotency key never creates a second workflow."""
        who.require(Perm.RUN_WORKFLOW)
        if idempotency_key is not None and not valid_id(idempotency_key):
            raise InvalidRequest("Idempotency-Key must be 1-100 chars of [A-Za-z0-9._:-]")
        wf, trace_id = obs.new_id(), obs.new_id()
        try:
            with db.connect(write=True) as c:
                if idempotency_key:
                    row = c.execute("SELECT Workflow_ID FROM workflow_runs WHERE Idempotency_Key=? AND Tenant_ID=?",
                                    (idempotency_key, obs.context().get("tenant_id") or who.tenant_id or "default")).fetchone()
                    if row:
                        return self.get(row[0]), False
                c.execute("INSERT INTO workflow_runs (Workflow_ID, Idempotency_Key, Status, Created_By, Created_At, Updated_At, Trace_ID, Stage, Tenant_ID) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (wf, idempotency_key, "QUEUED", who.actor, _now(), _now(), trace_id, "start",
                                                          obs.context().get("tenant_id") or who.tenant_id or "default"))
        except sqlite3.IntegrityError:        # lost a race with a concurrent identical request
            with db.connect() as c:
                row = c.execute("SELECT Workflow_ID FROM workflow_runs WHERE Idempotency_Key=? AND Tenant_ID=?",
                                (idempotency_key, obs.context().get("tenant_id") or who.tenant_id or "default")).fetchone()
            return self.get(row[0]), False
        db.supersede_pending()                # a new run replaces any stale pending orders (same behaviour as the console)
        self._submit(wf, initial_state(), who.actor, trace_id)
        return self.get(wf), True

    def get(self, wf: str) -> dict:
        return self._public(self._row(wf))

    def list(self, limit: int = 50, offset: int = 0, status: str | None = None) -> tuple[list[dict], int]:
        conds, params = [], []
        if status:
            conds.append("Status=?"); params.append(status)
        if obs.context().get("tenant_id"):
            conds.append("Tenant_ID=?"); params.append(obs.context()["tenant_id"])
        where = "WHERE " + " AND ".join(conds) if conds else ""
        with db.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM workflow_runs {where}", params).fetchone()[0]
            rows = c.execute(f"SELECT * FROM workflow_runs {where} ORDER BY Created_At DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return [self._public(dict(r)) for r in rows], total

    def decide(self, who: Principal, wf: str, action: str, reason: str = "", edits: dict | None = None, authorized: bool = False) -> dict:
        """action: approve | reject | resource. `edits` ({po_id: {"quantity": int}}) require EDIT_PROCUREMENT and are re-validated by policy.
        `authorized=True` is passed only by enterprise.approvals after it verified the (tiered, possibly multi-step) approval chain."""
        if not authorized:
            who.require(Perm.APPROVE)
        if action not in ("approve", "reject", "resource"):
            raise InvalidRequest("action must be approve, reject or resource")
        row = self._row(wf)
        if row["Status"] in ("RESUMING", "COMPLETED") and row["Stage"] in ("approved", "rejected", "no_vendor", "policy_blocked", "healthy"):
            return self._public(row)        # idempotent: a repeated decision returns the existing outcome
        if row["Status"] != "AWAITING_APPROVAL":
            raise Conflict(f"workflow is {row['Status']}, not awaiting approval")
        cfg = {"configurable": {"thread_id": wf}}
        snap = self.graph.get_state(cfg)
        if not snap.next or snap.next[0] != HITL_NODE:
            raise Conflict("workflow is not paused at the human gate")
        values = {"decision": action, "approver": who.actor, "decision_reason": reason[:500]}
        if edits:
            who.require(Perm.EDIT_PROCUREMENT)
            values.update(self._apply_edits(wf, who, snap.values, edits, reason))
        with self._lock:
            self._update_run(wf, Status="RESUMING")
        self.graph.update_state(cfg, values, as_node="supervisor")
        self._submit(wf, None, who.actor, row["Trace_ID"])
        return self.get(wf)

    @staticmethod
    def _split_body(body: str) -> tuple[str, str]:
        """(intro, closing) of a body produced by graph.compose_body: greeting / intro / table / totals / closing / signature."""
        parts = body.split(chr(10) * 2)
        if len(parts) >= 6:
            return parts[1], parts[-2]
        return "", "Thank you for your continued partnership."

    def edit(self, who: Principal, wf: str, edits: dict, reason: str = "") -> dict:
        """Change quantities while the workflow waits at the gate (policy re-validated, audited) WITHOUT deciding. Returns the refreshed run."""
        who.require(Perm.EDIT_PROCUREMENT)
        row = self._row(wf)
        if row["Status"] != "AWAITING_APPROVAL":
            raise Conflict(f"workflow is {row['Status']}, not awaiting approval")
        cfg = {"configurable": {"thread_id": wf}}
        snap = self.graph.get_state(cfg)
        if not snap.next or snap.next[0] != HITL_NODE:
            raise Conflict("workflow is not paused at the human gate")
        values = self._apply_edits(wf, who, snap.values, edits, reason)
        self.graph.update_state(cfg, values, as_node="supervisor")
        self._finish(wf)
        return self.get(wf)

    def _apply_edits(self, wf: str, who: Principal, v: dict, edits: dict, reason: str) -> dict:
        previous = {p["po_id"]: p["qty"] for p in v["proposals"]}
        proposals = [dict(p) for p in v["proposals"]]
        by_po = {p["po_id"]: p for p in proposals}
        for po, change in edits.items():
            p = by_po.get(po)
            if p is None:
                raise InvalidRequest(f"unknown purchase order {po}")
            qty = change.get("quantity")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise InvalidRequest("edited quantity must be a positive integer")
            p["qty"], p["total"] = qty, round(qty * p["unit_price"], 2)
        rep = policy.PolicyValidator().validate(proposals)       # edits can never bypass the deterministic policy
        if rep.blocked:
            msgs = "; ".join(x.message for x in rep.violations if x.severity == "block")
            audit.safe_record(audit.EventType.POLICY_VIOLATION, wf, who.actor, {"edit_rejected": True, "violations": msgs[:500]})
            raise InvalidRequest(f"edit rejected by policy: {msgs}")
        with db.connect(write=True) as c:                         # PO rows + audit events commit together
            for po, change in edits.items():
                c.execute("UPDATE purchase_orders SET Quantity=? WHERE PO_ID=? AND Status=?", (change["quantity"], po, db.PENDING))
                audit.record(audit.EventType.PROCUREMENT_EDITED, wf, who.actor,
                             {"po_id": po, "field": "quantity", "previous_value": previous[po], "new_value": change["quantity"], "reason": reason}, conn=c)
        drafts = [dict(d) for d in v["drafts"]]
        for d in drafts:
            if not any(l["po_id"] in edits for l in d["lines"]):
                continue
            lines = [by_po[l["po_id"]] for l in d["lines"]]
            intro, closing = self._split_body(d["body"])
            d.update(lines=lines, total=round(sum(l["total"] for l in lines), 2), edited=True, original_body=d["body"])
            d["body"] = compose_body(d["Vendor_Name"], lines, d["total"], intro, closing, datetime.now().strftime("%d %b %Y"))
        return {"proposals": proposals, "drafts": drafts}

    def recover(self) -> dict:
        """Startup recovery for a durable service: runs that were QUEUED / RUNNING / RESUMING when the previous process died are resumed from their last checkpoint
        (nodes are idempotent: orders carry idempotency keys) or marked FAILED when no checkpoint survived. Safe to call repeatedly."""
        out: dict = {"resumed": [], "at_gate": [], "failed": []}
        tenant = obs.context().get("tenant_id")
        with db.connect() as c:
            if tenant:
                stuck = [r[0] for r in c.execute("SELECT Workflow_ID FROM workflow_runs WHERE Status IN ('QUEUED','RUNNING','RESUMING') AND Tenant_ID=?", (tenant,))]
            else:
                stuck = [r[0] for r in c.execute("SELECT Workflow_ID FROM workflow_runs WHERE Status IN ('QUEUED','RUNNING','RESUMING')")]
        for wf in stuck:
            fut = self.futures.get(wf)
            if fut is not None and not fut.done():
                continue                                          # genuinely still running in this process
            snap = self.graph.get_state({"configurable": {"thread_id": wf}})
            if not snap.values:
                self._update_run(wf, Status="FAILED", Error="lost on restart: no checkpoint")
                out["failed"].append(wf)
            elif snap.next and snap.next[0] == HITL_NODE:
                self._finish(wf)
                out["at_gate"].append(wf)
            elif snap.next:
                self._update_run(wf, Status="RESUMING")
                self._submit(wf, None, "system:recovery", self._row(wf)["Trace_ID"])
                out["resumed"].append(wf)
            else:
                self._finish(wf)
                out["at_gate"].append(wf)
        return out

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def require_known(principal: Principal | None) -> Principal:
    if principal is None:
        raise PermissionDenied("authentication required")
    return principal
