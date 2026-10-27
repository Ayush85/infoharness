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
from app.templating import templates
from app import accounts
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
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


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
    return templates.TemplateResponse(request, "index.html", {"max_pages": config.settings.max_pages})


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
        ctx = {"scan": scan, "grouped": [(t, grouped[t]) for t in TYPE_LABELS if t in grouped],
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
