"""Customer data import: CSV / JSON / Excel -> mapping -> validation -> data-quality report -> preview -> atomic commit.

    preview()  parses, maps, validates and stores NOTHING in the business tables (the upload is staged in the tenant's own directory)
    commit()   re-validates the staged file and writes it in ONE transaction: any failure rolls the whole import back

Seven entities: products, inventory, suppliers, quotes, demand, purchase_orders, supplier_performance (see `SPECS`). Customer-specific column names,
formats and ids are handled by `enterprise.mapping`, never by code in this file.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from scm import audit, db, obs, suppliers
from scm.ingest import sanitize_text
from scm.security import resolve_within

from . import mapping as M

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 300_000
MAX_XLSX_UNCOMPRESSED = 150 * 1024 * 1024
MAX_ISSUES = 500
PO_STATUSES = ("PENDING_APPROVAL", "SENT", "IN_TRANSIT", "RECEIVED", "REJECTED", "CANCELLED")


class ImportError_(Exception):
    pass


class ImportBlocked(ImportError_):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str                                  # id | str | int | float | date | enum | email
    required: bool = False
    min: float | None = None
    max: float | None = None
    enum: tuple[str, ...] = ()
    default: object = None
    maxlen: int = 200


@dataclass(frozen=True)
class EntitySpec:
    fields: tuple[FieldSpec, ...]
    key: tuple[str, ...]
    append_only: bool = False


SPECS: dict[str, EntitySpec] = {
    "products": EntitySpec((FieldSpec("sku_id", "id", True), FieldSpec("name", "str", True), FieldSpec("category", "str", False, default="Imported", maxlen=80),
                            FieldSpec("reorder_point", "int", True, min=0), FieldSpec("target_stock", "int", True, min=0)), ("sku_id",)),
    "inventory": EntitySpec((FieldSpec("sku_id", "id", True), FieldSpec("quantity", "int", True, min=0), FieldSpec("warehouse_id", "id", False)), ("sku_id", "warehouse_id")),
    "suppliers": EntitySpec((FieldSpec("supplier_id", "id", True), FieldSpec("name", "str", True), FieldSpec("email", "email", False, default="orders@unknown.example"),
                             FieldSpec("region", "str", False, default="GLOBAL", maxlen=40), FieldSpec("reliability", "float", False, min=0, max=100, default=90.0)),
                            ("supplier_id",)),
    "quotes": EntitySpec((FieldSpec("supplier_id", "id", True), FieldSpec("sku_id", "id", True), FieldSpec("unit_price", "float", True, min=0.0000001),
                          FieldSpec("lead_time_days", "int", True, min=0, max=730), FieldSpec("moq", "int", False, min=1, default=1),
                          FieldSpec("capacity", "int", False, min=1)), ("supplier_id", "sku_id")),
    "demand": EntitySpec((FieldSpec("sku_id", "id", True), FieldSpec("date", "date", True), FieldSpec("quantity", "float", True, min=0)), ("sku_id", "date")),
    "purchase_orders": EntitySpec((FieldSpec("po_id", "id", True), FieldSpec("sku_id", "id", True), FieldSpec("supplier_id", "id", True),
                                   FieldSpec("quantity", "int", True, min=1), FieldSpec("unit_price", "float", True, min=0.0000001),
                                   FieldSpec("status", "enum", False, enum=PO_STATUSES, default="RECEIVED"), FieldSpec("order_date", "date", True)),
                                  ("po_id",), append_only=True),
    "supplier_performance": EntitySpec((FieldSpec("supplier_id", "id", True), FieldSpec("sku_id", "id", True), FieldSpec("order_date", "date", True),
                                        FieldSpec("promised_days", "int", True, min=0, max=730), FieldSpec("actual_days", "int", False, min=0, max=1000),
                                        FieldSpec("qty_ordered", "int", True, min=1), FieldSpec("qty_received", "int", False, min=0, default=0),
                                        FieldSpec("qty_defective", "int", False, min=0, default=0), FieldSpec("unit_price", "float", True, min=0.0000001),
                                        FieldSpec("status", "enum", False, enum=("DELIVERED", "CANCELLED"), default="DELIVERED")),
                                       ("supplier_id", "sku_id", "order_date", "qty_ordered", "promised_days"), append_only=True),
}


@dataclass
class ImportOptions:
    allow_partial: bool = True            # import the valid rows even if some are rejected
    max_reject_rate: float = 0.5          # ... unless more than this fraction is rejected (probably the wrong file or mapping)
    unknown_sku: str = "reject"           # reject | create (placeholder product)
    unmapped_ids: str = "pass"            # pass | reject (ids without an id-mapping)
    use_id_mappings: bool = True


@dataclass
class Report:
    entity: str
    filename: str
    file_sha256: str
    processed: int = 0
    valid: int = 0
    rejected: int = 0
    warnings: int = 0
    errors: int = 0
    duplicates_in_file: int = 0
    duplicates_existing: int = 0
    unmapped_columns: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    null_rates: dict[str, float] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    status: str = "PREVIEWED"
    committed: dict = field(default_factory=dict)
    mapping_used: dict = field(default_factory=dict)
    previously_imported: bool = False
    truncated_issues: bool = False

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ---------------------------------------------------------------------------------------------- parsing
def read_table(content: bytes, filename: str, fmt: str | None = None) -> pd.DataFrame:
    """Everything is read as TEXT (no silent type coercion); typing is the validator's job."""
    if not content:
        raise ImportError_("the file is empty")
    if len(content) > MAX_BYTES:
        raise ImportError_(f"file larger than {MAX_BYTES // (1024 * 1024)} MB")
    ext = (fmt or Path(filename or "").suffix.lstrip(".")).lower()
    try:
        if ext in ("csv", "txt", "tsv"):
            sep = "\t" if ext == "tsv" else None
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, sep=sep, engine="python", encoding="utf-8-sig")
        elif ext == "json":
            data = json.loads(content.decode("utf-8-sig"))
            rows = data.get("records", data.get("data")) if isinstance(data, dict) else data
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ImportError_("JSON must be a list of objects or {\"records\": [...]}")
            df = pd.DataFrame(rows).astype(object).where(lambda x: x.notna(), "").astype(str)
        elif ext in ("xlsx", "xlsm"):
            with zipfile.ZipFile(io.BytesIO(content)) as z:             # zip-bomb guard before openpyxl inflates anything
                if sum(i.file_size for i in z.infolist()) > MAX_XLSX_UNCOMPRESSED:
                    raise ImportError_("the workbook expands to an unreasonable size")
            df = pd.read_excel(io.BytesIO(content), dtype=str, keep_default_na=False, engine="openpyxl")
        elif ext == "xls":
            raise ImportError_("legacy .xls is not supported: save as .xlsx or .csv")
        else:
            raise ImportError_(f"unsupported file type {ext!r} (use csv, json or xlsx)")
    except ImportError_:
        raise
    except zipfile.BadZipFile as exc:
        raise ImportError_("the file is not a valid .xlsx workbook") from exc
    except Exception as exc:
        raise ImportError_(f"could not parse the file: {type(exc).__name__}: {str(exc)[:160]}") from exc
    if df.empty:
        raise ImportError_("the file has a header but no data rows")
    if len(df) > MAX_ROWS:
        raise ImportError_(f"more than {MAX_ROWS:,} rows")
    df.columns = [str(c).strip() for c in df.columns]
    return df.astype(str)


# ---------------------------------------------------------------------------------------------- coercion
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/ :#]{0,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _coerce(f: FieldSpec, raw: str) -> tuple[object, str | None]:
    """Returns (value, error). Empty strings are handled by the caller."""
    v = raw.strip()
    try:
        if f.kind == "id":
            if not _ID_RE.match(v):
                return None, "must be 1-64 characters: letters, digits and . _ - / : #"
            return v.strip(), None
        if f.kind == "str":
            clean = sanitize_text(v)[:f.maxlen]
            return (clean, None) if clean else (None, "is empty after removing markup")
        if f.kind == "email":
            return (v.lower(), None) if _EMAIL_RE.match(v) and len(v) <= 200 else (None, "is not a valid e-mail address")
        if f.kind == "enum":
            up = v.upper().replace(" ", "_")
            return (up, None) if up in f.enum else (None, f"must be one of {', '.join(f.enum)}")
        if f.kind == "date":
            for fmt in _DATE_FORMATS:
                try:
                    return datetime.strptime(v, fmt).date().isoformat(), None
                except ValueError:
                    continue
            return None, "is not a recognised date (use YYYY-MM-DD)"
        if f.kind in ("int", "float"):
            txt = v.replace(" ", "")
            ok = re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+(\.\d+)?", txt) or re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", txt)
            num = float(txt.replace(",", "")) if ok else None
            if num is None or num != num or abs(num) > 1e12:
                return None, "must be a number"
            if f.kind == "int":
                if num != int(num):
                    return int(round(num)), "W:decimals were rounded to a whole number"
                return int(num), None
            return num, None
    except Exception as exc:
        return None, f"could not be read ({type(exc).__name__})"
    return v, None


# ---------------------------------------------------------------------------------------------- pipeline
def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _existing(entity: str) -> dict:
    out: dict = {}
    with db.connect() as c:
        out["skus"] = {r[0] for r in c.execute("SELECT Item_ID FROM inventory")}
        out["vendors"] = {r[0] for r in c.execute("SELECT Vendor_ID FROM vendors")}
        if entity == "purchase_orders":
            out["po_ids"] = {r[0] for r in c.execute("SELECT PO_ID FROM purchase_orders")}
        if entity == "supplier_performance":
            out["deliveries"] = {(r[0], r[1], r[2], r[3], r[4]) for r in c.execute(
                "SELECT Vendor_ID, Item_ID, Ordered_At, Qty_Ordered, Promised_Days FROM supplier_deliveries")}
    return out


def build_report(entity: str, df: pd.DataFrame, filename: str, content: bytes, mapping: M.FieldMapping | None, options: ImportOptions) -> tuple[Report, list[dict]]:
    if entity not in SPECS:
        raise ImportError_(f"unknown entity {entity!r}; choose one of {', '.join(SPECS)}")
    spec = SPECS[entity]
    mapping = mapping or M.load_mapping(entity)
    if mapping is None or not mapping.columns:                       # fall back to the platform's own column names when they are all present
        names = {f.name for f in spec.fields}
        mapping = M.FieldMapping(columns={c: c for c in df.columns if c in names})
    rep = Report(entity, filename, _fingerprint(content), processed=len(df), mapping_used=mapping.columns)
    with db.connect() as c:
        rep.previously_imported = c.execute("SELECT COUNT(*) FROM import_jobs WHERE Tenant_ID=? AND Status='COMMITTED' AND Report_JSON LIKE ?",
                                            (obs.context().get("tenant_id") or "default", f'%"{rep.file_sha256}"%')).fetchone()[0] > 0
    unknown_cols = [c for c in mapping.columns if c not in df.columns]
    if unknown_cols:
        raise ImportError_(f"mapping refers to columns that are not in the file: {unknown_cols}")
    rep.unmapped_columns = [c for c in df.columns if c not in mapping.columns]
    mapped_fields = set(mapping.columns.values())
    rep.missing_fields = [f.name for f in spec.fields if f.name not in mapped_fields]
    missing_required = [f.name for f in spec.fields if f.required and f.name not in mapped_fields]
    if missing_required:
        raise ImportError_(f"required field(s) {missing_required} are not mapped to any column. Save a mapping first "
                           f"(suggested: {M.suggest_mapping(entity, list(df.columns))['columns']})")
    id_maps = {"sku_id": M.load_id_mappings("sku"), "supplier_id": M.load_id_mappings("supplier")} if options.use_id_mappings else {}
    ctx = _existing(entity)
    seen: set = set()
    recs: list[dict] = []
    nulls = {f.name: 0 for f in spec.fields}

    def issue(row: int, level: str, fld: str, code: str, msg: str) -> None:
        if level == "error":
            rep.errors += 1
        else:
            rep.warnings += 1
        if len(rep.issues) < MAX_ISSUES:
            rep.issues.append({"row": row, "level": level, "field": fld, "code": code, "message": msg})
        else:
            rep.truncated_issues = True
    inv = {v: k for k, v in mapping.columns.items()}                 # internal field -> customer column
    for idx, row in enumerate(df.to_dict("records")):
        line = idx + 2                                                # +1 header, +1 for 1-based spreadsheet rows
        rec: dict = {}
        bad = False
        for f in spec.fields:
            raw = row.get(inv[f.name], "") if f.name in inv else ""
            raw = str(M.apply_transform(raw, mapping.transforms.get(f.name)))
            if f.kind == "id" and raw and f.name in id_maps:
                if raw in id_maps[f.name]:
                    raw = id_maps[f.name][raw]
                elif options.unmapped_ids == "reject":
                    issue(line, "error", f.name, "UNMAPPED_ID", f"{raw!r} has no id mapping")
                    bad = True
                    continue
            if raw.strip() == "":
                nulls[f.name] += 1
                if f.required:
                    issue(line, "error", f.name, "MISSING_REQUIRED", f"{f.name} is required")
                    bad = True
                else:
                    rec[f.name] = f.default
                continue
            val, err = _coerce(f, raw)
            if err and err.startswith("W:"):
                issue(line, "warning", f.name, "ROUNDED", f"{f.name}: {err[2:]}")
            elif err:
                issue(line, "error", f.name, "INVALID_VALUE", f"{f.name} {err} (got {raw[:40]!r})")
                bad = True
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if f.min is not None and val < f.min:
                    if f.name == "quantity" and entity == "inventory":
                        issue(line, "warning", "quantity", "NEGATIVE_STOCK", f"NEGATIVE STOCK ({val}) intercepted; stored as 0, reconcile the source system")
                        val = 0
                    else:
                        issue(line, "error", f.name, "OUT_OF_RANGE", f"{f.name} must be >= {f.min:g} (got {val})")
                        bad = True
                        continue
                if f.max is not None and val > f.max:
                    issue(line, "error", f.name, "OUT_OF_RANGE", f"{f.name} must be <= {f.max:g} (got {val})")
                    bad = True
                    continue
            rec[f.name] = val
        if bad:
            rep.rejected += 1
            continue
        # ---- entity rules
        if entity == "products" and rec["target_stock"] <= rec["reorder_point"]:
            issue(line, "error", "target_stock", "TARGET_LE_REORDER", f"BLOCKED: target_stock ({rec['target_stock']}) <= reorder_point ({rec['reorder_point']})")
            rep.rejected += 1
            continue
        if entity == "supplier_performance":
            if rec["status"] == "DELIVERED" and rec.get("actual_days") is None:
                issue(line, "error", "actual_days", "MISSING_REQUIRED", "actual_days is required for delivered orders")
                rep.rejected += 1
                continue
            if rec["qty_defective"] > rec["qty_received"]:
                issue(line, "error", "qty_defective", "OUT_OF_RANGE", "qty_defective exceeds qty_received")
                rep.rejected += 1
                continue
            if rec["qty_received"] > rec["qty_ordered"] * 1.5:
                issue(line, "warning", "qty_received", "OVER_DELIVERY", "received more than 150% of the quantity ordered")
        # ---- referential integrity
        ref_bad = False
        if "sku_id" in rec and entity != "products" and rec["sku_id"] not in ctx["skus"]:
            if options.unknown_sku == "create" and entity == "inventory":
                issue(line, "warning", "sku_id", "PLACEHOLDER_PRODUCT", f"unknown SKU {rec['sku_id']!r}: a placeholder product will be created (import products to set its thresholds)")
            else:
                issue(line, "error", "sku_id", "UNKNOWN_SKU", f"SKU {rec['sku_id']!r} does not exist (import products first)")
                ref_bad = True
        if "supplier_id" in rec and entity != "suppliers" and rec["supplier_id"] not in ctx["vendors"]:
            issue(line, "error", "supplier_id", "UNKNOWN_SUPPLIER", f"supplier {rec['supplier_id']!r} does not exist (import suppliers first)")
            ref_bad = True
        if ref_bad:
            rep.rejected += 1
            continue
        # ---- duplicates
        key = tuple(rec.get(k) for k in spec.key)
        if key in seen:
            issue(line, "error", ",".join(spec.key), "DUPLICATE_IN_FILE", f"duplicate {key}: the first occurrence was kept")
            rep.duplicates_in_file += 1
            rep.rejected += 1
            continue
        seen.add(key)
        if entity == "purchase_orders" and rec["po_id"] in ctx["po_ids"]:
            issue(line, "warning", "po_id", "ALREADY_IMPORTED", f"PO {rec['po_id']} already exists: skipped")
            rep.duplicates_existing += 1
            rec["_skip"] = True
        if entity == "supplier_performance" and (rec["supplier_id"], rec["sku_id"], rec["order_date"], rec["qty_ordered"], rec["promised_days"]) in ctx["deliveries"]:
            issue(line, "warning", "order_date", "ALREADY_IMPORTED", "this delivery record already exists: skipped")
            rep.duplicates_existing += 1
            rec["_skip"] = True
        recs.append(rec)
    rep.valid = len(recs)
    rep.null_rates = {k: round(v / max(1, len(df)), 4) for k, v in nulls.items() if v}
    if entity == "demand" and recs:                                    # data-quality extras for time series
        by_sku: dict[str, list[date]] = {}
        for r in recs:
            by_sku.setdefault(r["sku_id"], []).append(date.fromisoformat(r["date"]))
        gappy = [s for s, ds in by_sku.items() if len(ds) >= 14 and (max(ds) - min(ds)).days + 1 > len(ds) * 1.2]
        if gappy:
            issue(0, "warning", "date", "GAPS", f"{len(gappy)} SKU(s) have gaps in their daily history (e.g. {', '.join(gappy[:3])}); gaps are treated as missing, not zero")
    if rep.rejected / max(1, rep.processed) > options.max_reject_rate or rep.rejected and not options.allow_partial:
        rep.status = "BLOCKED"
    else:
        rep.status = "READY"
    return rep, recs


# ---------------------------------------------------------------------------------------------- writers (inside ONE transaction)
def _write(entity: str, recs: list[dict], options: ImportOptions, c) -> dict:
    ins = upd = skipped = 0
    recs_ok = [r for r in recs if not r.get("_skip")]
    skipped = len(recs) - len(recs_ok)
    if entity == "products":
        have = {r[0] for r in c.execute("SELECT Item_ID FROM inventory")}
        for r in recs_ok:
            c.execute("INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock) VALUES (?,?,?,0,?,?) ON CONFLICT(Item_ID) DO UPDATE SET "
                      "Item_Name=excluded.Item_Name, Category=excluded.Category, Reorder_Point=excluded.Reorder_Point, Target_Stock=excluded.Target_Stock",
                      (r["sku_id"], r["name"], r["category"], r["reorder_point"], r["target_stock"]))
            upd, ins = (upd + 1, ins) if r["sku_id"] in have else (upd, ins + 1)
    elif entity == "inventory":
        have = {r[0] for r in c.execute("SELECT Item_ID FROM inventory")}
        touched: set[str] = set()
        for r in recs_ok:
            if r["sku_id"] not in have:                                  # only reachable with unknown_sku == 'create'
                c.execute("INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock) VALUES (?,?,?,?,0,0)",
                          (r["sku_id"], r["sku_id"], "Imported", 0))
                have.add(r["sku_id"])
                ins += 1
            else:
                upd += 1
            if r.get("warehouse_id"):
                tid = obs.context().get("tenant_id") or "default"
                c.execute("INSERT OR IGNORE INTO warehouses (Tenant_ID, Warehouse_ID, Name) VALUES (?,?,?)", (tid, r["warehouse_id"], r["warehouse_id"]))
                c.execute("INSERT INTO inventory_by_warehouse (Tenant_ID, Item_ID, Warehouse_ID, Quantity) VALUES (?,?,?,?) ON CONFLICT(Tenant_ID, Item_ID, Warehouse_ID) "
                          "DO UPDATE SET Quantity=excluded.Quantity", (tid, r["sku_id"], r["warehouse_id"], max(0, r["quantity"])))
                touched.add(r["sku_id"])
            else:
                c.execute("UPDATE inventory SET Stock_Level=? WHERE Item_ID=?", (max(0, r["quantity"]), r["sku_id"]))
        for sku in touched:                                              # the engine plans on the total across warehouses
            c.execute("UPDATE inventory SET Stock_Level=(SELECT COALESCE(SUM(Quantity),0) FROM inventory_by_warehouse WHERE Item_ID=?) WHERE Item_ID=?", (sku, sku))
    elif entity == "suppliers":
        have = {r[0] for r in c.execute("SELECT Vendor_ID FROM vendors")}
        for r in recs_ok:
            c.execute("INSERT INTO vendors (Vendor_ID, Vendor_Name, Base_Price, Contact_Email, Reliability, Region) VALUES (?,?,1.0,?,?,?) ON CONFLICT(Vendor_ID) DO UPDATE SET "
                      "Vendor_Name=excluded.Vendor_Name, Contact_Email=excluded.Contact_Email, Reliability=excluded.Reliability, Region=excluded.Region",
                      (r["supplier_id"], r["name"], r["email"], r["reliability"], r["region"] or "GLOBAL"))
            upd, ins = (upd + 1, ins) if r["supplier_id"] in have else (upd, ins + 1)
    elif entity == "quotes":
        have = {(r[0], r[1]) for r in c.execute("SELECT Vendor_ID, Item_ID FROM vendor_listings")}
        for r in recs_ok:
            c.execute("INSERT INTO vendor_listings (Vendor_ID, Item_ID, Unit_Price, Lead_Time_Days, MOQ, Capacity) VALUES (?,?,?,?,?,?) ON CONFLICT(Vendor_ID, Item_ID) DO UPDATE SET "
                      "Unit_Price=excluded.Unit_Price, Lead_Time_Days=excluded.Lead_Time_Days, MOQ=excluded.MOQ, Capacity=excluded.Capacity",
                      (r["supplier_id"], r["sku_id"], r["unit_price"], r["lead_time_days"], r["moq"], r["capacity"]))
            upd, ins = (upd + 1, ins) if (r["supplier_id"], r["sku_id"]) in have else (upd, ins + 1)
    elif entity == "demand":
        c.executemany("INSERT INTO demand_history (Item_ID, Day, Quantity) VALUES (?,?,?) ON CONFLICT(Item_ID, Day) DO UPDATE SET Quantity=excluded.Quantity",
                      [(r["sku_id"], r["date"], r["quantity"]) for r in recs_ok])
        ins = len(recs_ok)
    elif entity == "purchase_orders":
        tid = obs.context().get("tenant_id") or "default"
        c.executemany("INSERT INTO purchase_orders (PO_ID, Item_ID, Vendor_ID, Quantity, Status, Timestamp, Unit_Price, Savings, Tenant_ID) VALUES (?,?,?,?,?,?,?,0,?)",
                      [(r["po_id"], r["sku_id"], r["supplier_id"], r["quantity"], r["status"], r["order_date"] + " 00:00:00", r["unit_price"], tid) for r in recs_ok])
        ins = len(recs_ok)
    elif entity == "supplier_performance":
        c.executemany("INSERT INTO supplier_deliveries (Vendor_ID, Item_ID, Ordered_At, Promised_Days, Actual_Days, Qty_Ordered, Qty_Received, Qty_Defective, Unit_Price, Status) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      [(r["supplier_id"], r["sku_id"], r["order_date"], r["promised_days"], r["actual_days"] if r["status"] == "DELIVERED" else None, r["qty_ordered"],
                        r["qty_received"] if r["status"] == "DELIVERED" else 0, r["qty_defective"], r["unit_price"], r["status"]) for r in recs_ok])
        ins = len(recs_ok)
    return {"inserted": ins, "updated": upd, "skipped_already_present": skipped}


# ---------------------------------------------------------------------------------------------- jobs
def _staging_dir() -> Path:
    d = db.outbox_dir() / "imports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_job(job_id: str, entity: str, filename: str, status: str, rep: Report, actor: str, committed: bool = False) -> None:
    tid = obs.context().get("tenant_id") or "default"
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect(write=True) as c:
        c.execute("INSERT INTO import_jobs (Tenant_ID, Job_ID, Entity, Filename, Status, Report_JSON, Created_By, Created_At, Committed_At) VALUES (?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(Tenant_ID, Job_ID) DO UPDATE SET Status=excluded.Status, Report_JSON=excluded.Report_JSON, Committed_At=excluded.Committed_At",
                  (tid, job_id, entity, (filename or "")[:200], status, json.dumps(rep.to_dict(), default=str), actor, now, now if committed else None))


def preview(entity: str, content: bytes, filename: str, actor: str, mapping: M.FieldMapping | None = None, options: ImportOptions | None = None,
            fmt: str | None = None) -> dict:
    """Parse + map + validate. Writes only the job record and the staged upload; the business tables are untouched."""
    options = options or ImportOptions()
    df = read_table(content, filename, fmt)
    rep, recs = build_report(entity, df, filename, content, mapping, options)
    job_id = "imp_" + obs.new_id()[:16]
    staged = resolve_within(_staging_dir(), f"{job_id}.bin")
    staged.write_bytes(content)
    (staged.with_suffix(".meta.json")).write_text(json.dumps({"filename": filename, "fmt": fmt, "entity": entity, "options": options.__dict__,
                                                              "mapping": mapping.model_dump() if mapping else None}), encoding="utf-8")
    _save_job(job_id, entity, filename, "PREVIEWED", rep, actor)
    audit.safe_record(audit.EventType.IMPORT_PREVIEWED, None, actor, {"job_id": job_id, "entity": entity, "processed": rep.processed, "valid": rep.valid,
                                                                      "rejected": rep.rejected})
    sample = [{k: v for k, v in r.items() if k != "_skip"} for r in recs[:20]]
    return {"job_id": job_id, "report": rep.to_dict(), "sample": sample, "can_commit": rep.status == "READY"}


def commit(job_id: str, actor: str) -> dict:
    """Re-validate the staged upload and write it atomically. Any exception rolls back EVERYTHING and marks the job FAILED."""
    if not re.match(r"^imp_[a-f0-9]{16}$", job_id or ""):
        raise ImportError_("malformed job id")
    tid = obs.context().get("tenant_id") or "default"
    with db.connect() as c:
        row = c.execute("SELECT Entity, Filename, Status FROM import_jobs WHERE Tenant_ID=? AND Job_ID=?", (tid, job_id)).fetchone()
    if row is None:
        raise ImportError_("unknown import job")
    if row[2] == "COMMITTED":
        raise ImportError_("this import was already committed")
    entity, filename = row[0], row[1]
    staged = resolve_within(_staging_dir(), f"{job_id}.bin")
    if not staged.exists():
        raise ImportError_("the staged upload is no longer available: upload the file again")
    meta = json.loads(staged.with_suffix(".meta.json").read_text(encoding="utf-8"))
    options = ImportOptions(**meta["options"])
    mapping = M.FieldMapping(**meta["mapping"]) if meta.get("mapping") else None
    content = staged.read_bytes()
    df = read_table(content, filename, meta.get("fmt"))
    rep, recs = build_report(entity, df, filename, content, mapping, options)
    if rep.status == "BLOCKED":
        _save_job(job_id, entity, filename, "BLOCKED", rep, actor)
        raise ImportBlocked(f"import blocked: {rep.rejected} of {rep.processed} rows rejected (limit {options.max_reject_rate:.0%}"
                            + ("" if options.allow_partial else ", partial imports disabled") + ")")
    try:
        with db.connect(write=True) as c:                                 # one transaction: all rows or none
            counts = _write(entity, recs, options, c)
            rep.committed, rep.status = counts, "COMMITTED"
            audit.record(audit.EventType.IMPORT_COMMITTED, None, actor, {"job_id": job_id, "entity": entity, "file_sha256": rep.file_sha256, **counts,
                                                                          "rejected": rep.rejected}, conn=c)
    except Exception as exc:
        rep.status, rep.committed = "FAILED", {}
        _save_job(job_id, entity, filename, "FAILED", rep, actor)
        audit.safe_record(audit.EventType.IMPORT_FAILED, None, actor, {"job_id": job_id, "entity": entity, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        raise ImportError_(f"import failed and was rolled back: {type(exc).__name__}: {str(exc)[:200]}") from exc
    suppliers.invalidate_cache()
    _save_job(job_id, entity, filename, "COMMITTED", rep, actor, committed=True)
    for p in (staged, staged.with_suffix(".meta.json")):
        p.unlink(missing_ok=True)
    return {"job_id": job_id, "report": rep.to_dict()}


def list_jobs(limit: int = 50, offset: int = 0) -> list[dict]:
    tid = obs.context().get("tenant_id") or "default"
    with db.connect() as c:
        rows = c.execute("SELECT Job_ID, Entity, Filename, Status, Report_JSON, Created_By, Created_At, Committed_At FROM import_jobs WHERE Tenant_ID=? "
                         "ORDER BY Created_At DESC, Job_ID LIMIT ? OFFSET ?", (tid, limit, offset)).fetchall()
    out = []
    for r in rows:
        rep = json.loads(r[4])
        out.append({"job_id": r[0], "entity": r[1], "filename": r[2], "status": r[3], "processed": rep["processed"], "valid": rep["valid"], "rejected": rep["rejected"],
                    "warnings": rep["warnings"], "errors": rep["errors"], "created_by": r[5], "created_at": r[6], "committed_at": r[7]})
    return out


def get_job(job_id: str) -> dict | None:
    tid = obs.context().get("tenant_id") or "default"
    with db.connect() as c:
        r = c.execute("SELECT Entity, Filename, Status, Report_JSON, Created_By, Created_At FROM import_jobs WHERE Tenant_ID=? AND Job_ID=?", (tid, job_id)).fetchone()
    return None if r is None else {"job_id": job_id, "entity": r[0], "filename": r[1], "status": r[2], "report": json.loads(r[3]), "created_by": r[4], "created_at": r[5]}
