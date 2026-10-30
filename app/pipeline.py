"""Orchestrates one scan: crawl -> analyse -> check links -> (LLM verify) -> score -> save."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from app import config, embeddings, llm
from app.analysis import (Finding, PageDoc, build_chunks, check_duplicate_pages, check_duplicate_sections,
                          check_orphans, check_staleness, NON_DOC_EXT, different_languages, check_thin_pages, extract_facts, numeric_conflicts,
                          numeric_finding, top_pairs, _short)
from app.analysis import admin_target_kind
from app import speed
from app.crawler import INFRA_PATH, CrawlResult, Crawler, _bare_host, check_links, is_bot_hostile
from app.db import Issue, Page, Scan, ScanMeta, session_scope, utcnow
from app.scoring import score

log = logging.getLogger(__name__)


class Progress:
    """Throttled progress writer so the UI can poll without hammering the DB."""

    def __init__(self, scan_id: str):
        self.scan_id = scan_id
        self._last = 0.0
        self.live: dict = {"recent": []}

    def note(self, **info) -> None:
        """Remember live details (current page, counters). They are written with the next progress update."""
        url = info.pop("current", None)
        if url:
            self.live["current"] = url
            recent = self.live["recent"]
            if not recent or recent[-1] != url:
                recent.append(url)
                del recent[:-6]
        self.live.update(info)

    def set(self, stage: str, pct: int, pages: int | None = None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 0.8:
            return
        self._last = now
        with session_scope() as s:
            scan = s.get(Scan, self.scan_id)
            if scan:
                scan.stage = stage
                scan.progress = max(scan.progress or 0, min(99, pct))
                if pages is not None:
                    scan.pages_crawled = pages
                scan.summary = {"live": dict(self.live, recent=list(self.live["recent"]))}


def run_scan(scan_id: str) -> None:
    started = time.monotonic()
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None:
            return
        url = scan.url
        meta = s.get(ScanMeta, scan_id)
        limits = (meta.max_pages, meta.user_id) if meta else (None, None)
        scan.status, scan.stage, scan.started_at, scan.progress = "running", "crawling", utcnow(), 1
    prog = Progress(scan_id)
    try:
        findings, pages, crawl = asyncio.run(_run(url, prog, max_pages=limits[0]))
        value, grade, summary = score(findings, max(1, len(pages)))
        summary.update({
            "stopped_reason": crawl.stopped_reason,
            "llm_verified": config.settings.llm_enabled,
            "embeddings": embeddings.provider(),
            "duration_s": round(time.monotonic() - started, 1),
            "pages_total": len(crawl.pages),
            "pages_found": len(crawl.pages) + len(crawl.overflow),
            "rendered_pages": sum(1 for p in crawl.pages.values() if p.rendered),
            "partial": _is_partial(crawl),
            "links_checked": getattr(crawl, "links_checked", 0),
            "links_skipped": getattr(crawl, "links_skipped", 0),
        })
        if getattr(crawl, "speed", None):
            summary["speed"] = crawl.speed
        _save(scan_id, crawl, pages, findings, value, grade, summary)
    except ScanError as e:
        _fail(scan_id, str(e))
    except Exception:  # never leave a scan stuck in "running"
        log.exception("scan %s failed", scan_id)
        _fail(scan_id, "Something went wrong while scanning this site. Please try again later.")


class ScanError(Exception):
    pass


def _fail(scan_id: str, message: str) -> None:
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan:
            scan.status, scan.stage, scan.error, scan.finished_at = "failed", "failed", message, utcnow()


def _crawl_progress(prog: Progress, n: int, mx: int, url: str | None, found: int | None) -> None:
    found = max(found or n, n, 1)
    prog.note(current=url, pages_found=found)
    # the bar follows pages read out of pages found so far, so small sites fill quickly instead of crawling to a limit
    prog.set("crawling", int(5 + 55 * n / min(found, mx)), pages=n)


def _is_partial(crawl: CrawlResult) -> bool:
    r = crawl.stopped_reason or ""
    return bool(crawl.overflow) or r.startswith("page limit") or r.startswith("time limit")


async def _run(url: str, prog: Progress, max_pages: int | None = None) -> tuple[list[Finding], list[PageDoc], CrawlResult]:
    s = config.settings
    pro = bool(max_pages and max_pages > s.max_pages)
    budget = s.pro_scan_seconds if pro else s.max_scan_seconds
    deadline = time.monotonic() + budget * 0.6
    crawler = Crawler(url, max_pages=max_pages, deadline=deadline,
                      on_progress=lambda n, mx, url=None, found=None: _crawl_progress(prog, n, mx, url, found))
    speed_task = asyncio.create_task(speed.measure(url))  # Google measures while we crawl
    crawl = await crawler.run()
    prog.set("analysing content", 62, pages=len(crawl.pages), force=True)

    if crawl.stopped_reason.startswith("robots"):
        raise ScanError("This site's robots.txt asks crawlers not to read these pages, so we didn't scan it.")
    ok_pages = [p for p in crawl.pages.values() if p.extracted and p.status < 400
                and not NON_DOC_EXT.search(urlsplit(p.url).path)]
    if not ok_pages:
        statuses = {p.status for p in crawl.pages.values()}
        if statuses and all(st == 0 for st in statuses):
            err = next((p.error for p in crawl.pages.values() if p.error), "")
            if err and ("private" in err.lower() or "resolve" in err.lower() or "blocked" in err.lower()):
                raise ScanError(err)
            raise ScanError("We couldn't connect to this site. Check the URL and try again.")
        raise ScanError("We couldn't read any pages. The site may block bots or render its docs with JavaScript only.")

    if len(ok_pages) < 2:
        raise ScanError("We could only read one page at this address, so there is nothing to compare. "
                        "Try the main docs or help-center URL (for example https://example.com/docs/).")

    # ---- page docs with inbound link counts
    inbound = {u: len({src for src in sources if src != u}) for u, sources in crawl.link_sources.items()}
    pages = [PageDoc(url=p.url, title=p.extracted.title, blocks=p.extracted.blocks,
                     word_count=p.extracted.word_count, link_count=len(p.extracted.links),
                     from_sitemap=p.from_sitemap, inbound=inbound.get(p.url, 0),
                     last_modified=p.extracted.last_modified, lang=p.extracted.lang)
             for p in ok_pages]
    pages.sort(key=lambda p: p.url)
    findings: list[Finding] = []

    # ---- duplicates & similarity
    dup_findings, dup_page_pairs = check_duplicate_pages(pages)
    findings += dup_findings
    chunks = build_chunks(pages, max_chunks=s.max_chunks)
    band = embeddings.active_band()
    pairs = []
    if len(chunks) >= 2:
        matrix = await asyncio.to_thread(embeddings.embed, [f"{c.heading}\n{c.text}" for c in chunks])
        pairs = await asyncio.to_thread(top_pairs, matrix, [c.page for c in chunks],
                                        lo=band["semantic_lo"], hi=1.01, k=4)
        pairs = [pc for pc in pairs if not different_languages(pages[chunks[pc.a].page], pages[chunks[pc.b].page])]
    findings += check_duplicate_sections(chunks, pages, pairs, dup_page_pairs, threshold=band["dup"])

    # ---- contradictions
    facts = extract_facts(chunks)
    numeric = numeric_conflicts(facts, pages=pages)
    prog.set("checking links", 72, force=True)

    # ---- links (runs while nothing else is pending; cheap)
    findings += await _link_findings(crawl, crawler, prog, pro=pro)
    prog.set("verifying contradictions" if s.llm_enabled else "scoring", 85, force=True)

    if s.llm_enabled:
        findings += await _llm_contradictions(numeric, pairs, chunks, pages, band)
    else:
        findings += [numeric_finding(a, b, pages, verified=False) for a, b, _ in numeric[:25]]

    # ---- staleness & structure
    findings += check_staleness(pages)
    findings += check_thin_pages(pages)
    findings += check_orphans(pages, crawl.start_url, partial=_is_partial(crawl))
    prog.set("saving report", 96, force=True)
    try:  # it ran while we crawled, so this usually returns at once
        crawl.speed = await asyncio.wait_for(speed_task, timeout=45)
    except Exception:
        speed_task.cancel()
        crawl.speed = None
    return findings, pages, crawl


def _contexts(crawl: CrawlResult, target: str, limit: int = 8) -> tuple[list[dict], int, bool]:
    """Where a link sits: [{page, text, region, heading}], total sources, and whether it is a template link."""
    ctx = crawl.link_ctx.get(target, {})
    sources = sorted(crawl.link_sources.get(target, set()) | set(ctx))
    rows = []
    for src in sources[:limit]:
        c = ctx.get(src, {})
        rows.append({"page": src, "text": c.get("text", ""), "region": c.get("region", "content"),
                     "heading": c.get("heading", "")})
    regions = [ctx[x].get("region", "content") for x in ctx]
    site_wide = len(sources) >= 5 and bool(regions) and sum(r != "content" for r in regions) / len(regions) >= 0.6
    return rows, len(sources), site_wide


def _finding_for(crawl: CrawlResult, url: str, status: int, internal: bool) -> Finding:
    rows, total, site_wide = _contexts(crawl, url)
    label = f"HTTP {status}" if status else "unreachable"
    where = ""
    if site_wide:
        where = f" It sits in the site menu, header or footer, so it appears on {total} pages: fix it once."
    elif total:
        where = f" Linked from {total} page(s)."
    return Finding(
        type="broken_link", severity="medium" if internal and status else "low",
        title=f"Broken {'internal' if internal else 'external'} link ({label})",
        detail=f"Returns {label}.{where}".strip(),
        page_a=rows[0]["page"] if rows else "", page_b=url,
        extra={"status": status, "internal": internal, "sources": [r["page"] for r in rows],
               "contexts": rows, "sources_total": total, "site_wide": site_wide,
               "region": rows[0]["region"] if rows else "content"},
        points=(2.0 if internal else 0.5) if status else 0.3)


async def _link_findings(crawl: CrawlResult, crawler: Crawler, prog: Progress, pro: bool = False) -> list[Finding]:
    s = config.settings
    out: list[Finding] = []
    site_host = crawler.host

    # Internal pages we crawled that came back broken
    for url, page in crawl.pages.items():
        if admin_target_kind(url, site_host) or INFRA_PATH.match(urlsplit(url).path or "/"):
            continue
        if page.status in (404, 410) or page.status >= 500:
            if crawl.link_sources.get(url):
                out.append(_finding_for(crawl, url, page.status, True))

    # Links that leave the crawl scope, and links to pages we saw but did not crawl
    same_site, external, admin = [], [], {}
    candidates = set(crawl.out_of_scope_links) | set(crawl.overflow)
    for u in sorted(candidates):
        if INFRA_PATH.match(urlsplit(u).path or "/"):
            continue
        kind = admin_target_kind(u, site_host)
        if kind:
            admin[u] = kind
            continue
        host = urlsplit(u).hostname or ""
        if is_bot_hostile(host):
            crawl.links_skipped = getattr(crawl, "links_skipped", 0) + 1
            continue
        (same_site if _bare_host(host) == _bare_host(site_host) else external).append(u)

    for u, kind in admin.items():
        rows, total, site_wide = _contexts(crawl, u)
        out.append(Finding(
            type="admin_link", severity="medium", title=f"Link to an admin or draft page ({kind})",
            detail="Visitors cannot open this page (it needs a login, or it points at a draft or test site)."
                   + (f" It is in the site menu, header or footer, so it shows on {total} pages." if site_wide else ""),
            page_a=rows[0]["page"] if rows else "", page_b=u,
            extra={"kind": kind, "contexts": rows, "sources_total": total, "site_wide": site_wide,
                   "sources": [r["page"] for r in rows], "region": rows[0]["region"] if rows else "content"},
            points=1.0))

    limit = s.pro_max_link_checks if pro else s.max_link_checks
    targets = sorted(same_site) + (sorted(external) if s.check_external_links else [])
    crawl.links_skipped = getattr(crawl, "links_skipped", 0) + max(0, len(targets) - limit)
    if targets:
        total = min(len(targets), limit)
        prog.note(links_total=total, links_done=0)

        def link_progress(done: int, tot: int, u: str) -> None:
            prog.note(current=u, links_done=done)
            prog.set("checking links", 72 + int(12 * done / max(1, tot)))

        statuses = await check_links(targets, limit=limit, timeout_s=s.link_check_seconds, on_progress=link_progress)
        crawl.links_checked = len(statuses)
        same = set(same_site)
        for u, st in statuses.items():
            if not (st in (404, 410) or st >= 500 or st == 0):
                continue
            out.append(_finding_for(crawl, u, st, u in same))
    return out


async def _llm_contradictions(numeric, pairs, chunks, pages, band) -> list[Finding]:
    s = config.settings
    budget = s.llm_max_pairs
    jobs: list[tuple[str, object, tuple[dict, dict]]] = []

    def payload(ci: int) -> dict:
        c = chunks[ci]
        return {"url": pages[c.page].url, "heading": c.heading, "text": c.text}

    covered: set[tuple[int, int]] = set()
    for fa, fb, _ in numeric[:budget]:
        jobs.append(("numeric", (fa, fb), (payload(fa.chunk), payload(fb.chunk))))
        covered.add((min(fa.page, fb.page), max(fa.page, fb.page)))
    for pc in pairs:
        if len(jobs) >= budget:
            break
        if pc.sim >= band["dup"]:
            continue
        a, b = chunks[pc.a], chunks[pc.b]
        key = (min(a.page, b.page), max(a.page, b.page))
        if key in covered:
            continue
        covered.add(key)
        jobs.append(("semantic", pc, (payload(pc.a), payload(pc.b))))

    verdicts = await llm.judge_many([j[2] for j in jobs])
    out: list[Finding] = []
    for (kind, ref, (pa, pb)), v in zip(jobs, verdicts):
        if v is None:
            if kind == "numeric":  # LLM unavailable for this pair: keep the unverified signal
                fa, fb = ref
                out.append(numeric_finding(fa, fb, pages, verified=False))
            continue
        if not v.contradiction or v.confidence < s.llm_min_confidence:
            continue
        out.append(Finding(
            type="contradiction", severity="high",
            title=_short(v.explanation or "These pages contradict each other", 140),
            detail="Verified by an AI review: both quotes appear on the pages and cannot both be true.",
            page_a=pa["url"], page_b=pb["url"], quote_a=v.claim_a, quote_b=v.claim_b,
            extra={"confidence": round(v.confidence, 2), "verified": True, "method": kind}, points=6.0))
    return out


def _save(scan_id: str, crawl: CrawlResult, pages: list[PageDoc], findings: list[Finding],
          value: float, grade: str, summary: dict) -> None:
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), -f.points))
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None:
            return
        statuses = {u: p.status for u, p in crawl.pages.items()}
        for p in pages:
            s.add(Page(scan_id=scan_id, url=p.url[:2048], title=(p.title or "")[:500], status_code=statuses.get(p.url, 200),
                       word_count=p.word_count, inbound_count=p.inbound, from_sitemap=p.from_sitemap,
                       last_modified=p.last_modified))
        for f in findings:
            s.add(Issue(scan_id=scan_id, type=f.type, severity=f.severity, title=f.title[:500], detail=f.detail,
                        page_a=f.page_a[:2048], page_b=f.page_b[:2048], quote_a=f.quote_a[:2000],
                        quote_b=f.quote_b[:2000], extra=f.extra, points=f.points))
        scan.root = crawl.root
        scan.pages_crawled = len(pages)
        scan.score, scan.grade, scan.summary = value, grade, summary
        scan.status, scan.stage, scan.progress, scan.finished_at = "done", "done", 100, utcnow()
