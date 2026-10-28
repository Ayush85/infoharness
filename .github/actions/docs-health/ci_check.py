#!/usr/bin/env python3
"""Scan a docs site with infoharness and fail when the grade is below a threshold. Standard library only."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("INFOHARNESS_URL", "https://infoharness.com").rstrip("/")
GRADES = "ABCDF"


def call(method, path, body=None, key=""):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "infoharness-action"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    url = os.environ["INPUT_URL"]
    fail_below = (os.environ.get("INPUT_FAIL_BELOW") or "").strip().upper()
    key = os.environ.get("INPUT_API_KEY", "")
    max_wait = int(os.environ.get("INPUT_MAX_WAIT") or 900)
    fresh = (os.environ.get("INPUT_FRESH") or "true").lower() == "true"
    if fail_below and fail_below not in GRADES:
        print(f"::error::fail-below must be one of A, B, C, D, F (got {fail_below!r})")
        return 2

    status, data = call("POST", "/api/scans", {"url": url, "fresh": fresh}, key)
    if status not in (200, 201):
        print(f"::error::Could not start the scan ({status}): {data.get('detail', 'unknown error')}")
        return 2
    scan_id = data["id"]
    print(f"Scan started: {BASE}/r/{scan_id}")
    deadline = time.time() + max_wait
    while True:
        status, data = call("GET", f"/api/scans/{scan_id}")
        if data.get("status") in ("done", "failed"):
            break
        if time.time() > deadline:
            print(f"::error::The scan did not finish within {max_wait} seconds. Report: {BASE}/r/{scan_id}")
            return 2
        live = data.get("live") or {}
        print(f"  {data.get('stage', 'waiting')}: {data.get('progress', 0)}% ({data.get('pages_crawled', 0)} pages)")
        time.sleep(5)

    if data["status"] == "failed":
        print(f"::error::The scan failed: {data.get('error')}")
        return 2
    grade, score = data["grade"], data["score"]
    summary = data.get("summary", {})
    report = f"{BASE}/r/{scan_id}"
    output("grade", grade)
    output("score", round(score))
    output("issues", summary.get("total_issues", 0))
    output("report-url", report)
    by_type = {k: v for k, v in (summary.get("by_type") or {}).items() if v}
    line = f"Docs health for {url}: grade {grade} ({round(score)}/100), {summary.get('total_issues', 0)} issues"
    print(line)
    for k, v in by_type.items():
        print(f"  {k.replace('_', ' ')}: {v}")
    print(f"Report: {report}")
    md = os.environ.get("GITHUB_STEP_SUMMARY")
    if md:
        rows = "\n".join(f"| {k.replace('_', ' ')} | {v} |" for k, v in by_type.items())
        with open(md, "a") as f:
            f.write(f"### Docs health: {grade} ({round(score)}/100)\n\n{url}\n\n| Check | Findings |\n|---|---|\n{rows}\n\n[Open the full report]({report})\n")
    if fail_below and GRADES.index(grade) > GRADES.index(fail_below):
        print(f"::error::Docs health grade {grade} is below the required {fail_below}. See {report}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
