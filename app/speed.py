"""Page speed and Core Web Vitals through Google's PageSpeed Insights API (free; an API key raises the quota).

Measured for the address that was scanned, on mobile and on desktop. Not part of the grade: it is shown next to it.
Never raises: a failed or rate-limited measurement just means the report has no speed card.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

from app import config

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CATEGORIES = (("performance", "Performance"), ("accessibility", "Accessibility"),
              ("best-practices", "Best practices"), ("seo", "SEO"))
# (audit id, label, good threshold, poor threshold, unit); values are raw numbers from Lighthouse
VITALS = (("largest-contentful-paint", "Largest Contentful Paint", 2500, 4000, "ms"),
          ("cumulative-layout-shift", "Layout shift (CLS)", 0.1, 0.25, ""),
          ("total-blocking-time", "Total Blocking Time", 200, 600, "ms"),
          ("first-contentful-paint", "First Contentful Paint", 1800, 3000, "ms"),
          ("speed-index", "Speed Index", 3400, 5800, "ms"))


def rating(score: float | None) -> str:
    if score is None:
        return "unknown"
    return "good" if score >= 90 else "ok" if score >= 50 else "poor"


def _fmt(value: float, unit: str) -> str:
    if unit == "ms":
        return f"{value / 1000:.1f} s" if value >= 1000 else f"{round(value)} ms"
    return f"{value:.2f}"


def parse(data: dict) -> dict | None:
    lh = data.get("lighthouseResult") or {}
    cats = lh.get("categories") or {}
    audits = lh.get("audits") or {}
    if not cats:
        return None
    scores = []
    for key, label in CATEGORIES:
        raw = (cats.get(key) or {}).get("score")
        sc = None if raw is None else round(raw * 100)
        scores.append({"key": key, "label": label, "score": sc, "rating": rating(sc)})
    vitals = []
    for aid, label, good, poor, unit in VITALS:
        a = audits.get(aid) or {}
        v = a.get("numericValue")
        if v is None:
            continue
        vitals.append({"label": label, "value": _fmt(v, unit),
                       "rating": "good" if v <= good else "ok" if v <= poor else "poor"})
    opps = []
    for a in audits.values():
        d = a.get("details") or {}
        saving = d.get("overallSavingsMs") or 0
        if d.get("type") == "opportunity" and saving >= 100 and (a.get("score") or 0) < 0.9:
            opps.append({"title": a.get("title", ""), "saving_ms": round(saving),
                         "saving": _fmt(saving, "ms")})
    opps.sort(key=lambda o: -o["saving_ms"])
    field = None
    le = (data.get("loadingExperience") or {})
    if le.get("overall_category"):
        field = {"category": le["overall_category"].lower()}  # FAST / AVERAGE / SLOW from real Chrome users
    return {"scores": scores, "vitals": vitals, "opportunities": opps[:4], "field": field}


async def _one(client: httpx.AsyncClient, url: str, strategy: str) -> dict | None:
    s = config.settings
    params = [("url", url), ("strategy", strategy), ("locale", "en")] + [("category", k) for k, _ in CATEGORIES]
    if s.pagespeed_api_key:
        params.append(("key", s.pagespeed_api_key))
    try:
        r = await client.get(API, params=params)
        if r.status_code == 429:
            log.info("pagespeed quota reached")
            return {"error": "quota"}
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                pass
            log.info("pagespeed %s for %s: %s", r.status_code, url, msg[:120])
            return {"error": "failed"}
        return parse(r.json())
    except (httpx.HTTPError, ValueError) as e:
        log.info("pagespeed error for %s: %s", url, type(e).__name__)
        return {"error": "failed"}


async def google_measure(url: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """Google's numbers (optional, needs PAGESPEED_API_KEY): {"mobile","desktop","report"} or None."""
    s = config.settings
    if not s.pagespeed_enabled or not s.pagespeed_api_key:
        return None
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=httpx.Timeout(s.pagespeed_timeout))
    try:
        mobile, desktop = await asyncio.gather(_one(client, url, "mobile"), _one(client, url, "desktop"))
    except Exception:  # never let speed testing break a scan
        log.exception("pagespeed failed")
        return None
    finally:
        if own:
            await client.aclose()
    ok = {k: v for k, v in (("mobile", mobile), ("desktop", desktop)) if v and "error" not in v}
    if not ok:
        return None
    return {**ok, "report": f"https://pagespeed.web.dev/analysis?url={quote(url, safe='')}"}


# ====================================================================== our own measurement
# Lab numbers taken from our server with no throttling, so they are best-case. They are fair for comparing
# one page with another, and for catching the common causes of a slow page. Real-user numbers need Google's data.
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True,
         "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Mobile Safari/537.36 InfoharnessSpeed/1.0"}
DESKTOP = {"viewport": {"width": 1366, "height": 800},
           "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36 InfoharnessSpeed/1.0"}
PERF_JS = """() => new Promise(resolve => {
  const out = {lcp: 0, cls: 0, fcp: 0};
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) out.lcp = e.startTime; }).observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) out.cls += e.value; }).observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  setTimeout(() => {
    const n = performance.getEntriesByType('navigation')[0] || {};
    const fcp = performance.getEntriesByName('first-contentful-paint')[0];
    const imgs = [...document.images].filter(i => i.complete && i.naturalWidth);
    resolve({lcp: out.lcp, cls: out.cls, fcp: fcp ? fcp.startTime : 0, ttfb: n.responseStart || 0,
             load: n.loadEventEnd || 0, proto: n.nextHopProtocol || '', dcl: n.domContentLoadedEventEnd || 0,
             oversized: imgs.filter(i => i.naturalWidth > i.clientWidth * 2.2 && i.clientWidth > 0).length,
             noDims: [...document.images].filter(i => !(i.getAttribute('width') && i.getAttribute('height')) && !i.style.aspectRatio).length,
             images: document.images.length});
  }, 1200);
})"""


def _rate(v: float, good: float, poor: float) -> str:
    return "good" if v <= good else "ok" if v <= poor else "poor"


def _kb(n: float) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{round(n / 1000)} KB"


def html_checks(html: str, headers: dict, http_ver: str, size: int, ttfb_ms: float | None, redirects: int) -> list[dict]:
    """Things you can fix, read from the page's own HTML and headers. Each: {"label","status","detail"}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")
    out = []

    def add(label, status, detail):
        out.append({"label": label, "status": status, "detail": detail})

    enc = (headers.get("content-encoding") or "").lower()
    if enc in ("br", "gzip", "zstd", "deflate"):
        add("Text compression", "good", f"The page is sent compressed ({enc}).")
    elif size > 5000:
        add("Text compression", "poor", f"The page ({_kb(size)}) is sent uncompressed. Turn on gzip or brotli on the server or CDN.")
    cc = (headers.get("cache-control") or "").lower()
    if "no-store" in cc or "max-age=0" in cc or not cc:
        add("Page caching", "ok", "The page itself isn't cached. Fine for docs that change often, but a short cache or a CDN makes repeat visits faster.")
    else:
        add("Page caching", "good", "The page sends cache headers.")
    if http_ver and http_ver.startswith("HTTP/1"):
        add("HTTP version", "ok", f"Served over {http_ver}. HTTP/2 or HTTP/3 loads many files faster.")
    elif http_ver:
        add("HTTP version", "good", f"Served over {http_ver}.")
    if redirects > 1:
        add("Redirects", "poor", f"{redirects} redirects before the page loads. Link to the final address.")
    elif redirects == 1:
        add("Redirects", "ok", "One redirect before the page loads (for example http to https, or www). Link to the final address where you can.")
    if ttfb_ms is not None:
        add("Server response", _rate(ttfb_ms, 600, 1500), f"The first byte arrived after {_fmt(ttfb_ms, 'ms')}."
            + ("" if ttfb_ms <= 600 else " Slow servers delay everything else. Look at caching, hosting and database queries."))
    head = soup.head or soup
    blocking = [sc for sc in head.find_all("script", src=True) if not sc.has_attr("async") and not sc.has_attr("defer")
                and (sc.get("type") or "").lower() != "module"]
    if blocking:
        add("Render-blocking scripts", "poor" if len(blocking) > 2 else "ok",
            f"{len(blocking)} script{'s' if len(blocking) != 1 else ''} in the head stop the page from showing until they load. Add defer or async.")
    else:
        add("Render-blocking scripts", "good", "No blocking scripts in the head.")
    if not soup.find("meta", attrs={"name": "viewport"}):
        add("Mobile viewport", "poor", "No viewport tag, so phones show a shrunken desktop page.")
    imgs = soup.find_all("img")
    nodim = [i for i in imgs if not (i.get("width") and i.get("height"))]
    if imgs and nodim:
        add("Image sizes set", "ok" if len(nodim) < 4 else "poor",
            f"{len(nodim)} of {len(imgs)} images have no width and height, so the page jumps as they load.")
    lazy = [i for i in imgs[3:] if i.get("loading") != "lazy"]
    if len(imgs) > 6 and len(lazy) > 4:
        add("Lazy-loaded images", "ok", f"{len(lazy)} images below the top load immediately. Add loading=\"lazy\".")
    if size > 300_000:
        add("Page size", "poor", f"The HTML alone is {_kb(size)}. Large pages are slow to parse.")
    return out


def own_score(vitals: list[dict], checks: list[dict]) -> int:
    pts = {"good": 1.0, "ok": 0.6, "poor": 0.15}
    items = [pts[v["rating"]] for v in vitals] * 2 + [pts[c["status"]] for c in checks]
    return round(100 * sum(items) / len(items)) if items else 0


async def _http_probe(url: str, client: httpx.AsyncClient) -> dict | None:
    import time
    t0 = time.monotonic()
    try:
        async with client.stream("GET", url, follow_redirects=True) as r:
            ttfb = (time.monotonic() - t0) * 1000
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > 3_000_000:
                    break
            return {"html": body.decode(r.encoding or "utf-8", errors="replace"), "headers": dict(r.headers),
                    "http": r.http_version, "ttfb": ttfb, "redirects": len(r.history), "size": len(body), "final": str(r.url),
                    "status": r.status_code}
    except httpx.HTTPError as e:
        log.info("speed probe failed for %s: %s", url, type(e).__name__)
        return None


async def _browser_run(browser, url: str, profile: dict) -> dict | None:
    from app.security import UnsafeURL, check_url_safe
    ctx = await browser.new_context(**profile)
    try:
        page = await ctx.new_page()
        total = {"bytes": 0, "n": 0, "third": 0}
        host = (httpx.URL(url).host or "").split(".", 1)[-1]
        pending = []

        async def gate(route):
            req = route.request
            if not req.url.startswith(("http://", "https://")):
                return await route.abort()
            try:
                await asyncio.to_thread(check_url_safe, req.url)
            except UnsafeURL:
                return await route.abort()
            await route.continue_()

        async def sized(resp):
            try:
                sz = await resp.request.sizes()
                total["bytes"] += (sz.get("responseBodySize") or 0) + (sz.get("responseHeadersSize") or 0)
            except Exception:
                pass

        def on_resp(resp):
            total["n"] += 1
            if host and host not in (httpx.URL(resp.url).host or ""):
                total["third"] += 1
            if total["n"] <= 200:
                pending.append(asyncio.ensure_future(sized(resp)))

        await page.route("**/*", gate)
        page.on("response", on_resp)
        await page.goto(url, wait_until="load", timeout=config.settings.pagespeed_timeout * 1000 / 2)
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        m = await page.evaluate(PERF_JS)
        if pending:
            await asyncio.wait(pending, timeout=3)
        m.update(total)
        return m
    except Exception as e:
        log.info("speed browser run failed for %s: %s", url, str(e)[:100])
        return None
    finally:
        await ctx.close()


def _browser_ok() -> bool:
    if not config.settings.speed_browser:
        return False
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def own_measure(url: str) -> dict | None:
    """Our own measurement: {"mobile": {...}, "desktop": {...}} each with vitals, checks, score, stats."""
    s = config.settings
    from app.security import UnsafeURL, check_url_safe
    try:
        await asyncio.to_thread(check_url_safe, url)
    except UnsafeURL:
        return None
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"user-agent": s.user_agent, "accept-encoding": "gzip, br"},
                                 http2=False) as client:
        probe = await _http_probe(url, client)
    if not probe or probe["status"] >= 400:
        return None
    # httpx speaks HTTP/1.1 only here, so the protocol is read from the browser run instead (empty = not checked)
    checks = html_checks(probe["html"], probe["headers"], "", probe["size"], probe["ttfb"], probe["redirects"])
    result: dict = {"browser": False}
    runs = {}
    if _browser_ok():
        pw = browser = None
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            for name, prof in (("mobile", PHONE), ("desktop", DESKTOP)):
                m = await _browser_run(browser, probe["final"], prof)
                if m:
                    runs[name] = m
        except Exception as e:
            log.info("speed browser unavailable: %s", str(e)[:100])
        finally:
            try:
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()
            except Exception:
                pass
    if runs:
        result["browser"] = True
        for name, m in runs.items():
            vitals = []
            if m.get("fcp"):
                vitals.append({"label": "First Contentful Paint", "value": _fmt(m["fcp"], "ms"), "rating": _rate(m["fcp"], 1800, 3000)})
            if m.get("lcp"):
                vitals.append({"label": "Largest Contentful Paint", "value": _fmt(m["lcp"], "ms"), "rating": _rate(m["lcp"], 2500, 4000)})
            vitals.append({"label": "Layout shift (CLS)", "value": f"{m['cls']:.2f}", "rating": _rate(m["cls"], 0.1, 0.25)})
            if m.get("load"):
                vitals.append({"label": "Fully loaded", "value": _fmt(m["load"], "ms"), "rating": _rate(m["load"], 3000, 6000)})
            extra = []
            if m["bytes"] > 3_000_000:
                extra.append({"label": "Page weight", "status": "poor", "detail": f"{_kb(m['bytes'])} downloaded. Aim for under 2 MB."})
            elif m["bytes"] > 1_500_000:
                extra.append({"label": "Page weight", "status": "ok", "detail": f"{_kb(m['bytes'])} downloaded."})
            elif m["bytes"]:
                extra.append({"label": "Page weight", "status": "good", "detail": f"{_kb(m['bytes'])} downloaded."})
            if m["n"] > 120:
                extra.append({"label": "Requests", "status": "poor", "detail": f"{m['n']} files requested. Combine or remove some."})
            if m["third"] > 15:
                extra.append({"label": "Third-party files", "status": "ok", "detail": f"{m['third']} files come from other domains (analytics, chat, fonts). Each one costs time."})
            if m.get("oversized"):
                extra.append({"label": "Oversized images", "status": "ok", "detail": f"{m['oversized']} images are much larger than they appear. Serve smaller versions."})
            proto = (m.get("proto") or "").lower()
            if proto in ("http/1.0", "http/1.1"):
                extra.append({"label": "HTTP version", "status": "ok", "detail": f"Served over {proto.upper()}. HTTP/2 or HTTP/3 loads many files faster."})
            elif proto:
                extra.append({"label": "HTTP version", "status": "good", "detail": f"Served over {proto.upper().replace('H2', 'HTTP/2').replace('H3', 'HTTP/3')}."})
            allc = checks + extra
            result[name] = {"vitals": vitals, "checks": allc, "score": own_score(vitals, allc),
                            "stats": {"requests": m["n"], "bytes": _kb(m["bytes"]) if m["bytes"] else None}}
    else:
        vitals = []
        if probe["ttfb"] is not None:
            vitals.append({"label": "Server response", "value": _fmt(probe["ttfb"], "ms"), "rating": _rate(probe["ttfb"], 600, 1500)})
        result["mobile"] = {"vitals": vitals, "checks": checks, "score": own_score(vitals, checks), "stats": {}}
    return result


async def measure(url: str) -> dict | None:
    """Everything shown in the report's speed card: our own measurement, plus Google's when a key is set."""
    if not config.settings.pagespeed_enabled:
        return None
    try:
        own, google = await asyncio.gather(own_measure(url), google_measure(url), return_exceptions=True)
    except Exception:
        return None
    own = own if isinstance(own, dict) else None
    google = google if isinstance(google, dict) else None
    if not own and not google:
        return None
    return {"url": url, "own": own, "google": google}
