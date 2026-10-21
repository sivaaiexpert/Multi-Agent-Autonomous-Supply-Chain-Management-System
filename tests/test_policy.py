import pytest

from scm import db
from scm.policy import PolicyValidator
from scm.settings import ApprovalCfg


def line(item="SKU-1001", vendor="V01", qty=50, price=None, **extra):
    if price is None:
        price = db.vendor_quotes(item)[0]["Unit_Price"]
        vendor = db.vendor_quotes(item)[0]["Vendor_ID"] if vendor == "V01" else vendor
    return {"Item_ID": item, "Vendor_ID": vendor, "qty": qty, "unit_price": price, **extra}


def codes(rep, severity=None):
    return {v.code for v in rep.violations if severity is None or v.severity == severity}


def test_clean_proposal_passes(fresh_db):
    rep = PolicyValidator().validate([line()])
    assert rep.ok and not rep.violations


@pytest.mark.parametrize("qty", [0, -5, 2.5, "10", None, True, float("nan")])
def test_bad_quantities_are_blocked(fresh_db, qty):
    rep = PolicyValidator().validate([line(qty=qty)])
    assert "BAD_QUANTITY" in codes(rep, "block") and not rep.ok


@pytest.mark.parametrize("price", [0, -1.0, "9", None, float("nan")])
def test_bad_prices_are_blocked(fresh_db, price):
    rep = PolicyValidator().validate([line(price=price)] if price is not None else [{"Item_ID": "SKU-1001", "Vendor_ID": "V01", "qty": 5, "unit_price": None}])
    assert not rep.ok


def test_unknown_sku_and_vendor_and_missing_listing(fresh_db):
    assert "UNKNOWN_SKU" in codes(PolicyValidator().validate([line(item="SKU-HALLUCINATED", vendor="V01", price=5.0)]))
    assert "UNKNOWN_VENDOR" in codes(PolicyValidator().validate([line(vendor="V99", price=5.0)]))
    q = db.vendor_quotes("SKU-1001")
    missing = next(v for v in ("V01", "V02", "V03", "V04", "V05", "V06") if v not in {x["Vendor_ID"] for x in q})
    assert "NO_LISTING" in codes(PolicyValidator().validate([line(vendor=missing, price=5.0)]))


def test_hallucinated_price_is_blocked_against_the_catalogue(fresh_db):
    q = db.vendor_quotes("SKU-1001")[0]
    rep = PolicyValidator().validate([line(vendor=q["Vendor_ID"], price=q["Unit_Price"] * 0.1)])
    assert "PRICE_MISMATCH" in codes(rep, "block")
    near = PolicyValidator().validate([line(vendor=q["Vendor_ID"], price=q["Unit_Price"] * 1.05)])
    assert near.ok


def test_absurd_price_blocked(fresh_db):
    assert "PRICE_ABSURD" in codes(PolicyValidator(ApprovalCfg(max_price_deviation=1e12)).validate([line(price=5e9)]))


def test_quantity_limit_relative_to_target(fresh_db):
    target = db.df("SELECT Target_Stock FROM inventory WHERE Item_ID='SKU-1001'").iloc[0, 0]
    assert "QTY_EXCEEDS_LIMIT" in codes(PolicyValidator().validate([line(qty=int(target * 3) + 5)]), "block")
    assert PolicyValidator().validate([line(qty=int(target))]).ok


def test_moq_and_capacity(fresh_db):
    q = db.vendor_quotes("SKU-1001")[0]
    with db.connect(write=True) as c:
        c.execute("UPDATE vendor_listings SET MOQ=60, Capacity=100 WHERE Vendor_ID=? AND Item_ID='SKU-1001'", (q["Vendor_ID"],))
    p = lambda n: PolicyValidator().validate([line(vendor=q["Vendor_ID"], price=q["Unit_Price"], qty=n)])
    assert "MOQ_VIOLATION" in codes(p(50), "block")
    assert "CAPACITY_EXCEEDED" in codes(p(105), "block")
    assert p(80).ok


def test_duplicate_lines_blocked(fresh_db):
    rep = PolicyValidator().validate([line(), line()])
    assert "DUPLICATE_LINE" in codes(rep, "block")


def test_budget_per_item(fresh_db):
    rep = PolicyValidator(budget_per_item=10.0).validate([line(qty=50)])
    assert "BUDGET_EXCEEDED" in codes(rep, "block")


def test_review_thresholds_are_warnings_not_blocks(fresh_db):
    cfg = ApprovalCfg(elevated_review_total=100, executive_review_total=1000, max_qty_multiple_of_target=1000)
    rep = PolicyValidator(cfg).validate([line(qty=100)])
    assert rep.ok and rep.requires_executive_review
    assert "EXECUTIVE_REVIEW" in codes(rep, "warn")
    elevated = PolicyValidator(ApprovalCfg(elevated_review_total=100, max_qty_multiple_of_target=1000)).validate([line(qty=5)])
    assert elevated.ok and elevated.requires_elevated_review


def test_informational_warnings_do_not_block(fresh_db):
    rep = PolicyValidator().validate([line(optimizer_status="fallback", forecast_confidence=0.1, days_until_stockout=1.0, lead_time=9)])
    assert rep.ok and {"OPTIMIZER_FALLBACK", "LOW_FORECAST_CONFIDENCE", "LEAD_TIME_EXCEEDS_COVER"} <= codes(rep, "warn")


def test_blocked_pairs_are_reported_per_line(fresh_db):
    good, bad = line(), line(item="SKU-1002", qty=-1, vendor="V03", price=1.0)
    rep = PolicyValidator().validate([good, bad])
    assert ("SKU-1002", "V03") in rep.blocked and (good["Item_ID"], good["Vendor_ID"]) not in rep.blocked
    assert rep.for_line("SKU-1002", "V03")
