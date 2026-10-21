"""Pull data from an ERP adapter into the tenant, THROUGH the same validated importer used for customer files (no second, laxer path into the database)."""
from __future__ import annotations

import csv
import io

from scm import db

from .. import importer as I
from .. import mapping as M
from .base import ERPAdapter


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _run(entity: str, content: bytes, mapping: M.FieldMapping, actor: str, options: I.ImportOptions | None, apply: bool) -> dict:
    pv = I.preview(entity, content, f"erp-sync-{entity}.csv", actor, mapping, options or I.ImportOptions())
    if not apply or not pv["can_commit"]:
        return {"applied": False, **pv}
    return {"applied": True, "job_id": pv["job_id"], "report": I.commit(pv["job_id"], actor)["report"]}


def sync_inventory(adapter: ERPAdapter, actor: str, apply: bool = True, unknown_sku: str = "reject") -> dict:
    items = adapter.get_inventory()
    mapping = M.FieldMapping(columns={"sku": "sku_id", "quantity": "quantity", **({"warehouse": "warehouse_id"} if any(i.warehouse for i in items) else {})})
    header = ["sku", "quantity"] + (["warehouse"] if "warehouse" in mapping.columns else [])
    rows = [[i.sku, i.quantity] + ([i.warehouse or ""] if "warehouse" in mapping.columns else []) for i in items]
    return _run("inventory", _csv(header, rows), mapping, actor, I.ImportOptions(unknown_sku=unknown_sku), apply)


def sync_suppliers(adapter: ERPAdapter, actor: str, apply: bool = True) -> dict:
    sup = adapter.get_suppliers()
    mapping = M.FieldMapping(columns={"supplier_id": "supplier_id", "name": "name", "email": "email", "region": "region"})
    return _run("suppliers", _csv(["supplier_id", "name", "email", "region"], [[s.supplier_id, s.name, s.email or "", s.region or ""] for s in sup]), mapping, actor, None, apply)


def sync_purchase_orders(adapter: ERPAdapter, actor: str, apply: bool = True) -> dict:
    rows = []
    for po in adapter.get_purchase_orders():
        for ln in po.lines:
            rows.append([f"{po.external_id or po.po_number}", ln.sku, po.supplier_id, ln.quantity, ln.unit_price, "IN_TRANSIT" if po.status in ("CREATED", "IN_TRANSIT") else "RECEIVED",
                         po.order_date or ""])
    mapping = M.FieldMapping(columns={c: c for c in ("po_id", "sku_id", "supplier_id", "quantity", "unit_price", "status", "order_date")})
    return _run("purchase_orders", _csv(["po_id", "sku_id", "supplier_id", "quantity", "unit_price", "status", "order_date"], rows), mapping, actor, None, apply)


def reconcile_inventory(adapter: ERPAdapter, tolerance_units: int = 0) -> dict:
    """Compare the ERP's stock with the platform's books WITHOUT writing anything: the way to detect an inventory mismatch before it drives a purchase."""
    erp: dict[str, int] = {}
    for i in adapter.get_inventory():
        erp[i.sku] = erp.get(i.sku, 0) + i.quantity
    with db.connect() as c:
        local = {r[0]: int(r[1]) for r in c.execute("SELECT Item_ID, Stock_Level FROM inventory")}
    mismatched = [{"sku": s, "platform": local[s], "erp": erp[s], "delta": erp[s] - local[s], "delta_pct": (erp[s] - local[s]) / local[s] if local[s] else None}
                  for s in sorted(set(local) & set(erp)) if abs(erp[s] - local[s]) > tolerance_units]
    return {"compared": len(set(local) & set(erp)), "matched": len(set(local) & set(erp)) - len(mismatched), "mismatched": mismatched,
            "only_in_erp": sorted(set(erp) - set(local)), "only_on_platform": sorted(set(local) - set(erp)), "in_sync": not mismatched}
