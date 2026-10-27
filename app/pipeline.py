"""Orchestrates one scan: crawl -> analyse -> check links -> (LLM verify) -> score -> save."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from app import config, embeddings, llm
from app.analysis import (Finding, PageDoc, build_chunks, check_duplicate_pages, check_duplicate_sections,
                          check_orphans, check_staleness, check_thin_pages, extract_facts, numeric_conflicts,
                          numeric_finding, top_pairs, _short)
from app.crawler import CrawlResult, Crawler, _bare_host, check_links
from app.db import Issue, Page, Scan, session_scope, utcnow
from app.scoring import score

log = logging.getLogger(__name__)


class Progress:
    """Throttled progress writer so the UI can poll without hammering the DB."""

    def __init__(self, scan_id: str):
        self.scan_id = scan_id
        self._last = 0.0

    def set(self, stage: str, pct: int, pages: int | None = None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 1.0:
            return
        self._last = now
        with session_scope() as s:
            scan = s.get(Scan, self.scan_id)
            if scan:
                scan.stage = stage
                scan.progress = max(scan.progress or 0, min(99, pct))
                if pages is not None:
                    scan.pages_crawled = pages


def run_scan(scan_id: str) -> None:
    started = time.monotonic()
    with session_scope() as s:
        scan = s.get(Scan, scan_id)
        if scan is None:
            return
        url = scan.url
        scan.status, scan.stage, scan.started_at, scan.progress = "running", "crawling", utcnow(), 1
    prog = Progress(scan_id)
    try:
        findings, pages, crawl = asyncio.run(_run(url, prog))
        value, grade, summary = score(findings, max(1, len(pages)))
        summary.update({
            "stopped_reason": crawl.stopped_reason,
            "llm_verified": config.settings.llm_enabled,
            "embeddings": embeddings.provider(),
            "duration_s": round(time.monotonic() - started, 1),
            "pages_total": len(crawl.pages),
        })
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


async def _run(url: str, prog: Progress) -> tuple[list[Finding], list[PageDoc], CrawlResult]:
    s = config.settings
    deadline = time.monotonic() + s.max_scan_seconds * 0.6
    crawler = Crawler(url, deadline=deadline,
                      on_progress=lambda n, mx: prog.set("crawling", int(5 + 55 * n / max(1, mx)), pages=n))
    crawl = await crawler.run()
    prog.set("analysing content", 62, pages=len(crawl.pages), force=True)

    if crawl.stopped_reason.startswith("robots"):
        raise ScanError("This site's robots.txt asks crawlers not to read these pages, so we didn't scan it.")
    ok_pages = [p for p in crawl.pages.values() if p.extracted and p.status < 400]
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
                     last_modified=p.extracted.last_modified)
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
    findings += check_duplicate_sections(chunks, pages, pairs, dup_page_pairs, threshold=band["dup"])

    # ---- contradictions
    facts = extract_facts(chunks)
    numeric = numeric_conflicts(facts)
    prog.set("checking links", 72, force=True)

    # ---- links (runs while nothing else is pending; cheap)
    findings += await _link_findings(crawl, crawler)
    prog.set("verifying contradictions" if s.llm_enabled else "scoring", 85, force=True)

    if s.llm_enabled:
        findings += await _llm_contradictions(numeric, pairs, chunks, pages, band)
    else:
        findings += [numeric_finding(a, b, pages, verified=False) for a, b, _ in numeric[:25]]

    # ---- staleness & structure
    findings += check_staleness(pages)
    findings += check_thin_pages(pages)
    findings += check_orphans(pages, crawl.start_url)
    prog.set("saving report", 96, force=True)
    return findings, pages, crawl


async def _link_findings(crawl: CrawlResult, crawler: Crawler) -> list[Finding]:
    s = config.settings
    out: list[Finding] = []
    # Internal pages we tried to crawl that came back broken
    for url, page in crawl.pages.items():
        if page.status in (404, 410) or page.status >= 500:
            sources = sorted(crawl.link_sources.get(url, set()))
            if not sources:
                continue
            out.append(Finding(
                type="broken_link", severity="medium", title=f"Broken link ({page.status})",
                detail=f"Linked from {len(sources)} page(s) but returns HTTP {page.status}.",
                page_a=sources[0], page_b=url, extra={"status": page.status, "sources": sources[:10], "internal": True},
                points=2.0))

    # Links leaving the crawl scope (same site outside the docs folder, or other sites)
    same_site, external = [], []
    for u in crawl.out_of_scope_links:
        host = urlsplit(u).hostname or ""
        (same_site if _bare_host(host) == _bare_host(crawler.host) else external).append(u)
    targets = sorted(same_site) + (sorted(external) if s.check_external_links else [])
    if targets:
        statuses = await check_links(targets, limit=s.max_link_checks)
        for u, st in statuses.items():
            dead = st in (404, 410) or st >= 500 or st == 0
            if not dead:
                continue
            internal = u in same_site
            sources = sorted(crawl.link_sources.get(u, set()))
            label = f"HTTP {st}" if st else "unreachable"
            out.append(Finding(
                type="broken_link", severity="medium" if internal and st else "low",
                title=f"Broken {'internal' if internal else 'external'} link ({label})",
                detail=f"Linked from {len(sources)} page(s).",
                page_a=sources[0] if sources else "", page_b=u,
                extra={"status": st, "sources": sources[:10], "internal": internal},
                points=(2.0 if internal else 0.5) if st else 0.3))
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
