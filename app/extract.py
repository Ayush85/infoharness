"""HTML -> clean structured text.

Keeps the main content, drops navigation/chrome, and records heading context for
each text block so findings can point to a section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urldefrag, urlsplit

from bs4 import BeautifulSoup, Comment, Tag

DROP_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form",
             "iframe", "button", "select", "template", "canvas"]
CHROME_HINTS = re.compile(
    r"(^|[\s_-])(nav|navbar|sidebar|side-bar|breadcrumbs?|cookie|consent|menu|toc|table-of-contents|"
    r"footer|header|banner|feedback|pagination|pager|related|share|social|search)([\s_-]|$)", re.I)
BLOCK_TAGS = {"p", "li", "td", "th", "pre", "blockquote", "dd", "dt", "figcaption", "summary"}
HEADING_TAGS = {"h1", "h2", "h3", "h4"}
WS = re.compile(r"\s+")


@dataclass
class Block:
    text: str
    heading: str
    kind: str  # "text" | "code"


@dataclass
class Extracted:
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    # absolute link target -> {"text": anchor text, "region": content|header|menu|footer|sidebar, "heading": nearest heading above it}
    link_ctx: dict[str, dict] = field(default_factory=dict)
    last_modified: datetime | None = None
    word_count: int = 0
    lang: str = ""

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


def _clean(s: str) -> str:
    return WS.sub(" ", s or "").strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_REGION_BY_TAG = {"header": "header", "nav": "menu", "footer": "footer", "aside": "sidebar"}
_REGION_BY_ROLE = {"banner": "header", "navigation": "menu", "contentinfo": "footer", "complementary": "sidebar"}
_REGION_HINTS = (("footer", "footer"), ("header", "header"), ("banner", "header"), ("sidebar", "sidebar"),
                 ("side-bar", "sidebar"), ("toc", "sidebar"), ("breadcrumb", "menu"), ("nav", "menu"), ("menu", "menu"))


def _region(a: Tag) -> str:
    """Where on the page a link sits: the page's own content, or shared template areas."""
    depth = 0
    for p in a.parents:
        depth += 1
        if depth > 14 or p.name in ("body", "html", "[document]"):
            break
        if p.name in _REGION_BY_TAG:
            return _REGION_BY_TAG[p.name]
        if p.get("role") in _REGION_BY_ROLE:
            return _REGION_BY_ROLE[p.get("role")]
        attrs = " ".join(filter(None, [p.get("id") or "", " ".join(p.get("class") or [])])).lower()
        if attrs:
            for word, region in _REGION_HINTS:
                if re.search(rf"(^|[\s_-]){word}([\s_-]|$)", attrs):
                    return region
    return "content"


def _anchor_text(a: Tag) -> str:
    text = _clean(a.get_text(" "))
    if not text:
        img = a.find("img")
        text = _clean((img.get("alt") if img else "") or a.get("aria-label") or a.get("title") or "")
    return text[:120]


def _is_chrome(tag: Tag) -> bool:
    if tag.get("role") in ("navigation", "banner", "contentinfo", "search", "complementary"):
        return True
    if tag.get("aria-hidden") == "true":
        return True
    attrs = " ".join(filter(None, [tag.get("id") or "", " ".join(tag.get("class") or [])]))
    return bool(attrs and CHROME_HINTS.search(attrs))


LANG_IN_PATH = re.compile(r"^/([a-z]{2})(?:[-_][a-z]{2,4})?(?:/|$)", re.I)


def page_language(declared: str | None, url: str) -> str:
    """Primary language subtag ('en', 'fr'): from <html lang>, else from a /fr/ style path segment, else ''."""
    d = (declared or "").strip().lower().replace("_", "-").split("-")[0]
    if re.fullmatch(r"[a-z]{2,3}", d) and d != "x":
        return d
    m = LANG_IN_PATH.match(urlsplit(url).path)
    return m.group(1).lower() if m else ""


def extract_pdf(data: bytes, url: str, max_pages: int = 60) -> Extracted:
    """Text of a PDF as paragraph blocks. Raises on unreadable files; the caller treats that as 'no content'."""
    import io

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("encrypted PDF")
    out = Extracted()
    meta = reader.metadata
    out.title = _clean(getattr(meta, "title", "") or "") if meta else ""
    chunks = []
    for page in reader.pages[:max_pages]:
        chunks.append(page.extract_text() or "")
    text = "\n".join(chunks)
    for para in re.split(r"\n\s*\n|(?<=[.!?])\n(?=[A-Z0-9])", text):
        para = _clean(para)
        if len(para.split()) >= 4:
            out.blocks.append(Block(text=para, heading="", kind="text"))
    if not out.title:
        first = out.blocks[0].text if out.blocks else ""
        out.title = first[:80]
    out.word_count = sum(len(b.text.split()) for b in out.blocks)
    mod = getattr(meta, "modification_date", None) if meta else None
    out.last_modified = mod if isinstance(mod, datetime) else None
    if out.last_modified is not None and out.last_modified.tzinfo is None:
        out.last_modified = out.last_modified.replace(tzinfo=timezone.utc)
    return out


def extract(html: str, base_url: str, header_last_modified: str | None = None) -> Extracted:
    soup = BeautifulSoup(html, "lxml")
    out = Extracted()
    html_tag = soup.find("html")
    out.lang = page_language(html_tag.get("lang") if html_tag else None, base_url)

    # Links come from the whole page (nav links count as "inbound links" for orphan detection).
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        region = _region(a)
        if absolute not in seen:
            seen.add(absolute)
            out.links.append(absolute)
            h = a.find_previous(["h1", "h2", "h3", "h4"]) if region == "content" else None
            out.link_ctx[absolute] = {"text": _anchor_text(a), "region": region,
                                      "heading": _clean(h.get_text(" "))[:120] if h else ""}
        elif region == "content" and out.link_ctx[absolute]["region"] != "content":
            h = a.find_previous(["h1", "h2", "h3", "h4"])  # prefer the in-content occurrence over a menu one
            out.link_ctx[absolute] = {"text": _anchor_text(a), "region": "content",
                                      "heading": _clean(h.get_text(" "))[:120] if h else ""}

    # Title
    if soup.title and soup.title.string:
        out.title = _clean(soup.title.string)[:500]
    h1 = soup.find("h1")
    if not out.title and h1:
        out.title = _clean(h1.get_text(" "))[:500]

    # Last modified: meta tags, <time>, or HTTP header
    lm = None
    for sel in [("meta", {"property": "article:modified_time"}), ("meta", {"name": "last-modified"}),
                ("meta", {"itemprop": "dateModified"}), ("meta", {"property": "og:updated_time"})]:
        tag = soup.find(*sel)
        if tag and tag.get("content"):
            lm = _parse_date(tag["content"])
            if lm:
                break
    if lm is None and header_last_modified:
        lm = _parse_date(header_last_modified)
    out.last_modified = lm

    # Main content selection
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup.find_all(DROP_TAGS):
        t.decompose()
    for t in list(soup.find_all(True)):
        if t.parent is None or t.name in ("html", "body", "main", "article"):
            continue
        if _is_chrome(t):
            t.decompose()

    root = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup

    current_heading = out.title
    for el in root.find_all(list(HEADING_TAGS | BLOCK_TAGS)):
        if el.name in HEADING_TAGS:
            current_heading = _clean(el.get_text(" "))[:300] or current_heading
            continue
        # Skip containers whose text is captured by a nested block (e.g. <li><p>..</p></li>)
        if el.find(list(BLOCK_TAGS)):
            continue
        text = _clean(el.get_text(" "))
        if len(text) < 3:
            continue
        kind = "code" if el.name == "pre" else "text"
        out.blocks.append(Block(text=text, heading=current_heading, kind=kind))

    # Fallback for pages that use plain <div>s for text
    if not out.blocks:
        text = _clean(root.get_text(" "))
        if text:
            out.blocks.append(Block(text=text, heading=current_heading, kind="text"))

    out.word_count = sum(len(b.text.split()) for b in out.blocks if b.kind == "text")
    return out
