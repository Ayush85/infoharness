"""Content analysis: chunking, similarity, and the individual health checks.

Every check returns `Finding` objects. Checks are deliberately conservative:
a false "contradiction" destroys user trust faster than a missed one.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from app.extract import Block

# ---------------------------------------------------------------- data types


@dataclass
class PageDoc:
    url: str
    title: str
    blocks: list[Block]
    word_count: int
    link_count: int = 0
    from_sitemap: bool = False
    inbound: int = 0
    last_modified: datetime | None = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.kind == "text")


@dataclass
class Chunk:
    page: int  # index into pages list
    heading: str
    text: str


@dataclass
class Finding:
    type: str
    severity: str
    title: str
    detail: str = ""
    page_a: str = ""
    page_b: str = ""
    quote_a: str = ""
    quote_b: str = ""
    extra: dict = field(default_factory=dict)
    points: float = 0.0


@dataclass
class PairCandidate:
    a: int  # chunk index
    b: int
    sim: float


# ---------------------------------------------------------------- chunking

WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _norm_key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()


def boilerplate_keys(pages: list[PageDoc]) -> set[str]:
    """Blocks repeated on many pages (e.g. 'Was this article helpful?') are template, not content."""
    if len(pages) < 4:
        return set()
    counts: Counter[str] = Counter()
    for p in pages:
        counts.update({_norm_key(b.text) for b in p.blocks})
    threshold = max(3, int(len(pages) * 0.3))
    return {k for k, c in counts.items() if c >= threshold}


def build_chunks(pages: list[PageDoc], *, target_words: int = 180, max_words: int = 320,
                 max_chunks: int = 4000) -> list[Chunk]:
    boiler = boilerplate_keys(pages)
    chunks: list[Chunk] = []
    for pi, p in enumerate(pages):
        cur: list[str] = []
        cur_words = 0
        cur_heading = ""
        for b in p.blocks:
            if b.kind != "text" or _norm_key(b.text) in boiler:
                continue
            words = len(b.text.split())
            if cur and (b.heading != cur_heading or cur_words + words > max_words):
                chunks.append(Chunk(pi, cur_heading, " ".join(cur)))
                cur, cur_words = [], 0
            cur_heading = b.heading
            cur.append(b.text)
            cur_words += words
            if cur_words >= target_words:
                chunks.append(Chunk(pi, cur_heading, " ".join(cur)))
                cur, cur_words = [], 0
        if cur:
            chunks.append(Chunk(pi, cur_heading, " ".join(cur)))
        if len(chunks) >= max_chunks:
            break
    return chunks[:max_chunks]


# ---------------------------------------------------------------- similarity


def tfidf_matrix(texts: list[str]):
    vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english",
                          max_features=60000, min_df=1, dtype=np.float32)
    return vec.fit_transform(texts)  # rows are L2-normalised


def top_pairs(matrix, owners: list[int], *, lo: float, hi: float, k: int = 5,
              block: int = 512) -> list[PairCandidate]:
    """For every row, its top-k neighbours (lo <= sim < hi) that belong to a *different* owner page.

    Works for sparse (TF-IDF) and dense (embedding) row-normalised matrices; computed in blocks
    so memory stays bounded on small servers.
    """
    n = matrix.shape[0]
    owners_arr = np.asarray(owners)
    seen: set[tuple[int, int]] = set()
    out: list[PairCandidate] = []
    for start in range(0, n, block):
        stop = min(n, start + block)
        sims = matrix[start:stop] @ matrix.T
        sims = sims.toarray() if hasattr(sims, "toarray") else np.asarray(sims)
        for r in range(stop - start):
            i = start + r
            row = sims[r]
            row[owners_arr == owners_arr[i]] = -1.0  # same page never pairs with itself
            mask = (row >= lo) & (row < hi)
            if not mask.any():
                continue
            idx = np.where(mask)[0]
            best = idx[np.argsort(-row[idx])[:k]]
            for j in best:
                key = (min(i, int(j)), max(i, int(j)))
                if key in seen:
                    continue
                seen.add(key)
                out.append(PairCandidate(key[0], key[1], float(row[j])))
    out.sort(key=lambda p: -p.sim)
    return out


# ---------------------------------------------------------------- duplicates


def check_duplicate_pages(pages: list[PageDoc], *, threshold: float = 0.9) -> tuple[list[Finding], set[tuple[int, int]]]:
    eligible = [i for i, p in enumerate(pages) if p.word_count >= 60]
    if len(eligible) < 2:
        return [], set()
    m = tfidf_matrix([pages[i].text for i in eligible])
    pairs = top_pairs(m, list(range(len(eligible))), lo=threshold, hi=1.01, k=10)
    parent = list(range(len(eligible)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    dup_pairs: set[tuple[int, int]] = set()
    for pc in pairs:
        parent[find(pc.a)] = find(pc.b)
        dup_pairs.add((eligible[pc.a], eligible[pc.b]))
    clusters: dict[int, list[int]] = defaultdict(list)
    for li in range(len(eligible)):
        clusters[find(li)].append(eligible[li])
    findings = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: (len(pages[i].url), pages[i].url))
        first = pages[members[0]]
        others = [pages[i].url for i in members[1:]]
        findings.append(Finding(
            type="duplicate_page", severity="medium",
            title=f"{len(members)} pages are near-identical",
            detail="These pages say almost the same thing. AI assistants may retrieve either copy; "
                   "if one is updated and the other is not, answers become inconsistent. Merge them or redirect.",
            page_a=first.url, page_b=others[0], extra={"pages": [first.url] + others},
            points=3.0 * (len(members) - 1),
        ))
    return findings, dup_pairs


def check_duplicate_sections(chunks: list[Chunk], pages: list[PageDoc], pairs: list[PairCandidate],
                             dup_page_pairs: set[tuple[int, int]], *, threshold: float = 0.93,
                             limit: int = 40) -> list[Finding]:
    findings = []
    reported_pages: set[tuple[int, int]] = set()
    for pc in pairs:
        if pc.sim < threshold:
            break
        a, b = chunks[pc.a], chunks[pc.b]
        key = (min(a.page, b.page), max(a.page, b.page))
        if key in dup_page_pairs or key in reported_pages:
            continue
        if len(a.text.split()) < 35:
            continue
        reported_pages.add(key)
        findings.append(Finding(
            type="duplicate_section", severity="low",
            title=f"Repeated section: “{_short(a.heading or pages[a.page].title, 70)}”",
            detail="The same content is maintained in two places. Keep one source of truth and link to it.",
            page_a=pages[a.page].url, page_b=pages[b.page].url,
            quote_a=_short(a.text, 280), quote_b=_short(b.text, 280),
            extra={"similarity": round(pc.sim, 3)}, points=1.0,
        ))
        if len(findings) >= limit:
            break
    return findings


# ---------------------------------------------------------------- contradictions (numeric)

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20,
                "thirty": 30, "forty-five": 45, "sixty": 60, "ninety": 90}
UNIT_SYNONYMS = {
    "business day": "business_day", "business days": "business_day", "working day": "business_day",
    "working days": "business_day",
    "day": "day", "days": "day", "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "second": "second", "seconds": "second", "week": "week", "weeks": "week",
    "month": "month", "months": "month", "year": "year", "years": "year",
    "%": "percent", "percent": "percent", "kb": "KB", "mb": "MB", "gb": "GB", "tb": "TB",
    "user": "user", "users": "user", "seat": "seat", "seats": "seat", "member": "member", "members": "member",
    "project": "project", "projects": "project", "request": "request", "requests": "request",
    "character": "character", "characters": "character", "file": "file", "files": "file",
    "device": "device", "devices": "device", "license": "license", "licenses": "license",
    "credit": "credit", "credits": "credit", "attempt": "attempt", "attempts": "attempt",
    "message": "message", "messages": "message", "email": "email", "emails": "email",
}
_unit_alt = "|".join(sorted((re.escape(u) for u in UNIT_SYNONYMS), key=len, reverse=True))
_num = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|" + "|".join(NUMBER_WORDS) + r")"
QTY_RE = re.compile(_num + r"\s*-?\s*(" + _unit_alt + r")\b", re.I)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%)")
MONEY_RE = re.compile(r"([$€£])\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)")
CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP"}

STOP = set("""the a an and or of to in on for with by at from as is are was were be been it its this that these those
you your we our they their can may will would should could must not no yes if then than any all each per via into
also more most less least only just about after before within up down out over under such other same new using use
used get gets how what when where which who why do does did has have had there here""".split())
QUALIFIERS = set("""free pro basic starter business enterprise team premium plus standard annual monthly yearly trial paid
personal growth scale professional individual student education nonprofit legacy old previous beta""".split())


@dataclass
class Fact:
    page: int
    chunk: int
    sentence: str
    unit: str
    value: float
    context: frozenset[str]


def _to_number(s: str) -> float | None:
    s = s.lower().replace(",", "")
    if s in NUMBER_WORDS:
        return float(NUMBER_WORDS[s])
    try:
        return float(s)
    except ValueError:
        return None


def extract_facts(chunks: list[Chunk]) -> list[Fact]:
    facts: list[Fact] = []
    for ci, ch in enumerate(chunks):
        for sent in SENT_SPLIT.split(ch.text):
            if not (6 <= len(sent.split()) <= 60):
                continue
            found: dict[str, set[float]] = defaultdict(set)
            for m in QTY_RE.finditer(sent):
                v = _to_number(m.group(1))
                if v is not None:
                    found[UNIT_SYNONYMS[m.group(2).lower()]].add(v)
            for m in PCT_RE.finditer(sent):
                found["percent"].add(float(m.group(1)))
            for m in MONEY_RE.finditer(sent):
                v = _to_number(m.group(2))
                if v is not None:
                    found[CURRENCY[m.group(1)]].add(v)
            if not found:
                continue
            tokens = {w.lower() for w in WORD.findall(sent)}
            context = frozenset(t for t in tokens if t not in STOP and t not in NUMBER_WORDS
                                and t not in UNIT_SYNONYMS)
            if len(context) < 3:
                continue
            for unit, values in found.items():
                if len(values) != 1:  # ranges / tables / comparisons in one sentence: skip
                    continue
                facts.append(Fact(ch.page, ci, sent.strip(), unit, next(iter(values)), context))
    return facts


def numeric_conflicts(facts: list[Fact], *, min_jaccard: float = 0.5, min_shared: int = 3,
                      limit: int = 60) -> list[tuple[Fact, Fact, float]]:
    by_unit: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_unit[f.unit].append(f)
    out = []
    seen_pages: set[tuple[int, int, str]] = set()
    for unit, group in by_unit.items():
        group = group[:1500]
        for i in range(len(group)):
            fi = group[i]
            for j in range(i + 1, len(group)):
                fj = group[j]
                if fi.page == fj.page or fi.value == fj.value:
                    continue
                shared = fi.context & fj.context
                if len(shared) < min_shared:
                    continue
                jac = len(shared) / len(fi.context | fj.context)
                if jac < min_jaccard:
                    continue
                if (fi.context ^ fj.context) & QUALIFIERS:
                    continue  # e.g. "Free plan: 5 users" vs "Pro plan: 50 users" is not a conflict
                key = (min(fi.page, fj.page), max(fi.page, fj.page), unit)
                if key in seen_pages:
                    continue
                seen_pages.add(key)
                out.append((fi, fj, jac))
    out.sort(key=lambda t: -t[2])
    return out[:limit]


def _fmt(v: float, unit: str) -> str:
    num = f"{int(v)}" if v == int(v) else f"{v}"
    if unit in ("USD",):
        return f"${num}"
    if unit in ("EUR",):
        return f"€{num}"
    if unit in ("GBP",):
        return f"£{num}"
    if unit == "percent":
        return f"{num}%"
    return f"{num} {unit.replace('_', ' ')}{'' if v == 1 else 's'}" if unit not in ("KB", "MB", "GB", "TB") else f"{num} {unit}"


def numeric_finding(fa: Fact, fb: Fact, pages: list[PageDoc], *, verified: bool) -> Finding:
    return Finding(
        type="contradiction", severity="high" if verified else "medium",
        title=f"Conflicting values: {_fmt(fa.value, fa.unit)} vs {_fmt(fb.value, fb.unit)}",
        detail=("Two pages state different values for what looks like the same fact. "
                "An AI assistant could quote either one to a customer.")
               + ("" if verified else " (Detected by pattern matching; review to confirm.)"),
        page_a=pages[fa.page].url, page_b=pages[fb.page].url,
        quote_a=fa.sentence, quote_b=fb.sentence,
        extra={"unit": fa.unit, "values": [fa.value, fb.value], "verified": verified, "method": "numeric"},
        points=6.0 if verified else 3.0,
    )


# ---------------------------------------------------------------- staleness

YEAR_CONTEXT = re.compile(
    r"(last\s+updated|last\s+modified|updated(?:\s+on)?|as\s+of|published(?:\s+on)?|effective(?:\s+(?:date|from))?|"
    r"reviewed(?:\s+on)?)\s*[:\-–]?\s*(?:[A-Za-z]{3,9}\.?\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?)?((?:19|20)\d{2})", re.I)
VERSION_RE = re.compile(r"\b(?:v|version\s+)(\d{1,3})(?:\.\d+){0,2}\b", re.I)


def check_staleness(pages: list[PageDoc], *, now: datetime | None = None, stale_years: int = 3) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []

    # 1. Explicit "Last updated 2019"-style dates in the text.
    for p in pages:
        m = None
        for cand in YEAR_CONTEXT.finditer(p.text):
            year = int(cand.group(2))
            if year <= now.year - stale_years and (m is None or year < int(m.group(2))):
                m = cand
        if m:
            year = int(m.group(2))
            age = now.year - year
            findings.append(Finding(
                type="stale", severity="medium" if age >= 5 else "low",
                title=f"Says “{_short(m.group(0), 60)}” ({age} years ago)",
                detail="This page declares itself old. Review it, or remove it so AI answers don't rely on outdated facts.",
                page_a=p.url, quote_a=_context(p.text, m.start(), m.end()),
                extra={"year": year, "method": "declared_date"}, points=2.0 if age >= 5 else 1.0,
            ))

    # 2. Version drift: page only mentions versions well behind the newest one used across the site.
    page_versions: dict[int, set[int]] = {}
    version_pages: Counter[int] = Counter()
    for i, p in enumerate(pages):
        vs = {int(v) for v in VERSION_RE.findall(p.text) if 0 < int(v) < 100}
        if vs:
            page_versions[i] = vs
            version_pages.update(vs)
    if version_pages:
        latest = max(v for v, c in version_pages.items() if c >= 3) if any(c >= 3 for c in version_pages.values()) else None
        if latest and latest >= 3:
            for i, vs in page_versions.items():
                if max(vs) <= latest - 2 and latest not in vs:
                    p = pages[i]
                    m = VERSION_RE.search(p.text)
                    findings.append(Finding(
                        type="stale", severity="low",
                        title=f"Refers to v{max(vs)} while most pages mention v{latest}",
                        detail="This page may describe an old version. Check it is still accurate or label it as legacy.",
                        page_a=p.url, quote_a=_context(p.text, m.start(), m.end()) if m else "",
                        extra={"versions": sorted(vs), "latest": latest, "method": "version_drift"}, points=1.0,
                    ))

    # 3. Page metadata says it hasn't been modified in years.
    flagged = {f.page_a for f in findings}
    for p in pages:
        if p.url in flagged or not p.last_modified:
            continue
        age_days = (now - p.last_modified).days
        if age_days >= 365 * stale_years:
            findings.append(Finding(
                type="stale", severity="low",
                title=f"Not modified in {age_days // 365} years",
                detail="The page's last-modified date is old. It may still be correct, but it's worth a review.",
                page_a=p.url, extra={"last_modified": p.last_modified.date().isoformat(), "method": "metadata"},
                points=0.5,
            ))
    return findings


# ---------------------------------------------------------------- structure


def check_thin_pages(pages: list[PageDoc], *, min_words: int = 30) -> list[Finding]:
    """Placeholder pages. Words in site-wide template blocks (footers, feedback widgets) don't count."""
    boiler = boilerplate_keys(pages)
    out = []
    for p in pages:
        words = sum(len(b.text.split()) for b in p.blocks if b.kind == "text" and _norm_key(b.text) not in boiler)
        if words < min_words and p.link_count < 15:
            out.append(Finding(
                type="thin_page", severity="low", title=f"Very little content ({words} words)",
                detail="Near-empty pages (placeholders, 'coming soon') add noise to AI search results.",
                page_a=p.url, quote_a=_short(p.text, 200), points=0.5))
    return out


def check_orphans(pages: list[PageDoc], start_url: str) -> list[Finding]:
    linked = sum(1 for p in pages if p.inbound > 0)
    if linked < 5:
        return []  # not enough link structure to judge
    out = []
    for p in pages:
        if p.from_sitemap and p.inbound == 0 and p.url != start_url:
            out.append(Finding(
                type="orphan_page", severity="low", title="No other page links here",
                detail="Listed in the sitemap but not linked from the docs. Orphan pages are rarely maintained, "
                       "yet AI retrieval still finds them.", page_a=p.url, points=0.5))
    return out


# ---------------------------------------------------------------- helpers


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _context(text: str, start: int, end: int, pad: int = 90) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    snippet = text[a:b].replace("\n", " ")
    return ("…" if a > 0 else "") + snippet.strip() + ("…" if b < len(text) else "")
