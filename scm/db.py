"""SQLite data layer: schema, seed data and every query the agents / UI need."""
from __future__ import annotations

import contextvars
import os
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

import pandas as pd

from . import migrations

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("SCM_DB_PATH") or ROOT / "supply_chain.db")  # env overrides let tests avoid the live DB
OUTBOX = Path(os.environ.get("SCM_OUTBOX") or ROOT / "outbox")

PENDING = "PENDING_APPROVAL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    Item_ID TEXT PRIMARY KEY, Item_Name TEXT NOT NULL, Category TEXT,
    Stock_Level INTEGER NOT NULL, Reorder_Point INTEGER NOT NULL, Target_Stock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vendors (
    Vendor_ID TEXT PRIMARY KEY, Vendor_Name TEXT NOT NULL,
    Base_Price REAL NOT NULL,            -- vendor price index (1.00 = market)
    Contact_Email TEXT NOT NULL,
    Reliability REAL DEFAULT 90          -- on-time-in-full score, 0-100
);
CREATE TABLE IF NOT EXISTS vendor_listings (
    Vendor_ID TEXT, Item_ID TEXT, Unit_Price REAL NOT NULL, Lead_Time_Days INTEGER NOT NULL,
    PRIMARY KEY (Vendor_ID, Item_ID)
);
CREATE TABLE IF NOT EXISTS app_state (Key TEXT PRIMARY KEY, Value TEXT);
CREATE TABLE IF NOT EXISTS access_requests (
    Request_ID INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT, Email TEXT, Company TEXT, Message TEXT, Timestamp TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    PO_ID TEXT PRIMARY KEY, Item_ID TEXT, Vendor_ID TEXT, Quantity INTEGER, Status TEXT,
    Timestamp TEXT, Unit_Price REAL, Savings REAL DEFAULT 0
);
"""

# (id, name, category, stock, reorder, target, reference unit price)
ITEMS = [
    ("SKU-1001", "NVMe SSD 1TB", "Tech Components", 18, 40, 200, 62.0),
    ("SKU-1002", "DDR5 RAM 32GB Kit", "Tech Components", 95, 60, 250, 118.0),
    ("SKU-1003", "USB-C Docking Station", "Peripherals", 12, 30, 120, 89.0),
    ("SKU-1004", '27" 4K Monitor', "Peripherals", 40, 25, 100, 310.0),
    ("SKU-1005", "Mechanical Keyboard", "Peripherals", 7, 25, 150, 54.0),
    ("SKU-1006", "Wireless Mouse", "Peripherals", 140, 80, 400, 19.0),
    ("SKU-1007", "Webcam 1080p", "Peripherals", 22, 35, 140, 38.0),
    ("SKU-1008", "Li-ion Battery Pack 20Ah", "Tech Components", 60, 50, 200, 46.0),
    ("SKU-1009", "Cat6 Cable 50ft", "Consumer Goods", 210, 150, 600, 11.0),
    ("SKU-1010", "Thermal Paste 4g", "Consumer Goods", 15, 50, 300, 6.0),
    ("SKU-1011", "Recycled Shipping Boxes", "Retail Materials", 320, 300, 1500, 0.8),
    ("SKU-1012", "Barcode Label Roll", "Retail Materials", 25, 40, 200, 7.0),
    ("SKU-1013", "GaN 100W Charger", "Consumer Goods", 66, 45, 180, 29.0),
    ("SKU-1014", 'Laptop Sleeve 15"', "Consumer Goods", 9, 30, 150, 16.0),
]
VENDORS = [
    ("V01", "Nexa Components", 0.97, "orders@nexa-components.example", 94.0),
    ("V02", "Orion Wholesale", 1.04, "procurement@orion-wholesale.example", 88.0),
    ("V03", "Pacific Tech Supply", 0.93, "sales@pacifictech.example", 91.0),
    ("V04", "Atlas Distribution", 1.00, "po@atlas-dist.example", 97.0),
    ("V05", "BrightLine Retail Supply", 1.08, "orders@brightline.example", 85.0),
    ("V06", "Helix Micro", 0.95, "b2b@helixmicro.example", 90.0),
]


_local = threading.local()          # per-thread DB/outbox override (used by the QA suite; never touches other sessions)
_WAL_READY: set[str] = set()
_WRITE_LOCK = threading.RLock()     # serialises writers inside this process; SQLite busy_timeout covers other processes
BUSY_TIMEOUT_S = 20


def current_path() -> Path:
    return getattr(_local, "path", None) or DB_PATH


def outbox_dir() -> Path:
    return getattr(_local, "outbox", None) or OUTBOX


@contextmanager
def use_path(db_path: Path, outbox: Path | None = None):
    """Redirect this thread (only) to another database / outbox, e.g. a throwaway DB for the QA matrix."""
    old = (getattr(_local, "path", None), getattr(_local, "outbox", None))
    _local.path, _local.outbox = db_path, outbox
    try:
        yield
    finally:
        _local.path, _local.outbox = old


def propagate(fn):
    """Wrap `fn` so that when it runs on ANOTHER thread (pool worker) it sees this thread's database / outbox override and its
    observability context (trace id, agent...). Without this a worker would silently fall back to the live database."""
    ctx, path, outbox = contextvars.copy_context(), current_path(), outbox_dir()

    def runner(*args, **kwargs):
        with use_path(path, outbox):
            return ctx.copy().run(fn, *args, **kwargs)
    return runner


@contextmanager
def connect(write: bool = False):
    """Always-closed SQLite connection: `with sqlite3.connect(..., timeout=20)` commits/rolls back, `finally` closes it
    (a bare `with conn:` never closes -> leaked handles and 'database is locked' under rapid clicks)."""
    if write:
        _WRITE_LOCK.acquire()
    conn = None
    try:
        conn = sqlite3.connect(current_path(), timeout=BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")     # safe in WAL mode (no corruption on crash); avoids an fsync per commit
        path_key = str(current_path())
        if path_key not in _WAL_READY:                # journal_mode is persistent in the file: set it once per path, not on every connection
            try:
                conn.execute("PRAGMA journal_mode=WAL")   # readers never block the writer
            except sqlite3.DatabaseError:
                pass
            _WAL_READY.add(path_key)
        with conn:
            yield conn
    finally:
        if conn is not None:
            conn.close()
        if write:
            _WRITE_LOCK.release()


def init_db(reset: bool = False, seed_demo: bool = True) -> None:
    with connect(write=True) as c:
        if reset:  # drop tables rather than deleting the file: works even while other sessions hold it open (Windows locks)
            migrations.reset_domain_data(c)     # decision data only: the audit log / run history are never reset
            c.executescript("DROP TABLE IF EXISTS purchase_orders; DROP TABLE IF EXISTS vendor_listings; "
                            "DROP TABLE IF EXISTS vendors; DROP TABLE IF EXISTS inventory;")
        c.executescript(SCHEMA)
        migrations.up(c, force=reset)
        if "Reliability" not in [r[1] for r in c.execute("PRAGMA table_info(vendors)")]:  # migrate older DBs
            c.execute("ALTER TABLE vendors ADD COLUMN Reliability REAL DEFAULT 90")
            c.executemany("UPDATE vendors SET Reliability=? WHERE Vendor_ID=?", [(v[4], v[0]) for v in VENDORS])
        if c.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] or not seed_demo:
            return        # customer tenants start empty: demo data is never seeded into them
        rng = random.Random(42)
        c.executemany("INSERT INTO inventory VALUES (?,?,?,?,?,?)", [i[:6] for i in ITEMS])
        c.executemany("INSERT INTO vendors (Vendor_ID, Vendor_Name, Base_Price, Contact_Email, Reliability) VALUES (?,?,?,?,?)", VENDORS)
        for item_id, *_rest, ref in ITEMS:
            for vid, _n, idx, _e, _r in rng.sample(VENDORS, rng.randint(3, 5)):
                price = round(ref * idx * rng.uniform(0.92, 1.12), 2)
                c.execute("INSERT INTO vendor_listings (Vendor_ID, Item_ID, Unit_Price, Lead_Time_Days) VALUES (?,?,?,?)",
                          (vid, item_id, price, rng.randint(2, 10)))
        # historical, already-shipped orders so the savings KPI has context
        now = datetime.now()
        for n, (item, vid, qty, price, sav) in enumerate([
            ("SKU-1002", "V03", 120, 109.4, 1380.0), ("SKU-1004", "V01", 60, 297.0, 2100.0),
            ("SKU-1006", "V06", 300, 17.9, 540.0), ("SKU-1008", "V03", 90, 43.1, 405.0),
            ("SKU-1009", "V04", 400, 10.2, 380.0), ("SKU-1013", "V01", 100, 27.6, 260.0),
        ], 1):
            ts = (now - timedelta(days=30 - n * 4)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("INSERT INTO purchase_orders (PO_ID, Item_ID, Vendor_ID, Quantity, Status, Timestamp, Unit_Price, Savings) VALUES (?,?,?,?,?,?,?,?)",
                      (f"PO-HIST-{n:04d}", item, vid, qty, "SENT", ts, price, sav))


def df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as c:
        return pd.read_sql_query(sql, c, params=params)


def low_stock_items() -> list[dict]:
    d = df("SELECT * FROM inventory WHERE Stock_Level < Reorder_Point ORDER BY Stock_Level * 1.0 / Reorder_Point")
    return d.to_dict("records")


def vendor_quotes(item_id: str) -> list[dict]:
    return df("""SELECT l.Vendor_ID, v.Vendor_Name, v.Contact_Email, v.Reliability, l.Unit_Price, l.Lead_Time_Days,
                        l.MOQ, l.Capacity, v.Region
                 FROM vendor_listings l JOIN vendors v USING (Vendor_ID)
                 WHERE l.Item_ID = ? ORDER BY l.Unit_Price, l.Lead_Time_Days""", (item_id,)).to_dict("records")


def _next_po_id(c: sqlite3.Connection) -> str:
    day = datetime.now().strftime("%Y%m%d")
    n = c.execute("SELECT COUNT(*) FROM purchase_orders WHERE PO_ID LIKE ?", (f"PO-{day}-%",)).fetchone()[0]
    return f"PO-{day}-{n + 1:04d}"


def create_pending_po(item_id: str, vendor_id: str, qty: int, unit_price: float, savings: float) -> str:
    with connect(write=True) as c:
        po = _next_po_id(c)
        c.execute("INSERT INTO purchase_orders (PO_ID, Item_ID, Vendor_ID, Quantity, Status, Timestamp, Unit_Price, Savings) VALUES (?,?,?,?,?,?,?,?)",
                  (po, item_id, vendor_id, qty, PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   unit_price, round(savings, 2)))
    return po


def set_po_status(po_ids: list[str], status: str, receive_stock: bool = False, conn: sqlite3.Connection | None = None) -> None:
    """Pass `conn` to run inside the caller's transaction (status + stock + audit then commit or roll back together)."""
    def _apply(c: sqlite3.Connection) -> None:
        for po in po_ids:
            row = c.execute("SELECT Status FROM purchase_orders WHERE PO_ID=?", (po,)).fetchone()
            if row is None:
                continue
            already = row[0] == status
            c.execute("UPDATE purchase_orders SET Status=? WHERE PO_ID=?", (status, po))
            if receive_stock and not already:  # idempotent: a double approval can never add stock twice
                c.execute("""UPDATE inventory SET Stock_Level = Stock_Level +
                             (SELECT Quantity FROM purchase_orders WHERE PO_ID=?)
                             WHERE Item_ID = (SELECT Item_ID FROM purchase_orders WHERE PO_ID=?)""", (po, po))
    if conn is not None:
        _apply(conn)
        return
    with connect(write=True) as c:
        _apply(c)


def rollback_pending() -> None:
    with connect(write=True) as c:
        c.execute("UPDATE purchase_orders SET Status='ROLLED_BACK' WHERE Status=?", (PENDING,))


def supersede_pending() -> None:
    with connect(write=True) as c:
        c.execute("UPDATE purchase_orders SET Status='SUPERSEDED' WHERE Status=?", (PENDING,))


def add_access_request(name: str, email: str, company: str, message: str) -> int:
    with connect(write=True) as c:
        cur = c.execute("INSERT INTO access_requests (Name, Email, Company, Message, Timestamp) VALUES (?,?,?,?,?)",
                        (name.strip()[:200], email.strip()[:200], company.strip()[:200], message.strip()[:2000],
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return cur.lastrowid


def get_kv(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute("SELECT Value FROM app_state WHERE Key=?", (key,)).fetchone()
    return row[0] if row else default


def set_kv(key: str, value: str | None) -> None:
    with connect(write=True) as c:
        if value is None:
            c.execute("DELETE FROM app_state WHERE Key=?", (key,))
        else:
            c.execute("INSERT INTO app_state (Key, Value) VALUES (?, ?) ON CONFLICT(Key) DO UPDATE SET Value=excluded.Value", (key, value))


def kpis() -> dict:
    with connect() as c:
        return {
            "lines": c.execute("SELECT COUNT(*) FROM inventory").fetchone()[0],
            "low": c.execute("SELECT COUNT(*) FROM inventory WHERE Stock_Level < Reorder_Point").fetchone()[0],
            "pending": c.execute("SELECT COUNT(*) FROM purchase_orders WHERE Status=?", (PENDING,)).fetchone()[0],
            "savings": c.execute("SELECT COALESCE(SUM(Savings),0) FROM purchase_orders WHERE Status='SENT'").fetchone()[0],
        }


def set_stock(item_id: str, level: int) -> None:
    with connect(write=True) as c:
        c.execute("UPDATE inventory SET Stock_Level=? WHERE Item_ID=?", (max(0, int(level)), item_id))


def drain_healthy(n: int = 3) -> list[str]:
    """Supply shock: push n currently-healthy items down to 10% of their reorder point."""
    with connect(write=True) as c:
        rows = c.execute("SELECT Item_ID, Reorder_Point FROM inventory WHERE Stock_Level >= Reorder_Point "
                         "ORDER BY Stock_Level * 1.0 / Reorder_Point DESC LIMIT ?", (n,)).fetchall()
        for r in rows:
            c.execute("UPDATE inventory SET Stock_Level=? WHERE Item_ID=?", (int(r[1] * 0.1), r[0]))
    return [r[0] for r in rows]


def avg_quote(quotes: list[dict]) -> float:
    return mean(q["Unit_Price"] for q in quotes)
