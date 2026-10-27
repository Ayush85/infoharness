"""Shared Jinja environment (used by the page routes and the account routes)."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app import __version__, config
from app.auth import current_user
from app.scoring import TYPE_LABELS

BASE = Path(__file__).parent


def mark_values(text: str, extra: dict | None) -> Markup:
    """Escape a quote and highlight the conflicting numbers (e.g. '14' and '30') inside it."""
    safe = str(escape(text or ""))
    values = (extra or {}).get("values") or []
    for v in values:
        num = str(int(v)) if float(v) == int(v) else str(v)
        safe = re.sub(rf"(?<![\d.,])({re.escape(num)})(?![\d])", r"<mark>\1</mark>", safe, count=1)
    return Markup(safe)


def _ctx(request: Request) -> dict:
    return {"me": current_user(request)}


templates = Jinja2Templates(directory=BASE / "templates", context_processors=[_ctx])
templates.env.globals.update(type_labels=TYPE_LABELS, version=__version__, settings=lambda: config.settings)
templates.env.filters["mark_values"] = mark_values
