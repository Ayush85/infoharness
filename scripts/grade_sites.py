#!/usr/bin/env python3
"""Scan a list of docs sites through the live API and write a ranked summary for a "state of docs" post.

  python3 scripts/grade_sites.py sites.txt [--base https://infoharness.com] [--key ih_...] [--out results]

sites.txt: one URL per line (lines starting with # are ignored). Create a free API key on your account page
(an account has a higher daily scan limit than anonymous use). Standard library only.
Writes results.json (raw), results.md (table + the stats you can quote) and prints progress.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

ap = argparse.ArgumentParser()
ap.add_argument("sites")
ap.add_argument("--base", default="https://infoharness.com")
ap.add_argument("--key", default="")
ap.add_argument("--out", default="results")
ap.add_argument("--wait", type=int, default=900, help="max seconds to wait per site")
a = ap.parse_args()
BASE = a.base.rstrip("/")


def call(path, body=None):
    headers = {"content-type": "application/json", "user-agent": "infoharness-grade-sites"}
    if a.key:
        headers["authorization"] = "Bearer " + a.key
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


urls = [l.strip() for l in open(a.sites) if l.strip() and not l.startswith("#")]
jobs = []
for u in urls:
    st, d = call("/api/scans", {"url": u})
    if st in (200, 201):
        jobs.append((u, d["id"]))
        print(f"queued  {u}")
    else:
        print(f"skipped {u}: {st} {d.get('detail', '')}")
        if st == 429:
            print("Daily scan limit reached. Run again tomorrow with the remaining sites."); break

results, t0 = [], time.time()
for u, sid in jobs:
    while True:
        st, d = call(f"/api/scans/{sid}")
        if d.get("status") in ("done", "failed") or time.time() - t0 > a.wait * max(1, len(jobs)):
            break
        time.sleep(8)
    if d.get("status") == "done":
        c = Counter(i["type"] for i in d.get("issues", []))
        results.append({"url": u, "grade": d["grade"], "score": d["score"], "pages": d["pages_crawled"],
                        "report": d["report_url"], "counts": dict(c), "issues": d.get("issues", [])})
        print(f"done    {u}  {d['grade']} ({d['score']})  {dict(c)}")
    else:
        print(f"failed  {u}: {d.get('error')}")

json.dump(results, open(a.out + ".json", "w"), indent=1)
results.sort(key=lambda r: r["score"])
n = len(results)
if n:
    with_contra = sum(1 for r in results if r["counts"].get("contradiction"))
    with_broken = sum(1 for r in results if r["counts"].get("broken_link"))
    with_dup = sum(1 for r in results if r["counts"].get("duplicate_page"))
    below_c = sum(1 for r in results if r["grade"] in ("D", "F"))
    md = [f"# Docs health of {n} help centers\n",
          f"- {with_contra} of {n} ({with_contra*100//n}%) had at least one pair of pages that contradict each other",
          f"- {with_dup} of {n} ({with_dup*100//n}%) had near-duplicate pages",
          f"- {with_broken} of {n} ({with_broken*100//n}%) had broken links",
          f"- {below_c} of {n} ({below_c*100//n}%) scored D or F\n",
          "**Review every example by hand before quoting it. Findings come from automated pattern matching.**\n",
          "| Site | Grade | Pages read | Contradictions | Duplicates | Broken links | Report |", "|---|---|---|---|---|---|---|"]
    for r in results:
        c = r["counts"]
        md.append(f"| {r['url']} | {r['grade']} | {r['pages']} | {c.get('contradiction', 0)} | {c.get('duplicate_page', 0)} | "
                  f"{c.get('broken_link', 0)} | {r['report']} |")
    open(a.out + ".md", "w").write("\n".join(md) + "\n")
    print(f"\nWrote {a.out}.md and {a.out}.json")
