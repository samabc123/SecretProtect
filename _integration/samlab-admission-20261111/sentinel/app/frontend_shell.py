"""Serve the built Vue application while retaining the legacy template fallback."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse

from .config import ROOT


DIST_INDEX = ROOT / "static" / "vue" / "index.html"


def render_vue_app(*, csrf_token: str = "", mode: str = "app") -> HTMLResponse | None:
    if not DIST_INDEX.is_file():
        return None
    html = DIST_INDEX.read_text(encoding="utf-8")
    html = html.replace("__CSRF_TOKEN__", csrf_token).replace("__APP_MODE__", mode)
    return HTMLResponse(html)
