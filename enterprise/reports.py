"""Downloadable customer reports. Every figure is calculated from the tenant's own data at generation time; each report states its provenance and method.

Formats: CSV (spreadsheet-formula injection neutralised), JSON, Markdown, standalone HTML (all values escaped).
"""
from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import dataclass, field
from datetime import datetime

from scm import audit, dashboard, db, obs

from . import comparison, impact, usage

KINDS = ("monthly_procurement", "supplier_performance", "forecast_accuracy", "inventory_risk", "savings", "ai_usage", "audit")
FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class Report:
    kind: str
    title: str
    columns: list[str]
    rows: list[dict]
    summary: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "columns": self.columns, "rows": self.rows, "summary": self.summary, "meta": self.meta}


def _period(p: str | None) -> tuple[str, str, str]:
    p = p or datetime.now().strftime("%Y-%m")
    y, m = int(p[:4]), int(p[5:7])
    nxt = f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"
    return p, f"{p}-01", f"{nxt}-01"


def _meta(kind: str, period: str, method: str, provenance: str = impact.OBSERVED) -> dict:
    return {"tenant_id": obs.context().get("tenant_id") or "default", "period": period, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "provenance": provenance, "data_provenance": impact.data_provenance(), "method": method}


def _rows_cols(rows: list[dict]) -> tuple[list[str], list[dict]]:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols, rows


def generate(kind: str, period: str | None = None) -> Report:
    if kind not in KINDS:
        raise ValueError(f"report kind must be one of {KINDS}")
    p, start, end = _period(period)
    if kind == "monthly_procurement":
        with db.connect() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT p.PO_ID AS po_id, substr(p.Timestamp,1,10) AS date, p.Item_ID AS sku, v.Vendor_Name AS supplier, p.Quantity AS quantity, p.Unit_Price AS unit_price, "
                "ROUND(p.Quantity*p.Unit_Price,2) AS line_total, p.Status AS status, p.Workflow_ID AS workflow_id FROM purchase_orders p LEFT JOIN vendors v USING(Vendor_ID) "
                "WHERE p.Status IN ('SENT','RECEIVED','IN_TRANSIT') AND p.Timestamp>=? AND p.Timestamp<? ORDER BY p.Timestamp", (start, end))]
        by_v: dict[str, float] = {}
        for r in rows:
            by_v[r["supplier"] or "?"] = by_v.get(r["supplier"] or "?", 0) + r["line_total"]
        cols, rows = _rows_cols(rows)
        return Report(kind, f"Monthly procurement {p}", cols, rows, {"order_lines": len(rows), "total_spend": round(sum(r["line_total"] for r in rows), 2), "spend_by_supplier": by_v},
                      _meta(kind, p, "orders with status SENT / RECEIVED / IN_TRANSIT dated in the period"))
    if kind == "supplier_performance":
        k = impact.supplier_kpis()
        rows = [{"supplier_id": s["vendor_id"], "name": s["name"], "reliability_score": s["reliability_score"], "on_time_delivery_rate": s["on_time_delivery_rate"], "average_lead_time_days": s["average_lead_time"],
                 "lead_time_variance": s["lead_time_variance"], "defect_rate": s["defect_rate"], "fill_rate": s["order_fulfillment_rate"], "cancellation_rate": s["cancellation_rate"],
                 "price_volatility": s["price_volatility"], "observations": s["observations"], "score_change_early_vs_late": (s["trend"] or {}).get("score_delta")} for s in k["suppliers"]]
        cols, rows = _rows_cols(rows)
        return Report(kind, "Supplier performance", cols, rows, {"mean_reliability_score": k["mean_reliability_score"], "mean_score_change": k["mean_score_change_early_vs_late"]},
                      _meta(kind, p, k["note"]))
    if kind == "forecast_accuracy":
        f = impact.forecast_accuracy()
        rows = [{"sku": s, "category": v["category"], "mae": v["mae"], "rmse": v["rmse"], "wape": v["wape"], "mape": v["mape"], "bias": v["bias"], "points": v["points"]} for s, v in f["by_sku"].items()]
        cols, rows = _rows_cols(rows)
        return Report(kind, "Forecast accuracy (rolling-origin back-test)", cols, rows, {"overall": f["overall"], "naive_last7_mean": f["naive_last7_mean"], "by_category": f["by_category"],
                                                                                           "by_period": f["by_period"]}, _meta(kind, p, f["note"], f["provenance"]))
    if kind == "inventory_risk":
        rows = [{"sku": r["Item_ID"], "name": r["Item_Name"], "category": r["Category"], "stock": r["Stock_Level"], "reorder_point": r["Reorder_Point"], "risk_level": r["risk_level"],
                 "days_until_stockout": r["days_until_stockout"], "stockout_probability": r["stockout_probability"], "recommended_order_quantity": r["recommended_order_quantity"],
                 "basis": r["basis"]} for r in dashboard.inventory_risk()]
        cols, rows = _rows_cols(rows)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["risk_level"]] = counts.get(r["risk_level"], 0) + 1
        return Report(kind, "Inventory risk", cols, rows, {"by_risk_level": counts}, _meta(kind, p, "deterministic risk engine; no language model involved"))
    if kind == "savings":
        k = impact.procurement_kpis(since=start)
        cmp_ = comparison.baseline_vs_optimized()
        rows = [{"po_id": r["po_id"], "sku": r["sku"], "qty": r["qty"], "baseline_unit_price": r["baseline_unit_price"], "paid_unit_price": r["paid_unit_price"],
                 "line_savings": round(r["qty"] * (r["baseline_unit_price"] - r["paid_unit_price"]), 2)} for r in k["lines_detail"]]
        cols, rows = _rows_cols(rows)
        return Report(kind, f"Procurement savings vs baseline ({p})", cols, rows,
                      {"baseline": k["baseline"], "baseline_definition": k["baseline_definition"], "baseline_cost": k["baseline_cost"], "optimized_cost": k["optimized_cost"], "savings": k["savings"],
                       "savings_pct": k["savings_pct"], "attribution": k["attribution"], "current_decision_point": {x: cmp_[x] for x in ("baseline_cost", "optimized_cost", "like_for_like_savings",
                                                                                                                                       "lead_time_difference_days", "risk_difference")}},
                      _meta(kind, p, k["attribution"], k["provenance"] if k["data_provenance"] != "SYNTHETIC" else impact.SIMULATED))
    if kind == "ai_usage":
        c = usage.llm_costs(start)
        rows = [{"agent": a, **v} for a, v in c["by_agent"].items()] + [{"model": m, **v} for m, v in c["by_model"].items()]
        cols, rows = _rows_cols(rows)
        return Report(kind, f"AI usage {p}", cols, rows, {**c["total"], "actual_marginal_cost_usd": c["actual_marginal_cost_usd"]}, _meta(kind, p, c["note"]))
    ev = [e for e in audit.events(limit=5000) if start <= e["timestamp"] < end]
    rows = [{"time": e["timestamp"], "actor": e["actor"], "event": e["event_type"], "workflow_id": e["workflow_id"], "payload": json.dumps(e["payload"], default=str)[:300]} for e in reversed(ev)]
    cols, rows = _rows_cols(rows)
    return Report("audit", f"Audit trail {p}", cols, rows, {"events": len(rows), "chain": audit.verify_chain()}, _meta("audit", p, "append-only, hash-chained audit events"))


def _safe_cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(FORMULA_PREFIX) else s


def to_csv(r: Report) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# {r.title}", f"tenant={r.meta.get('tenant_id')}", f"provenance={r.meta.get('provenance')}", f"data={r.meta.get('data_provenance')}", f"generated={r.meta.get('generated_at')}"])
    w.writerow(r.columns)
    for row in r.rows:
        w.writerow([_safe_cell(row.get(c)) for c in r.columns])
    return buf.getvalue()


def to_markdown(r: Report) -> str:
    esc = lambda v: str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")
    out = [f"# {r.title}", "", f"*Tenant `{r.meta.get('tenant_id')}` · period {r.meta.get('period')} · generated {r.meta.get('generated_at')}*", "",
           f"**Provenance: {r.meta.get('provenance')} · data: {r.meta.get('data_provenance')}**" + (" (SYNTHETIC / DEMO DATA: not customer results)" if r.meta.get("data_provenance") == "SYNTHETIC" else ""), "",
           f"Method: {r.meta.get('method')}", "", "## Summary", "", "```json", json.dumps(r.summary, indent=2, default=str), "```", ""]
    if r.rows:
        out += ["## Detail", "", "| " + " | ".join(r.columns) + " |", "|" + "---|" * len(r.columns)]
        out += ["| " + " | ".join(esc(row.get(c)) for c in r.columns) + " |" for row in r.rows[:500]]
    return "\n".join(out) + "\n"


def to_html(r: Report) -> str:
    e = html.escape
    head = "".join(f"<th>{e(c)}</th>" for c in r.columns)
    body = "".join("<tr>" + "".join(f"<td>{e('' if row.get(c) is None else str(row.get(c)))}</td>" for c in r.columns) + "</tr>" for row in r.rows[:2000])
    banner = "<p><b>SYNTHETIC / DEMO DATA: not customer results</b></p>" if r.meta.get("data_provenance") == "SYNTHETIC" else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(r.title)}</title><style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse}}"
            f"td,th{{border:1px solid #ccc;padding:4px 8px}}</style></head><body><h1>{e(r.title)}</h1>{banner}<p>Tenant {e(str(r.meta.get('tenant_id')))} · period {e(str(r.meta.get('period')))} · "
            f"provenance {e(str(r.meta.get('provenance')))}</p><p>{e(str(r.meta.get('method')))}</p><pre>{e(json.dumps(r.summary, indent=2, default=str))}</pre>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>")


def render(r: Report, fmt: str) -> tuple[str, str]:
    """(body, media type)"""
    if fmt == "csv":
        return to_csv(r), "text/csv"
    if fmt == "json":
        return json.dumps(r.to_dict(), default=str, indent=1), "application/json"
    if fmt in ("md", "markdown"):
        return to_markdown(r), "text/markdown"
    if fmt == "html":
        return to_html(r), "text/html"
    raise ValueError("format must be csv, json, md or html")
