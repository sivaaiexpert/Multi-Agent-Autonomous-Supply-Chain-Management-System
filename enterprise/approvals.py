"""Configurable approval workflow on top of the single LangGraph human gate.

The graph still stops at exactly one gate. This service decides WHO may release it, based on the tenant's tiered policy:

    amount <= tier.max_amount  ->  one approval per entry of tier.steps (each entry = minimum approval level of that step)

Capabilities: single / multi-step approval, rejection, edit (resets the chain), re-source, escalation (step overdue -> the next level is required),
timeout (total overdue -> automatic rejection), segregation of duties (one person cannot satisfy two steps). Every action is audited.
The workflow is resumed only when the whole chain is satisfied; reject / re-source act immediately for anyone authorised for the current step.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from scm import audit, db, obs
from scm.security import Perm, PermissionDenied, Principal, Role, approval_level
from scm.services import Conflict, InvalidRequest, NotFound, WorkflowService

from . import tenancy

SYSTEM_ACTOR = "system:approvals"


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _meta_key(wf: str) -> str:
    return f"approval_chain:{wf}"


class ApprovalService:
    def __init__(self, service: WorkflowService):
        self.svc = service

    # ------------------------------------------------------------------ chain creation
    def ensure_chain(self, wf: str, amount: float, rounds: int = 0) -> list[dict]:
        """Idempotent: creates the approval chain for the batch at the gate; recreates it if the batch changed (edit / re-source)."""
        pol = tenancy.current().policy.approval
        meta = json.dumps({"amount": round(amount, 2), "rounds": rounds})
        if db.get_kv(_meta_key(wf)) == meta and self.steps(wf):
            return self.steps(wf)
        levels = pol.chain_for(amount)
        now = _now()
        with db.connect(write=True) as c:
            c.execute("UPDATE approvals SET Status='SUPERSEDED' WHERE Tenant_ID=? AND Workflow_ID=? AND Status IN ('PENDING','ESCALATED')", (_tenant(), wf))
            for i, lvl in enumerate(levels, 1):
                c.execute("INSERT INTO approvals (Tenant_ID, Workflow_ID, Step, Required_Level, Status, Due_At, Created_At) VALUES (?,?,?,?,?,?,?)",
                          (_tenant(), wf, i, lvl, "PENDING", _iso(now + timedelta(hours=pol.step_timeout_hours)) if i == 1 else None, _iso(now)))
            c.execute("INSERT INTO app_state (Key, Value) VALUES (?, ?) ON CONFLICT(Key) DO UPDATE SET Value=excluded.Value", (_meta_key(wf), meta))
            audit.record(audit.EventType.APPROVAL_CHAIN_CREATED, wf, SYSTEM_ACTOR, {"amount": round(amount, 2), "levels": levels, "rounds": rounds}, conn=c)
        return self.steps(wf)

    # ------------------------------------------------------------------ queries
    def steps(self, wf: str, include_superseded: bool = False) -> list[dict]:
        sql = "SELECT Approval_ID, Step, Required_Level, Status, Decided_By, Decided_Role, Decided_At, Reason, Due_At, Created_At FROM approvals " \
              "WHERE Tenant_ID=? AND Workflow_ID=?" + ("" if include_superseded else " AND Status<>'SUPERSEDED'") + " ORDER BY Step, Approval_ID"
        with db.connect() as c:
            rows = c.execute(sql, (_tenant(), wf)).fetchall()
        return [dict(zip(("approval_id", "step", "required_level", "status", "decided_by", "decided_role", "decided_at", "reason", "due_at", "created_at"), r))
                for r in rows]

    def current_step(self, wf: str) -> dict | None:
        pend = [s for s in self.steps(wf) if s["status"] == "PENDING"]
        return min(pend, key=lambda s: s["step"]) if pend else None

    def inbox(self, who: Principal) -> list[dict]:
        """Workflows whose CURRENT step this principal is authorised to action."""
        lvl = approval_level(who.role)
        with db.connect() as c:
            rows = c.execute("SELECT Workflow_ID, MIN(Step), Required_Level, Due_At FROM approvals WHERE Tenant_ID=? AND Status='PENDING' "
                             "GROUP BY Workflow_ID", (_tenant(),)).fetchall()
        return [{"workflow_id": r[0], "step": r[1], "required_level": r[2], "due_at": r[3]} for r in rows if lvl >= r[2]]

    # ------------------------------------------------------------------ acting
    def act(self, who: Principal, wf: str, action: str, reason: str = "", edits: dict | None = None) -> dict:
        """action: approve | reject | resource. With `edits` (approve only) the quantities are changed first, which restarts the chain."""
        if action not in ("approve", "reject", "resource"):
            raise InvalidRequest("action must be approve, reject or resource")
        if not (approval_level(who.role) >= 1):
            raise PermissionDenied(f"role {who.role.value} has no purchasing authority")
        run = self.svc.get(wf)                                            # NotFound if it is not this tenant's workflow
        if run["status"] != "AWAITING_APPROVAL":
            return self.svc.decide(who, wf, action, reason, None, authorized=True)       # idempotent replay / proper Conflict from the service
        if edits:
            if not who.can(Perm.EDIT_PROCUREMENT):
                raise PermissionDenied(f"role {who.role.value} may not edit procurement")
            self.svc.edit(who, wf, edits, reason)
            audit.safe_record(audit.EventType.PROCUREMENT_EDITED, wf, who.actor, {"field": "approval_chain", "previous_value": None, "new_value": "restarted", "edits": edits, "reason": reason})
            self.svc.get(wf)
        steps = self.steps(wf)
        if not steps:                                                      # gate reached before a chain existed (e.g. hooks not installed): create it now
            summary = self.svc.get(wf)["result"] or {}
            self.ensure_chain(wf, sum(p.get("total") or 0 for p in summary.get("proposals", [])), summary.get("rounds", 0))
        with db.connect(write=True) as c:                                  # read-decide-write in ONE transaction: two concurrent approvals cannot both win
            pend = c.execute("SELECT Approval_ID, Step, Required_Level FROM approvals WHERE Tenant_ID=? AND Workflow_ID=? AND Status='PENDING' "
                             "ORDER BY Step LIMIT 1", (_tenant(), wf)).fetchone()
            if pend is None:
                raise Conflict("no approval step is pending for this workflow")
            aid, step, level = pend
            if approval_level(who.role) < level:
                audit.record(audit.EventType.ACCESS_DENIED, wf, who.actor, {"action": action, "needs_level": level, "has_level": approval_level(who.role)}, conn=c)
                raise PermissionDenied(f"step {step} needs approval level {level}; role {who.role.value} has level {approval_level(who.role)}")
            prior = [r[0] for r in c.execute("SELECT Decided_By FROM approvals WHERE Tenant_ID=? AND Workflow_ID=? AND Status='APPROVED'", (_tenant(), wf))]
            if action == "approve" and tenancy.current().policy.approval.distinct_approvers and who.actor in prior:
                raise Conflict("segregation of duties: the same person cannot approve two steps of one purchase")
            status = {"approve": "APPROVED", "reject": "REJECTED", "resource": "REJECTED"}[action]
            c.execute("UPDATE approvals SET Status=?, Decided_By=?, Decided_Role=?, Decided_At=?, Reason=? WHERE Approval_ID=?",
                      (status, who.actor, who.role.value, _iso(_now()), reason[:500], aid))
            remaining = c.execute("SELECT COUNT(*) FROM approvals WHERE Tenant_ID=? AND Workflow_ID=? AND Status='PENDING'", (_tenant(), wf)).fetchone()[0]
            if action == "approve":
                audit.record(audit.EventType.APPROVAL_STEP_APPROVED, wf, who.actor, {"step": step, "level": level, "role": who.role.value, "reason": reason,
                                                                                    "remaining_steps": remaining}, conn=c)
                if remaining:                                              # arm the timer of the next step
                    nxt = c.execute("SELECT Approval_ID FROM approvals WHERE Tenant_ID=? AND Workflow_ID=? AND Status='PENDING' ORDER BY Step LIMIT 1",
                                    (_tenant(), wf)).fetchone()[0]
                    c.execute("UPDATE approvals SET Due_At=? WHERE Approval_ID=?",
                              (_iso(_now() + timedelta(hours=tenancy.current().policy.approval.step_timeout_hours)), nxt))
            else:
                c.execute("UPDATE approvals SET Status='SUPERSEDED' WHERE Tenant_ID=? AND Workflow_ID=? AND Status='PENDING'", (_tenant(), wf))
        if action == "approve" and remaining:
            out = self.svc.get(wf)
            out["approval"] = {"status": "AWAITING_NEXT_APPROVAL", "steps": self.steps(wf)}
            return out
        approvers = [*prior, who.actor] if action == "approve" else [who.actor]
        out = self.svc.decide(who, wf, action, f"{reason} [chain: {', '.join(approvers)}]".strip(), None, authorized=True)
        out["approval"] = {"status": "DECIDED", "steps": self.steps(wf)}
        return out

    # ------------------------------------------------------------------ escalation and timeout
    def process_overdue(self, now: datetime | None = None) -> dict:
        """Escalate steps past their due time; reject workflows past the total timeout. Safe to run repeatedly (e.g. every minute)."""
        now = now or _now()
        pol = tenancy.current().policy.approval
        escalated, expired = [], []
        with db.connect() as c:
            pend = c.execute("SELECT Approval_ID, Workflow_ID, Step, Required_Level, Due_At FROM approvals WHERE Tenant_ID=? AND Status='PENDING' "
                             "AND Due_At IS NOT NULL AND Due_At < ?", (_tenant(), _iso(now))).fetchall()
        for aid, wf, step, level, due in pend:
            with db.connect() as c:
                created = c.execute("SELECT MIN(Created_At) FROM approvals WHERE Tenant_ID=? AND Workflow_ID=?", (_tenant(), wf)).fetchone()[0]
            total_overdue = datetime.fromisoformat(created) + timedelta(hours=pol.total_timeout_hours) < now
            if pol.on_timeout == "reject" or total_overdue or level >= 3:
                with db.connect(write=True) as c:
                    c.execute("UPDATE approvals SET Status='EXPIRED', Decided_At=?, Reason='timeout' WHERE Approval_ID=?", (_iso(now), aid))
                    c.execute("UPDATE approvals SET Status='SUPERSEDED' WHERE Tenant_ID=? AND Workflow_ID=? AND Status='PENDING'", (_tenant(), wf))
                    audit.record(audit.EventType.APPROVAL_EXPIRED, wf, SYSTEM_ACTOR, {"step": step, "level": level, "due_at": due}, conn=c)
                try:
                    self.svc.decide(Principal(SYSTEM_ACTOR, Role.ADMIN, _tenant()), wf, "reject", "approval timed out", None, authorized=True)
                except (Conflict, NotFound):
                    pass
                expired.append(wf)
            else:
                with db.connect(write=True) as c:
                    c.execute("UPDATE approvals SET Status='ESCALATED', Decided_At=?, Reason='step overdue' WHERE Approval_ID=?", (_iso(now), aid))
                    c.execute("INSERT INTO approvals (Tenant_ID, Workflow_ID, Step, Required_Level, Status, Due_At, Created_At) VALUES (?,?,?,?,?,?,?)",
                              (_tenant(), wf, step, level + 1, "PENDING", _iso(now + timedelta(hours=pol.step_timeout_hours)), _iso(now)))
                    audit.record(audit.EventType.APPROVAL_ESCALATED, wf, SYSTEM_ACTOR, {"step": step, "from_level": level, "to_level": level + 1}, conn=c)
                escalated.append(wf)
        return {"escalated": escalated, "expired": expired}


def install_hooks() -> None:
    """Create / refresh the approval chain whenever a workflow pauses at the gate (registered once, idempotent)."""
    from scm import services

    def on_gate(svc: WorkflowService, wf: str, summary: dict) -> None:
        amount = sum((p.get("total") or 0) for p in summary.get("proposals", []))
        ApprovalService(svc).ensure_chain(wf, amount, summary.get("rounds", 0))
    on_gate.__name__ = "approval_chain_on_gate"
    if not any(getattr(h, "__name__", "") == "approval_chain_on_gate" for h in services.GATE_HOOKS):
        services.GATE_HOOKS.append(on_gate)
