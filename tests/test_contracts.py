"""Contract tests: they fail when an interface changes silently (agent state, graph topology, LLM schemas, API shapes, DB tables)."""
import json

from fastapi.testclient import TestClient

from api.main import create_app
from scm import db, graph, qa
from scm.graph import HITL_NODE, NODE_LABEL, SupplyState, build_graph, initial_state
from scm.schemas import DemandAnalysis, ProcurementRecommendation, RiskExplanation, VendorMessage
from scm.security import Role, generate_key, load_keyring
from scm.services import WorkflowService
from ui import config as ui_config

NODES = {"supervisor", "demand", "analytics", "supplier_intel", "procurement", "policy_check", "vendor_comm", HITL_NODE}


def test_graph_topology_is_the_documented_one():
    g = build_graph().get_graph()
    assert {n for n in g.nodes if not n.startswith("__")} == NODES
    edges = {(e.source, e.target) for e in g.edges}
    for worker in NODES - {"supervisor"}:
        assert (worker, "supervisor") in edges                       # every worker reports back to the supervisor
    assert ("__start__", "supervisor") in edges


def test_human_gate_is_an_interrupt_before_dispatch():
    assert build_graph().interrupt_before_nodes == [HITL_NODE]


def test_node_labels_ui_steps_and_qa_nodes_stay_in_sync():
    assert set(NODE_LABEL) == NODES
    assert set(ui_config.STEPS) == NODES
    assert set(qa.NODES) == NODES
    assert {ui_config.AGENT_KEY[a] for a in ui_config.AGENT_KEY} == NODES - {HITL_NODE}
    assert [k for k, _, _ in ui_config.PROGRESS] == [n for n in ui_config.STEPS if n != "supervisor"]


def test_initial_state_only_uses_declared_state_keys():
    assert set(initial_state()) <= set(SupplyState.__annotations__)
    for key in ("workflow_id", "trace_id", "forecast_summary", "supplier_scores", "policy", "approver", "decision_reason"):
        assert key in SupplyState.__annotations__


def test_llm_schema_contracts_are_stable():
    assert set(DemandAnalysis.model_fields) == {"summary"}
    assert set(RiskExplanation.model_fields) == {"insights"}
    assert set(ProcurementRecommendation.model_fields) == {"summary", "notes"}
    assert set(VendorMessage.model_fields) == {"intro", "closing"}
    from scm.schemas import DECISION_FIELDS
    for model in (DemandAnalysis, RiskExplanation, ProcurementRecommendation, VendorMessage):
        assert not set(model.model_fields) & DECISION_FIELDS, f"{model.__name__} must never carry a decision field"


def test_database_contract_tables_and_columns(fresh_db):
    expected = {"inventory", "vendors", "vendor_listings", "purchase_orders", "app_state", "demand_history", "forecasts", "supplier_deliveries",
                "supplier_metrics", "purchase_requests", "audit_events", "workflow_runs", "node_runs", "llm_calls", "domain_events", "schema_migrations"}
    assert expected <= set(db.df("SELECT name FROM sqlite_master WHERE type='table'").name)
    cols = lambda t: set(db.df(f"PRAGMA table_info({t})").name)
    assert {"PO_ID", "Item_ID", "Vendor_ID", "Quantity", "Status", "Unit_Price", "Savings", "Workflow_ID", "Idempotency_Key"} <= cols("purchase_orders")
    assert {"Event_UID", "Ts", "Actor", "Event_Type", "Workflow_ID", "Payload", "Prev_Hash", "Hash"} <= cols("audit_events")
    assert {"MOQ", "Capacity"} <= cols("vendor_listings") and "Region" in cols("vendors")


def test_agent_to_agent_proposal_contract(fresh_db):
    from conftest import run_graph
    _, snap = run_graph(build_graph())
    required = {"Item_ID", "Item_Name", "urgency", "qty", "Vendor_ID", "Vendor_Name", "Contact_Email", "unit_price", "lead_time", "total", "savings", "po_id",
                "share", "split", "optimizer_status", "policy_flags", "quotes", "why"}
    assert all(required <= set(p) for p in snap.values["proposals"])
    draft_keys = {"Vendor_ID", "Vendor_Name", "to", "subject", "body", "po_ids", "total", "lines", "source"}
    assert all(draft_keys <= set(d) for d in snap.values["drafts"])
    trace_keys = {"ts", "agent", "step", "message", "detail", "level", "source"}
    assert all(trace_keys <= set(e) for e in snap.values["trace"])
    alert_keys = {"Item_ID", "urgency", "risk_level", "basis", "days_until_stockout", "stockout_probability", "rec_qty", "predictive"}
    assert all(alert_keys <= set(a) for a in snap.values["alerts"])
    payload = graph.erp_payload(snap.values["drafts"], snap.values["proposals"])
    assert {"event", "schema_version", "approval", "purchase_orders", "batch_total", "notifications"} <= set(payload)


def test_api_contract(fresh_db):
    key, entry = generate_key(Role.ADMIN, "admin")
    svc = WorkflowService()
    with TestClient(create_app(svc, load_keyring(entry))) as c:
        h = {"X-API-Key": key}
        paths = set(c.get("/openapi.json").json()["paths"])
        for path in ("/api/v1/workflows", "/api/v1/workflows/{workflow_id}", "/api/v1/workflows/{workflow_id}/approve", "/api/v1/workflows/{workflow_id}/reject",
                     "/api/v1/workflows/{workflow_id}/resource", "/api/v1/inventory", "/api/v1/suppliers", "/api/v1/metrics", "/api/v1/audit", "/health", "/ready"):
            assert path in paths, path
        run = c.post("/api/v1/workflows", headers=h).json()
        assert set(run) == {"workflow_id", "status", "stage", "created_by", "created_at", "updated_at", "trace_id", "error", "idempotency_key", "result"}
        svc.wait(run["workflow_id"])
        done = c.get(f"/api/v1/workflows/{run['workflow_id']}", headers=h).json()
        assert {"graph_status", "awaiting_approval", "alerts", "policy", "proposals", "drafts", "outcome", "errors", "trace", "rounds"} <= set(done["result"])
        assert {"po_id", "Item_ID", "Vendor_ID", "qty", "unit_price", "total", "risk_level", "why", "policy_flags"} <= set(done["result"]["proposals"][0])
        listing = c.get("/api/v1/workflows", headers=h).json()
        assert set(listing) == {"items", "total", "limit", "offset"}
        audit_body = c.get("/api/v1/audit", headers=h).json()
        assert {"event_id", "event_uid", "timestamp", "actor", "event_type", "workflow_id", "payload"} <= set(audit_body["items"][0])
        err = c.get("/api/v1/workflows/missing", headers=h).json()
        assert set(err) == {"error"} and set(err["error"]) == {"code", "message", "request_id"}
        json.dumps(done)                                           # everything the API returns is JSON-serialisable
    svc.shutdown()


def test_every_audit_event_type_is_documented():
    from scm.audit import EventType
    required = {"WORKFLOW_STARTED", "FORECAST_GENERATED", "RISK_DETECTED", "SUPPLIER_SELECTED", "PROCUREMENT_PROPOSED", "HUMAN_APPROVAL_REQUESTED", "HUMAN_APPROVED",
                "HUMAN_REJECTED", "PROCUREMENT_EDITED", "DISPATCH_STARTED", "DISPATCH_COMPLETED", "DISPATCH_FAILED"}
    assert required <= {e.value for e in EventType}


def test_every_domain_event_name_is_defined():
    from scm.events import EventName
    assert {"InventoryLow", "DemandForecastUpdated", "StockoutRiskDetected", "ProcurementCreated", "ApprovalRequested", "ProcurementApproved",
            "ProcurementRejected", "DispatchCompleted", "PurchaseOrderCreated"} == {e.value for e in EventName}
