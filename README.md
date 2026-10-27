# infoharness

Docs health checker for the AI era. Paste a help-center or docs URL and get a report of
**contradictions, duplicate pages, repeated sections, outdated content, broken links, thin pages and
orphan pages** — the problems that make support bots and AI assistants give wrong answers.

```
browser ──► Caddy (HTTPS) ──► app (FastAPI, :8000) ──► PostgreSQL ◄── worker (crawl + analyse)
```

| Part | What it does |
|---|---|
| `app/main.py` | Landing page, report page, JSON API, SVG badge, rate limiting |
| `app/worker.py` | Claims queued scans from Postgres (`SKIP LOCKED`), safe to run several |
| `app/crawler.py` | Async crawler: robots.txt, sitemaps, scope to the docs folder, size/time caps |
| `app/security.py` | SSRF guard: public IPs only, standard ports, no credentials, checked on every redirect |
| `app/analysis.py` | Chunking, similarity, and all checks |
| `app/llm.py` | Optional LLM verification; rejects any finding whose quotes aren't on the page |
| `app/scoring.py` | `score = 100·exp(−0.8 · penalty / pages)`, graded A–F |

## Deploy (DigitalOcean droplet or any Ubuntu server)

1. **DNS:** add `A` records `infoharness.com` and `www.infoharness.com` → your server IP.
2. **Copy the project** to the server, e.g. `scp -r infoharness root@SERVER:/opt/`.
3. **Run:**
   ```bash
   cd /opt/infoharness
   sudo ./deploy.sh infoharness.com you@example.com
   ```
   The script installs Docker if needed, creates `.env` with random secrets, checks ports and DNS,
   builds, starts Postgres + app + worker + Caddy, and waits for the health check.
   HTTPS certificates are issued automatically on first visit.

**Already running nginx on 80/443?** Use `sudo ./deploy.sh infoharness.com "" --proxy none`, then follow
`deploy/nginx.conf.example` (includes the certbot command).

**Small server (2 GB RAM)?** The stack uses roughly 400–700 MB while scanning. If the server also
runs other databases, add 2 GB swap — `deploy.sh` prints the exact command when swap is missing.

### Day-2 commands

| Task | Command |
|---|---|
| Logs | `docker compose logs -f app worker` |
| Update after copying new code | `sudo ./deploy.sh` |
| More scan throughput | `docker compose up -d --scale worker=2` |
| Database shell | `docker compose exec db psql -U harness infoharness` |
| Backup | `docker compose exec db pg_dump -U harness infoharness > backup.sql` |
| Stop | `docker compose --profile caddy down` (data is kept in volumes) |

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_PAGES` | 300 | Pages per scan |
| `MAX_SCAN_SECONDS` | 600 | Hard time budget per scan |
| `SCANS_PER_IP_PER_DAY` | 5 | Free-tier abuse limit |
| `CACHE_HOURS` | 24 | Same URL within this window returns the existing report |
| `CHECK_EXTERNAL_LINKS` | true | Also check links leaving the site (up to `MAX_LINK_CHECKS`) |
| `LLM_PROVIDER` | none | `anthropic` or `openai` to verify contradictions with AI |
| `ANTHROPIC_API_KEY` / `LLM_MODEL` | — / `claude-haiku-5-5` | Used when `LLM_PROVIDER=anthropic` |
| `EMBEDDINGS_PROVIDER` | tfidf | `openai` for better semantic matching (any OpenAI-compatible endpoint) |

### Without vs with an AI key

| | No key (free) | With `LLM_PROVIDER` set |
|---|---|---|
| Numeric contradictions (prices, days, limits, %) | ✅ pattern-matched, marked "review" | ✅ verified, unverifiable ones dropped |
| Wording contradictions ("admins can…" vs "only owners can…") | ❌ | ✅ |
| Cost per scan | $0 | Roughly cents with a small model (measure on your own scans) |

The LLM must quote both conflicting sentences; if either quote isn't found on the page, the finding is
discarded. That keeps hallucinated issues out of reports.

## Local development

```bash
pip install -r requirements.txt pytest
python -m pytest                      # unit + end-to-end tests against a fake docs site
ALLOW_PRIVATE_HOSTS=true uvicorn app.main:app --reload    # http://localhost:8000
ALLOW_PRIVATE_HOSTS=true python -m app.worker             # second terminal
```

SQLite is used by default locally; production uses Postgres via `DATABASE_URL`.

## Known limits (v0.1)

| Limit | Notes |
|---|---|
| JavaScript-only docs sites | Not rendered. Most docs platforms serve HTML; SPA-only sites report "couldn't read any pages" |
| Login-protected docs | Public pages only. Notion/Drive/Confluence connectors are the paid-tier roadmap |
| Payments | Not wired yet. Stripe doesn't onboard Nepal-based sellers directly — check Paddle/Lemon Squeezy eligibility or use a US entity before adding paid plans |
| DNS rebinding | URLs are checked on every hop, but a malicious DNS server could still race the check. Keep the server's own services bound to localhost |

## Roadmap

1. Accounts + paid monitoring (weekly rescans, email alerts)
2. Notion, Google Drive, Confluence, Zendesk, Intercom connectors
3. Coverage gaps: real customer questions the docs can't answer
4. API + MCP endpoint so AI agents can check doc freshness before answering
