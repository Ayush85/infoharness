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


TYPE_WHY = {
    "contradiction": "Two pages give different answers to the same question. An assistant (or a customer) can quote either one.",
    "duplicate_page": "Two copies of one page. When only one gets updated, the other keeps giving the old answer.",
    "duplicate_section": "The same text lives in several places, and edits rarely reach every copy.",
    "stale": "Pages that say they are old, or describe a version the rest of the docs have moved past.",
    "admin_link": "Links that point at an editing screen, a draft preview or a test server. Visitors can't open them, and they reveal how the site is built.",
    "broken_link": "Links that lead nowhere send readers, and crawlers, into dead ends.",
    "thin_page": "Near-empty pages still show up in search and in AI answers, and say nothing useful.",
    "orphan_page": "Nothing links to these pages, so nobody maintains them, yet search can still find them.",
}


def verdict(scan, issues) -> str:
    total = len(issues)
    important = sum(1 for i in issues if i.severity in ("high", "medium"))
    if total == 0:
        return "No problems found. These docs are in good shape for AI assistants."
    if important == 0:
        return f"No serious problems. {total} small thing{'s' if total != 1 else ''} left to tidy."
    wrong_answer = sum(1 for i in issues if i.severity in ("high", "medium")
                       and i.type in ("contradiction", "duplicate_page", "duplicate_section", "stale"))
    if wrong_answer == 0:  # only links, drafts or thin pages: real work, but it can't make an assistant quote a wrong answer
        return (f"{important} thing{'s' if important != 1 else ''} to fix. "
                "Nothing in your content contradicts itself or looks out of date.")
    return (f"{wrong_answer} problem{'s' if wrong_answer != 1 else ''} could make an AI assistant give a wrong answer. "
            "Fix those first.")


def verdict_sub(issues) -> str:
    total = len(issues)
    important = sum(1 for i in issues if i.severity in ("high", "medium"))
    if not total or important == total:
        return ""
    return f"{total} findings in all: {important} serious, {total - important} minor. Most serious first."


def issue_md(i, root: str = "") -> str:
    out = [f"- [ ] **{i.title}** ({i.severity})"]
    if i.type in ("broken_link", "admin_link"):
        out.append(f"  - URL: {i.page_b}")
        ex = i.extra or {}
        if ex.get("site_wide"):
            out.append(f"  - In the site menu/header/footer, on {ex.get('sources_total')} pages: fix once in the template")
        for c in (ex.get("contexts") or [])[:5]:
            bits = [c["page"]] + ([f'link text "{c["text"]}"'] if c.get("text") else []) + \
                   ([f'under "{c["heading"]}"'] if c.get("heading") else [])
            out.append("  - Found on: " + " · ".join(bits))
        return "\n".join(out)
    where = " · ".join(p.replace(root, "/") if root else p for p in (i.page_a, i.page_b) if p)
    if where:
        out.append(f"  - Pages: {where}")
    for q in (i.quote_a, i.quote_b):
        if q:
            out.append(f"  - > {' '.join(q.split())[:240]}")
    return "\n".join(out)


def report_md(scan, grouped) -> str:
    lines = [f"# Docs health of {scan.url}", "", f"Grade **{scan.grade}** ({round(scan.score or 0)}/100) · {scan.pages_crawled} pages read",
             f"{config.settings.public_base_url}/r/{scan.id}", ""]
    for t, items in grouped:
        lines += [f"## {TYPE_LABELS[t]} ({len(items)})", ""] + [issue_md(i, scan.root) for i in items] + [""]
    return "\n".join(lines)


def _ctx(request: Request) -> dict:
    return {"me": current_user(request)}


templates = Jinja2Templates(directory=BASE / "templates", context_processors=[_ctx])
templates.env.globals.update(type_labels=TYPE_LABELS, version=__version__, settings=lambda: config.settings)
templates.env.filters["mark_values"] = mark_values
templates.env.filters["issue_md"] = lambda i, root="": issue_md(i, root)
templates.env.globals.update(type_why=TYPE_WHY)


def spark(values, w: int = 96, h: int = 22):
    """A tiny inline SVG line of recent scores (0-100)."""
    from markupsafe import Markup
    vals = list(values)
    if len(vals) < 2:
        return ""
    step = (w - 4) / (len(vals) - 1)
    pts = " ".join(f"{2 + i * step:.1f},{2 + (h - 4) * (1 - max(0, min(100, v)) / 100):.1f}" for i, v in enumerate(vals))
    last = pts.split()[-1].split(",")
    return Markup(f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true"><polyline points="{pts}" fill="none" '
                  f'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="{last[0]}" cy="{last[1]}" r="2.2" fill="#d23b3b"/></svg>')


templates.env.filters["spark"] = spark
templates.env.filters["fix_steps"] = lambda i, root="": __import__("app.suggest", fromlist=["fix_steps"]).fix_steps(i, root)
