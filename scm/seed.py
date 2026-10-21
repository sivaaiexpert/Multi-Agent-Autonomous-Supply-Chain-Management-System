"""Deterministic synthetic history for demos, tests and the evaluation harness (clearly synthetic: real data arrives via the Provisioning page / ERP).

Nothing here runs implicitly inside `db.init_db()`: the legacy QA matrix relies on a history-free database (static reorder-point rules).
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

from . import db, suppliers

REGIONS = {"V01": "US", "V02": "EU", "V03": "APAC", "V04": "US", "V05": "EU", "V06": "APAC"}
HOT = {"SKU-1002": 1.9, "SKU-1008": 1.6, "SKU-1013": 1.5}     # demand surges: above the static reorder point but about to run out


def seed_demand(days: int = 90, seed: int = 7, only_missing: bool = True) -> int:
    rng = random.Random(seed)
    today = date.today()
    rows = 0
    with db.connect(write=True) as c:
        items = c.execute("SELECT Item_ID, Stock_Level, Reorder_Point FROM inventory").fetchall()
        have = {r[0] for r in c.execute("SELECT DISTINCT Item_ID FROM demand_history")}
        for item_id, _stock, rop in items:
            if only_missing and item_id in have:
                continue
            base = max(0.5, max(rop, 1) / 10.0)
            trend = rng.uniform(-0.002, 0.004)
            phase = rng.uniform(0, 2 * math.pi)
            batch = []
            for i in range(days):
                d = today - timedelta(days=days - i)
                weekly = 1.0 + 0.25 * math.sin(2 * math.pi * d.weekday() / 7 + phase)
                level = base * weekly * (1 + trend * i)
                if item_id in HOT and i > days - 21:
                    level *= 1 + (HOT[item_id] - 1) * (i - (days - 21)) / 21      # surge over the last 3 weeks
                qty = max(0.0, rng.gauss(level, math.sqrt(level) * 0.6))
                batch.append((item_id, d.isoformat(), round(qty, 2)))
            c.executemany("INSERT OR REPLACE INTO demand_history (Item_ID, Day, Quantity) VALUES (?,?,?)", batch)
            rows += len(batch)
    return rows


def seed_suppliers(seed: int = 11, deliveries_per_vendor: int = 40, terms: bool = True) -> int:
    rng = random.Random(seed)
    inserted = 0
    with db.connect(write=True) as c:
        vendors = c.execute("SELECT Vendor_ID, Reliability FROM vendors").fetchall()
        customised = c.execute("SELECT COUNT(*) FROM vendor_listings WHERE MOQ<>1 OR Capacity IS NOT NULL").fetchone()[0]
        if terms and not customised:          # never overwrite MOQ / capacity that someone has configured
            for vid, region in REGIONS.items():
                c.execute("UPDATE vendors SET Region=? WHERE Vendor_ID=?", (region, vid))
            for vid, item in [(r[0], r[1]) for r in c.execute("SELECT Vendor_ID, Item_ID FROM vendor_listings").fetchall()]:
                c.execute("UPDATE vendor_listings SET MOQ=?, Capacity=? WHERE Vendor_ID=? AND Item_ID=?",
                          (rng.choice([10, 20, 25, 50]), rng.choice([150, 300, 500, None]), vid, item))
        have = {r[0] for r in c.execute("SELECT DISTINCT Vendor_ID FROM supplier_deliveries")}
        for vid, rel in vendors:
            if vid in have:
                continue
            listings = c.execute("SELECT Item_ID, Unit_Price, Lead_Time_Days FROM vendor_listings WHERE Vendor_ID=?", (vid,)).fetchall()
            if not listings:
                continue
            p_ok = (rel or 90) / 100.0
            for _ in range(deliveries_per_vendor):
                item, price, lead = rng.choice(listings)
                ordered = rng.choice([40, 60, 100, 150, 200])
                at = (datetime.now() - timedelta(days=rng.randint(10, 170))).strftime("%Y-%m-%d")
                if rng.random() < 0.04 * (1 - p_ok) * 10:
                    c.execute("INSERT INTO supplier_deliveries (Vendor_ID, Item_ID, Ordered_At, Promised_Days, Actual_Days, Qty_Ordered, Qty_Received, "
                              "Qty_Defective, Unit_Price, Status) VALUES (?,?,?,?,?,?,?,?,?,'CANCELLED')",
                              (vid, item, at, lead, None, ordered, 0, 0, round(price * rng.uniform(0.9, 1.1), 2)))
                else:
                    actual = lead if rng.random() < p_ok else lead + rng.randint(1, 5)
                    recv = ordered if rng.random() < p_ok else int(ordered * rng.uniform(0.7, 0.98))
                    defects = int(recv * (1 - p_ok) * rng.uniform(0, 0.25))
                    c.execute("INSERT INTO supplier_deliveries (Vendor_ID, Item_ID, Ordered_At, Promised_Days, Actual_Days, Qty_Ordered, Qty_Received, "
                              "Qty_Defective, Unit_Price, Status) VALUES (?,?,?,?,?,?,?,?,?,'DELIVERED')",
                              (vid, item, at, lead, actual, ordered, recv, defects, round(price * rng.uniform(0.9, 1.1), 2)))
                inserted += 1
    suppliers.invalidate_cache()
    return inserted


def ensure_demo_data() -> dict:
    """Idempotent: fills only what is missing. Used by the Streamlit app and the evaluation runner."""
    d = seed_demand(only_missing=True)
    s = seed_suppliers()
    suppliers.all_metrics(persist=True)
    return {"demand_rows": d, "deliveries": s}
