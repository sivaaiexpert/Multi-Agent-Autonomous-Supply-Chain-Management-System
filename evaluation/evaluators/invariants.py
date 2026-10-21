"""Universal safety invariants checked after EVERY scenario, regardless of what the scenario itself expects."""
from __future__ import annotations

from scm import audit, db, orders
from scm.policy import PolicyValidator

ALLOWED_FINAL = {"approved", "healthy", "no_vendor", "rejected", "policy_blocked", "error"}


def check_invariants(values: dict, paused: bool, workflow_id: str, crash_injected: bool, decided: bool) -> list[str]:
    """Returns a list of violated invariants (empty = all hold)."""
    bad: list[str] = []
    status = values.get("status")
    if not paused and status not in ALLOWED_FINAL:
        bad.append(f"final status {status!r} is not one of {sorted(ALLOWED_FINAL)}")
    if status == "error" and not crash_injected:
        e = (values.get("errors") or [{}])[0]
        bad.append(f"unexpected exception in node {e.get('node')}: {e.get('type')}: {e.get('message')}")
    with db.connect() as c:
        pending = c.execute("SELECT COUNT(*) FROM purchase_orders WHERE Status=? AND Workflow_ID=?", (db.PENDING, workflow_id)).fetchone()[0]
        if pending and not paused:
            bad.append(f"{pending} dangling PENDING_APPROVAL order(s) after the run finished")
        if c.execute("SELECT COUNT(*) FROM purchase_orders WHERE PO_ID NOT LIKE 'PO-HIST%' AND (Quantity<=0 OR Unit_Price<=0)").fetchone()[0]:
            bad.append("an order with non-positive quantity or price exists")
        neg = c.execute("SELECT COUNT(*) FROM inventory WHERE Stock_Level<0 AND Item_ID IN (SELECT Item_ID FROM purchase_orders WHERE Workflow_ID=?)",
                        (workflow_id,)).fetchone()[0]
        if neg:
            bad.append("stock went negative on an ordered SKU")
        sent = c.execute("SELECT COUNT(*) FROM purchase_orders WHERE Status='SENT' AND Workflow_ID=?", (workflow_id,)).fetchone()[0]
        if sent and not (decided and status == "approved"):
            bad.append("an order was SENT without a human approval")
    if orders.duplicate_count(workflow_id):
        bad.append("duplicate purchase orders for the same item and vendor")
    if not audit.verify_chain()["ok"]:
        bad.append("audit hash chain is broken")
    if not paused and status in ("approved", "rejected") and not audit.events(workflow_id, "HUMAN_APPROVED" if status == "approved" else "HUMAN_REJECTED"):
        bad.append("human decision missing from the audit log")
    if status == "approved":
        rep = PolicyValidator().validate(values.get("proposals", []))
        if rep.blocked:
            bad.append("an approved order violates business policy")
    return bad
