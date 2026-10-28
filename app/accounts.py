"""Login, watched sites, pricing and the payment webhook."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select

from app import config
from app.auth import (COOKIE, SESSION_DAYS, clean_email, client_hash, current_user, issue_token, make_session,
                      read_session, redeem_token, site_limit)
from app.crawler import normalize
from app.db import ApiKey, IgnoredIssue, Issue, Scan, ScanMeta, Site, SiteScan, SiteSetting, User, Waitlist, session_scope, utcnow
from app.mailer import send_email
from app.security import UnsafeURL, check_url_safe, normalize_input_url
from app.templating import templates

log = logging.getLogger("infoharness.accounts")
router = APIRouter()
INTERVALS = {1: "daily", 7: "weekly", 30: "monthly"}


def _page(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    return templates.TemplateResponse(request, name, ctx or {}, status_code=status)


def _redirect(url: str, cookie: str | None = None, clear: bool = False) -> RedirectResponse:
    r = RedirectResponse(url, status_code=303)
    secure = config.settings.public_base_url.startswith("https")
    if cookie:
        r.set_cookie(COOKIE, cookie, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
    if clear:
        r.delete_cookie(COOKIE)
    return r


# ---------------------------------------------------------------- login
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return _redirect("/account")
    return _page(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form("")):
    e = clean_email(email)
    if not e:
        return _page(request, "login.html", {"error": "Enter a valid email address."}, 400)
    token = issue_token(e, client_hash(request))
    if token is None:
        return _page(request, "login.html", {"error": "Too many login emails requested. Try again in an hour."}, 429)
    link = f"{config.settings.public_base_url}/login/verify?token={quote(token)}"
    sent = send_email(e, "Your infoharness sign-in link",
                      f"Click to sign in (valid for 15 minutes, works once):\n\n{link}\n\n"
                      "If you didn't ask for this, ignore this email.")
    # Same answer whether or not the address exists or the mail went out, so the form can't be used to probe accounts.
    return _page(request, "login_sent.html", {"email": e, "mail_off": not config.settings.mail_enabled and not sent})


@router.get("/login/verify", response_class=HTMLResponse)
def verify_page(request: Request, token: str = ""):
    # A confirm button, not auto-login: mail scanners that prefetch links must not burn the one-time token.
    return _page(request, "verify.html", {"token": token})


@router.post("/login/verify")
def verify_submit(request: Request, token: str = Form("")):
    uid = redeem_token(token)
    if uid is None:
        return _page(request, "login.html", {"error": "That sign-in link has expired or was already used. Request a new one."}, 400)
    return _redirect("/account", make_session(uid))


@router.post("/logout")
def logout():
    return _redirect("/", clear=True)


# ---------------------------------------------------------------- account
def _scan_row(sc: Scan, source: str, site_url: str = "") -> dict:
    sm = sc.summary or {}
    return {"id": sc.id, "url": sc.url, "status": sc.status, "grade": sc.grade, "score": round(sc.score or 0) if sc.score is not None else None,
            "issues": sm.get("total_issues"), "pages": sc.pages_crawled, "by_type": sm.get("by_type", {}),
            "when": sc.finished_at or sc.created_at, "source": source, "error": sc.error}


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: str = "", err: str = ""):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    return _account_page(request, me, msg, err)


def _account_page(request: Request, me: dict, msg: str = "", err: str = "", new_key: str = ""):
    limit = site_limit(me["plan"])
    with session_scope() as s:
        sites = list(s.scalars(select(Site).where(Site.user_id == me["id"]).order_by(Site.id)))
        trend, hooks, last_two = {}, {}, {}
        for site in sites:
            rows = s.scalars(select(Scan).join(SiteScan, SiteScan.scan_id == Scan.id).where(
                SiteScan.site_id == site.id, Scan.status == "done").order_by(Scan.created_at.desc()).limit(24)).all()
            trend[site.id] = [round(r.score or 0) for r in reversed(rows)]
            last_two[site.id] = [(r.summary or {}).get("total_issues", 0) for r in rows[:2]]
            st = s.get(SiteSetting, site.id)
            hooks[site.id] = bool(st and st.webhook_url)
        keys = list(s.scalars(select(ApiKey).where(ApiKey.user_id == me["id"]).order_by(ApiKey.id)))
        graded = [x for x in sites if x.last_status == "done" and x.last_score is not None]
        dash = {
            "sites": len(sites), "avg": round(sum(x.last_score for x in graded) / len(graded)) if graded else None,
            "open": sum(last_two[x.id][0] for x in graded if last_two.get(x.id)),
            "worse": sum(1 for x in graded if len(last_two[x.id]) == 2 and last_two[x.id][0] > last_two[x.id][1]),
            "failing": sum(1 for x in sites if x.last_status == "failed"),
            "scans": s.scalar(select(func.count()).select_from(Scan).where(or_(
                Scan.id.in_(select(SiteScan.scan_id).where(SiteScan.site_id.in_([x.id for x in sites] or [0]))),
                Scan.id.in_(select(ScanMeta.scan_id).where(ScanMeta.user_id == me["id"]))))),
        }
    return _page(request, "account.html", {"keys": keys, "new_key": new_key, "trend": trend, "hooks": hooks, "last_two": last_two, "dash": dash, "sites": sites,
                                           "limit": limit, "intervals": INTERVALS,
                                           "allowed": [30] if me["plan"] != "pro" else [1, 7, 30],
                                           "msg": msg, "err": err, "now": utcnow()})


@router.post("/account/api-keys", response_class=HTMLResponse)
def create_api_key(request: Request, name: str = Form("")):
    import secrets as _secrets
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        if s.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == me["id"])) >= 5:
            return _redirect(f"/account?err={quote('You can have up to 5 API keys. Delete one first.')}")
        key = "ih_" + _secrets.token_urlsafe(30)
        s.add(ApiKey(user_id=me["id"], name=name.strip()[:80] or "API key", prefix=key[:9],
                     key_hash=hashlib.sha256(key.encode()).hexdigest()))
    return _account_page(request, me, msg="API key created. Copy it now: it is shown only once.", new_key=key)


@router.post("/account/api-keys/{key_id}/delete")
def delete_api_key(request: Request, key_id: int):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        row = s.get(ApiKey, key_id)
        if row and row.user_id == me["id"]:
            s.delete(row)
    return _redirect(f"/account?msg={quote('API key deleted.')}")


@router.get("/account/history", response_class=HTMLResponse)
def history(request: Request, page: int = 1):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    page = max(1, page)
    with session_scope() as s:
        sites = {x.id: x for x in s.scalars(select(Site).where(Site.user_id == me["id"]))}
        watched = {r.scan_id: r.site_id for r in s.scalars(select(SiteScan).where(SiteScan.site_id.in_(list(sites) or [0])))}
        scans = s.scalars(select(Scan).where(or_(
            Scan.id.in_(list(watched) or [""]),
            Scan.id.in_(select(ScanMeta.scan_id).where(ScanMeta.user_id == me["id"]))))
            .order_by(Scan.created_at.desc()).limit(101).offset((page - 1) * 100)).all()
        rows = [_scan_row(sc, "Watched" if sc.id in watched else "Manual") for sc in scans[:100]]
        more = len(scans) > 100
    return _page(request, "history.html", {"rows": rows, "page": page, "more": more})


@router.get("/account/sites/{site_id}", response_class=HTMLResponse)
def site_detail(request: Request, site_id: int, msg: str = "", err: str = ""):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        site = s.get(Site, site_id)
        if not site or site.user_id != me["id"]:
            return _redirect("/account")
        scans = s.scalars(select(Scan).join(SiteScan, SiteScan.scan_id == Scan.id).where(SiteScan.site_id == site_id)
                          .order_by(Scan.created_at.desc()).limit(100)).all()
        rows = [_scan_row(sc, "Watched") for sc in scans]
        done = [r for r in rows if r["status"] == "done"]
        ignored = [fp for fp in s.scalars(select(IgnoredIssue.fingerprint).where(IgnoredIssue.site_id == site_id))]
        st = s.get(SiteSetting, site_id)
        top = []
        if done:
            top = list(s.scalars(select(Issue).where(Issue.scan_id == done[0]["id"])
                                 .order_by(Issue.points.desc()).limit(8)))
            s.expunge_all()
        return _page(request, "site.html", {
            "site": site, "rows": rows, "latest": done[0] if done else None, "top": top,
            "trend": [r["score"] for r in reversed(done[:40])], "intervals": INTERVALS,
            "ignored": [{"fp": fp, "title": fp.split("|")[-1], "type": fp.split("|")[0]} for fp in ignored],
            "has_hook": bool(st and st.webhook_url), "msg": msg, "err": err})


@router.post("/account/sites/{site_id}/unignore")
def unignore(request: Request, site_id: int, fp: str = Form("")):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        site = s.get(Site, site_id)
        row = s.get(IgnoredIssue, (site_id, fp)) if site and site.user_id == me["id"] else None
        if row:
            s.delete(row)
    return _redirect(f"/account/sites/{site_id}")


@router.post("/account/sites")
async def add_site(request: Request, url: str = Form(""), interval: int = Form(30)):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    try:
        norm = normalize(normalize_input_url(url))
        await asyncio.to_thread(check_url_safe, norm)
    except UnsafeURL as e:
        return _redirect(f"/account?err={quote(str(e))}")
    if interval not in INTERVALS or (me["plan"] != "pro" and interval != 30):
        interval = 30
    with session_scope() as s:
        have = s.scalar(select(func.count()).select_from(Site).where(Site.user_id == me["id"]))
        if have >= site_limit(me["plan"]):
            return _redirect(f"/account?err={quote('You have reached the limit of watched sites.')}")
        if s.scalar(select(Site.id).where(Site.user_id == me["id"], Site.url == norm)):
            return _redirect(f"/account?err={quote('You already watch that site.')}")
        s.add(Site(user_id=me["id"], url=norm, interval_days=interval))  # next_run_at = now: first scan starts at once
    return _redirect(f"/account?msg={quote('Site added. The first scan starts in a minute and we will email you the result.')}")


@router.post("/account/sites/{site_id}/webhook")
async def set_webhook(request: Request, site_id: int, url: str = Form("")):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    url = url.strip()
    if url:
        try:
            if not url.startswith("https://"):
                raise UnsafeURL("The webhook address must start with https://")
            await asyncio.to_thread(check_url_safe, url)
        except UnsafeURL as e:
            return _redirect(f"/account?err={quote(str(e))}")
    with session_scope() as s:
        site = s.get(Site, site_id)
        if not site or site.user_id != me["id"]:
            return _redirect("/account")
        st = s.get(SiteSetting, site_id) or SiteSetting(site_id=site_id)
        st.webhook_url = url or None
        s.add(st)
    return _redirect(f"/account?msg={quote('Webhook saved. Alerts will also be posted there.' if url else 'Webhook removed.')}")


@router.post("/r/{scan_id}/ignore")
def ignore_issue(request: Request, scan_id: str, issue_id: int = Form(0), undo: str = Form("")):
    from app.monitor import fingerprint
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        link = s.get(SiteScan, scan_id)
        site = s.get(Site, link.site_id) if link else None
        issue = s.get(Issue, issue_id)
        if site and site.user_id == me["id"] and issue and issue.scan_id == scan_id:
            fp = fingerprint(issue)[:400]
            row = s.get(IgnoredIssue, (site.id, fp))
            if undo and row:
                s.delete(row)
            elif not undo and not row:
                s.add(IgnoredIssue(site_id=site.id, fingerprint=fp))
    return _redirect(f"/r/{scan_id}")


@router.post("/account/sites/{site_id}/delete")
def delete_site(request: Request, site_id: int):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        site = s.get(Site, site_id)
        if site and site.user_id == me["id"]:
            st = s.get(SiteSetting, site_id)
            if st:
                s.delete(st)
            for row in s.scalars(select(IgnoredIssue).where(IgnoredIssue.site_id == site_id)):
                s.delete(row)
            s.delete(site)
    return _redirect("/account")


@router.post("/account/sites/{site_id}/scan")
def scan_site_now(request: Request, site_id: int):
    me = current_user(request)
    if not me:
        return _redirect("/login")
    with session_scope() as s:
        site = s.get(Site, site_id)
        if site and site.user_id == me["id"]:
            recent = s.scalar(select(Scan.id).join(SiteScan, SiteScan.scan_id == Scan.id).where(
                SiteScan.site_id == site.id, Scan.created_at >= utcnow() - timedelta(hours=6)))
            if recent:
                return _redirect(f"/account?err={quote('This site was scanned less than 6 hours ago.')}")
            site.next_run_at = utcnow()
    return _redirect(f"/account?msg={quote('Scan queued.')}")


# ---------------------------------------------------------------- pricing
@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, joined: int = 0):
    s = config.settings
    me = current_user(request)
    checkout = ""
    if s.checkout_url:
        checkout = s.checkout_url + (("&" if "?" in s.checkout_url else "?") + "checkout[email]=" + quote(me["email"])
                                     if me else "")
    return _page(request, "pricing.html", {"checkout": checkout, "joined": bool(joined)})


@router.post("/pricing/waitlist")
def waitlist(email: str = Form("")):
    e = clean_email(email)
    if not e:
        return _redirect("/pricing")
    with session_scope() as s:
        if not s.scalar(select(Waitlist.id).where(Waitlist.email == e)):
            s.add(Waitlist(email=e))
    return _redirect("/pricing?joined=1")


# ---------------------------------------------------------------- payments (Lemon Squeezy)
ACTIVE = {"active", "on_trial", "past_due", "cancelled"}  # cancelled = paid period still running
INACTIVE = {"expired", "unpaid", "paused"}


@router.post("/webhooks/lemonsqueezy")
async def lemonsqueezy(request: Request):
    secret = config.settings.ls_webhook_secret
    if not secret:
        raise HTTPException(404)
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-signature", "")):
        raise HTTPException(401, "bad signature")
    try:
        body = json.loads(raw)
        event = body["meta"]["event_name"]
        attrs = body["data"]["attributes"]
        email = clean_email(attrs.get("user_email", ""))
        status = attrs.get("status", "")
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "malformed payload")
    if not event.startswith("subscription_") or not email:
        return JSONResponse({"ok": True, "ignored": True})
    plan = "pro" if status in ACTIVE and event != "subscription_expired" else ("free" if status in INACTIVE or event == "subscription_expired" else None)
    if plan is None:
        return JSONResponse({"ok": True, "ignored": True})
    with session_scope() as s:
        user = s.scalars(select(User).where(User.email == email)).first()
        if user is None:
            user = User(email=email)
            s.add(user)
        user.plan = plan
    log.info("plan for %s set to %s (%s/%s)", email, plan, event, status)
    return JSONResponse({"ok": True, "plan": plan})
