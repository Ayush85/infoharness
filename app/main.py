"""HTTP API and pages."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from app import __version__, config
from app.crawler import normalize
from app.db import Issue, Scan, init_engine, session_scope, utcnow
from app.auth import client_hash
from app.scoring import TYPE_LABELS
from app.templating import report_md, templates, verdict
from app import accounts, seo
from app.security import UnsafeURL, check_url_safe, normalize_input_url

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
BASE = Path(__file__).parent
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_engine()
    yield


app = FastAPI(title="infoharness", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(accounts.router)
app.include_router(seo.router)
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1000)


class CachedStatic(StaticFiles):
    """Fonts and images never change under the same name; CSS/JS carry ?v=<version> in their URL."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if path.startswith("fonts/") \
            else "public, max-age=86400"
        return resp


app.mount("/static", CachedStatic(directory=BASE / "static"), name="static")


# ---------------------------------------------------------------- helpers

def scan_json(scan: Scan, issues: list[Issue] | None = None) -> dict:
    data = {
        "id": scan.id, "url": scan.url, "root": scan.root, "status": scan.status, "stage": scan.stage,
        "progress": scan.progress, "pages_crawled": scan.pages_crawled, "score": scan.score,
        "grade": scan.grade, "summary": scan.summary or {}, "error": scan.error,
        "created_at": scan.created_at.isoformat() if scan.created_at else None,
        "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
        "report_url": f"{config.settings.public_base_url}/r/{scan.id}",
    }
    if issues is not None:
        data["issues"] = [{
            "id": i.id, "type": i.type, "severity": i.severity, "title": i.title, "detail": i.detail,
            "page_a": i.page_a, "page_b": i.page_b, "quote_a": i.quote_a, "quote_b": i.quote_b,
            "extra": i.extra or {},
        } for i in issues]
    return data


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "max_pages": config.settings.max_pages, "faqs": seo.FAQS, "guides": seo.GUIDES, "ld": seo.home_jsonld()})


@app.get("/r/{scan_id}", response_class=HTMLResponse)
def report(request: Request, scan_id: str):
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None:
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        issues = list(s.scalars(select(Issue).where(Issue.scan_id == scan_id).order_by(Issue.id))) \
            if scan.status == "done" else []
        grouped: dict[str, list[Issue]] = {}
        for i in issues:
            grouped.setdefault(i.type, []).append(i)
        history = list(s.scalars(select(Scan).where(Scan.url == scan.url, Scan.status == "done")
                                .order_by(Scan.created_at.desc()).limit(50))) if scan.status == "done" else []
        history = [{"id": h.id, "grade": h.grade, "score": round(h.score or 0), "when": h.finished_at or h.created_at,
                    "issues": (h.summary or {}).get("total_issues", 0), "pages": h.pages_crawled}
                   for h in history]
        diff = None
        if scan.status == "done":
            prev = s.scalars(select(Scan).where(Scan.url == scan.url, Scan.status == "done", Scan.created_at < scan.created_at)
                             .order_by(Scan.created_at.desc()).limit(1)).first()
            if prev:
                from app.monitor import fingerprint
                old = {fingerprint(x) for x in s.scalars(select(Issue).where(Issue.scan_id == prev.id))}
                cur = {i.id: fingerprint(i) for i in issues}
                new_ids = {k for k, v in cur.items() if v not in old}
                diff = {"prev_id": prev.id, "prev_when": prev.finished_at or prev.created_at, "prev_grade": prev.grade,
                        "new_ids": new_ids, "new": len(new_ids), "fixed": len(old - set(cur.values()))}
        indexable = seo.is_public_latest(scan)
        latest_id = scan.id
        if not indexable and scan.status == "done" and not (scan.client_hash or "").startswith("site:") and (scan.pages_crawled or 0) >= seo.MIN_PUBLIC_PAGES:
            latest_id = s.scalar(select(Scan.id).where(Scan.url == scan.url, Scan.status == "done",
                                                       ~Scan.client_hash.like("site:%")).order_by(Scan.created_at.desc()).limit(1)) or scan.id
        ctx = {"diff": diff, "indexable": indexable, "latest_id": latest_id, "history": history, "scan": scan, "grouped": [(t, grouped[t]) for t in TYPE_LABELS if t in grouped],
               "verdict": verdict(scan, issues), "total": len(issues), "report_md": report_md(scan, [(t, grouped[t]) for t in TYPE_LABELS if t in grouped]) if issues else "",
               "share_url": f"{config.settings.public_base_url}/r/{scan.id}",
               "badge_url": f"{config.settings.public_base_url}/badge/{scan.id}.svg"}
        return templates.TemplateResponse(request, "report.html", ctx)


@app.get("/bot", response_class=HTMLResponse)
def bot(request: Request):
    return templates.TemplateResponse(request, "bot.html", {"ua": config.settings.user_agent,
                                                            "max_pages": config.settings.max_pages})


@app.get("/healthz")
def healthz():
    with session_scope() as s:
        s.execute(select(1))
    return {"ok": True, "version": __version__}


# ---------------------------------------------------------------- API

class ScanIn(BaseModel):
    url: str
    fresh: bool = False


@app.post("/api/scans")
async def create_scan(body: ScanIn, request: Request):
    s = config.settings
    try:
        url = normalize_input_url(body.url)
        await asyncio.to_thread(check_url_safe, url)
    except UnsafeURL as e:
        raise HTTPException(400, str(e))
    key = normalize(url)
    who = client_hash(request)
    now = utcnow()
    with session_scope() as db:
        # Recent result for the same URL? Return it instead of re-crawling.
        if not body.fresh:
            recent = db.scalars(select(Scan).where(Scan.url == key, Scan.status.in_(("queued", "running", "done")),
                                                   Scan.created_at >= now - timedelta(hours=s.cache_hours))
                                .order_by(Scan.created_at.desc())).first()
            if recent:
                return JSONResponse({"id": recent.id, "cached": True, "report_url": f"/r/{recent.id}"})
        used = db.scalar(select(func.count()).select_from(Scan).where(
            Scan.client_hash == who, Scan.created_at >= now - timedelta(days=1)))
        if used >= s.scans_per_ip_per_day:
            raise HTTPException(429, f"You've reached the limit of {s.scans_per_ip_per_day} scans per day. "
                                     "Try again tomorrow.")
        waiting = db.scalar(select(func.count()).select_from(Scan).where(Scan.status == "queued"))
        if waiting >= s.max_queue:
            raise HTTPException(503, "We're busy scanning other sites right now. Try again in a few minutes.")
        scan = Scan(url=key, client_hash=who)
        db.add(scan)
        db.flush()
        return JSONResponse({"id": scan.id, "cached": False, "report_url": f"/r/{scan.id}"}, status_code=201)


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str):
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None:
            raise HTTPException(404, "Scan not found.")
        issues = list(s.scalars(select(Issue).where(Issue.scan_id == scan_id).order_by(Issue.id))) \
            if scan.status == "done" else None
        data = scan_json(scan, issues)
        if scan.status == "queued":
            data["queue_position"] = s.scalar(select(func.count()).select_from(Scan).where(
                Scan.status == "queued", Scan.created_at <= scan.created_at))
        return data


def _export_rows(scan_id: str):
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None or scan.status != "done":
            raise HTTPException(404, "Report not found.")
        rows = [(i.type, i.severity, i.title, i.page_a, i.page_b, (i.quote_a or "")[:300], (i.quote_b or "")[:300], i.detail or "")
                for i in s.scalars(select(Issue).where(Issue.scan_id == scan_id).order_by(Issue.id))]
        return scan.url, rows


@app.get("/r/{scan_id}/export.csv")
def export_csv(scan_id: str):
    import csv, io
    url, rows = _export_rows(scan_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["type", "severity", "title", "page_a", "page_b", "quote_a", "quote_b", "detail"])
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="docs-health-{scan_id}.csv"', "X-Robots-Tag": "noindex"})


BADGE = """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="docs health: {value}">
<linearGradient id="g" x2="0" y2="100%"><stop offset="0" stop-color="#fff" stop-opacity=".12"/><stop offset="1" stop-opacity=".12"/></linearGradient>
<rect width="{w}" height="20" rx="3" fill="#2b3a42"/><rect x="88" width="{rw}" height="20" rx="3" fill="{color}"/>
<rect x="88" width="4" height="20" fill="{color}"/><rect width="{w}" height="20" rx="3" fill="url(#g)"/>
<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">
<text x="8" y="14">docs health</text><text x="{tx}" y="14">{value}</text></g></svg>"""


@app.get("/badge/{scan_id}.svg")
def badge(scan_id: str):
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None or scan.status != "done" or scan.score is None:
            value, color = "pending", "#7a8a90"
        else:
            value = f"{scan.grade} · {round(scan.score)}"
            color = {"A": "#2f7d4f", "B": "#4f7d2f", "C": "#a07a12", "D": "#b4561d"}.get(scan.grade, "#a3332b")
    rw = 8 + 7 * len(value)
    svg = BADGE.format(w=88 + rw, rw=rw, color=color, tx=92, value=value)
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})
