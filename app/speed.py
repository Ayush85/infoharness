"""Page speed and Core Web Vitals through Google's PageSpeed Insights API (free; an API key raises the quota).

Measured for the address that was scanned, on mobile and on desktop. Not part of the grade: it is shown next to it.
Never raises: a failed or rate-limited measurement just means the report has no speed card.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

from app import config

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CATEGORIES = (("performance", "Performance"), ("accessibility", "Accessibility"),
              ("best-practices", "Best practices"), ("seo", "SEO"))
# (audit id, label, good threshold, poor threshold, unit); values are raw numbers from Lighthouse
VITALS = (("largest-contentful-paint", "Largest Contentful Paint", 2500, 4000, "ms"),
          ("cumulative-layout-shift", "Layout shift (CLS)", 0.1, 0.25, ""),
          ("total-blocking-time", "Total Blocking Time", 200, 600, "ms"),
          ("first-contentful-paint", "First Contentful Paint", 1800, 3000, "ms"),
          ("speed-index", "Speed Index", 3400, 5800, "ms"))


def rating(score: float | None) -> str:
    if score is None:
        return "unknown"
    return "good" if score >= 90 else "ok" if score >= 50 else "poor"


def _fmt(value: float, unit: str) -> str:
    if unit == "ms":
        return f"{value / 1000:.1f} s" if value >= 1000 else f"{round(value)} ms"
    return f"{value:.2f}"


def parse(data: dict) -> dict | None:
    lh = data.get("lighthouseResult") or {}
    cats = lh.get("categories") or {}
    audits = lh.get("audits") or {}
    if not cats:
        return None
    scores = []
    for key, label in CATEGORIES:
        raw = (cats.get(key) or {}).get("score")
        sc = None if raw is None else round(raw * 100)
        scores.append({"key": key, "label": label, "score": sc, "rating": rating(sc)})
    vitals = []
    for aid, label, good, poor, unit in VITALS:
        a = audits.get(aid) or {}
        v = a.get("numericValue")
        if v is None:
            continue
        vitals.append({"label": label, "value": _fmt(v, unit),
                       "rating": "good" if v <= good else "ok" if v <= poor else "poor"})
    opps = []
    for a in audits.values():
        d = a.get("details") or {}
        saving = d.get("overallSavingsMs") or 0
        if d.get("type") == "opportunity" and saving >= 100 and (a.get("score") or 0) < 0.9:
            opps.append({"title": a.get("title", ""), "saving_ms": round(saving),
                         "saving": _fmt(saving, "ms")})
    opps.sort(key=lambda o: -o["saving_ms"])
    field = None
    le = (data.get("loadingExperience") or {})
    if le.get("overall_category"):
        field = {"category": le["overall_category"].lower()}  # FAST / AVERAGE / SLOW from real Chrome users
    return {"scores": scores, "vitals": vitals, "opportunities": opps[:4], "field": field}


async def _one(client: httpx.AsyncClient, url: str, strategy: str) -> dict | None:
    s = config.settings
    params = [("url", url), ("strategy", strategy), ("locale", "en")] + [("category", k) for k, _ in CATEGORIES]
    if s.pagespeed_api_key:
        params.append(("key", s.pagespeed_api_key))
    try:
        r = await client.get(API, params=params)
        if r.status_code == 429:
            log.info("pagespeed quota reached")
            return {"error": "quota"}
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                pass
            log.info("pagespeed %s for %s: %s", r.status_code, url, msg[:120])
            return {"error": "failed"}
        return parse(r.json())
    except (httpx.HTTPError, ValueError) as e:
        log.info("pagespeed error for %s: %s", url, type(e).__name__)
        return {"error": "failed"}


async def measure(url: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """{"url", "mobile": {...}, "desktop": {...}, "report": "<pagespeed.web.dev link>"} or None when off or failed."""
    s = config.settings
    if not s.pagespeed_enabled:
        return None
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=httpx.Timeout(s.pagespeed_timeout))
    try:
        mobile, desktop = await asyncio.gather(_one(client, url, "mobile"), _one(client, url, "desktop"))
    except Exception:  # never let speed testing break a scan
        log.exception("pagespeed failed")
        return None
    finally:
        if own:
            await client.aclose()
    ok = {k: v for k, v in (("mobile", mobile), ("desktop", desktop)) if v and "error" not in v}
    if not ok:
        return None
    return {"url": url, **ok, "report": f"https://pagespeed.web.dev/analysis?url={quote(url, safe='')}"}
