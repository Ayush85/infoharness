"""Polite, bounded, SSRF-safe async crawler for documentation sites."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from app import config
from app import render
from app.extract import Extracted, extract
from app.security import UnsafeURL, check_url_safe

log = logging.getLogger(__name__)

SKIP_EXT = re.compile(
    r"\.(pdf|png|jpe?g|gif|webp|svg|ico|zip|gz|tgz|tar|rar|7z|mp4|mp3|wav|mov|avi|woff2?|ttf|eot|css|js|json|"
    r"xml|rss|atom|csv|xlsx?|docx?|pptx?|dmg|exe|apk|bin|iso)$", re.I)
TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|ref_src|gclid|fbclid|mc_cid|mc_eid|_hsenc|_hsmi)$", re.I)
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


def normalize(url: str) -> str:
    """Canonical form used for de-duplication."""
    p = urlsplit(url)
    host = (p.hostname or "").lower().rstrip(".")
    port = p.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    path = re.sub(r"/index\.html?$", "/", p.path or "/", flags=re.I)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((p.scheme.lower(), netloc, path, urlencode(q), ""))


def _bare_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str = ""
    text: str | None = None
    last_modified: str | None = None
    error: str | None = None


@dataclass
class CrawledPage:
    url: str
    status: int
    extracted: Extracted | None = None
    from_sitemap: bool = False
    error: str | None = None
    rendered: bool = False


@dataclass
class CrawlResult:
    start_url: str
    root: str
    pages: dict[str, CrawledPage] = field(default_factory=dict)
    # normalized target url -> set of normalized source page urls
    link_sources: dict[str, set[str]] = field(default_factory=dict)
    out_of_scope_links: set[str] = field(default_factory=set)
    # target url -> source page url -> {"text", "region", "heading"} (capped per target)
    link_ctx: dict[str, dict[str, dict]] = field(default_factory=dict)
    # in-scope pages we found but did not read because of the page limit
    overflow: set[str] = field(default_factory=set)
    sitemap_urls: set[str] = field(default_factory=set)
    stopped_reason: str = ""
    links_checked: int = 0
    links_skipped: int = 0


class Crawler:
    def __init__(self, start_url: str, *, max_pages: int | None = None, deadline: float | None = None,
                 on_progress=None, client: httpx.AsyncClient | None = None):
        s = config.settings
        self._set_scope(start_url)
        self.max_pages = max_pages or s.max_pages
        self.deadline = deadline or (time.monotonic() + s.max_scan_seconds * 0.6)
        self.on_progress = on_progress
        self._client = client
        self.robots: RobotFileParser | None = None
        self.result = CrawlResult(start_url=self.start_url, root=self.root)
        self._queued: set[str] = set()
        self._renderer: render.Renderer | None = render.Renderer(config.settings.user_agent) if render.available() else None
        self._sem = asyncio.Semaphore(s.crawl_concurrency)

    def _set_scope(self, url: str) -> None:
        """Derive host and folder prefix from the start URL (also used after a redirect moves the site)."""
        self.start_url = normalize(url)
        sp = urlsplit(self.start_url)
        self.scheme = sp.scheme
        self.host = sp.hostname or ""
        self.netloc = sp.netloc
        path = urlsplit(url).path or "/"
        if path.endswith("/"):
            self.prefix = path
        else:
            last = path.rsplit("/", 1)[-1]
            # "/docs" or "/hc/en-us" look like folders; "/help/article-12" or "/a/page.html" look like pages
            looks_like_folder = "." not in last and not re.search(r"\d", last)
            self.prefix = path + "/" if looks_like_folder else path.rsplit("/", 1)[0] + "/"
        self.root = f"{self.scheme}://{self.netloc}{self.prefix}"

    # ---------- scope ----------
    def in_scope(self, url: str) -> bool:
        p = urlsplit(url)
        if p.scheme not in ("http", "https"):
            return False
        if _bare_host(p.hostname or "") != _bare_host(self.host) or (p.port or None) != (urlsplit(self.start_url).port or None):
            return False
        path = p.path or "/"
        if not (path.startswith(self.prefix) or path + "/" == self.prefix):
            return False
        return not SKIP_EXT.search(path)

    # ---------- HTTP ----------
    async def fetch(self, url: str, *, method: str = "GET", want_body: bool = True) -> FetchResult:
        s = config.settings
        current = url
        for _ in range(6):
            try:
                await asyncio.to_thread(check_url_safe, current)
            except UnsafeURL as e:
                return FetchResult(url, current, 0, error=str(e))
            try:
                req = self._client.build_request(method, current)
                resp = await self._client.send(req, stream=True, follow_redirects=False)
            except httpx.HTTPError as e:
                return FetchResult(url, current, 0, error=type(e).__name__)
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue
                ctype = resp.headers.get("content-type", "").lower()
                res = FetchResult(url, current, resp.status_code, content_type=ctype,
                                  last_modified=resp.headers.get("last-modified"))
                if want_body and method == "GET" and resp.status_code < 400 and ("html" in ctype or "xml" in ctype or not ctype or "text/plain" in ctype):
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > s.max_page_bytes:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    enc = resp.encoding or "utf-8"
                    try:
                        res.text = raw.decode(enc, errors="replace")
                    except LookupError:
                        res.text = raw.decode("utf-8", errors="replace")
                return res
            finally:
                await resp.aclose()
        return FetchResult(url, current, 0, error="Too many redirects")

    # ---------- robots & sitemaps ----------
    async def _load_robots(self) -> list[str]:
        sitemaps: list[str] = []
        res = await self.fetch(f"{self.scheme}://{self.netloc}/robots.txt")
        rp = RobotFileParser()
        if res.status == 200 and res.text:
            lines = res.text.splitlines()
            rp.parse(lines)
            for line in lines:
                if line.lower().startswith("sitemap:"):
                    sitemaps.append(line.split(":", 1)[1].strip())
        else:
            rp.parse([])
        self.robots = rp
        return sitemaps

    def allowed(self, url: str) -> bool:
        if self.robots is None:
            return True
        return self.robots.can_fetch(config.settings.user_agent, url) and self.robots.can_fetch("*", url)

    async def _load_sitemaps(self, declared: list[str]) -> list[str]:
        candidates = list(dict.fromkeys(declared + [f"{self.scheme}://{self.netloc}/sitemap.xml",
                                                     f"{self.root}sitemap.xml"]))
        found: list[str] = []
        visited = 0
        while candidates and visited < 12 and len(found) < self.max_pages * 3:
            sm = candidates.pop(0)
            visited += 1
            if sm.endswith(".gz"):
                continue
            res = await self.fetch(sm)
            if res.status != 200 or not res.text:
                continue
            locs = [l.strip() for l in LOC_RE.findall(res.text)]
            if "<sitemapindex" in res.text.lower():
                candidates.extend(l for l in locs if l not in candidates)
                continue
            for loc in locs:
                n = normalize(loc.replace("&amp;", "&"))
                if self.in_scope(n):
                    found.append(n)
        return list(dict.fromkeys(found))

    # ---------- main loop ----------
    async def run(self) -> CrawlResult:
        s = config.settings
        own_client = self._client is None
        if own_client:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(s.crawl_timeout, connect=min(10.0, s.crawl_timeout)),
                headers={"User-Agent": s.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
                limits=httpx.Limits(max_connections=s.crawl_concurrency * 2),
            )
        try:
            # Vanity domains often redirect to the real docs host (docs.python-requests.org -> requests.readthedocs.io).
            # Follow that once, so the scan covers the real site instead of stopping at the first page.
            first = await self.fetch(self.start_url, want_body=False)
            if first.status and first.status < 400 and not first.error:
                moved = normalize(first.final_url)
                mp = urlsplit(moved)
                if _bare_host(mp.hostname or "") != _bare_host(self.host) or not (mp.path or "/").startswith(self.prefix):
                    self._set_scope(first.final_url)
                    self.result.start_url, self.result.root = self.start_url, self.root
            declared = await self._load_robots()
            if not self.allowed(self.start_url):
                self.result.stopped_reason = "robots.txt disallows crawling this URL"
                return self.result
            sitemap_urls = await self._load_sitemaps(declared)
            self.result.sitemap_urls = set(sitemap_urls)

            queue: asyncio.Queue[str] = asyncio.Queue()
            self._enqueue(queue, self.start_url)
            for u in sitemap_urls:
                self._enqueue(queue, u)

            workers = [asyncio.create_task(self._worker(queue)) for _ in range(s.crawl_concurrency)]
            await queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if time.monotonic() > self.deadline:
                self.result.stopped_reason = "time limit reached"
            elif len(self.result.pages) >= self.max_pages:
                self.result.stopped_reason = f"page limit ({self.max_pages}) reached"
            return self.result
        finally:
            if self._renderer:
                await self._renderer.close()
            if own_client:
                await self._client.aclose()

    def _enqueue(self, queue: asyncio.Queue, url: str) -> None:
        n = normalize(url)
        if n in self._queued:
            return
        if not self.in_scope(n) or not self.allowed(n):
            return
        if len(self._queued) >= self.max_pages:
            self.result.overflow.add(n)  # found, but over the page limit: counted so the report can say so
            return
        self._queued.add(n)
        queue.put_nowait(n)

    async def _worker(self, queue: asyncio.Queue) -> None:
        while True:
            url = await queue.get()
            try:
                if time.monotonic() > self.deadline:
                    continue
                async with self._sem:
                    res = await self.fetch(url)
                page = CrawledPage(url=url, status=res.status, from_sitemap=url in self.result.sitemap_urls,
                                   error=res.error)
                final_n = normalize(res.final_url)
                if res.text and res.status < 400 and ("html" in res.content_type or "xml" not in res.content_type):
                    try:
                        page.extracted = extract(res.text, res.final_url, res.last_modified)
                        if self._renderer and render.needs_render(res.text, page.extracted.word_count):
                            out = await self._renderer.render(res.final_url)
                            if out:
                                rendered = extract(out[0], out[1], res.last_modified)
                                if rendered.word_count > page.extracted.word_count:
                                    page.extracted = rendered
                                    page.rendered = True
                    except Exception as e:  # malformed HTML should never kill a scan
                        log.warning("extract failed for %s: %s", url, e)
                        page.error = "parse error"
                # A redirect to an already-crawled page is a duplicate, not a new page.
                if final_n != url and final_n in self.result.pages:
                    continue
                self.result.pages[url] = page
                if page.extracted:
                    for link in page.extracted.links:
                        ln = normalize(link)
                        if ln == url:
                            continue
                        self.result.link_sources.setdefault(ln, set()).add(url)
                        ctx = page.extracted.link_ctx.get(link)
                        if ctx:
                            per = self.result.link_ctx.setdefault(ln, {})
                            if len(per) < 60:
                                per[url] = ctx
                        if self.in_scope(ln):
                            self._enqueue(queue, ln)
                        else:
                            self.result.out_of_scope_links.add(ln)
                if self.on_progress:
                    self.on_progress(len(self.result.pages), self.max_pages, url, len(self._queued))
            finally:
                queue.task_done()


# Big platforms answer automated requests with errors (403, 404, 429, 999) even for working pages,
# so a failure from them says nothing about the link. We don't report these as broken.
BOT_HOSTILE = ("google.", "goo.gl", "g.co", "facebook.com", "fb.com", "fb.me", "instagram.com", "linkedin.com", "lnkd.in",
               "twitter.com", "x.com", "t.co", "tiktok.com", "pinterest.", "whatsapp.com", "wa.me", "snapchat.com",
               "amazon.", "amzn.to", "medium.com", "reddit.com", "quora.com", "t.me", "telegram.", "viber.com")


def is_bot_hostile(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == d.rstrip(".") or h.endswith("." + d.rstrip(".")) or (d.endswith(".") and (h.startswith(d) or ("." + d) in h))
               for d in BOT_HOSTILE)


async def check_links(urls: list[str], *, limit: int, client: httpx.AsyncClient | None = None,
                      timeout_s: float | None = None, on_progress=None) -> dict[str, int]:
    """HEAD (then GET) each URL; returns url -> status (0 = network error). 403/429 are reported as-is.
    Two requests per host at a time, one retry for errors that are often momentary, and an overall time budget."""
    s = config.settings
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers={"User-Agent": s.user_agent})
    crawler = Crawler.__new__(Crawler)
    crawler._client = client
    sem = asyncio.Semaphore(8)
    per_host: dict[str, asyncio.Semaphore] = {}
    results: dict[str, int] = {}
    refused = ("Blocked", "Private", "This address", "Could not resolve")

    async def probe(u: str):
        r = await crawler.fetch(u, method="HEAD", want_body=False)
        if r.status in (0, 403, 405, 501) and not (r.error or "").startswith(refused):
            r = await crawler.fetch(u, method="GET", want_body=False)
        return r

    async def one(u: str):
        host = urlsplit(u).hostname or ""
        async with sem, per_host.setdefault(host, asyncio.Semaphore(2)):
            r = await probe(u)
            if (r.status == 0 or r.status >= 500) and not (r.error or "").startswith(refused):
                await asyncio.sleep(1.5)  # one retry: timeouts and 5xx are often momentary
                r = await probe(u)
            if r.error and r.status == 0 and ("private" in r.error.lower() or "blocked" in r.error.lower()):
                return  # never report internal-network URLs
            results[u] = r.status
            if on_progress:
                on_progress(len(results), min(len(urls), limit), u)

    tasks = [asyncio.create_task(one(u)) for u in urls[:limit]]
    try:
        await asyncio.wait(tasks, timeout=timeout_s)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if own:
            await client.aclose()
    return results
