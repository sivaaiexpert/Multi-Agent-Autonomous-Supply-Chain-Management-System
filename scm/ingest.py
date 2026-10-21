"""Data ingestion hub: (1) CSV/Excel inventory import, (2) on-premise DB link test, (3) ERP webhook shaping + delivery.

Everything here is defensive: untrusted input is validated, sizes are capped, secrets are never persisted or logged,
and every network call has a hard timeout and returns a result dict instead of raising.
"""
from __future__ import annotations

import html
import re
import base64
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import pandas as pd

from . import db, security

# ====================================================================== 1. CSV / Excel inventory import
REQUIRED = ["Item_ID", "Item_Name", "Stock_Level", "Reorder_Point", "Target_Stock"]
OPTIONAL = ["Category"]
ALIASES = {
    "item_id": "Item_ID", "itemid": "Item_ID", "sku": "Item_ID", "id": "Item_ID", "part_number": "Item_ID", "item_code": "Item_ID",
    "item_name": "Item_Name", "name": "Item_Name", "itemname": "Item_Name", "description": "Item_Name", "item": "Item_Name",
    "stock_level": "Stock_Level", "stock": "Stock_Level", "on_hand": "Stock_Level", "qty": "Stock_Level", "quantity": "Stock_Level", "onhand": "Stock_Level",
    "reorder_point": "Reorder_Point", "reorder": "Reorder_Point", "rop": "Reorder_Point", "min_stock": "Reorder_Point", "min": "Reorder_Point",
    "target_stock": "Target_Stock", "target": "Target_Stock", "max_stock": "Target_Stock", "max": "Target_Stock", "par_level": "Target_Stock",
    "category": "Category", "type": "Category",
}
MAX_BYTES = 5_000_000
MAX_ROWS = 5_000


def template_csv() -> str:
    return ("Item_ID,Item_Name,Category,Stock_Level,Reorder_Point,Target_Stock\n"
            "SKU-2001,Industrial Sensor Kit,Tech Components,12,40,200\n"
            "SKU-2002,Packaging Foam Sheets,Retail Materials,310,150,800\n"
            "SKU-2003,Thermal Label Printer,Peripherals,4,15,60\n")


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    """Parse an uploaded .csv / .xlsx into a DataFrame. Raises ValueError with a user-facing message."""
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"File is {len(data) / 1e6:.1f} MB; the limit is {MAX_BYTES / 1e6:.0f} MB.")
    name = (filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xlsm")):
            df = pd.read_excel(io.BytesIO(data), engine="openpyxl", dtype=object)
        elif name.endswith(".xls"):
            raise ValueError("Legacy .xls is not supported. Save the sheet as .xlsx or .csv and upload again.")
        else:
            last = None
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    df = pd.read_csv(io.BytesIO(data), sep=None, engine="python", dtype=object, encoding=enc)  # sniff , ; tab
                    break
                except UnicodeDecodeError as exc:
                    last = exc
            else:
                raise ValueError(f"Could not decode the file ({last}).")
    except ValueError:
        raise
    except ImportError as exc:
        raise ValueError(f"Excel support needs the 'openpyxl' package ({exc}).") from exc
    except Exception as exc:  # corrupt zip, bad csv, ...
        raise ValueError(f"Could not read the file: {type(exc).__name__}: {str(exc)[:160]}") from exc
    if df.empty:
        raise ValueError("The file has no data rows.")
    if len(df) > MAX_ROWS:
        raise ValueError(f"{len(df):,} rows found; the limit is {MAX_ROWS:,} per import.")
    return df


def normalize_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict, list[str]]:
    """Map flexible headers ('SKU', 'On Hand', ...) onto the canonical schema. Returns (df, mapping, missing_required)."""
    mapping, out = {}, {}
    for col in df.columns:
        key = re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")
        target = ALIASES.get(key) or next((c for c in REQUIRED + OPTIONAL if key == c.lower()), None)
        if target and target not in out:
            out[target] = df[col]
            mapping[str(col)] = target
    norm = pd.DataFrame(out)
    return norm, mapping, [c for c in REQUIRED if c not in norm.columns]


def sanitize_text(text: str) -> str:
    """Plain text only: script/style blocks and every tag are removed, entities are decoded and the result is cleaned AGAIN
    (so entity-encoded markup such as &lt;script&gt; cannot survive), control characters are dropped. Repeats until stable."""
    t = text
    for _ in range(4):
        before = t
        t = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", "", t)
        t = re.sub(r"(?s)<[^>]*>?", "", t)
        t = html.unescape(t)
        if t == before:
            break
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" _-")


def validate_inventory(norm: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Row-level validation. Bad rows are rejected (with reasons); suspicious-but-legal rows only produce warnings."""
    errors, warnings, flags, good = [], [], [], []
    seen: set[str] = set()
    for i, r in norm.iterrows():
        line = int(i) + 2  # +1 header, +1 for 1-based spreadsheet rows
        item_id = str(r.get("Item_ID") or "").strip()
        raw_name = str(r.get("Item_Name") or "").strip()
        name = sanitize_text(raw_name)
        if name != raw_name and name:
            flags.append((line, "MEDIUM", f"{item_id}: markup/script stripped from Item_Name -> '{name[:80]}'"))
        if not item_id or item_id.lower() == "nan":
            errors.append((line, "missing Item_ID")); continue
        if len(item_id) > 64:
            errors.append((line, "Item_ID longer than 64 characters")); continue
        if item_id in seen:
            errors.append((line, f"duplicate Item_ID '{item_id}' (first occurrence kept)")); continue
        if not name or name.lower() == "nan":
            errors.append((line, f"{item_id}: missing Item_Name")); continue
        try:
            nums = {c: float(str(r[c]).replace(",", "").strip()) for c in ("Stock_Level", "Reorder_Point", "Target_Stock")}
        except (TypeError, ValueError):
            errors.append((line, f"{item_id}: Stock_Level / Reorder_Point / Target_Stock must be numbers")); continue
        if any(v != v or abs(v) > 1e9 for v in nums.values()):
            errors.append((line, f"{item_id}: number is NaN or larger than 1,000,000,000")); continue
        if any(v != int(v) for v in nums.values()):
            warnings.append((line, f"{item_id}: decimals were rounded to whole units"))
        stock, rop, target = (int(round(nums[c])) for c in ("Stock_Level", "Reorder_Point", "Target_Stock"))
        if target <= rop:
            errors.append((line, f"{item_id}: BLOCKED: Target_Stock ({target}) <= Reorder_Point ({rop}); a reorder could never reach its target"))
            continue
        if stock < 0:
            flags.append((line, "HIGH", f"{item_id}: NEGATIVE STOCK ({stock}) intercepted; stored as 0, reconcile the source system"))
            stock = 0
        seen.add(item_id)
        cat = str(r.get("Category") or "").strip()
        good.append({"Item_ID": item_id, "Item_Name": name[:200], "Category": (cat if cat.lower() != "nan" else "")[:80] or "Imported",
                     "Stock_Level": stock, "Reorder_Point": rop, "Target_Stock": target})
    clean = pd.DataFrame(good, columns=["Item_ID", "Item_Name", "Category", "Stock_Level", "Reorder_Point", "Target_Stock"])
    return clean, {"rows_in": len(norm), "rows_ok": len(clean), "errors": errors, "warnings": warnings, "flags": flags}


def inventory_backup() -> list[dict]:
    return db.df("SELECT * FROM inventory").to_dict("records")


def apply_inventory(clean: pd.DataFrame, mode: str = "upsert") -> dict:
    """Write validated rows into SQLite in ONE transaction. mode: 'upsert' (insert new + update existing) or 'overwrite'
    (additionally removes items that are not in the file, together with their vendor listings)."""
    if clean.empty:
        return {"inserted": 0, "updated": 0, "removed": 0}
    rows = clean.to_dict("records")
    with db.connect(write=True) as c:
        existing = {r[0] for r in c.execute("SELECT Item_ID FROM inventory")}
        ids = {r["Item_ID"] for r in rows}
        removed = 0
        if mode == "overwrite":
            gone = existing - ids
            for item_id in gone:
                c.execute("DELETE FROM vendor_listings WHERE Item_ID=?", (item_id,))
                c.execute("DELETE FROM inventory WHERE Item_ID=?", (item_id,))
            removed = len(gone)
        for r in rows:
            c.execute("""INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock)
                         VALUES (:Item_ID, :Item_Name, :Category, :Stock_Level, :Reorder_Point, :Target_Stock)
                         ON CONFLICT(Item_ID) DO UPDATE SET Item_Name=excluded.Item_Name, Category=excluded.Category,
                         Stock_Level=excluded.Stock_Level, Reorder_Point=excluded.Reorder_Point, Target_Stock=excluded.Target_Stock""", r)
    return {"inserted": len(ids - existing), "updated": len(ids & existing), "removed": removed}


def restore_inventory(rows: list[dict]) -> int:
    """Undo an import: put the previous inventory table back (items imported since then are removed)."""
    with db.connect(write=True) as c:
        keep = {r["Item_ID"] for r in rows}
        for (item_id,) in c.execute("SELECT Item_ID FROM inventory").fetchall():
            if item_id not in keep:
                c.execute("DELETE FROM vendor_listings WHERE Item_ID=?", (item_id,))
                c.execute("DELETE FROM inventory WHERE Item_ID=?", (item_id,))
        for r in rows:
            c.execute("""INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock)
                         VALUES (:Item_ID, :Item_Name, :Category, :Stock_Level, :Reorder_Point, :Target_Stock)
                         ON CONFLICT(Item_ID) DO UPDATE SET Item_Name=excluded.Item_Name, Category=excluded.Category,
                         Stock_Level=excluded.Stock_Level, Reorder_Point=excluded.Reorder_Point, Target_Stock=excluded.Target_Stock""", r)
    return len(rows)


def seed_listings_for_new_items(ids: list[str], unit_price: float = 10.0) -> int:
    """Imported SKUs have no vendor quotes, so Procurement would skip them. Optionally attach demo quotes from 3 seeded vendors."""
    n = 0
    with db.connect(write=True) as c:
        vendors = [r[0] for r in c.execute("SELECT Vendor_ID FROM vendors ORDER BY Vendor_ID LIMIT 3")]
        for item_id in ids:
            if c.execute("SELECT 1 FROM vendor_listings WHERE Item_ID=?", (item_id,)).fetchone():
                continue
            for k, vid in enumerate(vendors):
                c.execute("INSERT OR IGNORE INTO vendor_listings (Vendor_ID, Item_ID, Unit_Price, Lead_Time_Days) VALUES (?,?,?,?)", (vid, item_id, round(unit_price * (0.92 + 0.08 * k), 2), 3 + 2 * k))
                n += 1
    return n


# ====================================================================== 2. direct on-premise database link test
DB_DEFAULT_PORTS = {"PostgreSQL": 5432, "MySQL": 3306}
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:.]+\]?$")


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def test_connection(dbtype: str, host: str, port, user: str, password: str, dbname: str, timeout: float = 5.0) -> dict:
    """Step-by-step link test: validate -> DNS -> TCP -> driver login + `SELECT version()`.
    Never raises, never returns or logs the password, closes everything it opens."""
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str, t0: float) -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail, "ms": round((time.perf_counter() - t0) * 1000)})
        return ok

    t0 = time.perf_counter()
    host = (host or "").strip()
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = -1
    problems = []
    if dbtype not in DB_DEFAULT_PORTS:
        problems.append("database type must be PostgreSQL or MySQL")
    if not host or not _HOST_RE.match(host):
        problems.append("host is empty or contains illegal characters")
    if not 1 <= port_i <= 65535:
        problems.append("port must be 1-65535")
    if not (user or "").strip():
        problems.append("user is required")
    if not (dbname or "").strip():
        problems.append("database name is required")
    if not step("1 · Validate parameters", not problems, "ok" if not problems else "; ".join(problems), t0):
        return {"ok": False, "state": "invalid", "steps": steps, "message": "Fix the highlighted parameters first."}

    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port_i, type=socket.SOCK_STREAM)
        step("2 · DNS / address resolution", True, f"{host} -> {infos[0][4][0]}", t0)
    except OSError as exc:
        step("2 · DNS / address resolution", False, f"cannot resolve host ({exc})", t0)
        return {"ok": False, "state": "dns", "steps": steps, "message": "The host name could not be resolved from this machine."}

    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port_i), timeout=timeout):
            step("3 · TCP connection", True, f"port {port_i} is reachable", t0)
    except OSError as exc:
        step("3 · TCP connection", False, f"{type(exc).__name__}: {exc}", t0)
        return {"ok": False, "state": "tcp", "steps": steps, "message": "Nothing is listening there (firewall, wrong port, or the server is down)."}

    t0 = time.perf_counter()
    conn = None
    try:
        if dbtype == "PostgreSQL":
            try:
                import psycopg2 as drv
                conn = drv.connect(host=host, port=port_i, user=user, password=password, dbname=dbname, connect_timeout=int(timeout))
            except ImportError:
                import psycopg as drv
                conn = drv.connect(host=host, port=port_i, user=user, password=password, dbname=dbname, connect_timeout=int(timeout))
            sql = "SELECT version()"
        else:
            try:
                import pymysql as drv
                conn = drv.connect(host=host, port=port_i, user=user, password=password, database=dbname, connect_timeout=int(timeout))
            except ImportError:
                import mysql.connector as drv
                conn = drv.connect(host=host, port=port_i, user=user, password=password, database=dbname, connection_timeout=int(timeout))
            sql = "SELECT VERSION()"
        cur = conn.cursor()
        cur.execute(sql)
        version = str(cur.fetchone()[0])[:120]
        cur.close()
        step("4 · Authenticate + query", True, version, t0)
        return {"ok": True, "state": "connected", "steps": steps, "message": "Connection verified (read-only `SELECT version()` only)."}
    except ImportError:
        pkg = "psycopg2-binary" if dbtype == "PostgreSQL" else "pymysql"
        step("4 · Authenticate + query", False, f"no {dbtype} driver installed (pip install {pkg})", t0)
        return {"ok": False, "state": "no_driver", "steps": steps,
                "message": f"The server is reachable, but credentials can't be verified until a driver is installed: pip install {pkg}"}
    except Exception as exc:
        step("4 · Authenticate + query", False, _scrub(f"{type(exc).__name__}: {str(exc)[:200]}", password or ""), t0)
        return {"ok": False, "state": "auth", "steps": steps, "message": "The server answered but rejected the login or database name."}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


# ====================================================================== 3. ERP webhooks
TARGETS = {
    "SAP S/4HANA": {"path": "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder", "note": "OData V2 A_PurchaseOrder (illustrative field mapping)"},
    "Oracle NetSuite": {"path": "/services/rest/record/v1/purchaseOrder", "note": "SuiteTalk REST purchaseOrder record (illustrative)"},
    "Zoho Inventory": {"path": "/inventory/v1/purchaseorders", "note": "Zoho Inventory purchaseorders API (illustrative)"},
    "Generic REST": {"path": "/purchase-orders", "note": "Neutral JSON batch exactly as the agent produces it"},
}
AUTH_TYPES = ["Bearer token", "API key header", "Basic auth"]
_wh_lock = threading.Lock()
WEBHOOK: dict = {"armed": False}     # process memory only; the secret is never written to disk


def sample_payload() -> dict:
    """A realistic batch for previews when no pipeline run exists yet (built from the same function the agents use)."""
    from .graph import erp_payload
    props = [{"po_id": "PO-SAMPLE-0001", "Vendor_ID": "V03", "Vendor_Name": "Pacific Tech Supply", "Contact_Email": "sales@pacifictech.example",
              "Item_ID": "SKU-1005", "Item_Name": "Mechanical Keyboard", "qty": 145, "unit_price": 48.31, "total": 7004.95, "lead_time": 6,
              "urgency": "critical", "savings": 375.07},
             {"po_id": "PO-SAMPLE-0002", "Vendor_ID": "V06", "Vendor_Name": "Helix Micro", "Contact_Email": "b2b@helixmicro.example",
              "Item_ID": "SKU-1010", "Item_Name": "Thermal Paste 4g", "qty": 285, "unit_price": 5.37, "total": 1530.45, "lead_time": 8,
              "urgency": "critical", "savings": 189.24}]
    drafts = [{"to": p["Contact_Email"], "subject": f"Purchase Order {p['po_id']}"} for p in props]
    return erp_payload(drafts, props, "APPROVED")


def format_requests(target: str, payload: dict) -> list[dict]:
    """Translate the neutral batch into per-ERP REST requests ({method, path, body}); one request per purchase order."""
    path = TARGETS.get(target, TARGETS["Generic REST"])["path"]
    today = datetime.now().strftime("%Y-%m-%d")
    reqs = []
    for po in payload.get("purchase_orders", []):
        line = po["lines"][0] if po.get("lines") else {}
        if target == "SAP S/4HANA":
            body = {"PurchaseOrderType": "NB", "CompanyCode": "1000", "PurchasingOrganization": "1000", "PurchasingGroup": "001",
                    "Supplier": po["vendor"]["id"], "DocumentCurrency": payload.get("currency", "USD"), "PurchaseOrderDate": today,
                    "YY1_ExternalReference": po["po_number"],
                    "to_PurchaseOrderItem": {"results": [{"PurchaseOrderItem": "10", "Material": line.get("sku"), "PurchaseOrderItemText": line.get("description"),
                                                          "OrderQuantity": str(line.get("quantity")), "NetPriceAmount": str(line.get("unit_price")), "Plant": "1000"}]}}
        elif target == "Oracle NetSuite":
            body = {"entity": {"refName": po["vendor"]["name"], "externalId": po["vendor"]["id"]}, "tranDate": today, "externalId": po["po_number"],
                    "currency": {"refName": payload.get("currency", "USD")}, "memo": f"Auto-drafted, human-approved ({po.get('priority', '')})",
                    "item": {"items": [{"item": {"refName": line.get("sku")}, "description": line.get("description"),
                                        "quantity": line.get("quantity"), "rate": line.get("unit_price")}]}}
        elif target == "Zoho Inventory":
            body = {"vendor_id": po["vendor"]["id"], "purchaseorder_number": po["po_number"], "date": today, "currency_code": payload.get("currency", "USD"),
                    "line_items": [{"sku": line.get("sku"), "name": line.get("description"), "quantity": line.get("quantity"), "rate": line.get("unit_price")}]}
        else:
            body = {**{k: v for k, v in payload.items() if k not in ("purchase_orders", "notifications")}, "purchase_order": po}
        reqs.append({"method": "POST", "path": path, "body": body})
    return reqs


def build_headers(auth_type: str, secret: str, key_header: str = "X-API-Key", user: str = "") -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "scm-agentic/1.0"}
    if secret:
        if auth_type == "Bearer token":
            h["Authorization"] = f"Bearer {secret}"
        elif auth_type == "API key header":
            h[key_header or "X-API-Key"] = secret
        elif auth_type == "Basic auth":
            h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
    return h


def mask_headers(headers: dict) -> dict:
    return {k: (v[:12] + "••••••••" if k.lower() in ("authorization",) or "key" in k.lower() else v) for k, v in headers.items()}


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[A-Za-z0-9._\-\[\]:]+(:\d+)?(/[^\s]*)?$", (url or "").strip()))


def deliver(cfg: dict, payload: dict, timeout: float = 8.0, limit: int = 50) -> dict:
    """POST every purchase order to the configured ERP. Returns {ok, sent, failed, results[]}; never raises."""
    if not valid_url(cfg.get("base_url", "")):
        return {"ok": False, "sent": 0, "failed": 0, "results": [], "error": "invalid base URL"}
    try:
        security.assert_public_url(cfg["base_url"])             # SSRF guard (re-checked at delivery time)
    except security.UnsafeUrl as exc:
        return {"ok": False, "sent": 0, "failed": 0, "results": [], "error": f"blocked: {exc}"}
    headers = build_headers(cfg.get("auth_type", ""), cfg.get("secret", ""), cfg.get("key_header", "X-API-Key"), cfg.get("user", ""))
    results = []
    for r in format_requests(cfg.get("target", "Generic REST"), payload)[:limit]:
        url = cfg["base_url"].rstrip("/") + r["path"]
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, data=json.dumps(r["body"]).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                results.append({"url": url, "ok": 200 <= resp.status < 300, "status": resp.status, "ms": round((time.perf_counter() - t0) * 1000)})
        except urllib.error.HTTPError as exc:
            results.append({"url": url, "ok": False, "status": exc.code, "ms": round((time.perf_counter() - t0) * 1000), "error": f"HTTP {exc.code}"})
        except Exception as exc:
            msg = str(exc)
            results.append({"url": url, "ok": False, "status": 0, "ms": round((time.perf_counter() - t0) * 1000),
                            "error": (msg.replace(cfg.get("secret", ""), "***") if cfg.get("secret") else msg)[:160]})
    sent = sum(r["ok"] for r in results)
    return {"ok": bool(results) and sent == len(results), "sent": sent, "failed": len(results) - sent, "results": results}


def arm(cfg: dict) -> None:
    with _wh_lock:
        WEBHOOK.clear()
        WEBHOOK.update(cfg, armed=True)


def disarm() -> None:
    with _wh_lock:
        WEBHOOK.clear()
        WEBHOOK["armed"] = False


def dispatch_hook(payload: dict) -> list[dict]:
    """Called by the agents right after a human approves. Returns trace events; a failing ERP never undoes the approval."""
    from .graph import ev
    with _wh_lock:
        cfg = dict(WEBHOOK)
    if not cfg.get("armed"):
        return []
    res = deliver(cfg, payload)
    if res.get("error") and not res["results"]:
        return [ev("Vendor Comms", "ERP webhook", f"Webhook not sent: {res['error']}", level="warn")]
    detail = "\n".join(f"{r['status'] or 'ERR'}  {r['ms']}ms  {r['url']}" + (f"  {r.get('error', '')}" if not r["ok"] else "") for r in res["results"])
    return [ev("Vendor Comms", "ERP webhook", f"{cfg.get('target')}: {res['sent']} delivered, {res['failed']} failed.", detail,
               level="success" if res["ok"] else "warn")]
