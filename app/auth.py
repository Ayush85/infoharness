"""Passwordless login: a one-time link by email, then a signed session cookie."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select

from app import config
from app.db import LoginToken, User, session_scope, utcnow

COOKIE = "ih_session"
SESSION_DAYS = 30
TOKEN_MINUTES = 15
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")


def client_hash(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    if config.settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            ip = fwd.split(",")[-1].strip() or ip  # rightmost = added by our own reverse proxy
    return hashlib.sha256(f"{config.settings.ip_hash_salt}:{ip}".encode()).hexdigest()


def clean_email(raw: str) -> str | None:
    e = (raw or "").strip().lower()
    return e if EMAIL_RE.match(e) and len(e) <= 320 else None


def _sig(payload: str) -> str:
    key = config.settings.secret_key.encode()
    return base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()[:18]).decode()


def make_session(user_id: int) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{user_id}.{exp}"
    return f"{payload}.{_sig(payload)}"


def read_session(value: str | None) -> int | None:
    try:
        uid, exp, sig = (value or "").split(".")
        if not hmac.compare_digest(sig, _sig(f"{uid}.{exp}")) or int(exp) < time.time():
            return None
        return int(uid)
    except ValueError:
        return None


def current_user(request: Request) -> dict | None:
    uid = read_session(request.cookies.get(COOKIE))
    if uid is None:
        return None
    with session_scope() as s:
        u = s.get(User, uid)
        return {"id": u.id, "email": u.email, "plan": effective_plan(u)} if u else None


def effective_plan(user: User) -> str:
    if not config.settings.paid_plans:
        return "pro"  # the product is free: every signed-in user gets the full limits
    return "pro" if user.plan == "pro" or user.email in config.settings.admins else "free"


def site_limit(plan: str) -> int:
    return config.settings.pro_sites if plan == "pro" else config.settings.free_sites


def issue_token(email: str, who: str) -> str | None:
    """Create a one-time login token. Returns None when this email or client asked too often."""
    now = utcnow()
    with session_scope() as s:
        hour = now - timedelta(hours=1)
        by_email = s.scalar(select(func.count()).select_from(LoginToken).where(
            LoginToken.email == email, LoginToken.created_at >= hour))
        by_client = s.scalar(select(func.count()).select_from(LoginToken).where(
            LoginToken.client_hash == who, LoginToken.created_at >= hour))
        if by_email >= 5 or by_client >= 15:
            return None
        token = secrets.token_urlsafe(32)
        s.add(LoginToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), email=email, client_hash=who,
                         expires_at=now + timedelta(minutes=TOKEN_MINUTES)))
        return token


def redeem_token(token: str) -> int | None:
    """Single use. Returns the user id (creating the account on first login)."""
    h = hashlib.sha256((token or "").encode()).hexdigest()
    now = utcnow()
    with session_scope() as s:
        t = s.scalars(select(LoginToken).where(LoginToken.token_hash == h).with_for_update()).first()
        if t is None or t.used_at is not None:
            return None
        exp = t.expires_at if t.expires_at.tzinfo else t.expires_at.replace(tzinfo=now.tzinfo)
        if exp < now:
            return None
        t.used_at = now
        user = s.scalars(select(User).where(User.email == t.email)).first()
        if user is None:
            user = User(email=t.email)
            s.add(user)
            s.flush()
        return user.id
