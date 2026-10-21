"""PAGE 2 · Tactical Operations Centre: live LangGraph engine, step tracker, audit logs, human approval gate, QA / fault-injection sandbox."""
import pandas as pd
import streamlit as st

from scm import audit, seed, security
from scm.llm import health, warm
from ui import assets, core
from ui.core import *

# ---- SECTION 2 header + control deck
core.restore_widgets()      # widget state is dropped when another page runs: re-seed it from the persisted copy first
core.save_widgets()

md(st, H("<div class='sh reveal' style='margin-top:1rem'><span class='eyebrow'>Tactical operations centre</span>",
         "<h2>The live multi-agent engine</h2><p>The real system, not a mock-up: LangGraph agents, local Ollama reasoning, a human approval checkpoint, ",
         "a live SQLite store and a fault-injection sandbox. Everything you run here is shared with the other pages.</p></div>"))
with st.container(key="ctrlbar"):
    c1, c2, c3, c4, c5, c6, c7 = st.columns([1.0, 1.4, 1.05, 1.0, 1.1, .9, .9], vertical_alignment="bottom")
    c1.text_input("Ollama model", key="cfg_model")
    c2.text_input("Ollama URL", key="cfg_url")
    c3.toggle("LLM reasoning", key="cfg_llm", help="Off = instant deterministic rule-based agents.")
    c4.toggle("Fast mode", key="cfg_fast", help="LLM writes only the 2 short analysis summaries (~45s on CPU). Off = it also writes every "
              "vendor email (~90s). Tip: `ollama pull llama3.2:1b` and set it here for much faster runs.")
    c5.slider("LLM timeout (s)", 10, 180, step=5, key="cfg_timeout")
    c6.button("Run Live Pipeline", key="runbtn", width="stretch", on_click=act, args=("run",))
    c7.button("Reset demo data", key="resetbtn", width="stretch", on_click=act, args=("reset",))
core.save_widgets()
_c = conf()
model, base_url, use_llm, fast_mode, timeout = _c.model, _c.base_url, _c.use_llm, _c.fast_mode, _c.timeout
state_h, msg_h = health(base_url, model)
if state_h == "online" and use_llm:
    warm(base_url, model)  # load the model in the background so the first run is not a cold start
h_color = {"online": "#059669", "no_model": "#D97706", "offline": "#DC2626"}[state_h]


# ------------------------------------------------------------------ layout skeleton
top_slot = st.empty()
with st.container(key="scene3d"):  # static HTML => Streamlit keeps this iframe mounted; state arrives via push_scene()
    if hasattr(st, "iframe"):
        st.iframe(assets.scene_html(), height=600)
    else:
        components.html(assets.scene_html(), height=600)
kpi_slot, tele_slot = st.empty(), st.empty()
alert_slot, fault_slot, errors_slot = st.empty(), st.empty(), st.container()
flow_slot = st.empty()
slots().update(flow=flow_slot, tele=tele_slot)
approval_slot = st.container()

# ------------------------------------------------------------------ actions (queued by button callbacks)
action, ss.action = ss.action, None
try:
    if action == "reset":
        db.init_db(reset=True)
        seed.ensure_demo_data()
        drop_thread(ss.thread_id)
        db.set_kv("active_thread", None)
        ss.thread_id, ss.node_status, ss.tele_prior = None, {}, []
        ss.notice = ("success", "Demo data restored.")
        st.rerun()
    elif action == "run":
        start_run()
    elif action == "sim":
        s = ss.sim or {}
        kind = s.get("kind")
        if kind == "shock":
            hit = db.drain_healthy(3)
            ss.notice = ("warning", f"Supply shock injected: {', '.join(hit)} drained to 10% of reorder point.")
        elif kind == "stockout":
            db.set_stock(s["item"], 0)
            ss.notice = ("warning", f"{s['item']} stocked out (0 units).")
        else:
            db.set_stock(s["item"], s["level"])
            ss.notice = ("info", f"{s['item']} stock set to {s['level']}.")
        if kind in ("shock", "stockout", "apply_run"):
            start_run()
        st.rerun()
    elif action == "resume":
        snap0 = current_state()
        if snap0 and snap0.next:
            ss.node_status = derive_status(snap0)
            stream(None)
        st.rerun()
    elif action in ("approve", "reject", "resource"):
        snap0 = current_state()
        who = security.ui_principal()
        if not who.can(security.Perm.APPROVE):          # RBAC: only APPROVER / ADMIN may decide
            audit.safe_record(audit.EventType.ACCESS_DENIED, ss.get("thread_id"), who.actor, {"action": action, "role": who.role.value})
            ss.notice = ("error", f"Your role ({who.role.value}) is not allowed to approve or deny orders.")
        elif snap0:
            rounds = snap0.values.get("rounds", 0)
            drafts = [{**d, "body": ss.get(email_key(d, rounds), d["body"]), "original_body": d["body"],
                       "edited": ss.get(email_key(d, rounds), d["body"]) != d["body"]} for d in snap0.values["drafts"]]
            GRAPH.update_state(cfg(), {"decision": action, "drafts": drafts, "approver": who.actor,
                                       "decision_reason": (ss.get("sb_reason") or "")[:500]}, as_node="supervisor")
            ss.node_status[sg.HITL_NODE] = "running"
            stream(None)
        st.rerun()
    elif action in ("dbfault", "dbfault_run"):
        ss.notice = ("warning", f"Injected into SQLite: {qa.apply_scenario(ss.qa_scn)}")
        if action == "dbfault_run":
            start_run()
        st.rerun()
    elif action == "suite":
        def _suite_progress(frac, label):
            push_scene(mode="suite", progress=frac, label="QA MATRIX · " + label.upper())
        _suite_progress(0.0, "starting")
        ss.suite = qa.run_suite(_suite_progress)
        ss.notice = ("success", "QA matrix finished — see the Automated QA suite sub-tab.")
        st.rerun()
except Exception as exc:
    if is_control_flow(exc):
        raise
    report_error(f"action:{action}", exc)

# ------------------------------------------------------------------ persisted state
snap = current_state()
vals = snap.values if snap else {}
paused = bool(snap and snap.next and snap.next[0] == sg.HITL_NODE)
incomplete = bool(snap and snap.next and snap.next[0] != sg.HITL_NODE and vals.get("status") not in ("error",))
if snap:
    ss.node_status = derive_status(snap)
    if not snap.next and db.get_kv("active_thread") == ss.thread_id:
        db.set_kv("active_thread", None)   # run finished: nothing left to recover
k = db.kpis()
inv_now = db.df("SELECT * FROM inventory")
crit = inv_now[inv_now.Stock_Level <= inv_now.Reorder_Point * 0.35]

# top bar
if paused:
    run_chip = ("AWAITING APPROVAL", "#D97706")
elif vals.get("status") in ("approved", "healthy"):
    run_chip = ("RUN COMPLETE", "#059669")
elif vals.get("status") == "error":
    run_chip = ("RUN FAILED", "#DC2626")
elif vals.get("status") in ("rejected", "no_vendor", "policy_blocked"):
    run_chip = ("RUN TERMINATED", "#DC2626")
else:
    run_chip = ("IDLE", "#94A3B8")
risk = scene_risk(vals)
md(top_slot, f"<div class='topbar risk-{risk}'><div class='brand'><div class='logo'>{LOGO_SVG}</div><div><div class='t'>Pilot environment</div>"
             f"<div class='s'>Supervisor · Analytics · Procurement · Vendor Comms</div></div></div>"
             f"<div class='chips'><span class='chip' style='--c:{h_color}'><i></i>Ollama · {esc(msg_h)}</span>"
             f"<span class='chip'><i style='--c:#2563EB'></i>{'LLM ' + esc(model) if use_llm else 'Rule-based mode'}</span>"
             f"<span class='chip live' style='--c:{run_chip[1]}'><i></i>{run_chip[0]}</span></div></div>")

if len(crit):
    names = ", ".join(f"<b>{esc(r.Item_Name)}</b> ({r.Stock_Level}/{r.Reorder_Point})" for r in crit.head(4).itertuples())
    more = f" +{len(crit) - 4} more" if len(crit) > 4 else ""
    md(alert_slot, f"<div class='alert'>{WARN_SVG}<div><b>Critical stock deficit · {len(crit)} item(s)</b> &nbsp;{names}{more}</div></div>")
else:
    alert_slot.empty()

md(kpi_slot,
   "<div class='kpis'>"
   f"<div class='card kpi' style='--c:#2563EB'><div class='l'>Active inventory lines</div><div class='v'>{k['lines']}</div><div class='s'>SKUs tracked</div></div>"
   f"<div class='card kpi' style='--c:{'#DC2626' if k['low'] else '#059669'}'><div class='l'>Low-stock alerts</div><div class='v'>{k['low']}</div>"
   f"<div class='s'>{'below reorder point' if k['low'] else 'all items healthy'}</div></div>"
   f"<div class='card kpi {'pulse' if k['pending'] else ''}' style='--c:#D97706'><div class='l'>Pending approvals</div><div class='v'>{k['pending']}</div>"
   "<div class='s'>purchase orders awaiting a human</div></div>"
   f"<div class='card kpi' style='--c:#2563EB'><div class='l'>Historical savings</div><div class='v'>${k['savings']:,.0f}</div>"
   "<div class='s'>vs. average market quote</div></div></div>")

armed = faults_armed()
push_scene(vals=vals, hops=sum(e["agent"] == "Supervisor" for e in vals.get("trace", [])))
if armed:
    md(fault_slot, "<div class='faultbar'><b>FAULT INJECTION ARMED</b>" + "".join(f"<span>{esc(a)}</span>" for a in armed) +
                   "<em>next pipeline run will exercise these failure modes</em></div>")
else:
    fault_slot.empty()
with flow_slot.container(), guarded("pipeline tracker"):
    md(st, flow_html(ss.node_status, sum(e["agent"] == "Supervisor" for e in vals.get("trace", []))))
    with st.expander("Graph state inspector · checkpoints (rewind token)", expanded=bool(snap)):
        render_inspector()
md(tele_slot, telemetry_html(session_telemetry(vals), vals.get("trace", [])))

if ss.notice:
    getattr(st, ss.notice[0])(ss.notice[1])
    ss.notice = None

# ------------------------------------------------------------------ human gate & dispatch terminal
with approval_slot, guarded("human gate"):
    if incomplete:
        md(st, H("<div class='faultbar'><b>Run interrupted</b><span>stopped before '", esc(snap.next[0]), "'</span>",
                 "<em>The checkpoint is intact. Resume to continue from where it stopped.</em></div>"))
        st.button("Resume run from checkpoint", key="resume_btn", on_click=act, args=("resume",))
    if paused:
        drafts, props, rounds = vals["drafts"], vals["proposals"], vals.get("rounds", 0)
        total, sav = sum(d["total"] for d in drafts), sum(p["savings"] for p in props)
        with st.container(key=f"hitl_{hitl_risk(vals)}"):
            md(st, f"<div class='hitl-h'><div><h2>Human Gate · Dispatch Terminal</h2><p>Execution is paused and state is checkpointed. "
                   f"Nothing has been sent. Review, edit if needed, then decide.</p></div><span class='badge-amber'>PAUSED · AWAITING HUMAN</span></div>"
                   f"<div class='tots'><div><b>${total:,.2f}</b><span>Total commitment</span></div><div><b>{len(props)}</b><span>Line items</span></div>"
                   f"<div><b>{len(drafts)}</b><span>Vendors</span></div><div><b style='color:#059669'>${sav:,.2f}</b><span>Est. savings</span></div></div>")
            lines = pd.DataFrame([{"PO": p["po_id"], "Item": f"{p['Item_ID']} · {p['Item_Name']}", "Urgency": p["urgency"].upper(),
                                   "Risk": p.get("risk_level") or "—", "Cover (d)": p.get("days_until_stockout"),
                                   "Vendor": p["Vendor_Name"], "Share": p.get("share", 1.0), "Qty": p["qty"], "Unit $": p["unit_price"],
                                   "Total $": p["total"], "Savings $": p["savings"], "Lead (d)": p["lead_time"],
                                   "Supplier score": p.get("supplier_score"),
                                   "Policy": ", ".join(f["code"] for f in p.get("policy_flags", [])) or "OK"} for p in props])
            st.dataframe(lines, hide_index=True, width="stretch", column_config={
                "Unit $": st.column_config.NumberColumn(format="$%.2f"), "Total $": st.column_config.NumberColumn(format="$%.2f"),
                "Savings $": st.column_config.NumberColumn(format="$%.2f"),
                "Share": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
                "Supplier score": st.column_config.NumberColumn(format="%.0f"), "Cover (d)": st.column_config.NumberColumn(format="%.1f")})
            if vals.get("policy", {}).get("requires_executive_review"):
                st.error("Executive review required: at least one line exceeds the executive approval threshold.")
            by_code: dict[str, list[str]] = {}
            for p in props:
                for f in p.get("policy_flags", []):
                    by_code.setdefault(f["code"], []).append(f"{p['Item_ID']}: {f['message']}")
            if by_code:
                st.warning("Policy warnings (non-blocking): " + " · ".join(f"{c} on {len(v)} line(s)" for c, v in by_code.items()))
                with st.expander("Policy warning details"):
                    for c, v in by_code.items():
                        st.markdown(f"**{c}**")
                        st.markdown(chr(10).join(f"- {x}" for x in v[:20]))
            with st.expander("Decision context · forecast, risk, supplier and reasoning"):
                for p in props:
                    st.markdown(f"**{p['Item_ID']} → {p['Vendor_Name']}** · {p['qty']} units · risk **{p.get('risk_level') or 'n/a'}**"
                                f" · P(stock-out) {next((a['stockout_probability'] for a in vals.get('alerts', []) if a['Item_ID'] == p['Item_ID']), 'n/a')}"
                                f" · forecast confidence {p.get('forecast_confidence') if p.get('forecast_confidence') is not None else 'n/a'}")
                    st.caption(p.get("why", ""))
            st.text_input("Decision reason (recorded in the audit log)", key="sb_reason", max_chars=300)
            t_mail, t_erp = st.tabs(["Automated Procurement Email", "ERP Webhook Payload"])
            with t_mail:
                st.caption("The draft is editable — your wording is what gets sent and archived on approval.")
                vt = st.tabs([d["Vendor_Name"] for d in drafts]) if len(drafts) > 1 else [st.container()]
                for tab, d in zip(vt, drafts):
                    with tab:
                        st.caption(f"To: {d['to']}  ·  Subject: {d['subject']}  ·  drafted by "
                                   f"{ {'llm': 'LLM', 'fallback': 'template (LLM fallback)'}.get(d['source'], 'template') }")
                        st.text_area("Email body", d["body"], height=330, key=email_key(d, rounds), label_visibility="collapsed")
            with t_erp:
                st.caption("POST https://erp.example.com/api/v1/purchase-orders · simulated — approval writes this payload to "
                           "/outbox as ERP_<PO>.json, no network call is made.")
                st.code(json.dumps(sg.erp_payload(drafts, props), indent=2), language="json", height=380)
            c1, c2, c3 = st.columns(3)
            c1.button("Approve Shipment", key="approve", width="stretch", on_click=act, args=("approve",))
            c2.button("Deny Order", key="deny", width="stretch", on_click=act, args=("reject",))
            left = sg.MAX_RESOURCE_ROUNDS - rounds
            c3.button(f"Deny & Re-source ({left} left)", key="resource", width="stretch", disabled=left <= 0,
                      on_click=act, args=("resource",), help="Rejects these vendors; Procurement picks next-best suppliers.")
    elif vals.get("status") in ("approved", "rejected", "healthy", "no_vendor", "policy_blocked", "error"):
        o = vals.get("outcome", {})
        title, color, body = {
            "approved": ("Shipment approved", "#059669", f"{len(o.get('po_ids', []))} POs sent to {o.get('emails', 0)} vendor(s) · "
                         f"${o.get('total', 0):,.2f} committed · ${o.get('savings', 0):,.2f} saved · inventory updated · ERP payload archived."),
            "rejected": ("Order denied", "#DC2626", "Purchase orders rejected. No emails were sent and stock is unchanged."),
            "healthy": ("All stock healthy", "#059669", "No item is below its reorder point (earlier approvals already restocked everything), so there was nothing to order. "
                        "Use Reset demo data, or the Simulation Sandbox, to create low-stock items."),
            "no_vendor": ("No eligible vendors", "#D97706", "All candidate vendors were excluded or invalid. Run terminated safely."),
            "policy_blocked": ("Blocked by business policy", "#DC2626", "Every proposed line violated a deterministic business rule (see the audit trail). Nothing was ordered."),
            "error": ("Pipeline halted by an exception", "#DC2626", "A node raised an error; pending orders were rolled back. See the exception panel above."),
        }[vals["status"]]
        md(st, f"<div class='result' style='--c:{color}'><b>{title}</b><div>{body}</div></div>")

# ------------------------------------------------------------------ tabs
t_audit, t_matrix, t_sandbox, t_db, t_qa = st.tabs(["System Audit Trail", "Procurement Matrix", "Simulation Sandbox", "Live Database",
                                                  "QA & Fault Injection"])

with t_audit, guarded("System Audit Trail"):
    md(st, "<div class='sec' style='margin-top:.4rem'><h3>System Audit Trail</h3>"
           "<span>supervisor routing decisions are separated from agent execution traces · timestamps local</span></div>")
    md(st, panes_html(vals.get("trace", []), 440))
    if vals.get("human_choices"):
        st.caption("Human decisions: " + " → ".join(f"{h['decision']} @ {h['ts']}" for h in vals["human_choices"]))

with t_matrix, guarded("Procurement Matrix"):
    props = vals.get("proposals") or []
    if not props:
        st.info("Run the pipeline to see how Procurement compares competing vendors for each low-stock item.")
    else:
        idx = st.selectbox("Low-stock item", range(len(props)),
                           format_func=lambda i: f"{props[i]['Item_ID']} · {props[i]['Item_Name']}  ({props[i]['urgency'].upper()})")
        pick = props[idx]
        qs = pick["quotes"]
        best_p, best_l, best_r = min(q["Unit_Price"] for q in qs), min(q["Lead_Time_Days"] for q in qs), max(q["Reliability"] for q in qs)
        avg = sum(q["Unit_Price"] for q in qs) / len(qs)
        rows = []
        for q in qs:
            tags = (["◆ SELECTED"] if q["Vendor_ID"] == pick["Vendor_ID"] else []) + \
                   (["Best price"] if q["Unit_Price"] == best_p else []) + (["Fastest"] if q["Lead_Time_Days"] == best_l else []) + \
                   (["Most reliable"] if q["Reliability"] == best_r else [])
            rows.append({"Vendor": q["Vendor_Name"], "Unit price": q["Unit_Price"], "vs avg": q["Unit_Price"] / avg - 1,
                         "Lead time (d)": q["Lead_Time_Days"], "Reliability %": q["Reliability"],
                         "Order total": q["Unit_Price"] * pick["qty"], "Verdict": " · ".join(tags) or "—"})
        mdf = pd.DataFrame(rows)
        styled = (mdf.style.format({"Unit price": "${:,.2f}", "vs avg": "{:+.1%}", "Reliability %": "{:.0f}%", "Order total": "${:,.2f}"})
                  .apply(lambda r: ["background-color:rgba(16,185,129,.2);font-weight:600" if r["Verdict"].startswith("◆") else ""] * len(r), axis=1)
                  .apply(lambda c: ["color:#10B981;font-weight:700" if v == best_p else "" for v in c], subset=["Unit price"])
                  .apply(lambda c: ["color:#10B981;font-weight:700" if v == best_l else "" for v in c], subset=["Lead time (d)"])
                  .apply(lambda c: ["color:#10B981;font-weight:700" if v == best_r else "" for v in c], subset=["Reliability %"]))
        st.dataframe(styled, hide_index=True, width="stretch")
        md(st, f"<div class='why'><b>Why {esc(pick['Vendor_Name'])}?</b> {esc(pick['why'])}</div>")
        m1, m2, m3 = st.columns(3)
        m1.metric("Order quantity", f"{pick['qty']:,}")
        m2.metric("Selected total", f"${pick['total']:,.2f}")
        m3.metric("Saved vs average quote", f"${pick['savings']:,.2f}")

with t_sandbox, guarded("Simulation Sandbox"):
    md(st, "<div class='sec' style='margin-top:.4rem'><h3>Dynamic Order Simulation Sandbox</h3>"
           "<span>force a stock crisis and watch the agents respond · changes the live SQLite database</span></div>")
    cl, cr = st.columns([3, 2])
    with cl:
        inv = db.df("SELECT * FROM inventory ORDER BY Item_ID")
        item = st.selectbox("Item", inv.Item_ID, format_func=lambda i: f"{i} · {inv.set_index('Item_ID').Item_Name[i]}", key="sb_item")
        row = inv.set_index("Item_ID").loc[item]
        lo, hi = min(0, int(row.Stock_Level)), max(int(row.Target_Stock), int(row.Stock_Level), int(row.Reorder_Point), 1)
        level = st.slider("Simulated stock level", lo, hi, int(row.Stock_Level), key=f"sb_level_{item}")
        ratio = max(level, 0) / max(row.Reorder_Point, 1)
        label, col = (("CRITICAL DEFICIT", "#DC2626") if ratio <= .35 else ("LOW STOCK", "#D97706") if ratio < 1 else ("HEALTHY", "#059669"))
        md(st, f"<span class='chip' style='--c:{col};color:{col};border-color:{col}'><i></i>{label} · {ratio:.0%} of reorder point "
               f"({level}/{int(row.Reorder_Point)}, target {int(row.Target_Stock)})</span>")
    with cr:
        st.caption("Scenarios")
        st.button("Apply stock level", width="stretch", on_click=sim, args=("apply",), key="sb_apply")
        st.button("Apply & run pipeline", width="stretch", type="primary", on_click=sim, args=("apply_run",), key="sb_apply_run")
        st.button("Trigger stock-out (0 units) & run", width="stretch", on_click=sim, args=("stockout",), key="sb_out")
        st.button("Inject supply shock: drain 3 healthy items & run", width="stretch", on_click=sim, args=("shock",), key="sb_shock")
    st.caption("Use **Reset demo data** in the control deck to restore the seeded inventory.")


@st.fragment(run_every="5s")
def db_explorer():
    inv = db.df("SELECT * FROM inventory")
    inv["Status"] = ["LOW" if s < r else "OK" for s, r in zip(inv.Stock_Level, inv.Reorder_Point)]
    inv["Fill vs target"] = (inv.Stock_Level / inv.Target_Stock.where(inv.Target_Stock > 0, 1)).clip(0, 1)
    a, b, c, d, e = st.tabs(["Inventory", "Vendors", "Vendor price listings", "Purchase orders", "Access requests"])
    with a:
        st.dataframe(inv, hide_index=True, width="stretch", column_config={
            "Fill vs target": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
        st.bar_chart(inv.set_index("Item_Name")[["Stock_Level", "Reorder_Point"]], height=260)
    with b:
        st.dataframe(db.df("SELECT * FROM vendors"), hide_index=True, width="stretch")
    with c:
        st.dataframe(db.df("""SELECT l.Item_ID, i.Item_Name, v.Vendor_Name, l.Unit_Price, l.Lead_Time_Days, v.Reliability
                              FROM vendor_listings l JOIN vendors v USING(Vendor_ID) JOIN inventory i USING(Item_ID)
                              ORDER BY l.Item_ID, l.Unit_Price"""), hide_index=True, width="stretch")
    with d:
        st.dataframe(db.df("SELECT * FROM purchase_orders ORDER BY Timestamp DESC, PO_ID DESC"), hide_index=True, width="stretch")
    with e:
        st.dataframe(db.df("SELECT * FROM access_requests ORDER BY Request_ID DESC"), hide_index=True, width="stretch")
    st.caption("Auto-refreshes every 5 seconds.")


with t_db, guarded("Live database"):
    db_explorer()

# ------------------------------------------------------------------ QA & fault injection terminal
with t_qa, guarded("QA terminal"), st.container(key=f"qaterm_{scene_risk(vals)}"):
    md(st, "<div class='qa-h'><h3>QA &amp; Fault Injection</h3><span class='tag'>QA MODE</span></div>")
    st.caption("Faults stay armed until disarmed — the next pipeline run exercises them.")
    q_fault, q_state, q_suite = st.tabs(["Fault injection", "State & DB deltas", "Automated QA suite"])

    with q_fault:
        cA, cB, cC = st.columns(3)
        with cA:
            md(st, "<div class='qa-card-t'>LLM Output Mutation Simulator</div>")
            st.selectbox("Replace the model's reply with…", [*MUTATIONS, CUSTOM],
                         format_func=lambda k: CUSTOM_LABEL if k == CUSTOM else MUTATIONS[k][0], key="qa_mut")
            payload = ss.get("qa_custom", "") if ss.qa_mut == CUSTOM else MUTATIONS[ss.qa_mut][1]
            if ss.qa_mut == CUSTOM:
                st.text_area("Raw model reply to inject", key="qa_custom", value=None, height=190, placeholder='{"selected_vendor": "...", "recommended_order_quantity": 185, ...')
            elif ss.qa_mut == "none":
                st.caption("Real Ollama responses are used.")
            elif isinstance(payload, tuple):
                st.code(f"raise {type(payload[1]).__name__}({str(payload[1])!r})", language="python")
            else:
                st.code(payload[:600] if payload.strip() else repr(payload), language="json", wrap_lines=True)
            st.caption("Hits the extraction engine in Analytics, Procurement and Vendor Comms. Active even with LLM reasoning off. "
                       "Expected: handled via parse → validate → fallback, never a crash.")
        with cB:
            md(st, "<div class='qa-card-t'>Database Inconsistency Stress-Tester</div>")
            st.selectbox("Edge-case data to inject", list(qa.SCENARIOS), format_func=lambda k: qa.SCENARIOS[k][0], key="qa_scn")
            st.button("Inject into SQLite", width="stretch", on_click=act, args=("dbfault",), key="qa_inject")
            st.button("Inject & run pipeline", width="stretch", on_click=act, args=("dbfault_run",), key="qa_inject_run")
            st.button("Restore seed data", width="stretch", on_click=act, args=("reset",), key="qa_restore")
        with cC:
            md(st, "<div class='qa-card-t'>Latency &amp; Timeout Emulator</div>")
            st.slider("Injected delay inside the Ollama call (s)", 0.0, 15.0, step=0.5, key="qa_latency")
            lat = float(ss.qa_latency)
            if lat == 0:
                st.caption("No delay.")
            elif lat >= timeout:
                st.caption(f"⏱ {lat:g}s ≥ timeout {timeout}s → the first call **times out**, the circuit breaker trips and all "
                           "agents fall back to rules.")
            else:
                st.caption(f"⏱ {lat:g}s < timeout {timeout}s → every LLM call is slow but succeeds. Lower the LLM timeout above "
                           f"below {lat:g}s to force a timeout.")
            st.selectbox("Crash a LangGraph node", ["none", *qa.NODES], key="qa_crash",
                         help="Raises RuntimeError inside the node to test the global exception wrapper.")
            st.button("Disarm all faults", width="stretch", on_click=disarm, key="qa_disarm")

    with q_state:
        b, a = ss.db_before, ss.db_after
        md(st, "<div class='qa-card-t'>Database before / after the last pipeline run</div>")
        if not (b and a):
            st.info("Run the pipeline to capture a before/after snapshot.")
        else:
            d = qa.diff(b, a)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Rows added", int((d.Change == "ADDED").sum()))
            m2.metric("Rows removed", int((d.Change == "REMOVED").sum()))
            m3.metric("Fields modified", int((d.Change == "MODIFIED").sum()))
            units = int(a["inventory"].Stock_Level.sum() - b["inventory"].Stock_Level.sum())
            m4.metric("Net stock units", f"{units:+,}")
            st.caption(f"before run @ {b['taken']}  →  latest snapshot @ {a['taken']}")
            t_d, t_b, t_a = st.tabs(["Delta", "Before snapshot", "After snapshot"])
            with t_d:
                if d.empty:
                    st.success("No data changed (pipeline paused or found nothing to do).")
                else:
                    tint = {"ADDED": "background-color:rgba(16,185,129,.18)", "REMOVED": "background-color:rgba(239,68,68,.16)", "MODIFIED": "background-color:rgba(245,158,11,.18)"}
                    st.dataframe(d.style.apply(lambda r: [tint[r["Change"]]] * len(r), axis=1), hide_index=True, width="stretch")
            for tab, snap_ in ((t_b, b), (t_a, a)):
                with tab:
                    st.dataframe(snap_["inventory"], hide_index=True, width="stretch")
                    st.dataframe(snap_["purchase_orders"], hide_index=True, width="stretch")
        md(st, "<div class='qa-card-t' style='margin-top:1rem'>Checkpoint history trace</div>")
        hist = list(GRAPH.get_state_history(cfg())) if ss.thread_id else []
        if hist:
            hist.reverse()
            st.dataframe(pd.DataFrame([{"#": i, "step": h.metadata.get("step"), "next": ", ".join(h.next) or "END",
                                        "stage": h.values.get("stage"), "status": h.values.get("status"),
                                        "alerts": len(h.values.get("alerts", [])), "proposals": len(h.values.get("proposals", [])),
                                        "drafts": len(h.values.get("drafts", [])), "trace events": len(h.values.get("trace", [])),
                                        "checkpoint_id": h.config["configurable"]["checkpoint_id"]} for i, h in enumerate(hist)]),
                         hide_index=True, width="stretch")
            st.caption("Use the *Rewind token* slider under the pipeline tracker to open the full JSON state at any checkpoint.")
        else:
            st.caption("No checkpoints yet.")

    with q_suite:
        n_cases = len(qa.SCENARIOS) * len(MUTATIONS) + len(qa.NODES)
        st.caption(f"Runs {len(qa.SCENARIOS)} DB scenarios × {len(MUTATIONS)} LLM faults + {len(qa.NODES)} node-crash tests = "
                   f"**{n_cases} cases** against a throwaway database (your live data and /outbox are untouched). "
                   "Each case drives the graph to the human gate, approves, then checks invariants: no exception, no dangling "
                   "pending POs, no non-positive PO quantities/prices, correct fallback behaviour.")
        st.button(f"Run full QA matrix ({n_cases} cases)", key="suite_btn", on_click=act, args=("suite",))
        res = ss.suite
        if res:
            r = pd.DataFrame(res)
            fails = r[r.Result == "FAIL"]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Cases", len(r))
            m2.metric("Passed", int((r.Result == "PASS").sum()))
            m3.metric("Failed", len(fails))
            m4.metric("Total time", f"{r.ms.sum() / 1000:.1f}s")
            show = r.drop(columns=[c for c in ("Trace",) if c in r])
            st.dataframe(show.style.apply(lambda row: ["background-color:rgba(239,68,68,.18)" if row["Result"] == "FAIL" else ""] * len(row), axis=1),
                         hide_index=True, width="stretch")
            for _, row in fails.iterrows():
                with st.expander(f"FAIL · {row['DB scenario']} × {row['LLM fault']} × {row['Node crash']}"):
                    st.write(row["Detail"])
                    if isinstance(row.get("Trace"), str):
                        st.code(row["Trace"], language="python")
            st.download_button("Download results (.csv)", show.to_csv(index=False), file_name="qa_matrix.csv", key="qa_csv")

# ------------------------------------------------------------------ exception visualizer (filled last so it sees every error)
with errors_slot:
    render_errors(vals)

core.save_widgets()   # persist this page's widget values for the other pages
