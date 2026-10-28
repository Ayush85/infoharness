"""Fix suggestions for findings: concrete steps from the finding itself, plus an optional AI-written rewrite."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from app import config, llm

log = logging.getLogger(__name__)


def _path(url: str, root: str = "") -> str:
    u = urlsplit(url or "")
    return (u.path or "/") + (f"?{u.query}" if u.query else "")


def fix_steps(i, root: str = "") -> list[str]:
    """Concrete next steps for one finding (an Issue row or Finding). Plain strings, safe to show."""
    ex = i.extra or {}
    t = i.type
    if t == "contradiction":
        vals = ex.get("values") or []
        pair = f" ({vals[0]:g} vs {vals[1]:g})" if len(vals) == 2 else ""
        return [f"Decide which value is correct{pair}. Ask the owner of the topic if unsure.",
                f"Fix the wrong page: {_path(i.page_a)} or {_path(i.page_b)}.",
                "Better: state the value in one place and link to it from the other page, so it cannot drift apart again.",
                "If both are right (different plans, regions or versions), say which applies on each page so readers and AI assistants can tell."]
    if t == "duplicate_page":
        pages = ex.get("pages") or [i.page_a, i.page_b]
        keep = _path(pages[0])
        return [f"Keep one page: {keep} is the shortest address, so it is usually the original.",
                "Copy anything unique from the other copies into it.",
                "Redirect the other addresses to it with a 301 redirect, or delete them.",
                "Update links that pointed at the removed copies."]
    if t == "duplicate_section":
        return ["Keep the text in one place and link to it from the other pages.",
                "If it must appear in several pages, use a shared include or snippet in your docs tool so one edit updates all copies."]
    if t == "stale":
        method = ex.get("method")
        if method == "version_drift":
            return [f"Open the page and check it against the current version (v{ex.get('latest')}).",
                    "Update the steps, or add a clear note at the top: which version it covers and a link to the current page."]
        return ["Open the page and check every statement against today's product.",
                "If it is still correct, update the date or remove the stale line. If not, rewrite it.",
                "If nobody needs it any more, redirect it to the current page or delete it."]
    if t == "broken_link":
        steps = []
        if ex.get("site_wide"):
            steps.append(f"This link is in your menu, header or footer and appears on {ex.get('sources_total')} pages. Fix it once in the template.")
        if ex.get("internal"):
            steps += ["If the page moved, add a 301 redirect from the old address to the new one. That repairs every link at once.",
                      "If the page is gone, remove the link or point it at the closest current page."]
        else:
            steps += ["Open the link. If the site moved, update the address; if it is gone, remove the link or link to an archived copy.",
                      "If it works for you, the other site may block automated requests, so ignore this one."]
        return steps
    if t == "admin_link":
        return ["Replace the link with the public address of the page. Visitors cannot open admin, edit, preview or staging addresses.",
                "If it was pasted from the editor, check the other links on the page for the same mistake."]
    if t == "thin_page":
        return ["Write the missing content, or delete the page and redirect it to the closest useful page.",
                "Placeholders such as 'coming soon' should not be published: they appear in search and in AI answers."]
    if t == "orphan_page":
        return ["Link it from the page where readers would look for it (a category page or a related article).",
                "If nobody needs it, delete it or redirect it."]
    return []


SUGGEST_SYSTEM = ("You help technical writers fix documentation problems. Give short, concrete advice in at most 4 sentences. "
                  "Do not invent facts about the product: if a fact must be confirmed, say who should confirm it.")


async def ai_suggestion(i) -> str | None:
    """A short AI-written fix for one finding, or None when no LLM is configured or the call fails."""
    s = config.settings
    if not s.llm_enabled:
        return None
    parts = [f"Problem: {i.title}", i.detail or ""]
    if i.quote_a:
        parts.append(f"Page {i.page_a} says: {i.quote_a[:500]}")
    if i.quote_b:
        parts.append(f"Page {i.page_b} says: {i.quote_b[:500]}")
    if not i.quote_a and i.page_a:
        parts.append(f"Page: {i.page_a}")
    prompt = "\n".join(p for p in parts if p) + "\n\nWhat should the writer change? If a rewrite helps, give the suggested wording."
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            text = await llm._call(client, prompt, system=SUGGEST_SYSTEM, max_tokens=300)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("suggestion call failed: %s", e)
        return None
    return text.strip()[:1200] or None
