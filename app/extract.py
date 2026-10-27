"""HTML -> clean structured text.

Keeps the main content, drops navigation/chrome, and records heading context for
each text block so findings can point to a section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urldefrag

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
    last_modified: datetime | None = None
    word_count: int = 0

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


def _is_chrome(tag: Tag) -> bool:
    if tag.get("role") in ("navigation", "banner", "contentinfo", "search", "complementary"):
        return True
    if tag.get("aria-hidden") == "true":
        return True
    attrs = " ".join(filter(None, [tag.get("id") or "", " ".join(tag.get("class") or [])]))
    return bool(attrs and CHROME_HINTS.search(attrs))


def extract(html: str, base_url: str, header_last_modified: str | None = None) -> Extracted:
    soup = BeautifulSoup(html, "lxml")
    out = Extracted()

    # Links come from the whole page (nav links count as "inbound links" for orphan detection).
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        if absolute not in seen:
            seen.add(absolute)
            out.links.append(absolute)

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
