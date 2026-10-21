"""SCM Agentic Enterprise: multi-page B2B platform (router + shared shell).

    streamlit run app.py

Layout
    app.py              this file: page config, session bootstrap, navigation
    ui/                 shared design system + helpers (config, core engine/state, shell, assets loader)
    views/              the pages: home (product chronicle), ops (operations centre), provision (data hub), executive (KPIs, workflow view, audit)
    assets/             CSS, JS and the two WebGL scenes
    scm/                backend: SQLite (db), LangGraph agents (graph), Ollama (llm), QA matrix (qa), ingestion + ERP webhooks (ingest)

All pages share one LangGraph checkpointer, one SQLite store and st.session_state, so a run (and its human-approval gate)
is never lost when you click between pages.
"""
import streamlit as st

st.set_page_config(page_title="SCM Agentic Enterprise", page_icon="◆", layout="wide", initial_sidebar_state="collapsed")

from ui import core, shell

core.init_session()          # create session state once, reset the per-run slot registry
core.ensure_integrity()      # re-attach to a live run after refresh; heal a lost checkpoint

PAGES = {
    "home": st.Page("views/home.py", title="Product", icon=":material/public:", default=True),
    "ops": st.Page("views/ops.py", title="Operations Centre", icon=":material/monitoring:", url_path="ops"),
    "provision": st.Page("views/provision.py", title="Provisioning", icon=":material/cable:", url_path="provision"),
    "executive": st.Page("views/executive.py", title="Executive", icon=":material/dashboard:", url_path="executive"),
    "impact": st.Page("views/impact.py", title="Business Impact", icon=":material/trending_up:", url_path="impact"),
}
current = st.navigation(list(PAGES.values()), position="hidden")   # our own glass nav bar replaces the default menu
shell.render(PAGES)
current.run()
