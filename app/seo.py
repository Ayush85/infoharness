"""Search and AI-answer-engine surface: robots.txt, sitemap.xml, llms.txt and the guides."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from sqlalchemy import func, select

from app import config
from app.db import Scan, session_scope
from app.templating import templates

router = APIRouter()

GUIDES = [
    {"slug": "contradictory-docs-ai-assistants",
     "title": "Why your AI support bot gives wrong answers",
     "description": "AI assistants answer from your help center. When two pages disagree, the answer depends on which one gets retrieved. How contradictions get in, and how to remove them.",
     "summary": "How contradictions between help-center pages turn into confident wrong answers, and what to do about it."},
    {"slug": "help-center-audit-checklist",
     "title": "A 10-point help center audit checklist",
     "description": "Ten checks for a help center or documentation site: contradictions, duplicates, outdated pages, broken links, thin and orphan pages. What to look for and how to fix each.",
     "summary": "Ten checks to run on any documentation site, with what to look for and how to fix each one."},
    {"slug": "find-duplicate-and-outdated-pages",
     "title": "How to find duplicate and outdated pages in your docs",
     "description": "Practical ways to find near-duplicate pages and pages that describe old versions, by hand and automatically, and what to do with each one you find.",
     "summary": "Manual and automatic ways to find duplicate and outdated pages, and what to do with them."},
]
FAQS = [
    ("Does it read private or logged-in pages?", "No. It only reads pages anyone can open without signing in. Documentation behind a login can't be scanned yet.", None),
    ("Will it work on my site?", "It works on any documentation site or help center that serves its text as HTML, which covers most platforms. Sites that only draw their content with JavaScript show up as unreadable.", None),
    ("How are contradictions found?", "By comparing the numbers, limits and dates that different pages state about the same thing. Each finding shows both quotes side by side so you can judge it in seconds.", ("Read the guide", "/guides/contradictory-docs-ai-assistants")),
    ("How do I find broken links in my help center?", "Paste the help center address into the scanner. It requests every link, skips sites that block automated checks, and lists each broken link with the pages and menu items that contain it.", ("Broken link checker", "/tools/broken-link-checker-for-documentation")),
    ("How do I find duplicate or outdated articles in a knowledge base?", "Scan it. Pages are compared with each other to find near-duplicates, and checked for old dates, old version numbers and long-unmodified pages.", ("Find duplicate and outdated pages", "/guides/find-duplicate-and-outdated-pages")),
    ("Why does my AI chatbot give wrong answers?", "Usually because the documentation it reads has conflicting or stale pages. The model repeats the page it retrieves, so fix the sources first.", ("Read why and how to fix it", "/guides/fix-ai-chatbot-wrong-answers")),
    ("Who can see my report?", "Anyone who has the link. Reports of publicly scanned sites also appear in the public reports list and in search results, so teams can compare. Reports for sites monitored from an account stay private. To remove a report, email us from the site's domain.", None),
    ("What does it cost?", "Nothing. Scanning, watching your sites on a schedule and alerts by email, Slack or webhook are all free.", ("What is included", "/pricing")),
    ("Can I stop the crawler?", "Yes. Add InfoharnessBot to your robots.txt and it will skip your whole site.", ("How to block it", "/bot#block")),
]


def home_jsonld() -> dict:
    base = config.settings.public_base_url
    return {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "@id": f"{base}/#org", "name": "infoharness", "url": base, "logo": f"{base}/static/favicon.svg"},
        {"@type": "WebSite", "@id": f"{base}/#site", "url": base, "name": "infoharness", "publisher": {"@id": f"{base}/#org"}},
        {"@type": "SoftwareApplication", "name": "infoharness", "url": base, "applicationCategory": "DeveloperApplication",
         "operatingSystem": "Web",
         "description": "Scans a documentation site or help center for contradicting pages, duplicates, outdated content, "
                        "broken links, thin pages and orphan pages, and grades it from A to F.",
         "offers": {"@type": "Offer", "name": "Free scan", "price": "0", "priceCurrency": "USD"}},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a, _ in FAQS]},
    ]}


from app.seo_pages import NEW_GUIDES, TOOL_BY_SLUG, TOOLS  # noqa: E402

GUIDES += NEW_GUIDES
BY_SLUG = {g["slug"]: g for g in GUIDES}
PRIVATE = ("/api/", "/account", "/login", "/logout", "/webhooks/")
STATIC_URLS = ["/", "/pricing", "/tools", "/guides", "/reports", "/bot", "/contact", "/privacy", "/terms"]


def _base() -> str:
    return config.settings.public_base_url


MIN_PUBLIC_PAGES = 5


def public_reports(limit: int = 200, offset: int = 0) -> list[dict]:
    """Latest finished report per site, for sites anyone could scan. Watched (account) scans are never listed."""
    with session_scope() as s:
        latest = (select(Scan.url, func.max(Scan.created_at).label("m")).where(
            Scan.status == "done", ~Scan.client_hash.like("site:%"), Scan.pages_crawled >= MIN_PUBLIC_PAGES)
            .group_by(Scan.url).subquery())
        rows = s.scalars(select(Scan).join(latest, (Scan.url == latest.c.url) & (Scan.created_at == latest.c.m))
                         .where(~Scan.client_hash.like("site:%")).order_by(Scan.created_at.desc())
                         .limit(limit).offset(offset)).all()
        return [{"id": r.id, "url": r.url, "host": r.url.split("//")[-1].rstrip("/"), "grade": r.grade,
                 "score": round(r.score or 0), "pages": r.pages_crawled,
                 "issues": (r.summary or {}).get("total_issues", 0), "when": r.finished_at or r.created_at}
                for r in rows]


def is_public_latest(scan) -> bool:
    """True when this report may be indexed: finished, from a public scan, and the newest for its site."""
    if scan.status != "done" or (scan.client_hash or "").startswith("site:") or (scan.pages_crawled or 0) < MIN_PUBLIC_PAGES:
        return False
    with session_scope() as s:
        newest = s.scalar(select(Scan.id).where(Scan.url == scan.url, Scan.status == "done",
                                                ~Scan.client_hash.like("site:%"))
                          .order_by(Scan.created_at.desc()).limit(1))
    return newest == scan.id


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, page: int = 1):
    page = max(1, page)
    rows = public_reports(101, (page - 1) * 100)
    return templates.TemplateResponse(request, "reports.html", {"rows": rows[:100], "page": page, "more": len(rows) > 100})


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    rules = "\n".join(f"Disallow: {p}" for p in PRIVATE)
    return (f"# Search engines and AI answer engines are welcome on the public pages.\n"
            f"User-agent: *\nAllow: /\n{rules}\n\nSitemap: {_base()}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap():
    urls = STATIC_URLS + [f"/tools/{t['slug']}" for t in TOOLS] + [f"/guides/{g['slug']}" for g in GUIDES]
    body = "".join(f"<url><loc>{_base()}{u}</loc></url>" for u in urls)
    body += "".join(f"<url><loc>{_base()}/r/{r['id']}</loc><lastmod>{r['when'].strftime('%Y-%m-%d')}</lastmod></url>"
                    for r in public_reports(5000))
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    return Response(xml, media_type="application/xml")


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    s = config.settings
    tools = "\n".join(f"- [{t['h1']}]({_base()}/tools/{t['slug']}): {t['description']}" for t in TOOLS)
    guides = "\n".join(f"- [{g['title']}]({_base()}/guides/{g['slug']}): {g['summary']}" for g in GUIDES)
    return f"""# infoharness

> infoharness is a free web tool that scans a documentation site or help center and reports pages that contradict each other, duplicate pages, repeated sections, outdated content, broken links, thin pages and orphan pages. It gives each site a grade from A to F. Teams use it so that AI support assistants and search answer from consistent documentation.

## What it does
- Paste a docs or help-center URL; no signup. The crawler reads up to {s.max_pages} public pages, obeys robots.txt and does not run JavaScript.
- Every finding shows the exact quotes and page links, so it can be checked in seconds.
- Contradictions are found by comparing the numbers, limits and dates that different pages state about the same fact.
- Free account: watch sites on a schedule and get an email, Slack or webhook alert when new problems appear.

## Pages
- [Scan a site]({_base()}/): the scanner
- [Free]({_base()}/pricing): everything is free, including scheduled monitoring and alerts
- [Public reports]({_base()}/reports): latest docs-health grade for every publicly scanned site
- [About the crawler]({_base()}/bot): user agent `InfoharnessBot`, how to block it

## Tools (each is the same scanner, framed for one task)
{tools}

## Guides
{guides}
"""


@router.get("/tools", response_class=HTMLResponse)
def tools_index(request: Request):
    return templates.TemplateResponse(request, "tools/index.html", {"tools": TOOLS, "max_pages": config.settings.max_pages})


@router.get("/tools/{slug}", response_class=HTMLResponse)
def tool_page(request: Request, slug: str):
    t = TOOL_BY_SLUG.get(slug)
    if t is None:
        return templates.TemplateResponse(request, "notfound.html", status_code=404)
    related = [BY_SLUG[g] for g in t["guides"] if g in BY_SLUG]
    others = [x for x in TOOLS if x["slug"] != slug][:6]
    base = config.settings.public_base_url
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebApplication", "name": t["h1"], "url": f"{base}/tools/{slug}", "description": t["description"],
         "applicationCategory": "DeveloperApplication", "operatingSystem": "Web",
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
         "provider": {"@type": "Organization", "name": "infoharness", "url": base}},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in t["faq"]]},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "infoharness", "item": f"{base}/"},
            {"@type": "ListItem", "position": 2, "name": "Tools", "item": f"{base}/tools"},
            {"@type": "ListItem", "position": 3, "name": t["h1"]}]}]}
    return templates.TemplateResponse(request, "tools/page.html", {
        "ld": ld, "tool": t, "related": related, "others": others, "max_pages": config.settings.max_pages})


@router.get("/guides", response_class=HTMLResponse)
def guides_index(request: Request):
    return templates.TemplateResponse(request, "guides/index.html", {"guides": GUIDES})


@router.get("/guides/{slug}", response_class=HTMLResponse)
def guide(request: Request, slug: str):
    g = BY_SLUG.get(slug)
    if g is None:
        return templates.TemplateResponse(request, "notfound.html", status_code=404)
    others = [x for x in GUIDES if x["slug"] != slug]
    return templates.TemplateResponse(request, f"guides/{slug}.html", {"guide": g, "others": others})


@router.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(request, "contact.html", {"email": config.settings.contact_email})


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"email": config.settings.contact_email})


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html", {"email": config.settings.contact_email})
