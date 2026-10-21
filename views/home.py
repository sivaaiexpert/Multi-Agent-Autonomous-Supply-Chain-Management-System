"""PAGE 1 · Executive Product Chronicle: landing page (hero + 3D globe, solutions, FAQ, pricing matrix, access form)."""
import streamlit as st
import streamlit.components.v1 as components

from ui import assets
from ui.core import *

# ---- SECTION 1: executive hero
md(st, "<span id='hero' data-id='hero' class='sec-anchor'></span>")
with st.container(key="hero"):
    hl, hr = st.columns([1.05, 1], gap="large", vertical_alignment="center")
    with hl:
        md(st, H("<div class='hero-copy'>",
                 "<div class='eyebrow reveal' style='--i:0'>Autonomous logistics · governed by design</div>",
                 "<h1 class='hero-h reveal' style='--i:1'><span>Enterprise Orchestration.</span><span class='mute'>Absolute Determinism.</span>",
                 "<span class='grad'>Autonomously Scale Your Logistics Engine.</span></h1>",
                 "<p class='hero-p reveal' style='--i:2'>A governed multi-agent system that monitors inventory, selects suppliers by rule and drafts purchase orders, ",
                 "then holds every order at a human approval checkpoint. The model writes the narrative; deterministic code decides the numbers. ",
                 "Runs on your own hardware.</p>",
                 "<div class='cta-row reveal' style='--i:3'><a class='cta primary' data-go='access'>Request Enterprise Access ", ARROW, "</a>",
                 "<a class='cta ghost' data-page='ops'>Watch Live Demo</a></div>",
                 "<div class='trust reveal' style='--i:4'><div><b>On-premises</b><span>Local inference via Ollama</span></div>",
                 "<div><b>Human-approved</b><span>Every order is gated</span></div><div><b>125</b><span>Automated QA scenarios</span></div></div></div>"))
    with hr, st.container(key="herofx"):
        if hasattr(st, "iframe"):
            st.iframe(assets.hero_html(), height=540)
        else:
            components.html(assets.hero_html(), height=540)

# ------------------------------------------------------------------ SECTION 3: system blueprints & services
def svc_card(i: int, img: str, tag: str, title: str, text: str, bullets: list[str], badge: str = "") -> str:
    bd = f"<span class='badge'>{badge}</span>" if badge else ""
    return (f"<div class='reveal' style='--i:{i}'><div class='svc'><div class='img'><i style=\"background-image:url('{esc(img)}')\"></i></div>"
            f"<div class='body'><span class='tag'>{tag}</span><h3>{title}{bd}</h3><p>{text}</p>"
            f"<ul>{''.join(f'<li>{b}</li>' for b in bullets)}</ul></div></div></div>")


md(st, H("<span id='services' data-id='services' class='sec-anchor'></span><div class='sh reveal'><span class='eyebrow'>System blueprints &amp; services</span>",
         "<h2>Engineered for operations teams that cannot afford surprises.</h2>",
         "<p>Three capabilities that make autonomous procurement safe to run in production.</p></div>",
         "<div class='grid3'>",
         svc_card(0, IMG_ORCHESTRATION, "Orchestration", "Custom Agent Orchestration",
                  "A Supervisor routes shared state between specialised agents, with a checkpoint written at every step.",
                  ["Analytics, Procurement and Vendor Comms agents", "Human approval breakpoint before dispatch",
                   "Re-source loop: deny a vendor, get the next-best quote", "Fault-injection QA matrix, 125 automated cases"]),
         svc_card(1, IMG_INTEGRATION, "Integration", "Local Data &amp; ERP Integration",
                  "The whole data path runs on your infrastructure: SQLite for state, Ollama for reasoning, no cloud calls from the pipeline.",
                  ["Inventory, vendors, quotes and purchase orders in SQLite", "Emails and ERP payloads written to a local outbox",
                   "Vector / embedding retrieval as a planned extension"], "VECTOR = ROADMAP"),
         svc_card(2, IMG_METRICS, "LLMOps", "Cloud Metrics &amp; LLMOps Optimization",
                  "Per-call token and latency telemetry with a running comparison against GPT-4o pricing, so cost and speed are measured, not guessed.",
                  ["Input / output tokens and ms per token", "Estimated savings versus a cloud API",
                   "Timeouts, circuit breaker and malformed-output fallback", "Fast mode for CPU-only hosts"]),
         "</div><div class='flowmap'>",
         "<div class='fm reveal' style='--i:0'><i>01</i><b>Inventory data</b><span>SQLite / ERP</span></div>",
         "<div class='fm reveal' style='--i:1'><i>02</i><b>Analytics</b><span>Low-stock scan</span></div>",
         "<div class='fm reveal' style='--i:2'><i>03</i><b>Procurement</b><span>Quote comparison</span></div>",
         "<div class='fm reveal' style='--i:3'><i>04</i><b>Vendor Comms</b><span>PO drafts</span></div>",
         "<div class='fm reveal' style='--i:4'><i>05</i><b>Approval gate</b><span>Approve or deny</span></div>",
         "<div class='fm reveal' style='--i:5'><i>06</i><b>Dispatch</b><span>Email and ERP payload</span></div></div>"))

# ------------------------------------------------------------------ SECTION 4: enterprise FAQ matrix
FAQS = [
    ("How is the system secured, and who can approve an order?",
     "Every purchase order is drafted as pending and the workflow pauses at a LangGraph human-in-the-loop breakpoint. Nothing is sent or booked until a person approves, "
     "and every routing decision, model call and approval is written to an audit trail.",
     "This build has no user authentication of its own. Deploy it behind your corporate SSO or reverse proxy; role-based approval is part of the integration engagement."),
    ("Where does our data live? What about data residency?",
     "In your environment. State is a local SQLite file, approved emails and ERP payloads are written to a local folder, and the language model runs locally through Ollama, "
     "so no business data is sent to a cloud LLM.",
     "The web UI loads fonts and the marketing images from public CDNs in the browser. Self-host them for an air-gapped deployment."),
    ("Can it really run locally with Ollama Llama 3.2?",
     "Yes. The reference setup uses Llama 3.2 through Ollama on standard hardware, with no GPU required. Fast mode limits the model to two short analysis summaries.",
     "On our CPU-only test machine a run to the approval gate takes about 17 seconds in Fast mode and about 90 seconds when the model also writes every vendor email. llama3.2:1b is faster still."),
    ("What makes the workflow deterministic and safe?",
     "Order quantities, supplier selection and totals are computed by rule-based code, never by the model. The model only writes commentary, every reply is validated, "
     "and a timeout or malformed answer falls back to a template.",
     "An automated matrix of 125 fault scenarios (corrupt model output, bad database rows, injected latency, node crashes) runs against every build."),
]
md(st, H("<span id='faq' data-id='faq' class='sec-anchor'></span><div class='sh reveal'><span class='eyebrow'>Enterprise FAQ matrix</span>",
         "<h2>Answers for security, legal and operations review.</h2><p>The questions procurement committees ask first.</p></div>",
         "".join(f"<div class='reveal' style='--i:{i}'><details class='faq'{' open' if i == 0 else ''}><summary>{esc(q)}</summary>"
                 f"<div class='ans'>{esc(a)}<em>{esc(n)}</em></div></details></div>" for i, (q, a, n) in enumerate(FAQS))))

# ------------------------------------------------------------------ SECTION 5: go-to-market provisioning
md(st, H("<span id='access' data-id='access' class='sec-anchor'></span><div class='sh reveal'><span class='eyebrow'>Go-to-market provisioning</span>",
         "<h2>Two ways to bring it into your operation.</h2><p>Engagements are scoped to your systems and your risk policy.</p></div>",
         "<div class='reveal'><table class='ptable'><thead><tr><th>Engagement track</th><th>Scope and deliverables</th><th>Model</th><th>Investment</th></tr></thead><tbody>",
         "<tr><td><b>Custom Enterprise Integration</b><span class='pill'>Implementation</span></td><td><ul>",
         "<li>Adapt the data layer to your ERP or inventory database</li><li>Deliver approved purchase orders to your ERP endpoint</li>",
         "<li>Deploy behind your SSO, proxy and logging stack</li><li>Add your own QA and fault scenarios</li></ul></td>",
         "<td>Fixed-scope project</td><td class='price'>", esc(PRICE_INTEGRATION[0]), "<small>", esc(PRICE_INTEGRATION[1]), "</small></td></tr>",
         "<tr><td><b>Dedicated Architecture Consultation</b><span class='pill'>Advisory</span></td><td><ul>",
         "<li>Agent topology and approval-policy design</li><li>Model sizing and hosting plan for Ollama</li>",
         "<li>LLMOps cost model versus cloud APIs</li><li>Governance, audit and rollout roadmap</li></ul></td>",
         "<td>Monthly retainer</td><td class='price'>", esc(PRICE_CONSULT[0]), "<small>", esc(PRICE_CONSULT[1]), "</small></td></tr></tbody></table></div>"))


@st.fragment
def access_form():
    with st.form("access_form", clear_on_submit=True):
        st.markdown("**Request enterprise access**")
        f1, f2 = st.columns(2)
        name = f1.text_input("Full name")
        email = f2.text_input("Work email")
        company = f1.text_input("Company")
        track = f2.selectbox("Engagement track", ["Custom Enterprise Integration", "Dedicated Architecture Consultation", "Not sure yet"])
        note = st.text_area("What would you like to automate?", height=90)
        sent = st.form_submit_button("Submit request")
    if sent:
        if not name.strip() or "@" not in email or "." not in email.split("@")[-1]:
            st.error("Please enter your name and a valid work email address.")
        else:
            db.add_access_request(name, email, company, f"[{track}] {note}")
            st.success("Thank you. Your request has been recorded in this deployment's local database.")


with st.container(key="access_wrap"):
    access_form()
md(st, "<div class='foot'><span>© SCM Agentic Enterprise</span><span>LangGraph · Ollama · SQLite · Streamlit</span><span>Human-approved by design</span></div>")

md(st, H("<div class='cta-row reveal' style='margin-top:1.6rem'>",
         "<a class='cta primary' data-page='provision'>Connect your own data ", ARROW, "</a>",
         "<a class='cta ghost' data-page='ops'>Open the live engine</a></div>"))

# the hero globe reacts to the live pipeline state (shared across pages)
_snap = current_state()
push_scene(vals=_snap.values if _snap else {})
