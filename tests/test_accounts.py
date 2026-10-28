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


def test_free_mode_gives_everyone_full_limits_and_alerts(env, monkeypatch, docs_site):
    monkeypatch.setenv("PAID_PLANS", "false")
    from app import config, worker
    config.reload_settings()
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    monkeypatch.setattr("app.monitor.send_email", out)
    hooks = []
    monkeypatch.setattr("app.monitor.send_webhook", lambda url, text: hooks.append((url, text)) or True)
    c = TestClient(app)
    login(c, out)
    assert "Everything is free" in c.get("/account").text and "Free. All of it." in c.get("/pricing").text
    c.post("/account/sites", data={"url": f"{docs_site}/docs/", "interval": 1})
    with session_scope() as s:
        site = s.scalars(select(Site)).one()
        assert site.interval_days == 1  # daily is allowed for everyone
        sid = site.id
    assert "err=" in c.post(f"/account/sites/{sid}/webhook", data={"url": "http://x.example/hook"}, follow_redirects=False).headers["location"]
    monkeypatch.setattr("app.accounts.check_url_safe", lambda u: None)
    c.post(f"/account/sites/{sid}/webhook", data={"url": "https://hooks.example.com/abc"})
    worker.run_once()
    from app import monitor
    monitor.queue_due_scans()
    while worker.run_once():
        pass
    assert hooks and "docs health" in hooks[0][1].lower()
    with session_scope() as s:
        scan_id = s.scalars(select(Site)).one().last_scan_id
        issue = s.scalars(select(Issue).where(Issue.scan_id == scan_id)).first()
        issue_id = issue.id
    page = c.get(f"/r/{scan_id}").text
    assert "Ignore" in page and "Slack" in c.get("/account").text
    c.post(f"/r/{scan_id}/ignore", data={"issue_id": issue_id})
    assert "Ignored" in c.get(f"/r/{scan_id}").text
    other = TestClient(app)
    login(other, out, email="stranger@example.com")
    other.post(f"/r/{scan_id}/ignore", data={"issue_id": issue_id, "undo": "1"})
    assert "Ignored" in c.get(f"/r/{scan_id}").text  # a stranger cannot change it


def test_seo_surface(env):
    import json as _json
    c = TestClient(app)
    robots = c.get("/robots.txt").text
    assert "Sitemap:" in robots and "Disallow: /r/" not in robots and "Disallow: /account" in robots
    sm = c.get("/sitemap.xml").text
    assert "/guides/contradictory-docs-ai-assistants" in sm and "/pricing" in sm and "/r/" not in sm
    llms = c.get("/llms.txt").text
    assert llms.startswith("# infoharness") and "/guides/" in llms
    home = c.get("/")
    assert '<link rel="canonical"' in home.text and 'og:image' in home.text
    ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', home.text, re.S)
    graph = _json.loads(ld[0].replace("&#34;", '"'))["@graph"]
    assert {n["@type"] for n in graph} >= {"Organization", "SoftwareApplication", "FAQPage"}
    for slug in ("contradictory-docs-ai-assistants", "help-center-audit-checklist", "find-duplicate-and-outdated-pages"):
        r = c.get(f"/guides/{slug}")
        assert r.status_code == 200 and "<h1>" in r.text and "application/ld+json" in r.text
    assert c.get("/guides/nope").status_code == 404
    assert c.get("/guides").status_code == 200
    # private pages stay out of the index
    assert 'content="noindex"' in c.get("/login").text
    assert "immutable" in c.get("/static/fonts/schibsted-grotesk-latin-wght-normal.woff2").headers["cache-control"]


def test_dashboard_history_and_site_detail(env, monkeypatch, docs_site):
    from app import monitor, worker
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    monkeypatch.setattr("app.monitor.send_email", out)
    c = TestClient(app)
    login(c, out)
    c.post("/account/sites", data={"url": f"{docs_site}/docs/", "interval": 30})
    monitor.queue_due_scans()
    while worker.run_once():
        pass
    page = c.get("/account").text
    assert "watched sites" in page and "average score" in page and "Scan history (1)" in page
    with session_scope() as s:
        sid = s.scalars(select(Site)).one().id
    assert "Biggest problems" in c.get(f"/account/sites/{sid}").text
    hist = c.get("/account/history").text
    assert "Watched" in hist and "/r/" in hist
    other = TestClient(app)
    login(other, out, email="stranger@example.com")
    assert other.get(f"/account/sites/{sid}", follow_redirects=False).headers["location"] == "/account"
    assert "Watched" not in other.get("/account/history").text


def test_keyword_pages_and_sitemap(env):
    import json, re
    from app.seo import GUIDES, TOOLS
    c = TestClient(app)
    sitemap = c.get("/sitemap.xml").text
    llms = c.get("/llms.txt").text
    assert len(TOOLS) >= 9 and len(GUIDES) >= 8
    for t in TOOLS:
        r = c.get(f"/tools/{t['slug']}")
        assert r.status_code == 200 and f"/tools/{t['slug']}" in sitemap and t["slug"] in llms
        assert len(t["title"]) <= 66 and 70 <= len(t["description"]) <= 200, t["slug"]
        assert "data-scan-form" in r.text and r.text.count("<h1") == 1
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
            json.loads(block)
    for g in GUIDES:
        assert c.get(f"/guides/{g['slug']}").status_code == 200 and f"/guides/{g['slug']}" in sitemap
    assert c.get("/tools").status_code == 200 and c.get("/tools/nope").status_code == 404
    assert "/tools/documentation-checker" in c.get("/").text


def test_feedback_and_admin_view(env, monkeypatch, docs_site):
    from app import worker
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    from app import config
    config.reload_settings()
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    c = TestClient(app)
    sid = c.post("/api/scans", json={"url": f"{docs_site}/docs"}).json()["id"]
    worker.run_once()
    issue_id = c.get(f"/api/scans/{sid}").json()["issues"][0]["id"]
    assert c.post(f"/api/issues/{issue_id}/feedback", json={"verdict": "bogus"}).status_code == 400
    assert c.post(f"/api/issues/{issue_id}/feedback", json={"verdict": "not_a_problem"}).status_code == 200
    assert c.post(f"/api/issues/{issue_id}/feedback", json={"verdict": "not_a_problem"}).status_code == 200  # same visitor: updated, not duplicated
    with session_scope() as s:
        from app.db import Feedback
        assert len(s.scalars(select(Feedback)).all()) == 1
    assert c.get("/admin/feedback").status_code == 404
    admin = TestClient(app)
    login(admin, out, email="admin@example.com")
    page = admin.get("/admin/feedback").text
    assert "Finding feedback" in page and "not a problem" in page


def test_api_keys(env, monkeypatch, docs_site):
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    c = TestClient(app)
    login(c, out)
    page = c.post("/account/api-keys", data={"name": "CI"}).text
    import re
    key = re.search(r"ih_[A-Za-z0-9_\-]+", page).group(0)
    assert "shown only once" in page and key not in c.get("/account").text  # the plain key is never shown again
    anon = TestClient(app)
    r = anon.post("/api/scans", json={"url": f"{docs_site}/docs"}, headers={"Authorization": "Bearer ih_wrong"})
    assert r.status_code == 201  # a wrong key is ignored, not an error: it just gets anonymous limits
    other = anon.post("/api/scans", json={"url": f"{docs_site}/docs/other", "fresh": True}, headers={"Authorization": f"Bearer {key}"})
    assert other.status_code == 201
    from app.db import ScanMeta
    with session_scope() as s:
        assert s.get(ScanMeta, other.json()["id"]).user_id is not None and s.get(ScanMeta, r.json()["id"]) is None
    assert "Delete" in c.get("/account").text and "never used" not in c.get("/account").text
    assert anon.get("/api-docs").status_code == 200


def test_ai_fix_endpoint_only_when_enabled(env, monkeypatch, docs_site):
    from app import config, worker
    c = TestClient(app)
    sid = c.post("/api/scans", json={"url": f"{docs_site}/docs"}).json()["id"]
    worker.run_once()
    issue_id = c.get(f"/api/scans/{sid}").json()["issues"][0]["id"]
    assert c.post(f"/api/issues/{issue_id}/suggestion").status_code == 404 and "How to fix" in c.get(f"/r/{sid}").text
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config.reload_settings()
    calls = []

    async def fake(i):
        calls.append(i.title)
        return "Use 30 days everywhere."
    monkeypatch.setattr("app.suggest.ai_suggestion", fake)
    assert c.post(f"/api/issues/{issue_id}/suggestion").json()["text"] == "Use 30 days everywhere."
    assert c.post(f"/api/issues/{issue_id}/suggestion").json()["text"] == "Use 30 days everywhere." and len(calls) == 1  # cached
    assert "Use 30 days everywhere." in c.get(f"/r/{sid}").text


def test_weekly_digest_and_unsubscribe(env, monkeypatch, docs_site):
    from app import monitor, worker
    out = Outbox()
    monkeypatch.setattr("app.accounts.send_email", out)
    monkeypatch.setattr("app.monitor.send_email", out)
    c = TestClient(app)
    login(c, out)
    c.post("/account/sites", data={"url": f"{docs_site}/docs/", "interval": 30})
    assert monitor.send_digests() == 0  # nothing scanned yet: nothing to report
    monitor.queue_due_scans()
    while worker.run_once():
        pass
    before = len(out.sent) if hasattr(out, "sent") else 0
    assert monitor.send_digests() == 1
    assert monitor.send_digests() == 0  # at most one a week
    with session_scope() as s:
        uid = s.scalars(select(User)).one().id
    sig = monitor.digest_signature(uid)
    assert c.get(f"/unsubscribe/{uid}/bad").status_code == 400
    assert c.get(f"/unsubscribe/{uid}/{sig}").status_code == 200
    assert "stopped" in c.post(f"/unsubscribe/{uid}/{sig}").text
    from app.db import UserSetting
    with session_scope() as s:
        st = s.get(UserSetting, uid); st.last_digest_at = None
    assert monitor.send_digests() == 0  # opted out
    c.post("/account/digest", data={"on": "1"})
    assert monitor.send_digests() == 1
