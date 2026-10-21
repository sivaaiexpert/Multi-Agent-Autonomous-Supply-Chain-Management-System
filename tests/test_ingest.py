import io

import pandas as pd
import pytest

from scm import db, ingest

CSV = "Item_ID,Item_Name,Stock_Level,Reorder_Point,Target_Stock\n"


def load(text, name="x.csv"):
    norm, mapping, missing = ingest.normalize_columns(ingest.read_table(text.encode(), name))
    assert not missing
    return ingest.validate_inventory(norm)


STRESS = CSV + """SKU-INT-001,Premium AI Edge Accelerator Card,12,50,200
SKU-MAL-999,<script>alert('XSS_ATTACK')</script>_CORE_SWITCH,-15,40,150
SKU-ERR-404,Broken Data Stream Router Matrix,85,30,0
SKU-LNK-777,High-Throughput Quantum Fiber Node,250,10,10
"""


def test_the_stress_file_is_processed_row_by_row_under_the_three_rules():
    clean, rep = load(STRESS)
    assert list(clean.Item_ID) == ["SKU-INT-001", "SKU-MAL-999"]
    mal = clean.set_index("Item_ID").loc["SKU-MAL-999"]
    assert mal.Item_Name == "CORE_SWITCH" and "<" not in mal.Item_Name and "script" not in mal.Item_Name.lower()    # 1) sanitised
    assert mal.Stock_Level == 0                                                                                       # 2) clamped ...
    high = [f for f in rep["flags"] if f[1] == "HIGH"]
    assert len(high) == 1 and "NEGATIVE STOCK (-15)" in high[0][2]                                                    # ... and flagged HIGH
    blocked = {e[1].split(":")[0] for e in rep["errors"]}
    assert blocked == {"SKU-ERR-404", "SKU-LNK-777"} and all("BLOCKED" in e[1] for e in rep["errors"])                # 3) blocked


def test_sanitisation_removes_every_markup_variant():
    cases = {"<b>Bold</b> Item": "Bold Item", "A<img src=x onerror=alert(1)>B": "AB", "<script>x()</script>Name": "Name",
             "&lt;script&gt;alert(1)&lt;/script&gt;Name": "Name", "Plain Name": "Plain Name", "  spaced   out  ": "spaced out"}
    for evil, expect in cases.items():
        assert ingest.sanitize_text(evil) == expect, evil
    for nasty in ("<<script>script>alert(1)<</script>/script>", "<scr<script>ipt>alert(1)</scr</script>ipt>", "<svg/onload=alert(1)>"):
        out = ingest.sanitize_text(nasty)
        assert "<" not in out and ">" not in out and "alert" not in out and "onload" not in out.lower(), (nasty, out)


def test_required_columns_and_flexible_headers():
    norm, mapping, missing = ingest.normalize_columns(ingest.read_table(b"SKU,Name,On Hand,Reorder,Max\nA,Thing,5,10,50\n", "a.csv"))
    assert not missing and mapping["SKU"] == "Item_ID"
    _, _, missing = ingest.normalize_columns(ingest.read_table(b"SKU,Name\nA,Thing\n", "a.csv"))
    assert missing


@pytest.mark.parametrize("row,reason", [(",Name,1,2,3", "missing Item_ID"), ("A,,1,2,3", "missing Item_Name"), ("A,N,x,2,3", "must be numbers"),
                                         ("A,N,1e12,2,3", "larger than"), ("A,N,nan,2,3", "must be numbers|NaN")])
def test_bad_rows_are_rejected_with_a_reason(row, reason):
    clean, rep = load(CSV + row + "\n")
    assert clean.empty and rep["errors"] and any(__import__("re").search(reason, e[1]) for e in rep["errors"])


def test_duplicate_ids_keep_the_first_occurrence():
    clean, rep = load(CSV + "A,One,1,2,30\nA,Two,1,2,30\n")
    assert list(clean.Item_Name) == ["One"] and any("duplicate" in e[1] for e in rep["errors"])


def test_decimals_are_rounded_with_a_warning():
    clean, rep = load(CSV + "A,One,1.6,2,30\n")
    assert clean.Stock_Level[0] == 2 and any("rounded" in w[1] for w in rep["warnings"])


def test_limits_and_unsupported_formats():
    with pytest.raises(ValueError):
        ingest.read_table(b"x" * (6 * 1024 * 1024), "big.csv")
    with pytest.raises(ValueError):
        ingest.read_table(b"a", "old.xls")
    with pytest.raises(ValueError):
        ingest.read_table(b"", "e.csv")


def test_xlsx_round_trip():
    buf = io.BytesIO()
    pd.DataFrame({"Item_ID": ["X1"], "Item_Name": ["Widget"], "Stock_Level": [3], "Reorder_Point": [10], "Target_Stock": [60]}).to_excel(buf, index=False)
    clean, _ = ingest.validate_inventory(ingest.normalize_columns(ingest.read_table(buf.getvalue(), "inv.xlsx"))[0])
    assert list(clean.Item_ID) == ["X1"]


def test_apply_upsert_then_undo(fresh_db):
    before = ingest.inventory_backup()
    clean, _ = load(CSV + "NEW-1,New thing,1,10,50\nSKU-1001,Renamed,99,10,200\n")
    res = ingest.apply_inventory(clean, "upsert")
    assert res == {"inserted": 1, "updated": 1, "removed": 0}
    assert db.df("SELECT Item_Name FROM inventory WHERE Item_ID='SKU-1001'").Item_Name[0] == "Renamed"
    ingest.restore_inventory(before)
    assert sorted(db.df("SELECT Item_ID FROM inventory").Item_ID) == sorted(r["Item_ID"] for r in before)
    assert db.df("SELECT Item_Name FROM inventory WHERE Item_ID='SKU-1001'").Item_Name[0] != "Renamed"


def test_overwrite_removes_items_not_in_the_file_including_their_history(demo_db):
    clean, _ = load(CSV + "ONLY-1,Only,1,10,50\n")
    res = ingest.apply_inventory(clean, "overwrite")
    assert res["removed"] == 14 and db.df("SELECT COUNT(*) n FROM inventory").n[0] == 1
    assert db.df("SELECT COUNT(*) n FROM demand_history").n[0] == 0                # cascade


def test_import_is_atomic(fresh_db, monkeypatch):
    clean, _ = load(CSV + "A,One,1,10,50\nB,Two,1,10,50\n")
    clean.loc[1, "Item_Name"] = None                                                  # NOT NULL violation on the second row
    with pytest.raises(Exception):
        ingest.apply_inventory(clean, "upsert")
    assert db.df("SELECT COUNT(*) n FROM inventory WHERE Item_ID IN ('A','B')").n[0] == 0


def test_webhook_payload_shapes_and_secret_masking():
    payload = ingest.sample_payload()
    for target in ingest.TARGETS:
        reqs = ingest.format_requests(target, payload)
        assert reqs and all(r["method"] == "POST" and r["path"].startswith("/") for r in reqs)
    masked = ingest.mask_headers(ingest.build_headers("Bearer token", "supersecrettoken123"))
    assert "supersecrettoken123" not in json_dump(masked)
    assert ingest.valid_url("https://erp.example.com") and not ingest.valid_url("ftp://x") and not ingest.valid_url("javascript:alert(1)")


def test_connection_test_validates_input_and_never_leaks_the_password():
    r = ingest.test_connection("PostgreSQL", "", 5432, "u", "hunter2-secret", "db")
    assert not r["ok"] and r["state"] == "invalid" and "hunter2-secret" not in json_dump(r)
    r = ingest.test_connection("PostgreSQL", "definitely-not-a-real-host.invalid", 5432, "u", "hunter2-secret", "db", timeout=1.0)
    assert not r["ok"] and "hunter2-secret" not in json_dump(r)


def json_dump(x):
    import json
    return json.dumps(x, default=str)
