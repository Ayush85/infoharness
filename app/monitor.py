"""Watched sites: schedule rescans and email the owner when new problems appear."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.auth import effective_plan
from app.db import IgnoredIssue, Issue, Scan, ScanMeta, Site, SiteScan, SiteSetting, User, session_scope, utcnow
from app.mailer import send_email
from app.scoring import TYPE_LABELS

log = logging.getLogger("infoharness.monitor")
GRADES = "ABCDF"


def fingerprint(i: Issue) -> str:
    return "|".join((i.type, i.page_a or "", i.page_b or "", (i.title or "")[:120]))


def queue_due_scans(limit: int = 20) -> int:
    """Create a queued scan for every watched site whose time has come."""
    now = utcnow()
    n = 0
    with session_scope() as s:
        due = s.scalars(select(Site).where(Site.next_run_at <= now).order_by(Site.next_run_at).limit(limit)).all()
        for site in due:
            busy = s.scalar(select(Scan.id).join(SiteScan, SiteScan.scan_id == Scan.id)
                            .where(SiteScan.site_id == site.id, Scan.status.in_(("queued", "running"))))
            site.next_run_at = now + timedelta(days=site.interval_days)
            if busy:
                continue
            scan = Scan(url=site.url, client_hash=f"site:{site.id}")
            s.add(scan)
            s.flush()
            s.add(SiteScan(scan_id=scan.id, site_id=site.id))
            owner = s.get(User, site.user_id)
            pro = owner is not None and effective_plan(owner) == "pro"
            s.add(ScanMeta(scan_id=scan.id, user_id=site.user_id,
                           max_pages=config.settings.pro_max_pages if pro else config.settings.max_pages))
            site.last_scan_id = scan.id
            n += 1
    return n


def after_scan(scan_id: str) -> None:
    """Called by the worker when any scan ends. Updates the site and sends an alert if something is new."""
    with session_scope() as s:
        link = s.get(SiteScan, scan_id)
        if link is None:
            return
        site, scan = s.get(Site, link.site_id), s.get(Scan, scan_id)
        if site is None or scan is None:
            return
        owner = s.get(User, site.user_id)
        previous = s.scalars(select(Scan).join(SiteScan, SiteScan.scan_id == Scan.id).where(
            SiteScan.site_id == site.id, Scan.id != scan_id, Scan.status == "done")
            .order_by(Scan.created_at.desc())).first()
        site.last_status, site.last_error = scan.status, scan.error
        if scan.status != "done":
            return
        site.last_grade, site.last_score = scan.grade, scan.score
        issues = list(s.scalars(select(Issue).where(Issue.scan_id == scan_id).order_by(Issue.id)))
        old = {fingerprint(i) for i in s.scalars(select(Issue).where(Issue.scan_id == previous.id))} if previous else set()
        ignored = {r for r in s.scalars(select(IgnoredIssue.fingerprint).where(IgnoredIssue.site_id == site.id))}
        new = [i for i in issues if fingerprint(i) not in old and fingerprint(i) not in ignored]
        setting = s.get(SiteSetting, site.id)
        hook = setting.webhook_url if setting else None
        worse = bool(previous and previous.grade and scan.grade and GRADES.index(scan.grade) > GRADES.index(previous.grade))
        if previous and not new and not worse:
            return
        text = build_alert(site.url, scan, previous, new, len(issues))
        subject = (f"{site.url}: docs health {scan.grade} ({len(new)} new problem{'s' if len(new) != 1 else ''})"
                   if previous else f"Your docs health report: {scan.grade} for {site.url}")
        to = owner.email if owner else None
    if to:
        send_email(to, subject, text)
    if hook:
        send_webhook(hook, f"{subject}\n\n{text}")


def send_webhook(url: str, text: str) -> bool:
    """Post to a Slack/Discord/generic incoming webhook. Never raises; the URL is re-checked every time."""
    import httpx
    from app.security import UnsafeURL, check_url_safe
    try:
        if not url.startswith("https://"):
            return False
        check_url_safe(url)
        r = httpx.post(url, json={"text": text[:3500], "content": text[:1900]}, timeout=8, follow_redirects=False)
        return r.status_code < 300
    except (UnsafeURL, httpx.HTTPError, OSError) as e:
        log.warning("webhook failed: %s", e)
        return False


def build_alert(url: str, scan: Scan, previous: Scan | None, new: list[Issue], total: int) -> str:
    base = config.settings.public_base_url
    lines = []
    if previous:
        change = f"{previous.grade} → {scan.grade}" if previous.grade != scan.grade else f"still {scan.grade}"
        lines.append(f"New problems found on {url} since the last check ({change}).")
    else:
        lines.append(f"First scan of {url} is done: grade {scan.grade}, {total} problem{'s' if total != 1 else ''} found.")
    lines.append("")
    shown = sorted(new, key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(i.severity, 3))[:10]
    for i in shown:
        label = TYPE_LABELS.get(i.type, i.type)
        lines.append(f"- [{label}] {i.title}")
        lines.append(f"    {i.page_a}" + (f"\n    {i.page_b}" if i.page_b else ""))
    if len(new) > len(shown):
        lines.append(f"…and {len(new) - len(shown)} more in the full report.")
    lines += ["", f"Full report: {base}/r/{scan.id}", f"Manage watched sites: {base}/account"]
    return "\n".join(lines)


# ---------------------------------------------------------------- weekly digest
DIGEST_DAYS = 7


def digest_signature(user_id: int) -> str:
    from app.auth import _sig
    return _sig(f"digest-off.{user_id}")


def build_digest(sites: list[dict]) -> str:
    base = config.settings.public_base_url
    lines = ["Your weekly docs health summary:", ""]
    for s in sites:
        change = ""
        if s["prev_grade"] and s["prev_grade"] != s["grade"]:
            change = f" (was {s['prev_grade']})"
        lines.append(f"- {s['url']}: grade {s['grade']}{change}, {s['issues']} open problem{'s' if s['issues'] != 1 else ''}"
                     + (f", {s['new']} new this week" if s["new"] else ""))
        lines.append(f"    {base}/r/{s['scan_id']}")
    lines += ["", f"Dashboard: {base}/account"]
    return "\n".join(lines)


def send_digests(limit: int = 50) -> int:
    """Email each user with watched sites a weekly summary, at most once every 7 days. Returns emails sent."""
    from app.db import UserSetting
    now = utcnow()
    sent = 0
    with session_scope() as s:
        user_ids = [r for r in s.scalars(select(Site.user_id).distinct())]
        for uid in user_ids[:500]:
            if sent >= limit:
                break
            setting = s.get(UserSetting, uid) or UserSetting(user_id=uid, digest=True)
            last = setting.last_digest_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            if not setting.digest or (last is not None and last > now - timedelta(days=DIGEST_DAYS)):
                continue
            user = s.get(User, uid)
            rows = []
            for site in s.scalars(select(Site).where(Site.user_id == uid).order_by(Site.id)):
                scans = s.scalars(select(Scan).join(SiteScan, SiteScan.scan_id == Scan.id).where(
                    SiteScan.site_id == site.id, Scan.status == "done").order_by(Scan.created_at.desc()).limit(8)).all()
                if not scans:
                    continue
                cur = scans[0]
                week_ago = next((x for x in scans[1:] if (x.created_at.replace(tzinfo=now.tzinfo) if x.created_at.tzinfo is None else x.created_at) <= now - timedelta(days=DIGEST_DAYS - 1)), None)
                prev = week_ago or (scans[1] if len(scans) > 1 else None)
                new = 0
                if prev:
                    old = {fingerprint(i) for i in s.scalars(select(Issue).where(Issue.scan_id == prev.id))}
                    new = sum(1 for i in s.scalars(select(Issue).where(Issue.scan_id == cur.id)) if fingerprint(i) not in old)
                rows.append({"url": site.url, "grade": cur.grade, "prev_grade": prev.grade if prev else None,
                             "issues": (cur.summary or {}).get("total_issues", 0), "new": new, "scan_id": cur.id})
            if not rows or not user:
                continue
            base = config.settings.public_base_url
            body = build_digest(rows) + f"\n\nStop these weekly emails: {base}/unsubscribe/{uid}/{digest_signature(uid)}"
            to = user.email
            setting.last_digest_at = now
            s.add(setting)
            s.flush()
            s.commit()
            if send_email(to, "Your weekly docs health summary", body):
                sent += 1
    return sent
