#!/usr/bin/env python3
"""Scan many real docs sites with the production pipeline (no server needed) and write a review report.
   python3 bench/run.py [sites_file] [out_dir]      -- runs on a GitHub runner (needs internet)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sites_file = sys.argv[1] if len(sys.argv) > 1 else "bench/sites.txt"
out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "bench/results")
out_dir.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/bench.db"
os.environ.setdefault("MAX_PAGES", "150")
os.environ.setdefault("MAX_SCAN_SECONDS", "240")
os.environ.setdefault("CHECK_EXTERNAL_LINKS", "true")
os.environ.setdefault("LLM_PROVIDER", "none")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select  # noqa: E402
from app import config, db  # noqa: E402
from app.db import Issue, Scan, session_scope  # noqa: E402
from app.pipeline import run_scan  # noqa: E402

config.reload_settings()
db.init_engine()
urls = [u.strip() for u in Path(sites_file).read_text().splitlines() if u.strip() and not u.startswith("#")]
rows, detail = [], {}
for n, url in enumerate(urls, 1):
    t0 = time.time()
    with session_scope() as s:
        sc = Scan(url=url, client_hash="bench")
        s.add(sc); s.flush(); sid = sc.id
    try:
        run_scan(sid)
    except Exception as e:  # a crash is a finding too
        print(f"[{n}/{len(urls)}] CRASH {url}: {e!r}", flush=True)
    with session_scope() as s:
        sc = s.get(Scan, sid)
        iss = s.execute(select(Issue).where(Issue.scan_id == sid)).scalars().all()
        counts = {}
        for i in iss:
            counts[i.type] = counts.get(i.type, 0) + 1
        rows.append({"url": url, "status": sc.status, "error": sc.error, "pages": sc.pages_crawled,
                     "grade": sc.grade, "score": sc.score, "secs": round(time.time() - t0), "issues": counts})
        detail[url] = [{"type": i.type, "severity": i.severity, "title": i.title, "page_a": i.page_a,
                        "page_b": i.page_b, "quote_a": (i.quote_a or "")[:200], "quote_b": (i.quote_b or "")[:200]}
                       for i in iss]
    r = rows[-1]
    print(f"[{n}/{len(urls)}] {r['status']:7} pages={r['pages']} grade={r['grade']} {r['issues']} {url} {r['error'] or ''}", flush=True)

(out_dir / "summary.json").write_text(json.dumps(rows, indent=1))
(out_dir / "issues.json").write_text(json.dumps(detail, indent=1))
md = ["| site | status | pages | grade | issues |", "|---|---|---|---|---|"]
for r in rows:
    md.append(f"| {r['url']} | {r['status']}{(': ' + r['error']) if r['error'] else ''} | {r['pages']} | {r['grade']} | {r['issues']} |")
(out_dir / "summary.md").write_text("\n".join(md) + "\n")
print("\nwritten to", out_dir)
