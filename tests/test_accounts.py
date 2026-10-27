import hashlib
import hmac
import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, worker
from app.db import Issue, Scan, Site, SiteScan, User, session_scope, utcnow
from app.main import app


class Outbox(list):
    def __call__(self, to, subject, text):
        self.append((to, subject, text))
        return True


def login(client, outbox, email="dev@example.com"):
    assert client.post("/login", data={"email": email}, follow_redirects=False).status_code == 200
    token = re.search(r"token=([\w\-%.]+)", outbox[-1][2]).group(1)
    r = client.post("/login/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and "ih_session" in r.headers["set-cookie"]
    return token


def test_login_flow_and_one_time_token(env, monkeypatch):
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    c = TestClient(app)
    assert c.post("/login", data={"email": "not-an-email"}).status_code == 400
    token = login(c, out)
    assert c.get("/account").status_code == 200 and "dev@example.com" in c.get("/account").text
    # the same link can't be used twice
    again = TestClient(app).post("/login/verify", data={"token": token}, follow_redirects=False)
    assert again.status_code == 400
    # a forged session cookie is rejected
    forged = TestClient(app)
    forged.cookies.set("ih_session", "1.9999999999.AAAA")
    assert forged.get("/account", follow_redirects=False).status_code == 303
    # signed out
    c.post("/logout")
    assert TestClient(app).get("/account", follow_redirects=False).headers["location"] == "/login"


def test_login_is_rate_limited(env, monkeypatch):
    monkeypatch.setattr("app.accounts.send_email", Outbox())
    c = TestClient(app)
    codes = [c.post("/login", data={"email": "same@example.com"}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_site_limits_by_plan(env, monkeypatch, docs_site):
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    c = TestClient(app)
    login(c, out)
    c.post("/account/sites", data={"url": f"{docs_site}/docs/", "interval": 1})
    with session_scope() as s:
        site = s.scalars(select(Site)).one()
        assert site.interval_days == 30  # free plan can't pick daily
    r = c.post("/account/sites", data={"url": f"{docs_site}/docs/other/"}, follow_redirects=False)
    assert "limit" in r.headers["location"]
    r = c.post("/account/sites", data={"url": "http://169.254.169.254/"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_monitor_emails_first_report_then_only_new_problems(env, monkeypatch, docs_site):
    from app import monitor
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    monkeypatch.setattr("app.monitor.send_email", out)
    c = TestClient(app)
    login(c, out)
    c.post("/account/sites", data={"url": f"{docs_site}/docs/", "interval": 30})
    out.clear()

    assert monitor.queue_due_scans() == 1
    assert monitor.queue_due_scans() == 0  # not queued twice
    assert worker.run_once() is True
    assert len(out) == 1 and out[0][0] == "dev@example.com" and "Your docs health report" in out[0][1]
    assert "Full report:" in out[0][2]

    # Rescan with nothing changed: no email
    with session_scope() as s:
        s.scalars(select(Site)).one().next_run_at = utcnow()
    monitor.queue_due_scans()
    worker.run_once()
    assert len(out) == 1

    # Pretend two problems were fixed last time -> they look new now -> alert
    with session_scope() as s:
        first = s.scalars(select(SiteScan).order_by(SiteScan.scan_id)).first()
        ids = [sc.scan_id for sc in s.scalars(select(SiteScan))]
        oldest = s.scalars(select(Scan).where(Scan.id.in_(ids)).order_by(Scan.created_at)).first()
        for iss in list(s.scalars(select(Issue).where(Issue.scan_id == oldest.id, Issue.type.in_(("broken_link", "thin_page"))))):
            s.delete(iss)
        # make the oldest scan the "previous" for the next one by dropping the middle scan
        for scan_id in ids:
            if scan_id != oldest.id:
                s.delete(s.get(Scan, scan_id))
        s.scalars(select(Site)).one().next_run_at = utcnow()
    monitor.queue_due_scans()
    worker.run_once()
    assert len(out) == 2 and "2 new problems" in out[1][1]
    assert "Broken link" in out[1][2] or "broken" in out[1][2].lower()

    page = c.get("/account").text
    assert "/r/" in page


def test_lemonsqueezy_webhook(env, monkeypatch):
    monkeypatch.setenv("LS_WEBHOOK_SECRET", "whsec")
    config.reload_settings()
    c = TestClient(app)

    def send(event, status, email="buyer@example.com", sig=None):
        raw = json.dumps({"meta": {"event_name": event}, "data": {"attributes": {"user_email": email, "status": status}}}).encode()
        good = hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()
        return c.post("/webhooks/lemonsqueezy", content=raw, headers={"x-signature": sig or good})

    assert send("subscription_created", "active", sig="bad").status_code == 401
    assert send("subscription_created", "active").json()["plan"] == "pro"
    with session_scope() as s:
        assert s.scalars(select(User).where(User.email == "buyer@example.com")).one().plan == "pro"
    assert send("subscription_cancelled", "cancelled").json()["plan"] == "pro"  # paid period still running
    assert send("subscription_expired", "expired").json()["plan"] == "free"
    assert send("order_created", "paid").json().get("ignored")


def test_webhook_disabled_without_secret(env):
    assert TestClient(app).post("/webhooks/lemonsqueezy", content=b"{}").status_code == 404


def test_pricing_and_waitlist(env):
    c = TestClient(app)
    assert "Pro is almost ready" in c.get("/pricing").text
    r = c.post("/pricing/waitlist", data={"email": "me@example.com"})
    assert "on the list" in r.text
