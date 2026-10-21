"""Customer-to-platform data mapping.

Nothing here is specific to one company. A tenant stores, per entity:
  * a COLUMN mapping   customer column  -> internal field        (e.g. item_code -> sku_id)
  * per-field TRANSFORMS (strip / case / prefix / value map / date format / multiplier / decimal comma / default), applied by a closed, safe interpreter
  * ID mappings        customer id      -> internal id           (e.g. Customer SKU 'A-100' -> internal 'SKU-100')
Mappings are validated, versioned by `Updated_At`, audited and reusable for every later import.
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scm import audit, db, obs

ENTITIES = ("products", "inventory", "suppliers", "quotes", "demand", "purchase_orders", "supplier_performance")

# internal field -> accepted aliases (lower-case, punctuation-insensitive). Used ONLY for suggestions: the tenant confirms the mapping.
ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "products": {"sku_id": ("sku", "skuid", "itemid", "itemcode", "item", "productcode", "partnumber", "material", "articleno"),
                 "name": ("name", "itemname", "description", "descr", "productname", "title"),
                 "category": ("category", "cat", "group", "productgroup", "family", "class"),
                 "reorder_point": ("reorderpoint", "rop", "reorder", "minstock", "minimumstock", "safetystock"),
                 "target_stock": ("targetstock", "target", "maxstock", "maximumstock", "orderupto", "max")},
    "inventory": {"sku_id": ("sku", "skuid", "itemid", "itemcode", "item", "productcode", "partnumber", "material"),
                  "quantity": ("quantity", "qty", "onhand", "stock", "stocklevel", "available", "balance", "qtyonhand"),
                  "warehouse_id": ("warehouse", "warehouseid", "wh", "location", "site", "plant", "storagelocation")},
    "suppliers": {"supplier_id": ("supplierid", "vendorid", "vendor", "supplier", "suppliercode", "vendorcode", "id"),
                  "name": ("name", "suppliername", "vendorname", "company"),
                  "email": ("email", "contactemail", "orderemail", "mail"),
                  "region": ("region", "country", "area", "zone"),
                  "reliability": ("reliability", "score", "rating", "otif")},
    "quotes": {"supplier_id": ("supplierid", "vendorid", "vendor", "supplier", "suppliercode"),
               "sku_id": ("sku", "skuid", "itemid", "itemcode", "item", "productcode", "partnumber"),
               "unit_price": ("unitprice", "price", "cost", "unitcost", "rate"),
               "lead_time_days": ("leadtimedays", "leadtime", "lead", "deliverydays", "days"),
               "moq": ("moq", "minorderqty", "minimumorderquantity", "minqty"),
               "capacity": ("capacity", "maxqty", "maxorderqty", "supplycapacity")},
    "demand": {"sku_id": ("sku", "skuid", "itemid", "itemcode", "item", "productcode", "partnumber"),
               "date": ("date", "day", "orderdate", "salesdate", "txndate", "postingdate"),
               "quantity": ("quantity", "qty", "demand", "sold", "units", "consumption", "issued")},
    "purchase_orders": {"po_id": ("poid", "ponumber", "pono", "orderid", "purchaseorder", "ordernumber"),
                        "sku_id": ("sku", "skuid", "itemid", "itemcode", "item", "productcode"),
                        "supplier_id": ("supplierid", "vendorid", "vendor", "supplier"),
                        "quantity": ("quantity", "qty", "orderedqty", "orderquantity"),
                        "unit_price": ("unitprice", "price", "cost", "unitcost"),
                        "status": ("status", "state", "postatus"),
                        "order_date": ("orderdate", "date", "createdon", "podate")},
    "supplier_performance": {"supplier_id": ("supplierid", "vendorid", "vendor", "supplier"),
                             "sku_id": ("sku", "skuid", "itemid", "itemcode", "item"),
                             "order_date": ("orderdate", "date", "podate", "ordered"),
                             "promised_days": ("promiseddays", "promisedleadtime", "promised", "quotedlead", "plannedlead"),
                             "actual_days": ("actualdays", "actualleadtime", "actual", "deliverydays", "realizedlead"),
                             "qty_ordered": ("qtyordered", "orderedqty", "quantityordered", "ordered"),
                             "qty_received": ("qtyreceived", "receivedqty", "quantityreceived", "received", "delivered"),
                             "qty_defective": ("qtydefective", "defects", "defective", "rejected", "rejectedqty"),
                             "unit_price": ("unitprice", "price", "cost"),
                             "status": ("status", "state")},
}


for _fields in ALIASES.values():                       # ERP vocabulary for the item key is the same everywhere
    if "sku_id" in _fields:
        _fields["sku_id"] = tuple(dict.fromkeys((*_fields["sku_id"], "material", "materialnumber", "article", "articleno", "stockcode")))


class Transform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strip: bool = True
    case: Literal["none", "upper", "lower"] = "none"
    remove_prefix: str | None = Field(None, max_length=40)
    add_prefix: str | None = Field(None, max_length=40)
    value_map: dict[str, str] = Field(default_factory=dict, max_length=1000)
    date_format: str | None = Field(None, max_length=40)          # strptime format of the customer's dates
    multiplier: float | None = None                                # unit conversion (e.g. cases -> units)
    decimal_comma: bool = False                                    # '1.234,56' style numbers
    default: str | None = Field(None, max_length=200)             # used when the customer value is empty


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, str] = Field(default_factory=dict, max_length=200)      # customer column -> internal field
    transforms: dict[str, Transform] = Field(default_factory=dict)             # internal field -> transform


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def suggest_mapping(entity: str, columns: list[str]) -> dict:
    """Best-guess column mapping with a confidence per column; the tenant reviews and saves it. Each internal field is used at most once."""
    if entity not in ALIASES:
        raise ValueError(f"unknown entity {entity!r}")
    out, used, scored = {}, set(), []
    for col in columns:
        n = norm(col)
        for field, aliases in ALIASES[entity].items():
            exact = n == norm(field) or n in aliases
            sim = max([difflib.SequenceMatcher(None, n, a).ratio() for a in (norm(field), *aliases)] + [0.0])
            scored.append((1.0 if exact else sim, col, field))
    for score, col, field in sorted(scored, key=lambda t: -t[0]):
        if score < 0.75 or col in out or field in used:
            continue
        out[col] = {"field": field, "confidence": round(score, 2)}
        used.add(field)
    return {"columns": {c: v["field"] for c, v in out.items()}, "confidence": {c: v["confidence"] for c, v in out.items()},
            "unmapped_columns": [c for c in columns if c not in out], "missing_fields": [f for f in ALIASES[entity] if f not in used]}


def apply_transform(value: object, t: Transform | None) -> object:
    """Closed, side-effect-free transform interpreter (no eval, no formats beyond strptime)."""
    v = "" if value is None else str(value)
    if t is None:
        return v.strip()
    if t.strip:
        v = v.strip()
    if v == "" and t.default is not None:
        v = t.default
    if v in t.value_map:
        v = t.value_map[v]
    if t.remove_prefix and v.startswith(t.remove_prefix):
        v = v[len(t.remove_prefix):]
    if t.add_prefix and v:
        v = t.add_prefix + v
    if t.case == "upper":
        v = v.upper()
    elif t.case == "lower":
        v = v.lower()
    if t.decimal_comma and v:
        v = v.replace(".", "").replace(",", ".")
    if t.date_format and v:
        try:
            v = datetime.strptime(v, t.date_format).date().isoformat()
        except ValueError:
            pass                                                      # left as-is: validation reports it as a date error
    if t.multiplier is not None and v:
        try:
            v = repr(float(v) * t.multiplier)
        except ValueError:
            pass
    return v


# ------------------------------------------------------------------ persistence (tenant database)
def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def save_mapping(entity: str, mapping: FieldMapping, actor: str) -> None:
    if entity not in ALIASES:
        raise ValueError(f"unknown entity {entity!r}")
    unknown = [f for f in mapping.columns.values() if f not in ALIASES[entity]]
    if unknown:
        raise ValueError(f"unknown internal field(s) for {entity}: {unknown}")
    if len(set(mapping.columns.values())) != len(mapping.columns):
        raise ValueError("two customer columns map to the same internal field")
    with db.connect(write=True) as c:
        c.execute("INSERT INTO field_mappings (Tenant_ID, Entity, Mapping_JSON, Transforms_JSON, Updated_At) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(Tenant_ID, Entity) DO UPDATE SET Mapping_JSON=excluded.Mapping_JSON, Transforms_JSON=excluded.Transforms_JSON, "
                  "Updated_At=excluded.Updated_At",
                  (_tenant(), entity, json.dumps(mapping.columns), json.dumps({k: v.model_dump() for k, v in mapping.transforms.items()}),
                   datetime.now().isoformat(timespec="seconds")))
        audit.record(audit.EventType.MAPPING_UPDATED, None, actor, {"entity": entity, "columns": mapping.columns}, conn=c)


def load_mapping(entity: str) -> FieldMapping | None:
    with db.connect() as c:
        r = c.execute("SELECT Mapping_JSON, Transforms_JSON FROM field_mappings WHERE Tenant_ID=? AND Entity=?", (_tenant(), entity)).fetchone()
    if r is None:
        return None
    return FieldMapping(columns=json.loads(r[0]), transforms={k: Transform(**v) for k, v in json.loads(r[1]).items()})


def save_id_mappings(entity: str, pairs: dict[str, str], actor: str) -> int:
    """External id -> internal id (entity: 'sku' or 'supplier'). Upserts; returns the number stored."""
    if entity not in ("sku", "supplier"):
        raise ValueError("entity must be 'sku' or 'supplier'")
    clean = {str(k).strip(): str(v).strip() for k, v in pairs.items() if str(k).strip() and str(v).strip()}
    if any(len(k) > 120 or len(v) > 120 for k, v in clean.items()):
        raise ValueError("ids must be at most 120 characters")
    with db.connect(write=True) as c:
        c.executemany("INSERT INTO id_mappings (Tenant_ID, Entity, External_ID, Internal_ID) VALUES (?,?,?,?) ON CONFLICT(Tenant_ID, Entity, External_ID) "
                      "DO UPDATE SET Internal_ID=excluded.Internal_ID", [(_tenant(), entity, k, v) for k, v in clean.items()])
        audit.record(audit.EventType.MAPPING_UPDATED, None, actor, {"id_mappings": entity, "count": len(clean)}, conn=c)
    return len(clean)


def load_id_mappings(entity: str) -> dict[str, str]:
    with db.connect() as c:
        return {r[0]: r[1] for r in c.execute("SELECT External_ID, Internal_ID FROM id_mappings WHERE Tenant_ID=? AND Entity=?", (_tenant(), entity))}
