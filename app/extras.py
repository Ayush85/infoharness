"""Feedback on findings, the admin view of it, and other small routes that don't belong to a page."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app import config
from app.auth import client_hash, current_user
from app.db import Feedback, Issue, Scan, session_scope, utcnow
from app.scoring import TYPE_LABELS
from app.templating import templates

router = APIRouter()
FEEDBACK_PER_DAY = 80


class FeedbackIn(BaseModel):
    verdict: str
    note: str = ""


@router.post("/api/issues/{issue_id}/feedback")
def feedback(issue_id: int, body: FeedbackIn, request: Request):
    if body.verdict not in ("not_a_problem", "useful"):
        raise HTTPException(400, "Unknown verdict.")
    who = client_hash(request)
    with session_scope() as s:
        issue = s.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(404, "Finding not found.")
        today = s.scalar(select(func.count()).select_from(Feedback).where(
            Feedback.client_hash == who, Feedback.created_at >= utcnow() - timedelta(days=1)))
        if today >= FEEDBACK_PER_DAY:
            raise HTTPException(429, "Thanks, that is plenty of feedback for today.")
        row = s.scalar(select(Feedback).where(Feedback.issue_id == issue_id, Feedback.client_hash == who))
        if row is None:
            row = Feedback(issue_id=issue_id, scan_id=issue.scan_id, issue_type=issue.type, client_hash=who, verdict=body.verdict)
            s.add(row)
        row.verdict, row.note = body.verdict, body.note.strip()[:500]
    return JSONResponse({"ok": True})


def _is_admin(request: Request) -> bool:
    me = current_user(request)
    return bool(me and me["email"] in config.settings.admins)


@router.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(request: Request):
    if not _is_admin(request):
        return templates.TemplateResponse(request, "notfound.html", status_code=404)
    with session_scope() as s:
        by_type = {t: {"not_a_problem": 0, "useful": 0} for t in TYPE_LABELS}
        for t, v, n in s.execute(select(Feedback.issue_type, Feedback.verdict, func.count()).group_by(Feedback.issue_type, Feedback.verdict)):
            by_type.setdefault(t, {"not_a_problem": 0, "useful": 0})[v] = n
        recent = []
        for fb in s.scalars(select(Feedback).order_by(Feedback.created_at.desc()).limit(100)):
            issue = s.get(Issue, fb.issue_id)
            scan = s.get(Scan, fb.scan_id)
            recent.append({"when": fb.created_at, "verdict": fb.verdict, "type": fb.issue_type, "note": fb.note,
                           "title": issue.title if issue else "(deleted)", "a": issue.page_a if issue else "",
                           "b": issue.page_b if issue else "", "scan": fb.scan_id, "site": scan.url if scan else ""})
    return templates.TemplateResponse(request, "admin_feedback.html", {"by_type": by_type, "recent": recent, "labels": TYPE_LABELS})
