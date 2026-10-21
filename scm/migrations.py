"""Versioned, idempotent schema migrations (a deliberately small runner: the project is SQLite-only, so Alembic/SQLAlchemy would add a
heavy ORM layer for no benefit; see docs/DECISION_LOG.md).

    python -m scm.migrations status | up | down <version>

Rules: every `up` is written with IF NOT EXISTS / guarded ALTERs, so it is safe to re-run; every migration has a `down`.
Version 1 is the legacy baseline created by `db.SCHEMA`.
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

AUDIT_TABLE = "audit_events"


def _cols(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _add_column(c: sqlite3.Connection, table: str, ddl: str) -> None:
    name = ddl.split()[0]
    if table_exists(c, table) and name not in _cols(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _drop_column(c: sqlite3.Connection, table: str, name: str) -> None:
    if table_exists(c, table) and name in _cols(c, table):
        c.execute(f"ALTER TABLE {table} DROP COLUMN {name}")


def table_exists(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# --------------------------------------------------------------------------------------------- v2: decision-intelligence tables
def _up_v2(c: sqlite3.Connection) -> None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS demand_history (
        Item_ID TEXT NOT NULL REFERENCES inventory(Item_ID) ON DELETE CASCADE,
        Day TEXT NOT NULL,
        Quantity REAL NOT NULL CHECK (Quantity >= 0),
        PRIMARY KEY (Item_ID, Day)
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS forecasts (
        Forecast_ID INTEGER PRIMARY KEY AUTOINCREMENT,
        Workflow_ID TEXT, Item_ID TEXT NOT NULL, Created_At TEXT NOT NULL, Method TEXT NOT NULL,
        Horizon_Days INTEGER NOT NULL CHECK (Horizon_Days > 0), Expected_Demand REAL NOT NULL CHECK (Expected_Demand >= 0),
        Confidence REAL NOT NULL CHECK (Confidence BETWEEN 0 AND 1), Daily_JSON TEXT NOT NULL,
        MAE REAL, RMSE REAL, MAPE REAL
    );
    CREATE INDEX IF NOT EXISTS ix_forecasts_item ON forecasts (Item_ID, Created_At);
    CREATE TABLE IF NOT EXISTS supplier_deliveries (
        Delivery_ID INTEGER PRIMARY KEY AUTOINCREMENT,
        Vendor_ID TEXT NOT NULL REFERENCES vendors(Vendor_ID) ON DELETE CASCADE,
        Item_ID TEXT NOT NULL, Ordered_At TEXT NOT NULL,
        Promised_Days INTEGER NOT NULL CHECK (Promised_Days >= 0), Actual_Days INTEGER CHECK (Actual_Days IS NULL OR Actual_Days >= 0),
        Qty_Ordered INTEGER NOT NULL CHECK (Qty_Ordered > 0), Qty_Received INTEGER NOT NULL DEFAULT 0 CHECK (Qty_Received >= 0),
        Qty_Defective INTEGER NOT NULL DEFAULT 0 CHECK (Qty_Defective >= 0),
        Unit_Price REAL NOT NULL CHECK (Unit_Price > 0),
        Status TEXT NOT NULL CHECK (Status IN ('DELIVERED','CANCELLED'))
    );
    CREATE INDEX IF NOT EXISTS ix_deliveries_vendor ON supplier_deliveries (Vendor_ID, Item_ID);
    CREATE TABLE IF NOT EXISTS supplier_metrics (
        Vendor_ID TEXT PRIMARY KEY REFERENCES vendors(Vendor_ID) ON DELETE CASCADE,
        On_Time_Rate REAL, Avg_Lead_Time REAL, Lead_Time_Variance REAL, Quality_Rate REAL, Defect_Rate REAL,
        Historical_Price REAL, Price_Volatility REAL, Fulfillment_Rate REAL, Cancellation_Rate REAL,
        Reliability_Score REAL NOT NULL, Observations INTEGER NOT NULL, Source TEXT NOT NULL, Updated_At TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_requests (
        Request_ID INTEGER PRIMARY KEY AUTOINCREMENT,
        Idempotency_Key TEXT NOT NULL UNIQUE, Workflow_ID TEXT, Item_ID TEXT NOT NULL, Vendor_ID TEXT NOT NULL,
        PO_ID TEXT NOT NULL, Created_At TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_requests_po ON purchase_requests (PO_ID);
    CREATE TABLE IF NOT EXISTS audit_events (
        Event_ID INTEGER PRIMARY KEY AUTOINCREMENT, Event_UID TEXT NOT NULL UNIQUE, Ts TEXT NOT NULL, Actor TEXT NOT NULL,
        Event_Type TEXT NOT NULL, Workflow_ID TEXT, Payload TEXT NOT NULL, Prev_Hash TEXT NOT NULL, Hash TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_audit_wf ON audit_events (Workflow_ID, Event_ID);
    CREATE INDEX IF NOT EXISTS ix_audit_type ON audit_events (Event_Type);
    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
    CREATE TABLE IF NOT EXISTS workflow_runs (
        Workflow_ID TEXT PRIMARY KEY, Idempotency_Key TEXT UNIQUE, Status TEXT NOT NULL, Created_By TEXT NOT NULL,
        Created_At TEXT NOT NULL, Updated_At TEXT NOT NULL, Trace_ID TEXT, Stage TEXT, Result_JSON TEXT, Error TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_runs_status ON workflow_runs (Status, Created_At);
    CREATE TABLE IF NOT EXISTS node_runs (
        Span_ID TEXT PRIMARY KEY, Trace_ID TEXT, Workflow_ID TEXT, Agent TEXT NOT NULL, Node TEXT NOT NULL, Started_At TEXT NOT NULL,
        Duration_MS REAL NOT NULL, Status TEXT NOT NULL, Error TEXT, Model TEXT, Tokens_In INTEGER DEFAULT 0, Tokens_Out INTEGER DEFAULT 0,
        Cost_USD REAL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_spans_wf ON node_runs (Workflow_ID, Started_At);
    CREATE TABLE IF NOT EXISTS llm_calls (
        Call_ID INTEGER PRIMARY KEY AUTOINCREMENT, Trace_ID TEXT, Workflow_ID TEXT, Agent TEXT, Model TEXT, Status TEXT NOT NULL,
        Latency_MS REAL NOT NULL, Tokens_In INTEGER DEFAULT 0, Tokens_Out INTEGER DEFAULT 0, Cost_USD REAL DEFAULT 0,
        Retries INTEGER DEFAULT 0, Fallback INTEGER DEFAULT 0, Ts TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_llm_wf ON llm_calls (Workflow_ID, Ts);
    CREATE TABLE IF NOT EXISTS domain_events (
        Event_ID INTEGER PRIMARY KEY AUTOINCREMENT, Event_UID TEXT NOT NULL UNIQUE, Type TEXT NOT NULL, Workflow_ID TEXT,
        Payload TEXT NOT NULL, Ts TEXT NOT NULL, Published INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_events_unpub ON domain_events (Published, Event_ID);
    CREATE INDEX IF NOT EXISTS ix_inventory_category ON inventory (Category);
    CREATE INDEX IF NOT EXISTS ix_listings_item ON vendor_listings (Item_ID, Unit_Price);
    CREATE INDEX IF NOT EXISTS ix_po_status ON purchase_orders (Status, Item_ID);
    """)
    _add_column(c, "vendor_listings", "MOQ INTEGER NOT NULL DEFAULT 1")
    _add_column(c, "vendor_listings", "Capacity INTEGER")           # NULL = unlimited
    _add_column(c, "vendors", "Region TEXT NOT NULL DEFAULT 'GLOBAL'")
    _add_column(c, "purchase_orders", "Workflow_ID TEXT")
    _add_column(c, "purchase_orders", "Idempotency_Key TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_po_idem ON purchase_orders (Idempotency_Key) WHERE Idempotency_Key IS NOT NULL")


def _down_v2(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS ux_po_idem")
    for t, col in (("purchase_orders", "Idempotency_Key"), ("purchase_orders", "Workflow_ID"), ("vendors", "Region"),
                   ("vendor_listings", "Capacity"), ("vendor_listings", "MOQ")):
        _drop_column(c, t, col)
    c.executescript("""
    DROP INDEX IF EXISTS ix_po_status; DROP INDEX IF EXISTS ix_listings_item; DROP INDEX IF EXISTS ix_inventory_category;
    DROP TRIGGER IF EXISTS audit_no_update; DROP TRIGGER IF EXISTS audit_no_delete;
    DROP TABLE IF EXISTS domain_events; DROP TABLE IF EXISTS llm_calls; DROP TABLE IF EXISTS node_runs;
    DROP TABLE IF EXISTS workflow_runs; DROP TABLE IF EXISTS audit_events; DROP TABLE IF EXISTS purchase_requests;
    DROP TABLE IF EXISTS supplier_metrics; DROP TABLE IF EXISTS supplier_deliveries; DROP TABLE IF EXISTS forecasts;
    DROP TABLE IF EXISTS demand_history;
    """)



# --------------------------------------------------------------------------------------------- v3: multi-tenant platform tables
TENANT_STAMPED = ("audit_events", "workflow_runs", "node_runs", "llm_calls", "domain_events", "forecasts", "purchase_orders", "purchase_requests")
PLATFORM_TABLES = ("warehouses", "inventory_by_warehouse", "organizations", "tenant_policy", "tenant_policy_history", "tenant_models", "onboarding",
                   "integrations", "secrets", "webhook_events_in", "outbound_subscriptions", "outbound_deliveries", "field_mappings", "id_mappings",
                   "import_jobs", "business_metrics", "approvals", "decision_traces", "erp_dispatches", "usage_events", "experiments")


def _up_v3(c: sqlite3.Connection) -> None:
    for t in TENANT_STAMPED:                                   # every customer-owned record carries its tenant
        _add_column(c, t, "Tenant_ID TEXT NOT NULL DEFAULT 'default'")
    _add_column(c, "workflow_runs", "Started_At TEXT")
    _add_column(c, "workflow_runs", "Finished_At TEXT")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS warehouses (
        Tenant_ID TEXT NOT NULL, Warehouse_ID TEXT NOT NULL, Name TEXT NOT NULL, Location TEXT, PRIMARY KEY (Tenant_ID, Warehouse_ID)
    );
    CREATE TABLE IF NOT EXISTS inventory_by_warehouse (
        Tenant_ID TEXT NOT NULL, Item_ID TEXT NOT NULL REFERENCES inventory(Item_ID) ON DELETE CASCADE, Warehouse_ID TEXT NOT NULL,
        Quantity INTEGER NOT NULL CHECK (Quantity >= 0), PRIMARY KEY (Tenant_ID, Item_ID, Warehouse_ID)
    );
    CREATE TABLE IF NOT EXISTS organizations (
        Tenant_ID TEXT NOT NULL, Org_ID TEXT NOT NULL, Name TEXT NOT NULL, Country TEXT, Currency TEXT NOT NULL DEFAULT 'USD', Created_At TEXT NOT NULL,
        PRIMARY KEY (Tenant_ID, Org_ID)
    );
    CREATE TABLE IF NOT EXISTS tenant_policy (
        Tenant_ID TEXT PRIMARY KEY, Version INTEGER NOT NULL, Policy_JSON TEXT NOT NULL, Updated_By TEXT NOT NULL, Updated_At TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tenant_policy_history (
        Tenant_ID TEXT NOT NULL, Version INTEGER NOT NULL, Policy_JSON TEXT NOT NULL, Updated_By TEXT NOT NULL, Updated_At TEXT NOT NULL,
        PRIMARY KEY (Tenant_ID, Version)
    );
    CREATE TABLE IF NOT EXISTS tenant_models (Tenant_ID TEXT PRIMARY KEY, Config_JSON TEXT NOT NULL, Updated_At TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS onboarding (Tenant_ID TEXT PRIMARY KEY, State_JSON TEXT NOT NULL, Updated_At TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS integrations (
        Tenant_ID TEXT NOT NULL, Integration_ID TEXT NOT NULL, Kind TEXT NOT NULL, Name TEXT NOT NULL, Config_JSON TEXT NOT NULL, Secret_ID TEXT,
        Status TEXT NOT NULL DEFAULT 'ACTIVE', Created_At TEXT NOT NULL, PRIMARY KEY (Tenant_ID, Integration_ID)
    );
    CREATE TABLE IF NOT EXISTS secrets (
        Tenant_ID TEXT NOT NULL, Secret_ID TEXT NOT NULL, Ciphertext TEXT NOT NULL, Created_At TEXT NOT NULL, PRIMARY KEY (Tenant_ID, Secret_ID)
    );
    CREATE TABLE IF NOT EXISTS webhook_events_in (
        Tenant_ID TEXT NOT NULL, Source TEXT NOT NULL, Event_ID TEXT NOT NULL, Event_Type TEXT NOT NULL, Received_At TEXT NOT NULL,
        Status TEXT NOT NULL CHECK (Status IN ('RECEIVED','PROCESSED','FAILED','DEAD')), Attempts INTEGER NOT NULL DEFAULT 0, Last_Error TEXT,
        Payload_JSON TEXT NOT NULL, Next_Attempt_At TEXT, Processed_At TEXT, PRIMARY KEY (Tenant_ID, Source, Event_ID)
    );
    CREATE TABLE IF NOT EXISTS outbound_subscriptions (
        Tenant_ID TEXT NOT NULL, Sub_ID TEXT NOT NULL, Url TEXT NOT NULL, Event_Types_JSON TEXT NOT NULL, Secret_ID TEXT NOT NULL,
        Active INTEGER NOT NULL DEFAULT 1, Created_At TEXT NOT NULL, PRIMARY KEY (Tenant_ID, Sub_ID)
    );
    CREATE TABLE IF NOT EXISTS outbound_deliveries (
        Delivery_ID INTEGER PRIMARY KEY AUTOINCREMENT, Tenant_ID TEXT NOT NULL, Sub_ID TEXT NOT NULL, Event_Type TEXT NOT NULL, Event_UID TEXT NOT NULL,
        Payload_JSON TEXT NOT NULL, Status TEXT NOT NULL CHECK (Status IN ('PENDING','DELIVERED','FAILED','DEAD')), Attempts INTEGER NOT NULL DEFAULT 0,
        Last_Error TEXT, Created_At TEXT NOT NULL, Next_Attempt_At TEXT, Delivered_At TEXT, Response_Code INTEGER, UNIQUE (Sub_ID, Event_UID)
    );
    CREATE INDEX IF NOT EXISTS ix_deliveries_due ON outbound_deliveries (Status, Next_Attempt_At);
    CREATE TABLE IF NOT EXISTS field_mappings (
        Tenant_ID TEXT NOT NULL, Entity TEXT NOT NULL, Mapping_JSON TEXT NOT NULL, Transforms_JSON TEXT NOT NULL DEFAULT '{}', Updated_At TEXT NOT NULL,
        PRIMARY KEY (Tenant_ID, Entity)
    );
    CREATE TABLE IF NOT EXISTS id_mappings (
        Tenant_ID TEXT NOT NULL, Entity TEXT NOT NULL, External_ID TEXT NOT NULL, Internal_ID TEXT NOT NULL, PRIMARY KEY (Tenant_ID, Entity, External_ID)
    );
    CREATE TABLE IF NOT EXISTS import_jobs (
        Tenant_ID TEXT NOT NULL, Job_ID TEXT NOT NULL, Entity TEXT NOT NULL, Filename TEXT, Status TEXT NOT NULL, Report_JSON TEXT NOT NULL,
        Created_By TEXT NOT NULL, Created_At TEXT NOT NULL, Committed_At TEXT, PRIMARY KEY (Tenant_ID, Job_ID)
    );
    CREATE TABLE IF NOT EXISTS business_metrics (
        Metric_ID INTEGER PRIMARY KEY AUTOINCREMENT, Tenant_ID TEXT NOT NULL, Metric_Key TEXT NOT NULL, Period TEXT NOT NULL, Value REAL, Unit TEXT NOT NULL,
        Provenance TEXT NOT NULL, Computed_At TEXT NOT NULL, Details_JSON TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_metrics_key ON business_metrics (Tenant_ID, Metric_Key, Period);
    CREATE TABLE IF NOT EXISTS approvals (
        Approval_ID INTEGER PRIMARY KEY AUTOINCREMENT, Tenant_ID TEXT NOT NULL, Workflow_ID TEXT NOT NULL, Step INTEGER NOT NULL, Required_Level INTEGER NOT NULL,
        Status TEXT NOT NULL CHECK (Status IN ('PENDING','APPROVED','REJECTED','ESCALATED','EXPIRED','SUPERSEDED')), Decided_By TEXT, Decided_Role TEXT,
        Decided_At TEXT, Reason TEXT, Due_At TEXT, Created_At TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_approvals_wf ON approvals (Tenant_ID, Workflow_ID, Step);
    CREATE TABLE IF NOT EXISTS decision_traces (
        Tenant_ID TEXT NOT NULL, Workflow_ID TEXT NOT NULL, Trace_JSON TEXT NOT NULL, Created_At TEXT NOT NULL, Updated_At TEXT NOT NULL,
        PRIMARY KEY (Tenant_ID, Workflow_ID)
    );
    CREATE TABLE IF NOT EXISTS erp_dispatches (
        Tenant_ID TEXT NOT NULL, PO_ID TEXT NOT NULL, Integration_ID TEXT NOT NULL, Status TEXT NOT NULL CHECK (Status IN ('PENDING','SENT','FAILED','DEAD')),
        Attempts INTEGER NOT NULL DEFAULT 0, External_ID TEXT, Last_Error TEXT, Idempotency_Key TEXT NOT NULL UNIQUE, Updated_At TEXT NOT NULL,
        PRIMARY KEY (Tenant_ID, PO_ID, Integration_ID)
    );
    CREATE TABLE IF NOT EXISTS usage_events (
        Usage_ID INTEGER PRIMARY KEY AUTOINCREMENT, Tenant_ID TEXT NOT NULL, Ts TEXT NOT NULL, Dimension TEXT NOT NULL, Quantity REAL NOT NULL,
        Cost_USD REAL NOT NULL DEFAULT 0, Ref TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_usage_dim ON usage_events (Tenant_ID, Dimension, Ts);
    CREATE TABLE IF NOT EXISTS experiments (
        Tenant_ID TEXT NOT NULL, Experiment_ID TEXT NOT NULL, Protocol_Hash TEXT NOT NULL, Result_JSON TEXT NOT NULL, Created_At TEXT NOT NULL,
        PRIMARY KEY (Tenant_ID, Experiment_ID)
    );
    CREATE INDEX IF NOT EXISTS ix_runs_tenant ON workflow_runs (Tenant_ID, Created_At);
    CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_events (Tenant_ID, Event_ID);
    CREATE INDEX IF NOT EXISTS ix_llm_tenant ON llm_calls (Tenant_ID, Ts);
    """)


def _down_v3(c: sqlite3.Connection) -> None:
    c.executescript("DROP INDEX IF EXISTS ix_runs_tenant; DROP INDEX IF EXISTS ix_audit_tenant; DROP INDEX IF EXISTS ix_llm_tenant;")
    for t in reversed(PLATFORM_TABLES):
        c.execute(f"DROP TABLE IF EXISTS {t}")
    _drop_column(c, "workflow_runs", "Finished_At")
    _drop_column(c, "workflow_runs", "Started_At")
    for t in TENANT_STAMPED:
        _drop_column(c, t, "Tenant_ID")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None] | None


MIGRATIONS = [
    Migration(1, "legacy baseline (inventory, vendors, listings, purchase_orders, app_state)", lambda c: None, None),
    Migration(2, "decision-intelligence tables, audit log, idempotency, observability, indexes", _up_v2, _down_v2),
    Migration(3, "multi-tenant platform: tenant stamping, policies, approvals, integrations, webhooks, imports, metrics, traces", _up_v3, _down_v3),
]
LATEST = MIGRATIONS[-1].version


def _ensure_table(c: sqlite3.Connection) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (Version INTEGER PRIMARY KEY, Name TEXT NOT NULL, Applied_At TEXT NOT NULL)")


def current_version(c: sqlite3.Connection) -> int:
    _ensure_table(c)
    row = c.execute("SELECT MAX(Version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def up(c: sqlite3.Connection, force: bool = False) -> list[int]:
    """Apply pending migrations (all of them again when force=True: idempotent DDL, used after a data reset)."""
    _ensure_table(c)
    done = {r[0] for r in c.execute("SELECT Version FROM schema_migrations")}
    applied = []
    for m in MIGRATIONS:
        if m.version in done and not force:
            continue
        m.up(c)
        c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)", (m.version, m.name, datetime.now().isoformat(timespec="seconds")))
        applied.append(m.version)
    return applied


def down(c: sqlite3.Connection, target: int) -> list[int]:
    """Roll back to `target` (newest first). The legacy baseline (v1) cannot be rolled back."""
    if target < 1:
        raise ValueError("cannot roll back below the legacy baseline (version 1)")
    _ensure_table(c)
    reverted = []
    for m in reversed(MIGRATIONS):
        if m.version <= target or m.down is None:
            continue
        if c.execute("SELECT 1 FROM schema_migrations WHERE Version=?", (m.version,)).fetchone():
            m.down(c)
            c.execute("DELETE FROM schema_migrations WHERE Version=?", (m.version,))
            reverted.append(m.version)
    return reverted


def reset_domain_data(c: sqlite3.Connection) -> None:
    """Used by db.init_db(reset=True): clear regenerable decision data. The audit log, run history and events are NEVER touched."""
    for t in ("supplier_metrics", "supplier_deliveries", "demand_history", "forecasts", "purchase_requests"):
        if table_exists(c, t):
            c.execute(f"DELETE FROM {t}")


def main(argv: list[str]) -> int:
    from . import db
    cmd = argv[0] if argv else "status"
    if cmd == "up":
        db.init_db()          # creates the legacy baseline tables when the file is new, then applies every pending migration
    with db.connect(write=True) as c:
        if cmd == "up":
            print(f"schema is at version {current_version(c)}")
        elif cmd == "down":
            print("reverted:", down(c, int(argv[1])))
        print(f"database: {db.current_path()}\ncurrent version: {current_version(c)} (latest {LATEST})")
        for r in c.execute("SELECT * FROM schema_migrations ORDER BY Version"):
            print(f"  v{r[0]}  {r[1]}  [{r[2]}]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
