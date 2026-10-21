"""Configurable approval workflow: tiers, multi-step, segregation of duties, edit, reject, escalation, timeout, concurrency, tenant isolation."""
import threading
from datetime import datetime, timedelta

import pytest

from conftest import make_tenant
from enterprise import tenancy
from enterprise.approvals import ApprovalService, install_hooks
from enterprise.policies import ApprovalPolicy, ApprovalTier, TenantPolicy
from scm import audit, db
from scm.security import PermissionDenied, Principal, Role
from scm.services import Conflict, NotFound, WorkflowService

T = "acme"


def P(actor, role, tenant=T):
    return Principal(actor, role, tenant)


@pytest.fixture()
def env(plat):
    install_hooks()
    make_tenant(T, "retailer", 0.08)
    yield plat


def run_gate(svc, who=None):
    run, _ = svc.start(who or P("op", Role.OPERATOR))
    done = svc.wait(run["workflow_id"], 120)
    assert done["status"] == "AWAITING_APPROVAL", done
    return run["workflow_id"], done


def amount_of(done):
    return sum(p["total"] for p in done["result"]["proposals"])


def set_policy(approval: ApprovalPolicy, **kw):
    from enterprise import policies
    policies.save_policy(TenantPolicy(approval=approval, **kw), "root")


def test_default_policy_requires_one_approver_level(env):
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, done = run_gate(svc)
        a = ApprovalService(svc)
        steps = a.steps(wf)
        assert [(s["step"], s["required_level"], s["status"]) for s in steps] == [(1, 2, "PENDING")]
        with pytest.raises(PermissionDenied):
            a.act(P("op", Role.OPERATOR), wf, "approve")                  # operators have level 1 only
        with pytest.raises(PermissionDenied):
            a.act(P("view", Role.VIEWER), wf, "approve")
        out = a.act(P("boss", Role.APPROVER), wf, "approve", "ok")
        assert out["approval"]["status"] == "DECIDED"
        assert svc.wait(wf)["stage"] == "approved"
        assert audit.events(wf, "HUMAN_APPROVED")[0]["actor"] == "boss"
        svc.shutdown()


def test_amount_tiers_choose_the_approval_chain(env):
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, done = run_gate(svc)
        amt = amount_of(done)
        assert amt > 0
        a = ApprovalService(svc)
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(max_amount=amt / 2, steps=[1]), ApprovalTier(max_amount=amt * 2, steps=[2]),
                                         ApprovalTier(steps=[2, 3])]))
    with tenancy.scope(T):                                                  # re-enter to load the new policy
        a = ApprovalService(svc)
        assert [s["required_level"] for s in a.ensure_chain(wf, amt / 4)] == [1]
        assert [s["required_level"] for s in a.ensure_chain(wf, amt)] == [2]
        assert [s["required_level"] for s in a.ensure_chain(wf, amt * 5)] == [2, 3]
        svc.shutdown()


def test_small_orders_can_be_approved_by_an_operator_when_the_tier_says_so(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[1])]))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        out = ApprovalService(svc).act(P("op", Role.OPERATOR), wf, "approve")
        assert out["approval"]["status"] == "DECIDED" and svc.wait(wf)["stage"] == "approved"
        svc.shutdown()


def test_multi_step_chain_and_segregation_of_duties(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[2, 3])]))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        first = a.act(P("alice", Role.APPROVER), wf, "approve", "step one")
        assert first["approval"]["status"] == "AWAITING_NEXT_APPROVAL" and first["status"] == "AWAITING_APPROVAL"
        with pytest.raises(Conflict, match="segregation"):
            a.act(P("alice", Role.TENANT_ADMIN), wf, "approve")                # the same person cannot satisfy both steps
        with pytest.raises(PermissionDenied):
            a.act(P("bob", Role.APPROVER), wf, "approve")                      # step 2 needs level 3
        done = a.act(P("carol", Role.TENANT_ADMIN), wf, "approve", "step two")
        assert done["approval"]["status"] == "DECIDED"
        assert svc.wait(wf)["stage"] == "approved"
        assert [e["actor"] for e in audit.events(wf, "APPROVAL_STEP_APPROVED")] == ["carol", "alice"]
        assert "alice, carol" in audit.events(wf, "HUMAN_APPROVED")[0]["payload"]["reason"]
        svc.shutdown()


def test_segregation_can_be_switched_off_by_the_tenant(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[2, 2])], distinct_approvers=False))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        a.act(P("alice", Role.APPROVER), wf, "approve")
        assert a.act(P("alice", Role.APPROVER), wf, "approve")["approval"]["status"] == "DECIDED"
        svc.shutdown()


def test_reject_acts_immediately_and_supersedes_remaining_steps(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[2, 3])]))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        a.act(P("bob", Role.APPROVER), wf, "reject", "price too high")
        assert svc.wait(wf)["stage"] == "rejected"
        statuses = {s["status"] for s in a.steps(wf, include_superseded=True)}
        assert "REJECTED" in statuses and "SUPERSEDED" in statuses and not a.current_step(wf)
        assert not [r for r in db.df("SELECT * FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT'", (wf,)).to_dict("records")]
        svc.shutdown()


def test_resource_loops_back_and_creates_a_fresh_chain(env):
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        a.act(P("bob", Role.APPROVER), wf, "resource", "try other suppliers")
        again = svc.wait(wf, 120)
        assert again["status"] == "AWAITING_APPROVAL" and again["result"]["rounds"] == 1
        cur = a.current_step(wf)
        assert cur is not None and cur["status"] == "PENDING"
        svc.shutdown()


def test_edit_changes_quantities_and_restarts_the_chain(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[2, 3])]))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, done = run_gate(svc)
        a = ApprovalService(svc)
        a.act(P("alice", Role.APPROVER), wf, "approve", "first")
        moq = {(r.Vendor_ID, r.Item_ID): r.MOQ for r in db.df("SELECT * FROM vendor_listings").itertuples()}
        po = next(p for p in done["result"]["proposals"] if p["qty"] - 5 >= moq[(p["Vendor_ID"], p["Item_ID"])])       # a change that policy accepts
        out = a.act(P("bob", Role.APPROVER), wf, "approve", "adjusted", edits={po["po_id"]: {"quantity": po["qty"] - 5}})
        assert out["approval"]["status"] == "AWAITING_NEXT_APPROVAL"           # alice's earlier approval no longer counts: a new chain began
        assert db.df("SELECT Quantity FROM purchase_orders WHERE PO_ID=?", (po["po_id"],)).Quantity[0] == po["qty"] - 5
        edited = [e for e in audit.events(wf, "PROCUREMENT_EDITED") if e["payload"].get("field") == "quantity"]
        assert edited and edited[0]["payload"]["previous_value"] == po["qty"]
        with pytest.raises(PermissionDenied):
            a.act(P("op", Role.OPERATOR), wf, "approve", edits={po["po_id"]: {"quantity": 5}})
        svc.shutdown()


def test_policy_still_validates_edits(env):
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, done = run_gate(svc)
        po = done["result"]["proposals"][0]
        from scm.services import InvalidRequest
        with pytest.raises(InvalidRequest):
            ApprovalService(svc).act(P("bob", Role.APPROVER), wf, "approve", edits={po["po_id"]: {"quantity": 10 ** 9}})
        svc.shutdown()


def test_overdue_steps_escalate_to_the_next_level(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[1])], step_timeout_hours=1, total_timeout_hours=100, on_timeout="escalate"))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        later = datetime.now() + timedelta(hours=2)
        assert a.process_overdue(later)["escalated"] == [wf]
        cur = a.current_step(wf)
        assert cur["required_level"] == 2 and cur["status"] == "PENDING"
        with pytest.raises(PermissionDenied):
            a.act(P("op", Role.OPERATOR), wf, "approve")                       # the operator can no longer approve it
        assert [s["status"] for s in a.steps(wf)] == ["ESCALATED", "PENDING"]
        assert audit.events(wf, "APPROVAL_ESCALATED")
        a.process_overdue(later)                                                # idempotent for a step that is not overdue again yet
        svc.shutdown()


def test_total_timeout_rejects_the_workflow(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[2])], step_timeout_hours=1, total_timeout_hours=2, on_timeout="escalate"))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        out = a.process_overdue(datetime.now() + timedelta(hours=5))
        assert out["expired"] == [wf]
        assert svc.wait(wf)["stage"] == "rejected"
        assert audit.events(wf, "APPROVAL_EXPIRED")
        svc.shutdown()


def test_on_timeout_reject_policy(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[2])], step_timeout_hours=1, on_timeout="reject"))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        assert ApprovalService(svc).process_overdue(datetime.now() + timedelta(hours=2))["expired"] == [wf]
        svc.shutdown()


def test_two_simultaneous_final_approvals_release_the_gate_exactly_once(env):
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        outcomes, ctx_scope = [], tenancy.scope

        def approve(name):
            with ctx_scope(T):
                try:
                    ApprovalService(svc).act(P(name, Role.APPROVER), wf, "approve")
                    outcomes.append("ok")
                except Exception as exc:
                    outcomes.append(type(exc).__name__)
        ts = [threading.Thread(target=approve, args=(f"u{i}",)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert outcomes.count("ok") >= 1
        svc.wait(wf)
        assert len(audit.events(wf, "HUMAN_APPROVED")) == 1                    # the dispatch happened once, whatever the interleaving
        sent = db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID=? AND Status='SENT'", (wf,)).n[0]
        assert sent == len(db.df("SELECT * FROM purchase_orders WHERE Workflow_ID=?", (wf,)))
        svc.shutdown()


def test_an_approver_of_another_tenant_cannot_act(plat):
    install_hooks()
    make_tenant(T, "retailer", 0.08)
    make_tenant("globex", "manufacturer", 0.1)
    with tenancy.scope(T):
        svc_a = WorkflowService()
        wf, _ = run_gate(svc_a)
    with tenancy.scope("globex"):
        svc_b = WorkflowService()
        with pytest.raises(NotFound):
            ApprovalService(svc_b).act(P("mallory", Role.TENANT_ADMIN, "globex"), wf, "approve")
        assert ApprovalService(svc_b).steps(wf) == []
        svc_b.shutdown()
    with tenancy.scope(T):
        assert svc_a.get(wf)["status"] == "AWAITING_APPROVAL"                  # untouched
        svc_a.shutdown()


def test_inbox_lists_only_what_the_role_may_action(env):
    with tenancy.scope(T):
        set_policy(ApprovalPolicy(tiers=[ApprovalTier(steps=[3])]))
    with tenancy.scope(T):
        svc = WorkflowService()
        wf, _ = run_gate(svc)
        a = ApprovalService(svc)
        assert a.inbox(P("op", Role.OPERATOR)) == [] and a.inbox(P("bob", Role.APPROVER)) == []
        assert [i["workflow_id"] for i in a.inbox(P("root", Role.TENANT_ADMIN))] == [wf]
        svc.shutdown()
