"""QA tooling: DB stress scenarios, snapshot/diff, and an automated fault-injection test matrix."""
from __future__ import annotations

import shutil
import tempfile
import time
import traceback
from pathlib import Path

import pandas as pd

from . import db
from .llm import FORMAT_BAD, FORMAT_WARNING, MUTATIONS, RECOVERABLE

NODES = ["supervisor", "demand", "analytics", "supplier_intel", "procurement", "policy_check", "vendor_comm", "vendor_dispatch"]
LONG = "SUPERCALIFRAGILISTIC" * 14  # 280 chars, no spaces: worst case for wrapping
INJECT = '<img src=x onerror=alert(1)> <script>alert("xss")</script> **bold** $100$ `code` {{template}}'


# ------------------------------------------------------------------ DB stress scenarios
def _sql(*stmts: str):
    with db.connect() as c:
        for s in stmts:
            c.execute(s)


def _long_text():
    name = f"{INJECT} {LONG}"
    _sql(f"UPDATE inventory SET Item_Name='{name.replace(chr(39), chr(39) * 2)}' WHERE Item_ID='SKU-1005'",
         f"UPDATE vendors SET Vendor_Name='{LONG} Industrial Holdings International Ltd', "
         f"Contact_Email='{'a' * 120}@{'b' * 120}.example' WHERE Vendor_ID IN ('V01','V03')")


SCENARIOS: dict[str, tuple[str, callable]] = {
    "baseline": ("Untouched seed data (control)", lambda: None),
    "zero_stock": ("Zero stock on 3 SKUs", lambda: _sql(
        "UPDATE inventory SET Stock_Level=0 WHERE Item_ID IN ('SKU-1001','SKU-1005','SKU-1004')")),
    "negative_stock": ("Negative stock integers (-25, -1)", lambda: _sql(
        "UPDATE inventory SET Stock_Level=-25 WHERE Item_ID='SKU-1001'",
        "UPDATE inventory SET Stock_Level=-1 WHERE Item_ID='SKU-1005'")),
    "missing_vendor": ("No vendor listings for critical SKU-1005", lambda: _sql(
        "DELETE FROM vendor_listings WHERE Item_ID='SKU-1005'")),
    "no_vendors_at_all": ("No vendor listings for ANY low-stock SKU", lambda: _sql(
        "DELETE FROM vendor_listings WHERE Item_ID IN (SELECT Item_ID FROM inventory WHERE Stock_Level < Reorder_Point)")),
    "zero_reorder": ("Reorder point 0 with negative stock (div-by-zero)", lambda: _sql(
        "UPDATE inventory SET Reorder_Point=0, Stock_Level=-3 WHERE Item_ID='SKU-1002'")),
    "inverted_target": ("Target stock below reorder point (negative order qty)", lambda: _sql(
        "UPDATE inventory SET Target_Stock=5 WHERE Item_ID='SKU-1003'")),
    "bad_prices": ("Zero / negative vendor prices and lead times", lambda: _sql(
        "UPDATE vendor_listings SET Unit_Price=0 WHERE Item_ID='SKU-1001' AND rowid%2=0",
        "UPDATE vendor_listings SET Unit_Price=-9.99, Lead_Time_Days=-3 WHERE Item_ID='SKU-1007'")),
    "huge_quantity": ("Extreme order size (target 2 billion units)", lambda: _sql(
        "UPDATE inventory SET Stock_Level=0, Target_Stock=2000000000 WHERE Item_ID='SKU-1004'")),
    "long_text": ("300-char names, HTML/markdown injection, giant emails", _long_text),
}


def apply_scenario(key: str) -> str:
    desc, fn = SCENARIOS[key]
    fn()
    return desc


# ------------------------------------------------------------------ snapshots & deltas
KEYS = {"inventory": ["Item_ID"], "purchase_orders": ["PO_ID"], "vendor_listings": ["Vendor_ID", "Item_ID"],
        "vendors": ["Vendor_ID"]}


def snapshot() -> dict:
    return {"taken": time.strftime("%H:%M:%S"), **{t: db.df(f"SELECT * FROM {t}") for t in KEYS}}


def diff(before: dict, after: dict) -> pd.DataFrame:
    rows = []
    for table, keys in KEYS.items():
        b, a = before[table].set_index(keys), after[table].set_index(keys)
        name = lambda k: "/".join(map(str, k if isinstance(k, tuple) else (k,)))
        for k in a.index.difference(b.index):
            rows.append((table, name(k), "ADDED", "(row)", "", ", ".join(f"{c}={v}" for c, v in a.loc[k].items())[:140]))
        for k in b.index.difference(a.index):
            rows.append((table, name(k), "REMOVED", "(row)", ", ".join(f"{c}={v}" for c, v in b.loc[k].items())[:140], ""))
        for k in a.index.intersection(b.index):
            for col in a.columns:
                x, y = b.loc[k, col], a.loc[k, col]
                if not (pd.isna(x) and pd.isna(y)) and x != y:
                    rows.append((table, name(k), "MODIFIED", col, str(x), str(y)))
    return pd.DataFrame(rows, columns=["Table", "Key", "Change", "Field", "Before", "After"])


# ------------------------------------------------------------------ automated matrix
def _case(graph, i: int, scn: str, mut: str, crash: str | None) -> dict:
    from .graph import initial_state
    t0, row = time.perf_counter(), {"DB scenario": scn, "LLM fault": mut, "Node crash": crash or "—"}
    problems: list[str] = []
    try:
        db.init_db(reset=True)
        SCENARIOS[scn][1]()
        cfg = {"configurable": {"thread_id": f"qa{i}", "llm_enabled": mut != "none", "llm_model": "qa",
                                "llm_base_url": "", "llm_timeout": 5, "llm_backoff_base": 0.0, "fault_llm": mut, "fault_latency": 0,
                                "fault_crash": crash or "none"}}
        for _ in graph.stream(initial_state(), cfg, stream_mode="updates"):
            pass
        if graph.get_state(cfg).next:  # paused at HITL gate -> approve to exercise dispatch too
            graph.update_state(cfg, {"decision": "approve"}, as_node="supervisor")
            for _ in graph.stream(None, cfg, stream_mode="updates"):
                pass
        v = graph.get_state(cfg).values
        status, errors, trace = v.get("status"), v.get("errors", []), v.get("trace", [])
        row.update(Status=status, Alerts=len(v.get("alerts", [])), POs=len(v.get("outcome", {}).get("po_ids", [])))
        if crash:
            if not errors or status != "error":
                problems.append(f"crash in '{crash}' not captured gracefully (status={status})")
        else:
            if errors:
                problems.append(f"unexpected exception in node '{errors[0]['node']}': {errors[0]['type']}: {errors[0]['message']}")
            if status not in ("approved", "healthy", "no_vendor", "rejected", "policy_blocked"):
                problems.append(f"unexpected final status '{status}'")
        with db.connect() as c:
            if c.execute("SELECT COUNT(*) FROM purchase_orders WHERE Status='PENDING_APPROVAL'").fetchone()[0]:
                problems.append("dangling PENDING_APPROVAL purchase orders after run")
            if c.execute("SELECT COUNT(*) FROM purchase_orders WHERE PO_ID NOT LIKE 'PO-HIST%' "
                         "AND (Quantity<=0 OR Unit_Price<=0)").fetchone()[0]:
                problems.append("purchase order created with non-positive quantity or price")
        if mut != "none" and not crash and v.get("alerts"):
            used_llm = any(e["source"] == "llm" for e in trace)
            fell_back = any(e["source"] == "fallback" for e in trace)
            if mut in RECOVERABLE and not used_llm:
                problems.append("recoverable LLM output was rejected instead of parsed")
            if mut not in RECOVERABLE and (used_llm or not fell_back):
                problems.append("corrupt LLM output was accepted or no fallback was recorded")
            if mut in FORMAT_BAD and not any(FORMAT_WARNING in e["message"] for e in trace):
                problems.append("unrecoverable format did not log the 'LLM Format Exception Handled' warning")
        row["Detail"] = "; ".join(problems) or (errors[0]["message"] if errors else "")
    except Exception as exc:  # the harness itself must never die
        problems.append(f"harness exception {type(exc).__name__}: {exc}")
        row["Detail"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        row["Trace"] = traceback.format_exc()
    row["Result"] = "FAIL" if problems else "PASS"
    row["ms"] = round((time.perf_counter() - t0) * 1000)
    return row


def run_suite(progress=None) -> list[dict]:
    """Runs scenario x LLM-fault matrix + per-node crash tests against a throwaway DB (live data untouched)."""
    from .graph import build_graph
    tmp = Path(tempfile.mkdtemp(prefix="scm_qa_"))
    cases = [(s, m, None) for s in SCENARIOS for m in MUTATIONS] + [("baseline", "none", n) for n in NODES]
    results = []
    with db.use_path(tmp / "qa.db", tmp / "outbox"):   # thread-local: other browser sessions keep using the live database
        graph = build_graph()
        for i, (s, m, crash) in enumerate(cases):
            results.append(_case(graph, i, s, m, crash))
            if progress:
                progress((i + 1) / len(cases), f"{i + 1}/{len(cases)} · {s} × {m}{' × crash:' + crash if crash else ''}")
    shutil.rmtree(tmp, ignore_errors=True)
    return results
