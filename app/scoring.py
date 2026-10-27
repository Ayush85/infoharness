"""Turns findings into a 0-100 health score and an A-F grade.

score = 100 * exp(-0.8 * penalty_points / pages)

Points are per finding (set by each check): verified contradiction 6, unverified 3,
duplicate page 3 per extra copy, stale 0.5-2, broken internal link 2, external 0.5, etc.
Normalising by page count means a large site isn't punished just for being large.
"""
from __future__ import annotations

import math
from collections import Counter

from app.analysis import Finding

TYPE_LABELS = {
    "contradiction": "Contradictions",
    "duplicate_page": "Duplicate pages",
    "duplicate_section": "Repeated sections",
    "stale": "Outdated content",
    "broken_link": "Broken links",
    "admin_link": "Admin and draft links",
    "thin_page": "Thin pages",
    "orphan_page": "Orphan pages",
}


def grade_for(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 65:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def score(findings: list[Finding], pages: int) -> tuple[float, str, dict]:
    penalty = sum(f.points for f in findings)
    density = penalty / max(pages, 5)
    value = round(100 * math.exp(-0.8 * density), 1)
    by_type = Counter(f.type for f in findings)
    by_sev = Counter(f.severity for f in findings)
    summary = {
        "penalty": round(penalty, 1),
        "pages": pages,
        "by_type": {k: by_type.get(k, 0) for k in TYPE_LABELS},
        "by_severity": {k: by_sev.get(k, 0) for k in ("high", "medium", "low")},
        "total_issues": len(findings),
    }
    return value, grade_for(value), summary
