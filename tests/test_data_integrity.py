import sqlite3

import pytest

from scm import audit, db, events, migrations, orders
from scm.audit import EventType
from scm.events import BUS, EventBus, EventName


# ---------------------------------------------------------------- audit log
def test_audit_events_are_recorded_with_all_required_fields(fresh_db):
    uid = audit.record(EventType.HUMAN_APPROVED, "wf-1", "alice", {"po_ids": ["PO-1"], "api_key": "leak"})
    ev = audit.events("wf-1")[0]
    assert ev["event_uid"] == uid and ev["actor"] == "alice" and ev["event_type"] == "HUMAN_APPROVED" and ev["workflow_id"] == "wf-1" and ev["timestamp"]
    assert ev["payload"]["po_ids"] == ["PO-1"] and ev["payload"]["api_key"] == "***REDACTED***"


def test_audit_log_is_append_only(fresh_db):
    audit.record("X", "wf", "a", {})
    with db.connect(write=True) as c, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE audit_events SET Actor='mallory'")
    with db.connect(write=True) as c, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM audit_events")


def test_hash_chain_verifies_and_detects_tampering(fresh_db):
    for i in range(5):
        audit.record("E", "wf", "a", {"i": i})
    assert audit.verify_chain() == {"ok": True, "checked": 5, "first_bad_event_id": None}
    with db.connect(write=True) as c:                      # an attacker with raw DB access drops the guard triggers...
        c.execute("DROP TRIGGER audit_no_update")
        c.execute("UPDATE audit_events SET Payload='{\"i\": 99}' WHERE Event_ID=3")
    res = audit.verify_chain()                              # ...but cannot hide the edit
    assert res["ok"] is False and res["first_bad_event_id"] == 3


def test_audit_rows_join_the_callers_transaction(fresh_db):
    before = audit.count()
    with pytest.raises(RuntimeError):
        with db.connect(write=True) as c:
            audit.record("E", "wf", "a", {}, conn=c)
            raise RuntimeError("business write failed")
    assert audit.count() == before                         # the audit row rolled back with the business write


def test_audit_survives_a_data_reset(fresh_db):
    audit.record("E", "wf", "a", {})
    db.init_db(reset=True)
    assert audit.count() == 1 and audit.verify_chain()["ok"]


def test_audit_filters_and_pagination(fresh_db):
    for i in range(7):
        audit.record("A" if i % 2 else "B", f"wf{i % 2}", "a", {"i": i})
    assert len(audit.events(limit=3)) == 3 and len(audit.events(limit=3, offset=5)) == 2
    assert {e["event_type"] for e in audit.events(event_type="A")} == {"A"}
    assert all(e["workflow_id"] == "wf0" for e in audit.events("wf0"))


# ---------------------------------------------------------------- events
def test_events_are_persisted_before_delivery_and_delivered_to_subscribers(fresh_db):
    bus, seen = EventBus(), []
    bus.subscribe(EventName.INVENTORY_LOW, lambda e: seen.append(e.payload))
    bus.publish(EventName.INVENTORY_LOW, {"count": 2, "password": "x"}, "wf")
    assert seen == [{"count": 2, "password": "***REDACTED***"}]
    assert db.df("SELECT * FROM domain_events").Type[0] == "InventoryLow"


def test_a_failing_subscriber_never_breaks_the_publisher_or_other_subscribers(fresh_db):
    bus, seen = EventBus(), []

    def bad(e):
        raise RuntimeError("consumer down")
    bus.subscribe(EventName.PROCUREMENT_CREATED, bad)
    bus.subscribe(EventName.PROCUREMENT_CREATED, lambda e: seen.append(1))
    bus.publish(EventName.PROCUREMENT_CREATED, {})
    assert seen == [1] and any("consumer down" in x for x in bus.delivery_errors)


def test_outbox_relay_ships_unpublished_events_once(fresh_db):
    bus, sent = EventBus(), []

    class T:
        def send(self, ev):
            sent.append(ev.type)
    bus.publish(EventName.APPROVAL_REQUESTED, {})
    bus.publish(EventName.DISPATCH_COMPLETED, {})
    assert events.pending_outbox() == 2
    assert bus.relay(T()) == 2 and bus.relay(T()) == 0 and events.pending_outbox() == 0
    assert sent == [EventName.APPROVAL_REQUESTED, EventName.DISPATCH_COMPLETED]


def test_relay_failure_leaves_events_unpublished(fresh_db):
    class Down:
        def send(self, ev):
            raise ConnectionError("broker down")
    bus = EventBus()
    bus.publish(EventName.APPROVAL_REQUESTED, {})
    with pytest.raises(ConnectionError):
        bus.relay(Down())
    assert events.pending_outbox() == 1


def test_global_bus_exists():
    assert isinstance(BUS, EventBus)


# ---------------------------------------------------------------- idempotent, transactional orders
def lines(*specs):
    return [{"Item_ID": i, "Vendor_ID": v, "qty": q, "unit_price": p, "savings": 1.0} for i, v, q, p in specs]


def test_same_request_twice_creates_one_purchase_order(fresh_db):
    first = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")
    second = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")
    assert first[0]["created"] and not second[0]["created"] and first[0]["po_id"] == second[0]["po_id"]
    assert db.df("SELECT COUNT(*) n FROM purchase_orders WHERE Workflow_ID='wf-1'").n[0] == 1
    assert orders.duplicate_count("wf-1") == 0


def test_different_workflow_or_round_gets_its_own_order(fresh_db):
    a = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")[0]["po_id"]
    b = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-2")[0]["po_id"]
    c = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1", round_no=1)[0]["po_id"]
    assert len({a, b, c}) == 3


def test_a_bad_line_rolls_back_the_whole_batch(fresh_db):
    before = db.df("SELECT COUNT(*) n FROM purchase_orders").n[0]
    with pytest.raises(ValueError):
        orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0), ("SKU-1002", "V03", 0, 10.0)), "wf-x")
    assert db.df("SELECT COUNT(*) n FROM purchase_orders").n[0] == before
    assert db.df("SELECT COUNT(*) n FROM purchase_requests WHERE Workflow_ID='wf-x'").n[0] == 0
    assert not [e for e in audit.events("wf-x")]


@pytest.mark.parametrize("qty,price", [(-5, 10.0), (0, 10.0), (5, 0.0), (5, -1.0), (2.5, 3.0)])
def test_invalid_order_lines_are_rejected(fresh_db, qty, price):
    with pytest.raises(ValueError):
        orders.create_pending_pos(lines(("SKU-1001", "V01", qty, price)), "wf-bad")


def test_unique_idempotency_key_is_enforced_by_the_database(fresh_db):
    orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")
    with db.connect(write=True) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO purchase_requests (Idempotency_Key, Workflow_ID, Item_ID, Vendor_ID, PO_ID, Created_At) "
                  "SELECT Idempotency_Key, Workflow_ID, Item_ID, Vendor_ID, PO_ID, Created_At FROM purchase_requests LIMIT 1")


def test_finalize_is_atomic_with_stock_receipt_and_audit(fresh_db):
    po = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")[0]["po_id"]
    before = db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0]
    orders.finalize_orders([po], "SENT", True, "wf-1", "alice", EventType.HUMAN_APPROVED)
    assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0] == before + 50
    assert [e["event_type"] for e in audit.events("wf-1")][0] == "HUMAN_APPROVED"


def test_failed_finalize_rolls_everything_back(fresh_db, monkeypatch):
    po = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")[0]["po_id"]
    stock = db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0]

    def boom(*a, **k):
        raise RuntimeError("audit store down")
    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(RuntimeError):
        orders.finalize_orders([po], "SENT", True, "wf-1", "alice", EventType.HUMAN_APPROVED)
    assert db.df("SELECT Status FROM purchase_orders WHERE PO_ID=?", (po,)).Status[0] == db.PENDING
    assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0] == stock


def test_double_approval_never_adds_stock_twice(fresh_db):
    po = orders.create_pending_pos(lines(("SKU-1001", "V01", 50, 60.0)), "wf-1")[0]["po_id"]
    stock = db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0]
    for _ in range(3):
        orders.finalize_orders([po], "SENT", True, "wf-1", "alice", EventType.HUMAN_APPROVED)
    assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0] == stock + 50


# ---------------------------------------------------------------- schema / migrations
def test_migrations_are_idempotent_and_reversible(fresh_db):
    with db.connect(write=True) as c:
        assert migrations.current_version(c) == migrations.LATEST
        assert migrations.up(c) == []                                  # nothing pending
        assert migrations.down(c, 1) == [3, 2]
        assert not migrations.table_exists(c, "audit_events") and "MOQ" not in [r[1] for r in c.execute("PRAGMA table_info(vendor_listings)")]
        assert migrations.up(c) == [2, 3]
        assert migrations.table_exists(c, "audit_events") and "MOQ" in [r[1] for r in c.execute("PRAGMA table_info(vendor_listings)")]
        with pytest.raises(ValueError):
            migrations.down(c, 0)


def test_legacy_database_is_upgraded_in_place(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    c = sqlite3.connect(legacy)
    c.executescript("CREATE TABLE inventory (Item_ID TEXT PRIMARY KEY, Item_Name TEXT NOT NULL, Category TEXT, Stock_Level INTEGER NOT NULL, "
                    "Reorder_Point INTEGER NOT NULL, Target_Stock INTEGER NOT NULL); INSERT INTO inventory VALUES ('A','Thing','c',5,10,50);")
    c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", legacy)
    db.init_db()
    assert db.df("SELECT * FROM inventory").Item_ID.tolist() == ["A"]            # data preserved
    with db.connect() as cx:
        assert migrations.current_version(cx) == migrations.LATEST


def test_foreign_keys_are_enforced(fresh_db):
    with db.connect(write=True) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO demand_history (Item_ID, Day, Quantity) VALUES ('NOPE', '2026-01-01', 1)")


def test_check_constraints_reject_impossible_data(fresh_db):
    with db.connect(write=True) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO demand_history (Item_ID, Day, Quantity) VALUES ('SKU-1001', '2026-01-01', -3)")


def test_deleting_an_item_cascades_to_its_history(demo_db):
    assert db.df("SELECT COUNT(*) n FROM demand_history WHERE Item_ID='SKU-1001'").n[0] > 0
    with db.connect(write=True) as c:
        c.execute("DELETE FROM inventory WHERE Item_ID='SKU-1001'")
    assert db.df("SELECT COUNT(*) n FROM demand_history WHERE Item_ID='SKU-1001'").n[0] == 0


def test_indexes_exist_for_hot_queries(fresh_db):
    names = set(db.df("SELECT name FROM sqlite_master WHERE type='index'").name)
    assert {"ix_po_status", "ix_listings_item", "ix_audit_wf", "ix_spans_wf", "ux_po_idem"} <= names
