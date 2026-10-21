"""Shared page shell: design system CSS, theme/nav scripts, Three.js cursor-reactive background, glass navigation bar.

Rendered by app.py on EVERY run, before the selected page, so the look, the WebGL background and the navigation are identical across pages.
"""
from __future__ import annotations

import streamlit as st

from ui import assets, core
from ui.core import LOGO_SVG, md

NAV_ITEMS = (("home", "Product", ":material/public:"),
             ("ops", "Operations Centre", ":material/monitoring:"),
             ("provision", "Provisioning", ":material/cable:"),
             ("executive", "Executive", ":material/dashboard:"),
             ("impact", "Business Impact", ":material/trending_up:"))

_SUN = ("<svg class='ti-sun' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round'><circle cx='12' cy='12' r='4'/>"
        "<path d='M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'/></svg>")
_MOON = ("<svg class='ti-moon' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
         "<path d='M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z'/></svg>")


def render(pages: dict) -> None:
    st.markdown(assets.css(), unsafe_allow_html=True)

    with st.container(key="jsbox"):          # hidden: scripts only
        try:
            st.html(assets.js_loader("js/nav.js"), unsafe_allow_javascript=True)
            st.html(assets.js_loader("js/bg.js"), unsafe_allow_javascript=True)
        except TypeError:                    # very old Streamlit without script support: pages still work, minus the effects
            pass
    core.slots()["push"] = st.container(key="pushbox").empty()   # live pipeline state -> 3D scenes (any page)

    with st.container(key="topnav"):
        cols = st.columns([2.3, 1, 1.5, 1.2, 1.15, 1.2, .55, 1.5], vertical_alignment="center")
        with cols[0]:
            md(st, "<div class='nav-l'>" + LOGO_SVG + "<span class='nav-t'>SCM Agentic <i>Enterprise</i></span></div>")
        for col, (key, label, icon) in zip(cols[1:6], NAV_ITEMS):
            col.page_link(pages[key], label=label, icon=icon)
        with cols[6]:
            md(st, "<div class='nav-r'><button class='theme-toggle' data-theme-toggle='1' title='Switch light / dark theme' "
                   "aria-label='Toggle light or dark theme'>" + _MOON + _SUN + "</button></div>")
        with cols[7]:
            md(st, "<div class='nav-r'><a class='nav-cta' data-page='home' data-go='access'>Request Access</a></div>")
