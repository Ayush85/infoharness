"""Search and AI-answer-engine surface: robots.txt, sitemap.xml, llms.txt and the guides."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app import config
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
    ("Who can see my report?", "Anyone who has the link. Reports are not listed anywhere and are hidden from search engines.", None),
    ("What does it cost?", "Scanning is free. The Pro plan watches your docs on a schedule and emails you when something new breaks.", ("See pricing", "/pricing")),
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


BY_SLUG = {g["slug"]: g for g in GUIDES}
PRIVATE = ("/r/", "/api/", "/account", "/login", "/logout", "/webhooks/")
STATIC_URLS = ["/", "/pricing", "/guides", "/bot"]


def _base() -> str:
    return config.settings.public_base_url


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    rules = "\n".join(f"Disallow: {p}" for p in PRIVATE)
    return (f"# Search engines and AI answer engines are welcome on the public pages.\n"
            f"User-agent: *\nAllow: /\n{rules}\n\nSitemap: {_base()}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap():
    urls = STATIC_URLS + [f"/guides/{g['slug']}" for g in GUIDES]
    body = "".join(f"<url><loc>{_base()}{u}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    return Response(xml, media_type="application/xml")


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    s = config.settings
    guides = "\n".join(f"- [{g['title']}]({_base()}/guides/{g['slug']}): {g['summary']}" for g in GUIDES)
    return f"""# infoharness

> infoharness is a free web tool that scans a documentation site or help center and reports pages that contradict each other, duplicate pages, repeated sections, outdated content, broken links, thin pages and orphan pages. It gives each site a grade from A to F. Teams use it so that AI support assistants and search answer from consistent documentation.

## What it does
- Paste a docs or help-center URL; no signup. The crawler reads up to {s.max_pages} public pages, obeys robots.txt and does not run JavaScript.
- Every finding shows the exact quotes and page links, so it can be checked in seconds.
- Contradictions are found by comparing the numbers, limits and dates that different pages state about the same fact.
- Paid plan: watch sites on a schedule and get an email when new problems appear.

## Pages
- [Scan a site]({_base()}/): the scanner
- [Pricing]({_base()}/pricing): free scans, Pro for scheduled monitoring
- [About the crawler]({_base()}/bot): user agent `InfoharnessBot`, how to block it

## Guides
{guides}
"""


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
