"""Watched sites: schedule rescans and email the owner when new problems appear."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.db import Issue, Scan, Site, SiteScan, User, session_scope, utcnow
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
        new = [i for i in issues if fingerprint(i) not in old]
        worse = bool(previous and previous.grade and scan.grade and GRADES.index(scan.grade) > GRADES.index(previous.grade))
        if previous and not new and not worse:
            return
        text = build_alert(site.url, scan, previous, new, len(issues))
        subject = (f"{site.url}: docs health {scan.grade} ({len(new)} new problem{'s' if len(new) != 1 else ''})"
                   if previous else f"Your docs health report: {scan.grade} for {site.url}")
        to = owner.email if owner else None
    if to:
        send_email(to, subject, text)


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
