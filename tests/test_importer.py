"""Customer data import + mapping layer."""
import io
import json
import zipfile

import pandas as pd
import pytest

from enterprise import importer as I
from enterprise import mapping as M
from enterprise import tenancy
from scm import audit, db


T = "acme"


@pytest.fixture()
def tn(plat):
    tenancy.provision(T, "Acme", "professional", "SANDBOX")
    with tenancy.scope(T):
        yield plat


def csv(rows: list[str], header: str) -> bytes:
    return (header + "\n" + "\n".join(rows) + "\n").encode()


def go(entity, content, name="f.csv", mapping=None, options=None, actor="op"):
    pv = I.preview(entity, content, name, actor, mapping, options)
    return pv, (I.commit(pv["job_id"], actor) if pv["can_commit"] else None)


PRODUCTS = csv(["SKU-1,Widget,Parts,20,100", "SKU-2,Gadget,Parts,10,60"], "sku_id,name,category,reorder_point,target_stock")
SUPPLIERS = csv(["S1,Acme Parts,s1@acme.example,US,92", "S2,Bolt Co,s2@bolt.example,EU,85"], "supplier_id,name,email,region,reliability")


def seed_master():
    go("products", PRODUCTS)
    go("suppliers", SUPPLIERS)


# ---------------------------------------------------------------- mapping layer
def test_mapping_is_suggested_for_foreign_headers(tn):
    s = M.suggest_mapping("inventory", ["item_code", "on_hand", "warehouse", "notes"])
    assert s["columns"] == {"item_code": "sku_id", "on_hand": "quantity", "warehouse": "warehouse_id"} and s["unmapped_columns"] == ["notes"]
    assert M.suggest_mapping("demand", ["Posting Date", "Material", "Qty Issued"])["columns"] == {"Posting Date": "date", "Material": "sku_id", "Qty Issued": "quantity"}


def test_each_internal_field_is_suggested_at_most_once(tn):
    s = M.suggest_mapping("quotes", ["vendor", "supplier", "price", "item"])
    assert len(set(s["columns"].values())) == len(s["columns"])


def test_saved_mappings_are_validated_and_reused(tn):
    with pytest.raises(ValueError):
        M.save_mapping("inventory", M.FieldMapping(columns={"a": "not_a_field"}), "admin")
    with pytest.raises(ValueError):
        M.save_mapping("inventory", M.FieldMapping(columns={"a": "sku_id", "b": "sku_id"}), "admin")
    M.save_mapping("inventory", M.FieldMapping(columns={"item_code": "sku_id", "on_hand": "quantity"}), "admin")
    assert M.load_mapping("inventory").columns == {"item_code": "sku_id", "on_hand": "quantity"}
    assert audit.events(event_type="MAPPING_UPDATED")
    go("products", PRODUCTS)
    pv = I.preview("inventory", csv(["SKU-1,55"], "item_code,on_hand"), "inv.csv", "op")          # no mapping passed: the saved one is used
    assert pv["can_commit"] and pv["report"]["mapping_used"] == {"item_code": "sku_id", "on_hand": "quantity"}


def test_transforms_are_a_closed_safe_interpreter():
    t = M.Transform(remove_prefix="CUST-", case="upper", add_prefix="SKU-")
    assert M.apply_transform("CUST-ab1", t) == "SKU-AB1"
    assert M.apply_transform(" x ", M.Transform(value_map={"x": "Y"})) == "Y"
    assert M.apply_transform("", M.Transform(default="0")) == "0"
    assert M.apply_transform("31/12/2026", M.Transform(date_format="%d/%m/%Y")) == "2026-12-31"
    assert float(M.apply_transform("12", M.Transform(multiplier=24))) == 288.0
    assert M.apply_transform("1.234,56", M.Transform(decimal_comma=True)) == "1234.56"
    with pytest.raises(Exception):
        M.Transform(code="__import__('os').system('x')")                                      # no field that could execute anything


def test_customer_ids_are_mapped_to_internal_ids(tn):
    seed_master()
    M.save_id_mappings("sku", {"A-100": "SKU-1", "A-200": "SKU-2"}, "admin")
    M.save_id_mappings("supplier", {"VND-9": "S1"}, "admin")
    mp = M.FieldMapping(columns={"item": "sku_id", "vendor": "supplier_id", "px": "unit_price", "lt": "lead_time_days"})
    pv, done = go("quotes", csv(["A-100,VND-9,9.5,6"], "item,vendor,px,lt"), mapping=mp)
    assert done["report"]["committed"]["inserted"] == 1
    assert db.df("SELECT Item_ID, Vendor_ID, Unit_Price FROM vendor_listings").to_dict("records") == [{"Item_ID": "SKU-1", "Vendor_ID": "S1", "Unit_Price": 9.5}]


def test_unmapped_ids_can_be_rejected(tn):
    seed_master()
    mp = M.FieldMapping(columns={"item": "sku_id", "wh": "warehouse_id", "q": "quantity"})
    M.save_id_mappings("sku", {"A-100": "SKU-1"}, "admin")
    pv = I.preview("inventory", csv(["A-100,W1,5", "ZZZ,W1,5"], "item,wh,q"), "i.csv", "op", mp, I.ImportOptions(unmapped_ids="reject"))
    assert pv["report"]["valid"] == 1 and any(i["code"] == "UNMAPPED_ID" for i in pv["report"]["issues"])


def test_mappings_do_not_leak_between_tenants(plat):
    tenancy.provision("tenant-a", "A")
    tenancy.provision("tenant-b", "B")
    with tenancy.scope("tenant-a"):
        M.save_mapping("inventory", M.FieldMapping(columns={"x": "sku_id", "y": "quantity"}), "a")
        M.save_id_mappings("sku", {"E1": "I1"}, "a")
    with tenancy.scope("tenant-b"):
        assert M.load_mapping("inventory") is None and M.load_id_mappings("sku") == {}


# ---------------------------------------------------------------- preview / commit / report
def test_preview_writes_nothing_to_business_tables_and_commit_does(tn):
    pv = I.preview("products", PRODUCTS, "p.csv", "op")
    r = pv["report"]
    assert (r["processed"], r["valid"], r["rejected"], r["status"]) == (2, 2, 0, "READY") and pv["can_commit"] and len(pv["sample"]) == 2
    assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 0                                   # nothing yet
    done = I.commit(pv["job_id"], "op")
    assert done["report"]["status"] == "COMMITTED" and done["report"]["committed"] == {"inserted": 2, "updated": 0, "skipped_already_present": 0}
    assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 2
    assert I.get_job(pv["job_id"])["status"] == "COMMITTED"
    with pytest.raises(I.ImportError_, match="already committed"):
        I.commit(pv["job_id"], "op")
    assert audit.events(event_type="IMPORT_COMMITTED") and audit.events(event_type="IMPORT_PREVIEWED")


def test_reimporting_the_same_file_is_flagged_and_updates_in_place(tn):
    go("products", PRODUCTS)
    pv, done = go("products", PRODUCTS)
    assert pv["report"]["previously_imported"] is True and done["report"]["committed"] == {"inserted": 0, "updated": 2, "skipped_already_present": 0}
    assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 2


def test_the_full_lifecycle_of_all_seven_entities(tn):
    seed_master()
    go("quotes", csv(["S1,SKU-1,10.5,6,10,500", "S2,SKU-1,11.0,4,1,", "S1,SKU-2,20,7,1,"], "supplier_id,sku_id,unit_price,lead_time_days,moq,capacity"))
    go("inventory", csv(["SKU-1,12", "SKU-2,40"], "sku_id,quantity"))
    days = pd.date_range("2026-01-01", periods=30)
    go("demand", csv([f"SKU-1,{d.date()},{5 + i % 3}" for i, d in enumerate(days)], "sku_id,date,quantity"))
    go("purchase_orders", csv(["PO-1,SKU-1,S1,100,10.0,RECEIVED,2026-01-05", "PO-2,SKU-2,S2,50,19.5,IN_TRANSIT,2026-01-20"], "po_id,sku_id,supplier_id,quantity,unit_price,status,order_date"))
    go("supplier_performance", csv(["S1,SKU-1,2026-01-05,6,6,100,100,1,10,DELIVERED", "S1,SKU-1,2026-01-10,6,9,100,95,5,10,DELIVERED", "S2,SKU-2,2026-01-11,4,,50,0,0,19,CANCELLED"],
                                   "supplier_id,sku_id,order_date,promised_days,actual_days,qty_ordered,qty_received,qty_defective,unit_price,status"))
    counts = {t: db.df(f"SELECT COUNT(*) n FROM {t}").n[0] for t in ("inventory", "vendors", "vendor_listings", "demand_history", "purchase_orders", "supplier_deliveries")}
    assert counts == {"inventory": 2, "vendors": 2, "vendor_listings": 3, "demand_history": 30, "purchase_orders": 2, "supplier_deliveries": 3}
    assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1'").Stock_Level[0] == 12
    assert set(db.df("SELECT Tenant_ID FROM purchase_orders").Tenant_ID) == {T}


def test_the_imported_data_drives_a_real_workflow(tn):
    from enterprise.approvals import install_hooks
    from scm.security import Principal, Role
    from scm.services import WorkflowService
    install_hooks()
    seed_master()
    go("quotes", csv(["S1,SKU-1,10.5,6,10,500", "S2,SKU-1,11.0,4,1,", "S1,SKU-2,20,7,1,", "S2,SKU-2,21,5,1,"], "supplier_id,sku_id,unit_price,lead_time_days,moq,capacity"))
    go("inventory", csv(["SKU-1,2", "SKU-2,1"], "sku_id,quantity"))
    svc = WorkflowService()
    run, _ = svc.start(Principal("op", Role.OPERATOR, T))
    done = svc.wait(run["workflow_id"], 120)
    assert done["status"] == "AWAITING_APPROVAL" and done["result"]["proposals"]
    svc.shutdown()


# ---------------------------------------------------------------- validation and data quality
def rep_of(entity, rows, header, **kw):
    return I.preview(entity, csv(rows, header), "x.csv", "op", **kw)["report"]


def codes(rep):
    return {i["code"] for i in rep["issues"]}


def test_missing_required_values_and_bad_types_are_rejected_with_row_numbers(tn):
    r = rep_of("products", [",NoId,c,1,2", "A,,c,1,2", "B,Bee,c,x,5", "C,Cee,c,5,1e99", "D,Dee,c,5,50"], "sku_id,name,category,reorder_point,target_stock")
    assert (r["processed"], r["valid"], r["rejected"]) == (5, 1, 4) and {"MISSING_REQUIRED", "INVALID_VALUE"} <= codes(r)
    assert sorted({i["row"] for i in r["issues"]}) == [2, 3, 4, 5]


def test_target_not_above_reorder_point_is_blocked(tn):
    r = rep_of("products", ["A,Aaa,c,50,50", "B,Bbb,c,50,10"], "sku_id,name,category,reorder_point,target_stock")
    assert r["valid"] == 0 and codes(r) == {"TARGET_LE_REORDER"}


def test_negative_stock_is_clamped_and_flagged(tn):
    go("products", PRODUCTS)
    pv, _ = go("inventory", csv(["SKU-1,-15"], "sku_id,quantity"))
    assert any(i["code"] == "NEGATIVE_STOCK" for i in pv["report"]["issues"])
    assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1'").Stock_Level[0] == 0


def test_markup_in_names_is_removed_on_import(tn):
    go("products", csv(["X1,<script>alert(1)</script>_CORE,Parts,5,50", "X2,&lt;b&gt;Bold&lt;/b&gt; item,Parts,5,50"], "sku_id,name,category,reorder_point,target_stock"))
    names = list(db.df("SELECT Item_Name FROM inventory ORDER BY Item_ID").Item_Name)
    assert names == ["CORE", "Bold item"]


def test_duplicates_in_the_file_keep_the_first(tn):
    r = rep_of("products", ["A,One,c,5,50", "A,Two,c,5,50"], "sku_id,name,category,reorder_point,target_stock")
    assert r["duplicates_in_file"] == 1 and r["valid"] == 1


def test_referential_integrity_is_enforced(tn):
    r = rep_of("inventory", ["NOPE,5"], "sku_id,quantity")
    assert codes(r) == {"UNKNOWN_SKU"} and r["rejected"] == 1
    seed_master()
    r = rep_of("quotes", ["S9,SKU-1,5,3", "S1,NOPE,5,3"], "supplier_id,sku_id,unit_price,lead_time_days")
    assert codes(r) == {"UNKNOWN_SUPPLIER", "UNKNOWN_SKU"}


def test_unknown_sku_can_create_a_placeholder_with_a_warning(tn):
    pv, done = go("inventory", csv(["NEW-1,5"], "sku_id,quantity"), options=I.ImportOptions(unknown_sku="create"))
    assert done["report"]["committed"]["inserted"] == 1 and any(i["code"] == "PLACEHOLDER_PRODUCT" for i in pv["report"]["issues"])


@pytest.mark.parametrize("value,ok", [("2026-01-31", True), ("31/01/2026", True), ("2026-13-45", False), ("yesterday", False), ("", False)])
def test_dates(tn, value, ok):
    seed_master()
    r = rep_of("demand", [f"SKU-1,{value},5"], "sku_id,date,quantity")
    assert (r["valid"] == 1) is ok


def test_numeric_formats(tn):
    r = rep_of("products", ["A,Aa,c,\"1,000\",\"2,500\"", "B,Bb,c,5.0,60", "C,Cc,c,5.4,70", "D,Dd,c,abc,70", "E,Ee,c,-5,70"], "sku_id,name,category,reorder_point,target_stock")
    assert r["valid"] == 3 and "ROUNDED" in codes(r) and r["rejected"] == 2


def test_enum_and_cross_field_rules_in_supplier_performance(tn):
    seed_master()
    hdr = "supplier_id,sku_id,order_date,promised_days,actual_days,qty_ordered,qty_received,qty_defective,unit_price,status"
    r = rep_of("supplier_performance", ["S1,SKU-1,2026-01-01,5,5,100,100,200,10,DELIVERED", "S1,SKU-1,2026-01-02,5,,100,100,0,10,DELIVERED",
                                        "S1,SKU-1,2026-01-03,5,5,100,100,0,10,LOST", "S1,SKU-1,2026-01-04,5,5,100,100,0,10,DELIVERED"], hdr)
    assert r["valid"] == 1 and r["rejected"] == 3


def test_missing_optional_values_use_defaults_and_report_null_rates(tn):
    pv, _ = go("suppliers", csv(["S1,Acme,,,", "S2,Bolt,b@x.example,EU,88"], "supplier_id,name,email,region,reliability"))
    row = db.df("SELECT * FROM vendors WHERE Vendor_ID='S1'").iloc[0]
    assert row.Reliability == 90.0 and row.Region == "GLOBAL" and pv["report"]["null_rates"]["email"] == 0.5


def test_required_field_without_a_mapped_column_is_explained(tn):
    with pytest.raises(I.ImportError_, match="not mapped"):
        I.preview("inventory", csv(["A,5"], "item_code,on_hand"), "i.csv", "op")


def test_import_is_blocked_when_most_rows_are_rejected(tn):
    pv = I.preview("products", csv(["A,x,c,9,1", "B,y,c,9,1", "C,Ok,c,1,9"], "sku_id,name,category,reorder_point,target_stock"), "p.csv", "op")
    assert pv["report"]["status"] == "BLOCKED" and not pv["can_commit"]
    with pytest.raises(I.ImportBlocked):
        I.commit(pv["job_id"], "op")
    assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 0


def test_partial_imports_can_be_disallowed(tn):
    rows = ["A,Aa,c,1,9", "B,Bb,c,9,1"]
    pv = I.preview("products", csv(rows, "sku_id,name,category,reorder_point,target_stock"), "p.csv", "op", options=I.ImportOptions(allow_partial=False, max_reject_rate=0.9))
    assert pv["report"]["status"] == "BLOCKED"
    pv2 = I.preview("products", csv(rows, "sku_id,name,category,reorder_point,target_stock"), "p.csv", "op", options=I.ImportOptions(allow_partial=True, max_reject_rate=0.9))
    assert pv2["can_commit"] and I.commit(pv2["job_id"], "op")["report"]["committed"]["inserted"] == 1


def test_a_failure_during_commit_rolls_everything_back(tn, monkeypatch):
    pv = I.preview("products", PRODUCTS, "p.csv", "op")
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit store down")))
    with pytest.raises(I.ImportError_, match="rolled back"):
        I.commit(pv["job_id"], "op")
    assert db.df("SELECT COUNT(*) n FROM inventory").n[0] == 0
    assert I.get_job(pv["job_id"])["status"] == "FAILED"


def test_purchase_orders_and_deliveries_are_deduplicated_on_reimport(tn):
    seed_master()
    po = csv(["PO-1,SKU-1,S1,100,10.0,RECEIVED,2026-01-05"], "po_id,sku_id,supplier_id,quantity,unit_price,status,order_date")
    go("purchase_orders", po)
    pv, done = go("purchase_orders", po)
    assert done["report"]["committed"]["skipped_already_present"] == 1 and db.df("SELECT COUNT(*) n FROM purchase_orders").n[0] == 1
    assert pv["report"]["duplicates_existing"] == 1


def test_warehouses_roll_up_into_the_stock_the_engine_plans_on(tn):
    go("products", PRODUCTS)
    go("inventory", csv(["SKU-1,WH-A,30", "SKU-1,WH-B,12", "SKU-2,WH-A,7"], "sku_id,warehouse_id,quantity"))
    stock = dict(db.df("SELECT Item_ID, Stock_Level FROM inventory").values)
    assert stock == {"SKU-1": 42, "SKU-2": 7}
    assert set(db.df("SELECT Warehouse_ID FROM warehouses").Warehouse_ID) == {"WH-A", "WH-B"}
    go("inventory", csv(["SKU-1,WH-A,10"], "sku_id,warehouse_id,quantity"))          # an update replaces that warehouse only
    assert db.df("SELECT Stock_Level FROM inventory WHERE Item_ID='SKU-1'").Stock_Level[0] == 22


def test_demand_gaps_are_reported(tn):
    seed_master()
    days = [d for i, d in enumerate(pd.date_range("2026-01-01", periods=60)) if i % 3 != 0]
    r = rep_of("demand", [f"SKU-1,{d.date()},4" for d in days], "sku_id,date,quantity")
    assert "GAPS" in codes(r)


# ---------------------------------------------------------------- formats and limits
def test_json_and_excel_inputs(tn):
    pj = json.dumps({"records": [{"sku_id": "J1", "name": "Json item", "category": "c", "reorder_point": 5, "target_stock": 50}]}).encode()
    assert go("products", pj, "p.json")[1]["report"]["committed"]["inserted"] == 1
    buf = io.BytesIO()
    pd.DataFrame({"sku_id": ["X1"], "name": ["Excel item"], "category": ["c"], "reorder_point": [5], "target_stock": [50]}).to_excel(buf, index=False)
    assert go("products", buf.getvalue(), "p.xlsx")[1]["report"]["committed"]["inserted"] == 1
    assert go("products", json.dumps([{"sku_id": "L1", "name": "List", "category": "c", "reorder_point": 5, "target_stock": 50}]).encode(), "p.json")[1]


@pytest.mark.parametrize("content,name,msg", [(b"", "e.csv", "empty"), (b"a,b\n", "e.csv", "no data rows"), (b"x" * 10, "e.exe", "unsupported"),
                                              (b"{not json", "e.json", "could not parse"), (b"[1,2]", "e.json", "list of objects"), (b"abc", "e.xlsx", "valid .xlsx"),
                                              (b"a", "e.xls", "xls")])
def test_unreadable_files_get_a_clear_error(tn, content, name, msg):
    with pytest.raises(I.ImportError_, match=msg):
        I.read_table(content, name)


def test_size_limits(tn, monkeypatch):
    with pytest.raises(I.ImportError_, match="larger"):
        I.read_table(b"a,b\n" + b"1,2\n" * (I.MAX_BYTES // 4 + 10), "big.csv")
    monkeypatch.setattr(I, "MAX_XLSX_UNCOMPRESSED", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/big.xml", "0" * 100_000)
    with pytest.raises(I.ImportError_, match="unreasonable"):
        I.read_table(buf.getvalue(), "bomb.xlsx")


def test_commit_with_a_forged_job_id_is_rejected(tn):
    for bad in ("../../etc/passwd", "imp_zzzz", "", "imp_" + "a" * 16):
        with pytest.raises(I.ImportError_):
            I.commit(bad, "op")


def test_import_jobs_are_tenant_private(plat):
    tenancy.provision("tenant-a", "A")
    tenancy.provision("tenant-b", "B")
    with tenancy.scope("tenant-a"):
        pv = I.preview("products", PRODUCTS, "p.csv", "a")
    with tenancy.scope("tenant-b"):
        assert I.list_jobs() == [] and I.get_job(pv["job_id"]) is None
        with pytest.raises(I.ImportError_, match="unknown"):
            I.commit(pv["job_id"], "b")
    with tenancy.scope("tenant-a"):
        assert [j["job_id"] for j in I.list_jobs()] == [pv["job_id"]]


def test_csv_with_bom_semicolons_are_handled(tn):
    bom = "﻿sku_id,name,category,reorder_point,target_stock\nA,Aa,c,5,50\n".encode()
    assert go("products", bom)[1]["report"]["committed"]["inserted"] == 1
