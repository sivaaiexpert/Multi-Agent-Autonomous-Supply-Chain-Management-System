"""Shared engine + UI helpers used by every page: session/state locks, graph access, HTML components, streaming executor.

Pages import from here; nothing in this module renders a page by itself. State that must survive page switches lives in
`st.session_state` (see `persist` / `restore_widgets` / `save_widgets`) and in the LangGraph checkpointer + SQLite.
"""
from __future__ import annotations

import copy
import json
import platform
import time
import traceback
import uuid
from contextlib import contextmanager
from html import escape
from types import SimpleNamespace

import streamlit as st
import streamlit.components.v1 as components

from scm import db, graph as sg, qa, seed
from scm.llm import CUSTOM, CUSTOM_LABEL, MUTATIONS
from ui.config import *



def md(target, html: str):
    """Render raw HTML; '$' is entity-escaped so Streamlit's markdown never treats prices as LaTeX."""
    target.markdown(html.replace("$", "&#36;"), unsafe_allow_html=True)


def esc(text) -> str:
    return escape(str(text)).replace("\n", "&#10;")


# ------------------------------------------------------------------ resources / session state locks
db.init_db()
try:
    seed.ensure_demo_data()      # synthetic demand + supplier history for the demo database (idempotent; real data comes via Provisioning)
except Exception:   # pragma: no cover - never block the UI on demo data
    pass


@st.cache_resource
def get_graph():
    return sg.build_graph()


GRAPH = get_graph()          # one compiled graph + checkpointer per server process (shared by every page and session)
ss = st.session_state

_DEFAULTS = {"thread_id": None, "node_status": {}, "action": None, "notice": None, "tele_prior": [], "sim": None,
             "app_errors": [], "db_before": None, "db_after": None, "suite": None, "persist": {}, "ing_backup": None, "ing_last": None,
             "ing_conn": None, "hook_log": None}
# Widget values that must survive page switches (Streamlit drops widget state when a page does not render the widget).
_PERSIST_DEFAULTS = {"cfg_model": OLLAMA_MODEL, "cfg_url": OLLAMA_BASE_URL, "cfg_llm": True, "cfg_fast": True, "cfg_timeout": LLM_TIMEOUT_SECONDS,
                     "qa_mut": "none", "qa_latency": 0.0, "qa_crash": "none", "qa_scn": "baseline"}
PERSIST_PREFIXES = ("cfg_", "qa_", "email_", "sb_", "hook_", "ing_")    # never "sec_*": secrets are not kept


def init_session() -> None:
    """Called once per script run by the router: create state once, reset the per-run slot registry."""
    for k, v in _DEFAULTS.items():
        if k not in ss:
            ss[k] = copy.deepcopy(v)
    for k, v in _PERSIST_DEFAULTS.items():
        ss.persist.setdefault(k, v)
    ss["_slots"] = {}


def slots() -> dict:
    """DeltaGenerator registry for the current run (per session, so concurrent browsers never share slots)."""
    return ss.setdefault("_slots", {})


def pv(key: str, default=None):
    """Persistent value: live widget value if rendered this run, else the last value seen on any page."""
    if key in ss:
        return ss[key]
    return ss.persist.get(key, default)


def restore_widgets() -> None:
    """Re-seed widget keys from the persisted copy BEFORE the page creates its widgets."""
    for k, v in ss.persist.items():
        if k not in ss:
            ss[k] = v


def save_widgets() -> None:
    for k in list(ss.keys()):
        if isinstance(k, str) and k.startswith(PERSIST_PREFIXES):
            try:
                ss.persist[k] = ss[k]
            except Exception:
                pass


def conf() -> SimpleNamespace:
    return SimpleNamespace(model=pv("cfg_model", OLLAMA_MODEL), base_url=pv("cfg_url", OLLAMA_BASE_URL), use_llm=bool(pv("cfg_llm", True)),
                           fast_mode=bool(pv("cfg_fast", True)), timeout=int(pv("cfg_timeout", LLM_TIMEOUT_SECONDS)))


def cfg() -> dict:
    """LangGraph run config for THIS session (thread id + Ollama settings + armed QA faults)."""
    c = conf()
    return {"configurable": {"thread_id": ss.get("thread_id") or "none", "llm_enabled": c.use_llm, "llm_model": c.model,
                             "llm_base_url": c.base_url, "llm_timeout": c.timeout, "llm_emails": not c.fast_mode,
                             "fault_llm": pv("qa_mut", "none"), "fault_payload": pv("qa_custom", ""), "fault_latency": float(pv("qa_latency", 0.0)),
                             "fault_crash": pv("qa_crash", "none")}}


def act(name: str):
    ss.action = name


def disarm():
    for k, v in (("qa_mut", "none"), ("qa_latency", 0.0), ("qa_crash", "none")):
        ss[k] = v
        ss.persist[k] = v


def sim(kind: str):
    item = ss.get("sb_item")
    ss.sim = {"kind": kind, "item": item, "level": ss.get(f"sb_level_{item}")}
    ss.action = "sim"


def current_state():
    if not ss.thread_id:
        return None
    snap = GRAPH.get_state(cfg())
    return snap if snap and snap.values else None


def ensure_integrity() -> None:
    """Run on every page load: the run (and its approval gate) survives refreshes, page switches and server code reloads."""
    if not ss.thread_id:
        tid = db.get_kv("active_thread")
        if tid:  # browser refresh / new tab: re-attach to the run that is still checkpointed in memory
            ss.thread_id = tid
            if current_state() is None:
                ss.thread_id = None
                db.set_kv("active_thread", None)
            else:
                ss.node_status = {}
                ss.notice = ("info", "Restored your in-progress run from the saved checkpoint.")
    elif current_state() is None:
        # checkpoint vanished (server restarted / code reloaded): never leave orphaned PENDING orders or a dead thread id behind
        ss.thread_id, ss.node_status = None, {}
        db.supersede_pending()
        db.set_kv("active_thread", None)
        ss.notice = ("warning", "The saved run checkpoint was lost (server restart). Pending orders were superseded; run the pipeline again.")


# ------------------------------------------------------------------ HTML components
STEP_ICON = {  # professional status glyphs; colour comes from CSS
    "running": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.8' stroke-linecap='round'><circle cx='12' cy='12' r='8' opacity='.25'/><path class='spin' d='M12 4a8 8 0 0 1 8 8'/></svg>",
    "done": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><path d='M5 12.5l4.5 4.5L19 7.5'/></svg>",
    "paused": "<svg viewBox='0 0 24 24' fill='currentColor'><rect x='7' y='5' width='3.6' height='14' rx='1'/><rect x='13.4' y='5' width='3.6' height='14' rx='1'/></svg>",
    "error": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' stroke-linecap='round'><path d='M6 6l12 12M18 6L6 18'/></svg>",
    "skipped": "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' stroke-linecap='round'><path d='M7 12h10'/></svg>",
}
LOGO_SVG = ("<svg viewBox='0 0 32 32' width='32' height='32'><rect x='2' y='2' width='28' height='28' rx='8' fill='#0F172A'/>"
            "<path d='M9 21l7-11 7 11' fill='none' stroke='#fff' stroke-width='2.4' stroke-linejoin='round' stroke-linecap='round'/><circle cx='16' cy='20' r='2' fill='#60A5FA'/></svg>")
WARN_SVG = ("<svg class='ic' viewBox='0 0 24 24'><path d='M12 3l10 18H2z' fill='#FEE2E2' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/>"
            "<path d='M12 10v5M12 18v.5' stroke='currentColor' stroke-width='2' stroke-linecap='round'/></svg>")


def flow_html(status: dict, hops: int = 0) -> str:
    label = {"idle": "Idle", "running": "Processing", "done": "Complete", "paused": "Awaiting approval", "denied": "Denied", "error": "Exception", "skipped": "Skipped"}
    prog = pipeline_progress(status)
    vs = list(status.values())
    tone = "err" if "error" in vs else "hold" if "paused" in vs else "done" if (status.get(sg.HITL_NODE) == "done" or "skipped" in vs) else ("live" if "running" in vs else "")
    nodes, prev = [], None
    for n, (key, (name, tip)) in enumerate(STEPS.items(), 1):
        s = status.get(key, "idle")
        if prev is not None:
            cls = "on" if prev == "done" else ""
            if prev in ("running",) or (prev == "done" and s == "running"):
                cls += " flow-on"
            nodes.append(f"<div class='conn {cls}'></div>")
        icon = STEP_ICON.get("error" if s == "denied" else s, str(n))
        sub = label[s] + (f" · {hops} hops" if key == "supervisor" and hops and s != "idle" else "")
        nodes.append(f"<div class='node {s}' title='{esc(tip)}'><div class='dot'>{icon}</div><div class='nl'>{name}</div><div class='ns'>{sub}</div></div>")
        prev = s
    legend = "".join(f"<span><i style='background:{c}'></i>{t}</span>" for c, t in
                     [("#94A3B8", "Idle"), ("#2563EB", "Processing"), ("#059669", "Complete"), ("#D97706", "Human gate"), ("#DC2626", "Fault")])
    return (f"<div class='card flow'><div class='flow-h'><b>Pipeline progress</b><div class='legend'>{legend}</div><span class='pct'>{round(prog * 100)}%</span></div>"
            f"<div class='pbar {tone}'><i style='width:{prog * 100:.0f}%'></i></div><div class='track'>{''.join(nodes)}</div></div>")


def term_html(entries: list[dict], title: str, height: int, empty: str, who: str = "agent") -> str:
    rows = []
    for i, e in enumerate(entries):
        key = AGENT_KEY.get(e["agent"], "supervisor")
        src = f"<span class='src {e['source']}'>{e['source'].upper()}</span>" if e["source"] in ("llm", "fallback") else ""
        rows.append(f"<div class='ln {e['level']}' style='--d:{i % 6}'><span class='ts'>{e['ts']}</span>"
                    f"<span class='ag a-{key}'>{esc(e['agent'])}</span>"
                    f"<span class='lv {e['level']}'>{LEVELS[e['level']]}</span>"
                    f"<span class='msg'><b>{esc(e['step'])}</b> — {esc(e['message'])}{src}</span></div>")
        if e["detail"]:
            rows.append(f"<div class='dl'>{esc(e['detail'])}</div>")
    body = "".join(rows) or f"<div class='empty'>{empty}</div>"
    return (f"<div class='term'><div class='term-h'><i></i><i></i><i></i><span>{title}</span><em>{len(entries)} events</em></div>"
            f"<div class='term-b' style='height:{height}px'><div>{body}</div></div></div>")


def panes_html(entries: list[dict], height: int = 360) -> str:
    sup = [e for e in entries if e["agent"] == "Supervisor"]
    oth = [e for e in entries if e["agent"] != "Supervisor"]
    return (f"<div class='panes'>{term_html(sup, 'Supervisor routing decisions', height, 'Awaiting first routing decision.', 'supervisor')}"
            f"{term_html(oth, 'Agent execution trace', height, 'No agent activity yet. Run the pipeline to begin.', 'agents')}</div>")


def telemetry_html(entries: list[dict], trace: list[dict]) -> str:
    tin, tout = sum(t["in"] for t in entries), sum(t["out"] for t in entries)
    eval_ms = sum(t["eval_ms"] for t in entries)
    cloud = tin * GPT4O_INPUT_PER_M / 1e6 + tout * GPT4O_OUTPUT_PER_M / 1e6
    mspt = eval_ms / tout if tout else None
    fb = sum(e["source"] == "fallback" for e in trace)
    cards = [
        ("Input tokens", f"{tin:,}", "#2563EB", "prompt tokens sent to the local model"),
        ("Output tokens", f"{tout:,}", "#2563EB", "completion tokens generated"),
        ("Est. cloud savings", f"${cloud:,.4f}", "#059669",
         f"vs GPT-4o at ${GPT4O_INPUT_PER_M:.2f}/${GPT4O_OUTPUT_PER_M:.2f} per 1M in/out tokens; local cost $0 (about ${cloud * 1000:,.2f} per 1k runs)"),
        ("ms per token", f"{mspt:,.1f}" if mspt else "—", "var(--ink)", f"{1000 / mspt:,.1f} tokens/s (Ollama eval time)" if mspt else "no LLM calls yet"),
        ("LLM calls · fallbacks", f"{len(entries)} · {fb}", "#D97706" if fb else "#94A3B8", "model calls this session; fallbacks this run"),
    ]
    out = "".join(f"<div class='card kpi' style='--c:{c}' title='{esc(t)}'><div class='l'>{l}</div><div class='v'>{v}</div></div>" for l, v, c, t in cards)
    return f"<div class='kpis'>{out}</div>"


def session_telemetry(vals: dict) -> list[dict]:
    return ss.tele_prior + vals.get("telemetry", [])


# ------------------------------------------------------------------ diagnostics: errors, debug log, inspector
def is_control_flow(exc: Exception) -> bool:
    """Streamlit's rerun/stop signals are exceptions too; they must never be swallowed."""
    return type(exc).__module__.startswith("streamlit.runtime.scriptrunner")


def faults_armed() -> list[str]:
    out = []
    if pv("qa_mut", "none") != "none":
        out.append("LLM output: " + (CUSTOM_LABEL if pv("qa_mut") == CUSTOM else MUTATIONS[pv("qa_mut")][0]))
    if float(pv("qa_latency", 0)) > 0:
        out.append(f"LLM latency: +{float(pv('qa_latency')):g}s")
    if pv("qa_crash", "none") != "none":
        out.append(f"Crash in node: {pv('qa_crash')}")
    return out


def scene_risk(vals: dict) -> str:
    """ok | warn | crit — drives the colour of the 3D scene, QA panel and accent borders."""
    if (vals.get("errors") or ss.app_errors or vals.get("status") == "error" or pv("qa_crash", "none") != "none"
            or any(r["Result"] == "FAIL" for r in (ss.get("suite") or []))):
        return "crit"
    return "warn" if faults_armed() else "ok"


def hitl_risk(vals: dict) -> str:
    """low | med | high — how worrying is the order waiting for human approval?"""
    props, trace = vals.get("proposals", []), vals.get("trace", [])
    total = sum(p["total"] for p in props)
    if vals.get("errors") or total > 250_000 or any(p["total"] > 1_000_000 for p in props) or any(e["level"] == "error" for e in trace):
        return "high"
    abnormal = [e for e in trace if e["level"] == "warn" and e["step"] != "awaiting approval"]
    return "med" if abnormal or total > 50_000 or faults_armed() else "low"


def pipeline_progress(status: dict) -> float:
    if "skipped" in status.values():   # nothing was below its reorder point: the downstream stages are intentionally skipped
        return 1.0
    p = 0.0
    for key, lo, hi in PROGRESS:
        s = status.get(key)
        if s == "done":
            p = hi
        elif s == "running":
            p = lo + (hi - lo) * .5
        elif s == "paused":
            p = lo + (hi - lo) * .3
        elif s in ("error", "denied"):
            p = max(p, lo + (hi - lo) * .5)
    return p


def pipeline_ceil(status: dict) -> float:
    """Upper bound of the stage that is currently running (lets the progress bar creep while a model call is in flight)."""
    for key, _lo, hi in PROGRESS:
        if status.get(key) == "running":
            return hi
    return pipeline_progress(status)


def derive_status(snap) -> dict:
    """Source of truth for the tracker: rebuild node states from the LangGraph checkpoint (never contradicts the graph)."""
    v, nxt = snap.values, list(snap.next or ())
    stage, st = v.get("stage"), {}
    order = ["demand", "analytics", "supplier_intel", "procurement", "policy_check", "vendor_comm"]
    reached = {"forecasted": 1, "analyzed": 2, "scored": 3, "procured": 4, "validated": 5, "drafted": 6, "dispatched": 6, "failed": 2}.get(stage, 0)
    if reached:
        st["supervisor"] = "done"
        for k in order[:reached]:
            st[k] = "done"
    if stage == "drafted" and nxt and nxt[0] == sg.HITL_NODE:
        st[sg.HITL_NODE] = "paused"
    if stage == "dispatched":
        st[sg.HITL_NODE] = "denied" if v.get("status") == "rejected" else "done"
    if v.get("status") in ("healthy", "policy_blocked"):
        st.update({k: "skipped" for k in DOWNSTREAM if st.get(k) != "done"})
    if v.get("status") == "error" and v.get("errors"):
        st[v["errors"][-1]["node"]] = "error"
    return st


def tele_dict(entries: list[dict]):
    if not entries:
        return None
    tin, tout, ms = sum(t["in"] for t in entries), sum(t["out"] for t in entries), sum(t["eval_ms"] for t in entries)
    return {"i": f"{tin:,}", "o": f"{tout:,}", "m": f"{ms / tout:.1f} ms/token" if tout else "— ms/token"}


def push_scene(status=None, hops: int = 0, vals=None, vendors=None, tele=None, mode=None, progress=None, label: str = ""):
    """Send live pipeline state to the persistent 3D iframe (no reload: a tiny script sets window.__scmState)."""
    status = ss.node_status if status is None else status
    vals = vals or {}
    vs = list(status.values())
    healthy = vals.get("status") == "healthy" or "skipped" in vs
    mode = mode or ("error" if "error" in vs else "run" if "running" in vs else "paused" if "paused" in vs
                    else "done" if (status.get(sg.HITL_NODE) == "done" or healthy) else "idle")
    if healthy and not label:
        label = "All stock healthy · nothing to order"
    if vendors is None:
        vendors = [p["Vendor_ID"] for p in vals.get("proposals", [])]
    if tele is None:
        tele = tele_dict(ss.tele_prior + vals.get("telemetry", []))
    payload = {"nonce": time.time_ns(), "status": status, "mode": mode, "hops": hops, "vendors": vendors, "tele": tele,
               "progress": pipeline_progress(status) if progress is None else progress, "ceil": pipeline_ceil(status), "label": label, "risk": scene_risk(vals)}
    js = json.dumps(payload).replace("<", "\\u003c")
    sl = slots().get("push")
    if sl is None:      # no push slot on this run (shell not rendered)
        return
    try:
        sl.html("<script>(function(){var s=" + js + ";window.__scmState=s;document.querySelectorAll('iframe').forEach("
                       "function(f){try{f.contentWindow.postMessage({scm:s},'*')}catch(e){}});})();</script>",
                       unsafe_allow_javascript=True)
    except TypeError:  # very old Streamlit: scene simply stays in its idle state
        pass


def report_error(source: str, exc: Exception):
    ss.app_errors = (ss.app_errors + [{"node": source, "type": type(exc).__name__, "message": str(exc),
                                       "traceback": traceback.format_exc(), "ts": time.strftime("%H:%M:%S")}])[-5:]


@contextmanager
def guarded(name: str):
    """UI-section guard: a rendering bug becomes a styled error card instead of Streamlit's red crash screen."""
    try:
        yield
    except Exception as exc:
        if is_control_flow(exc):
            raise
        report_error(f"ui:{name}", exc)


def build_debug_log(err: dict, vals: dict) -> str:
    import streamlit
    trace = vals.get("trace", [])[-12:]
    lines = [
        "SUPPLY CHAIN ENGINE - DEBUG LOG", "=" * 60,
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}   python: {platform.python_version()}   os: {platform.platform()}",
        f"streamlit: {streamlit.__version__}   model: {conf().model}   url: {conf().base_url}   llm_enabled: {conf().use_llm}   timeout: {conf().timeout}s",
        f"faults armed: {', '.join(faults_armed()) or 'none'}", "",
        f"ERROR in {err['node']} @ {err['ts']}", f"{err['type']}: {err['message']}", "", "TRACEBACK", err["traceback"].rstrip(), "",
        "GRAPH STATE (summary)",
        f"stage={vals.get('stage')} status={vals.get('status')} next={vals.get('next')} rounds={vals.get('rounds')} llm_ok={vals.get('llm_ok')}",
        f"alerts={len(vals.get('alerts', []))} proposals={len(vals.get('proposals', []))} drafts={len(vals.get('drafts', []))}",
        f"db kpis: {db.kpis()}", "", "LAST AUDIT-TRAIL EVENTS",
        *[f"[{t['ts']}] {t['agent']}/{t['step']} ({t['level']}): {t['message']}" for t in trace],
    ]
    return "\n".join(lines)


def copy_button(text: str, uid: str):
    payload = json.dumps(text).replace("<", "\\u003c")  # log may hold untrusted strings: never let them close the <script>
    html = (
        f"""<button id="b{uid}" style="font:600 13px Inter,system-ui,sans-serif;padding:9px 16px;border-radius:9px;border:0;
        background:#DC2626;color:#fff;cursor:pointer;width:100%">Copy Debug Log</button><script>
        const t={payload};const b=document.getElementById('b{uid}');
        b.onclick=async()=>{{try{{await navigator.clipboard.writeText(t);}}catch(e){{const a=document.createElement('textarea');
        a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();}}
        b.textContent='Copied \u2713';setTimeout(()=>b.textContent='Copy Debug Log',1800);}};</script>""")
    if hasattr(st, "iframe"):  # components.v1.html is deprecated in recent Streamlit
        st.iframe(html, height=44)
    else:
        components.html(html, height=44)


def render_errors(vals: dict):
    errs = list(vals.get("errors", [])) + list(ss.app_errors)
    for i, e in enumerate(errs):
        with st.container(key=f"errbox_{i}"):
            st.error(f"**Exception in `{e['node']}`** — `{e['type']}: {e['message']}`".replace("$", "\\$") +
                     "\n\nThe pipeline halted safely: pending purchase orders were rolled back and the app stayed up. "
                     "Copy the debug log below when reporting this.", icon="🚨")
            log = build_debug_log(e, vals)
            c1, c2, c3 = st.columns([1, 1, 1])
            with c1:
                copy_button(log, f"{i}")
            c2.download_button("Download log (.txt)", log, file_name=f"debug_{e['node']}_{e['ts'].replace(':', '')}.txt",
                               key=f"dl_{i}", width="stretch")
            if i >= len(vals.get("errors", [])):
                c3.button("Dismiss", key=f"dismiss_{i}", width="stretch",
                          on_click=lambda j=i - len(vals.get("errors", [])): ss.app_errors.pop(j))
            with st.expander("Stack trace", expanded=True):
                st.code(e["traceback"], language="python")


def render_inspector():
    """Persisted view: step through every LangGraph checkpoint (the 'rewind token')."""
    hist = list(GRAPH.get_state_history(cfg())) if ss.thread_id else []
    hist.reverse()
    if not hist:
        st.caption("No checkpoints yet — run the pipeline.")
        return
    idx = len(hist) - 1
    if len(hist) > 1:
        idx = st.slider("Rewind token · checkpoint", 0, len(hist) - 1, len(hist) - 1, key=f"rw_{ss.thread_id}_{len(hist)}",
                        help="Drag to inspect the exact graph state at any earlier checkpoint.")
    cur, prev = hist[idx], (hist[idx - 1].values if idx else {})
    changed = [k for k in cur.values if cur.values.get(k) != prev.get(k)]
    dropped = [k for k in prev if k not in cur.values]
    st.caption(f"checkpoint {cur.config['configurable']['checkpoint_id'][:13]}… · step {cur.metadata.get('step')} · "
               f"next → {', '.join(cur.next) or 'END'} · changed: {', '.join(changed) or 'nothing'}"
               + (f" · DROPPED: {', '.join(dropped)}" if dropped else ""))
    st.json(cur.values, expanded=1)


# ------------------------------------------------------------------ streaming executor
def stream(payload):
    """Run/resume the graph, painting tracker + terminals + live state inspector. Ends at completion or the HITL gate."""
    box = slots()["flow"].container()
    tracker, panes = box.empty(), box.empty()
    ex = box.expander("Graph state inspector · live (full state after every tick)", expanded=True)
    tick_cap, insp = ex.empty(), ex.empty()
    prior = current_state()
    entries = list(prior.values.get("trace", [])) if prior else []
    tele_run = list(prior.values.get("telemetry", [])) if prior else []
    status, hops, ticks, last = ss.node_status, sum(e["agent"] == "Supervisor" for e in entries), 0, {}
    vend = [p["Vendor_ID"] for p in prior.values.get("proposals", [])] if prior else []
    md(tracker, flow_html(status, hops))
    md(panes, panes_html(entries[-40:], 300))
    push_scene(status=status, hops=hops, vendors=vend, tele=tele_dict(ss.tele_prior + tele_run))
    try:
        for mode, chunk in GRAPH.stream(payload, cfg(), stream_mode=["updates", "values"]):
            if mode == "values":  # complete memory object after this computational tick
                ticks += 1
                ch = [k for k in chunk if chunk.get(k) != last.get(k)]
                tick_cap.caption(f"tick {ticks} · changed: {', '.join(ch) or 'nothing'}"
                                 + (f" · DROPPED: {', '.join(k for k in last if k not in chunk)}" if any(k not in chunk for k in last) else ""))
                insp.json(chunk, expanded=1)
                last = chunk
                continue
            for node, upd in chunk.items():
                if node.startswith("__") or not isinstance(upd, dict):
                    continue
                entries += upd.get("trace", [])
                tele_run += upd.get("telemetry", [])
                failed = bool(upd.get("errors"))
                if node == "supervisor" and not failed:
                    hops += 1
                    nxt = upd.get("next", "end")
                    status["supervisor"] = "done"
                    if nxt == "end":
                        for k in status:
                            if status[k] == "running":
                                status[k] = "done"
                        if upd.get("status") in ("healthy", "policy_blocked"):
                            for k in DOWNSTREAM:
                                if status.get(k) != "done":
                                    status[k] = "skipped"
                    else:
                        status[nxt] = "paused" if nxt == sg.HITL_NODE else "running"
                else:
                    status[node] = "error" if failed else "denied" if upd.get("status") == "rejected" else "done"
                    if node != "supervisor":
                        status["supervisor"] = "running"
                    if upd.get("stage") == "analyzed":  # re-source loop: later stages restart
                        for k in DOWNSTREAM:
                            status[k] = "idle"
                md(tracker, flow_html(status, hops))
                md(panes, panes_html(entries[-40:], 300))
                md(slots()["tele"], telemetry_html(ss.tele_prior + tele_run, entries))
                if "proposals" in upd:
                    vend = [p["Vendor_ID"] for p in upd["proposals"]]
                push_scene(status=status, hops=hops, vendors=vend, tele=tele_dict(ss.tele_prior + tele_run))
                if not conf().use_llm and not faults_armed():
                    time.sleep(0.8)  # keep the animation legible in instant rule-based mode
    except Exception as exc:  # never crash the dashboard
        if is_control_flow(exc):
            raise
        report_error("graph.stream", exc)
    ss.node_status = status
    try:
        ss.db_after = qa.snapshot()
    except Exception as exc:
        report_error("db.snapshot", exc)


def drop_thread(tid) -> None:
    """Free a finished/abandoned run's checkpoints (MemorySaver otherwise grows for the life of the server)."""
    if tid:
        try:
            GRAPH.checkpointer.delete_thread(tid)
        except Exception:
            pass


def start_run():
    ss.app_errors = []
    ss.db_before = qa.snapshot()  # rewind baseline: DB state before the pipeline touches anything
    prior = current_state()
    if prior:  # archive previous run's LLM telemetry into session totals (bounded: no unbounded session growth)
        ss.tele_prior = (ss.tele_prior + prior.values.get("telemetry", []))[-400:]
    db.supersede_pending()
    drop_thread(ss.thread_id)
    ss.thread_id, ss.node_status = uuid.uuid4().hex, {}
    db.set_kv("active_thread", ss.thread_id)   # lets a refreshed browser tab re-attach to an in-progress / paused run
    stream(sg.initial_state())
    st.rerun()


def email_key(d: dict, rounds: int) -> str:
    return f"email_{ss.thread_id}_{rounds}_{d['Vendor_ID']}"


def H(*parts: str) -> str:
    """Join HTML fragments on one line (blank lines / indentation would make Markdown treat them as code)."""
    return "".join(parts)


ARROW = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'><path d='M5 12h14M13 6l6 6-6 6'/></svg>"
