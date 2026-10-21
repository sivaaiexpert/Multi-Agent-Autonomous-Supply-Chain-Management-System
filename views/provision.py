"""PAGE 3 · Enterprise Provisioning Terminal: the three ways to hook your own data into the agents.

  1. CSV / Excel sandbox uploader   -> validates and writes into the live SQLite inventory (with undo)
  2. Direct on-premise link         -> step-by-step PostgreSQL / MySQL connection test (credentials never stored)
  3. ERP API webhooks               -> exact outgoing REST payload per ERP, optional live delivery when a human approves
"""
import json
from datetime import datetime

import pandas as pd
import streamlit as st

from scm import ingest
from ui import core
from ui.core import *

ss.setdefault("ing_port", 5432); ss.setdefault("ing_ro", True); ss.setdefault("ing_seed", False)
ss.setdefault("ing_port", 5432); ss.setdefault("ing_ro", True); ss.setdefault("ing_seed", False)
core.restore_widgets()
core.save_widgets()


def tile_status(ok: bool | None, text: str) -> str:
    cls = "ok" if ok else "warn" if ok is None else ""
    return f"<span class='st {cls}'>{esc(text)}</span>"


# ---------------------------------------------------------------- header + at-a-glance status
inv_n = int(db.df("SELECT COUNT(*) n FROM inventory").n[0])
ven_n = int(db.df("SELECT COUNT(*) n FROM vendors").n[0])
lst_n = int(db.df("SELECT COUNT(*) n FROM vendor_listings").n[0])
last, conn_res, hook_on = ss.ing_last, ss.ing_conn, bool(ingest.WEBHOOK.get("armed"))
md(st, H("<div class='sh reveal' style='margin-top:1rem'><span class='eyebrow'>Enterprise provisioning terminal</span>",
         "<h2>Hook your own data into the agents</h2><p>Three ingest paths, one engine. Try each of them against the live system: whatever you load here is what the ",
         "Operations Centre's agents analyse, source and report on.</p></div>"))
md(st, H("<div class='kpis'>",
         f"<div class='card kpi' style='--c:#2563EB'><div class='l'>Inventory lines</div><div class='v'>{inv_n:,}</div></div>",
         f"<div class='card kpi' style='--c:#059669'><div class='l'>Vendors</div><div class='v'>{ven_n:,}</div></div>",
         f"<div class='card kpi' style='--c:#D97706'><div class='l'>Vendor price quotes</div><div class='v'>{lst_n:,}</div></div>",
         f"<div class='card kpi' style='--c:var(--ink)'><div class='l'>Pending approvals</div><div class='v'>{db.kpis()['pending']}</div></div></div>"))
m1 = tile_status(True, f"Imported {last['res']['inserted'] + last['res']['updated']} rows · {last['ts']}") if last and not last.get("undone") else tile_status(None, "Demo dataset active")
m2 = (tile_status(True, "Connection verified") if conn_res and conn_res["ok"] else tile_status(False, f"Failed at: {conn_res['state']}") if conn_res else tile_status(None, "Not tested yet"))
m3 = tile_status(True, f"Armed → {ingest.WEBHOOK.get('target')}") if hook_on else tile_status(None, "Preview only (not armed)")
md(st, H("<div class='ing-steps'>",
         "<div class='ing-step reveal' style='--i:0'><div class='n'>Method 1</div><b>CSV / Excel sandbox</b><span>Upload an inventory sheet to overwrite or update the live table.</span>", m1, "</div>",
         "<div class='ing-step reveal' style='--i:1'><div class='n'>Method 2</div><b>Direct on-premise link</b><span>Verify network path and credentials to your PostgreSQL / MySQL server.</span>", m2, "</div>",
         "<div class='ing-step reveal' style='--i:2'><div class='n'>Method 3</div><b>ERP API webhooks</b><span>Exact REST payload for SAP, NetSuite or Zoho, fired when a human approves.</span>", m3, "</div></div>"))
if db.kpis()["pending"]:
    st.warning("A run is waiting for human approval. New data does not alter that run; it applies to the next run.")

# ================================================================ METHOD 1 — CSV / Excel
with st.container(key="ing_m1"):
    md(st, "<div class='m-h'><span class='badge-n'>1</span><h3>CSV / Excel sandbox uploader</h3></div>"
           "<p class='m-p'>Upload an inventory sheet to test your own parameters instantly. Headers are matched flexibly (SKU, On Hand, Reorder, Max...). "
           "Rows are validated before anything is written, the import is one atomic transaction, and you can undo it.</p>")
    left, right = st.columns([3, 2], gap="large")
    with right:
        st.download_button("Download CSV template", ingest.template_csv(), file_name="inventory_template.csv", mime="text/csv", key="dl_template", width="stretch")
        st.radio("Import mode", ["Update + add (upsert)", "Overwrite inventory"], key="ing_mode",
                 help="Upsert: update matching Item_IDs and add new ones. Overwrite: additionally remove items that are not in the file.")
        st.checkbox("Attach demo vendor quotes to new SKUs", key="ing_seed",
                    help="Imported SKUs have no supplier quotes, so Procurement would skip them. This adds 3 demo quotes so you can run the full pipeline.")
    with left:
        up = st.file_uploader("Inventory file (.csv, .xlsx)", type=["csv", "xlsx", "xlsm"], key="upl_inventory",
                              help="Max 5 MB / 5,000 rows. Required: Item_ID, Item_Name, Stock_Level, Reorder_Point, Target_Stock.")
    clean = pd.DataFrame()
    if up is not None:
        try:
            raw = ingest.read_table(up.getvalue(), up.name)
            norm, mapping, missing = ingest.normalize_columns(raw)
            if missing:
                st.error(f"Missing required column(s): {', '.join(missing)}. Found: {', '.join(map(str, raw.columns))}. Download the template to see the expected headers.")
            else:
                clean, rep = ingest.validate_inventory(norm)
                a, b, c, d = st.columns(4)
                a.metric("Rows read", f"{rep['rows_in']:,}")
                b.metric("Valid", f"{rep['rows_ok']:,}")
                c.metric("Rejected", f"{len(rep['errors']):,}")
                d.metric("Warnings", f"{len(rep['warnings']):,}")
                with st.expander("Column mapping"):
                    st.dataframe(pd.DataFrame({"Your column": list(mapping), "Mapped to": list(mapping.values())}), hide_index=True, width="stretch")
                for ln, sev, msg in rep["flags"]:
                    (st.error if sev == "HIGH" else st.warning)(f"{'HIGH-PRIORITY FLAG' if sev == 'HIGH' else 'Sanitised'} · row {ln}: {msg}")
                ss.ing_flags = rep["flags"]
                if rep["errors"]:
                    with st.expander(f"Rejected rows ({len(rep['errors'])})"):
                        st.dataframe(pd.DataFrame(rep["errors"], columns=["Spreadsheet row", "Reason"]), hide_index=True, width="stretch")
                if rep["warnings"]:
                    with st.expander(f"Warnings ({len(rep['warnings'])})"):
                        st.dataframe(pd.DataFrame(rep["warnings"], columns=["Spreadsheet row", "Warning"]), hide_index=True, width="stretch")
                st.dataframe(clean.head(100), hide_index=True, width="stretch")
        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:  # never show a raw crash for hostile input
            st.error(f"Unexpected error while reading the file: {type(exc).__name__}: {str(exc)[:200]}")
    overwrite = ss.get("ing_mode") == "Overwrite inventory"
    existing = set(db.df("SELECT Item_ID FROM inventory").Item_ID)
    if not clean.empty:
        new_ids = [i for i in clean.Item_ID if i not in existing]
        removing = len(existing - set(clean.Item_ID)) if overwrite else 0
        st.caption(f"Plan: **{len(new_ids)}** new, **{len(clean) - len(new_ids)}** updated" + (f", **{removing}** removed" if overwrite else "") + ".")
    confirm = True
    if overwrite and not clean.empty:
        confirm = st.checkbox(f"I understand this removes {removing} item(s) (and their vendor quotes) that are not in the file", key="ing_confirm")
    b1, b2 = st.columns([1, 1])
    if b1.button("Apply to live database", key="btn_apply", type="primary", disabled=clean.empty or not confirm, width="stretch"):
        try:
            backup = ingest.inventory_backup()
            res = ingest.apply_inventory(clean, "overwrite" if overwrite else "upsert")
            seeded = ingest.seed_listings_for_new_items([i for i in clean.Item_ID if i not in existing]) if ss.get("ing_seed") else 0
            ss.ing_backup = backup
            ss.ing_last = {"ts": datetime.now().strftime("%H:%M:%S"), "res": res, "before": len(backup), "after": int(db.df("SELECT COUNT(*) n FROM inventory").n[0]),
                           "blocked": len(rep["errors"]), "flags": len(rep["flags"]), "file": up.name if up else "", "seeded": seeded}
            st.success(f"Imported: {res['inserted']} added, {res['updated']} updated, {res['removed']} removed" + (f", {seeded} demo quotes attached." if seeded else "."))
            st.rerun()
        except Exception as exc:
            st.error(f"Import failed and was rolled back: {type(exc).__name__}: {str(exc)[:200]}")
    if ss.ing_backup is not None and b2.button("Undo last import", key="btn_undo", width="stretch"):
        n = ingest.restore_inventory(ss.ing_backup)
        ss.ing_backup = None
        if ss.ing_last:
            ss.ing_last["undone"] = True
        st.success(f"Restored the previous inventory ({n} rows).")
        st.rerun()
    if last and not last.get("undone"):
        st.info(f"Last import: **{last['file'] or 'file'}** at {last['ts']}: {last['res']['inserted']} added · {last['res']['updated']} updated · {last['res']['removed']} removed.")
        md(st, H("<div class='term'><div class='tl'>",
                 f"[{esc(last['ts'])}] DB UPDATE OK · file={esc(last['file'])}<br>",
                 f"inventory rows {last['before']} -> {last['after']} · inserted={last['res']['inserted']} updated={last['res']['updated']} removed={last['res']['removed']}<br>",
                 f"blocked rows={last['blocked']} · flags raised={last['flags']} · transaction committed atomically</div></div>"))
        st.page_link("views/ops.py", label="Run the agents on this data in the Operations Centre", icon=":material/arrow_forward:")

# ================================================================ METHOD 2 — direct on-premise link
with st.container(key="ing_m2"):
    md(st, "<div class='m-h'><span class='badge-n'>2</span><h3>Direct on-premise database link</h3></div>"
           "<p class='m-p'>Verify the network path and credentials to your own PostgreSQL or MySQL server in four checks: parameters, DNS, TCP, then a read-only "
           "<code>SELECT version()</code>. The password is used once, never stored, logged or shown.</p>")
    with st.form("ing_dbform"):
        f1, f2, f3 = st.columns([1.1, 2, 1])
        f1.selectbox("Database type", ["PostgreSQL", "MySQL"], key="ing_dbtype")
        f2.text_input("Host", key="ing_host", placeholder="db.internal.example.com")
        f3.number_input("Port", min_value=1, max_value=65535, step=1, key="ing_port")
        g1, g2, g3 = st.columns(3)
        g1.text_input("User", key="ing_user", placeholder="scm_readonly")
        g2.text_input("Password", type="password", key="sec_pw")
        g3.text_input("Database name", key="ing_db", placeholder="erp_inventory")
        ro = st.checkbox("These credentials are read-only (recommended)", key="ing_ro")
        go = st.form_submit_button("Test Connection Link", type="primary")
    if go:
        with st.spinner("Probing connection..."):
            ss.ing_conn = {**ingest.test_connection(ss.ing_dbtype, ss.ing_host, ss.ing_port, ss.ing_user, ss.get("sec_pw", ""), ss.ing_db),
                           "ts": datetime.now().strftime("%H:%M:%S"), "target": f"{ss.ing_dbtype}://{ss.ing_host}:{ss.ing_port}/{ss.ing_db}"}
    r = ss.ing_conn
    if r:
        rows = "".join(f"<div class='lnk-step {'ok' if s['ok'] else 'bad'}'><b>{'✓' if s['ok'] else '✕'} {esc(s['step'])}</b><span class='d'>{esc(s['detail'])}</span><em>{s['ms']} ms</em></div>"
                       for s in r["steps"])
        md(st, f"<div style='margin:.4rem 0'>{rows}</div>")
        (st.success if r["ok"] else st.error)(f"{r['message']}  ·  {r.get('target', '')}  ·  {r.get('ts', '')}")
        if not ro:
            st.warning("Use a read-only account: the agents only ever need SELECT access.")
    st.caption("Scope: this verifies connectivity and authentication. The agents currently read inventory from the local SQLite store; mapping your tables onto the "
               "schema (and nightly sync) is the Custom Enterprise Integration engagement. Drivers: `pip install psycopg2-binary` or `pymysql`.")

# ================================================================ METHOD 3 — ERP webhooks
with st.container(key="ing_m3"):
    md(st, "<div class='m-h'><span class='badge-n'>3</span><h3>Enterprise ERP API webhooks</h3></div>"
           "<p class='m-p'>When a human clicks <b>Approve</b>, the agent builds one purchase-order request per PO in your ERP's shape. Below is the exact outgoing REST call. "
           "Arm the webhook to deliver it for real; the credential stays in server memory only.</p>")
    h1, h2, h3 = st.columns([1.2, 2, 1.2])
    h1.selectbox("Target ERP", list(ingest.TARGETS), key="hook_target")
    h2.text_input("Base URL", key="hook_url", placeholder="https://your-erp.example.com")
    h3.selectbox("Authentication", ingest.AUTH_TYPES, key="hook_auth")
    k1, k2 = st.columns(2)
    if ss.hook_auth == "API key header":
        k1.text_input("Header name", key="hook_keyhdr")
    elif ss.hook_auth == "Basic auth":
        k1.text_input("Username", key="hook_user")
    k2.text_input("Secret / token", type="password", key="sec_hook", help="Held in memory while the server runs. Never written to disk or the database.")
    url_ok = ingest.valid_url(ss.hook_url)
    hook_cfg = {"base_url": ss.hook_url.strip(), "target": ss.hook_target, "auth_type": ss.hook_auth, "key_header": ss.get("hook_keyhdr", "X-API-Key"),
                "user": ss.get("hook_user", ""), "secret": ss.get("sec_hook", "")}
    a1, a2, a3 = st.columns(3)
    if a1.button("Arm webhook (fire on Approve)", key="btn_arm", type="primary", disabled=not url_ok, width="stretch"):
        ingest.arm(hook_cfg)
        st.rerun()
    if a2.button("Disarm", key="btn_disarm", disabled=not hook_on, width="stretch"):
        ingest.disarm()
        st.rerun()
    ack = a3.checkbox("Allow a real test request", key="hook_ack", help="Sends the SAMPLE batch (not your data) to the URL above.")
    if st.button("Send test webhook", key="btn_hooktest", disabled=not (url_ok and ack)):
        with st.spinner("Delivering sample batch..."):
            ss.hook_log = {"ts": datetime.now().strftime("%H:%M:%S"), **ingest.deliver(hook_cfg, ingest.sample_payload())}
    if hook_on:
        st.success(f"Webhook ARMED: {ingest.WEBHOOK.get('target')} at {ingest.WEBHOOK.get('base_url')}. Approvals in the Operations Centre now deliver automatically.")
    elif ss.hook_url and not url_ok:
        st.error("Enter a valid http(s) URL, for example https://your-erp.example.com")
    if ss.hook_log:
        lg = ss.hook_log
        (st.success if lg.get("ok") else st.error)(f"Test at {lg['ts']}: {lg.get('sent', 0)} delivered, {lg.get('failed', 0)} failed" + (f" ({lg['error']})" if lg.get("error") else ""))
        if lg.get("results"):
            st.dataframe(pd.DataFrame(lg["results"]), hide_index=True, width="stretch")

    st.radio("Payload source", ["Latest run (live from the graph)", "Sample batch"], horizontal=True, key="hook_src")
    snap = current_state()
    vals = snap.values if snap else {}
    live = ss.hook_src.startswith("Latest") and vals.get("drafts") and vals.get("proposals")
    if live:
        status = "APPROVED" if vals.get("status") == "approved" else "PENDING_HUMAN_REVIEW"
        payload = sg.erp_payload(vals["drafts"], vals["proposals"], status)
        st.caption(f"Live payload from your latest pipeline run (status: {status}).")
    else:
        payload = ingest.sample_payload()
        st.caption("Sample batch" + (" (no pipeline run with drafts yet: run the engine in the Operations Centre to see your own)." if ss.hook_src.startswith("Latest") else "."))
    reqs = ingest.format_requests(ss.hook_target, payload)
    if reqs:
        idx = st.selectbox("Purchase order", range(len(reqs)), format_func=lambda i: f"{i + 1} of {len(reqs)} · {payload['purchase_orders'][i]['po_number']}", key="hook_po") if len(reqs) > 1 else 0
        idx = min(idx, len(reqs) - 1)
        base = (ss.hook_url.strip().rstrip("/") if url_ok else "https://your-erp.example.com")
        headers = ingest.mask_headers(ingest.build_headers(ss.hook_auth, ss.get("sec_hook", "") or "<token>", ss.get("hook_keyhdr", "X-API-Key"), ss.get("hook_user", "")))
        md(st, f"<div class='hook-req'><span class='m'>{reqs[idx]['method']}</span><span class='u'>{esc(base + reqs[idx]['path'])}</span></div>")
        t_body, t_head, t_curl, t_batch = st.tabs(["Request body", "Headers", "cURL", "Neutral batch (as produced by the agent)"])
        body_json = json.dumps(reqs[idx]["body"], indent=2)
        t_body.caption(ingest.TARGETS[ss.hook_target]["note"] + f"  ·  {len(reqs)} request(s) per approved batch")
        t_body.code(body_json, language="json")
        t_head.code(json.dumps(headers, indent=2), language="json")
        hdr = " ".join(f"-H '{k}: {v}'" for k, v in headers.items())
        t_curl.code(f"curl -X {reqs[idx]['method']} '{base + reqs[idx]['path']}' {hdr} \\\n  -d '{json.dumps(reqs[idx]['body'])}'", language="bash")
        t_batch.code(json.dumps(payload, indent=2), language="json")

core.save_widgets()
