#!/usr/bin/env python3
"""Post-deploy check against the live site: python3 scripts/live_test.py https://infoharness.com [URL_TO_SCAN]
Exits non-zero (with the reason) if anything is wrong. Uses only the standard library."""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/")
SCAN_URL = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "https://docs.python-requests.org/en/latest/"
MIN_PAGES = 5
fails = []


def call(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"content-type": "application/json",
                                                                  "user-agent": "infoharness-live-test"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        fails.append(name)


s, b = call("/healthz")
check("healthz", s == 200 and '"ok":true' in b.replace(" ", ""), f"{s} {b[:60]}")
s, b = call("/")
check("home page", s == 200 and "Scan my docs" in b, str(s))
for path, needle in (("/pricing", "Pricing"), ("/login", "Email me a link")):
    s, b = call(path)
    check(f"{path} page", s == 200 and needle in b, str(s))
s, b = call("/account")
check("/account needs login", s == 200 and "Email me a link" in b, str(s))  # redirected to the login form
s, b = call("/api/scans", {"url": "ftp://example.com"})
check("rejects non-http URL", s == 400, str(s))
s, b = call("/api/scans", {"url": "http://127.0.0.1:8730/"})
if s == 201:  # accepted into the queue: the worker must then refuse it
    sid = json.loads(b)["id"]
    for _ in range(30):
        time.sleep(2)
        d = json.loads(call(f"/api/scans/{sid}")[1])
        if d["status"] in ("done", "failed"):
            break
    check("blocks private addresses (SSRF)", d["status"] == "failed", d["status"])
else:
    check("blocks private addresses (SSRF)", s == 400, str(s))

s, b = call("/api/scans", {"url": SCAN_URL, "fresh": True})
if s == 429:
    print("SKIP real scan: per-IP daily limit reached")
else:
    check("scan accepted", s == 201, f"{s} {b[:120]}")
    if s == 201:
        sid = json.loads(b)["id"]
        d = {}
        deadline = time.time() + 240
        while time.time() < deadline:
            time.sleep(4)
            d = json.loads(call(f"/api/scans/{sid}")[1])
            if d["status"] in ("done", "failed"):
                break
        check("scan finished", d.get("status") == "done", f"status={d.get('status')} error={d.get('error')}")
        if d.get("status") == "done":
            check(f"crawled >= {MIN_PAGES} pages", (d.get("pages_crawled") or 0) >= MIN_PAGES, f"pages={d.get('pages_crawled')}")
            check("has a grade", d.get("grade") in list("ABCDF"), str(d.get("grade")))
            kinds = {}
            for i in d.get("issues", []):
                kinds[i["type"]] = kinds.get(i["type"], 0) + 1
            print("     score", d.get("score"), "grade", d.get("grade"), "issues", kinds)
            check("report page", call(f"/r/{sid}")[0] == 200)
            check("badge", call(f"/badge/{sid}.svg")[0] == 200)

print("\nRESULT:", "ALL PASSED" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
