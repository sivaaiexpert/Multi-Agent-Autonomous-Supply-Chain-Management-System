"""Static assets (CSS / JS / WebGL scenes) live in ./assets and are read once per process."""
from __future__ import annotations

import base64
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "assets"


@cache
def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def css() -> str:
    return read("css/theme.css") + read("css/nav.css")


def scene_html() -> str:
    return read("html/scene.html")


def hero_html() -> str:
    return read("html/hero.html")


def js_loader(rel: str) -> str:
    """Inline loader for a JS asset. The code travels base64-encoded because Streamlit's HTML layer mangles '<', '>' and '&&'."""
    b64 = base64.b64encode(read(rel).encode("utf-8")).decode("ascii")
    return ("<script>(function(){var s=document.createElement('script');"
            "s.textContent=decodeURIComponent(escape(atob('" + b64 + "')));document.head.appendChild(s);})();</script>")
