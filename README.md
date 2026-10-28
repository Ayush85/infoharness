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

## Automatic deploys from GitHub (recommended)

1. On the droplet, once: `sudo bash scripts/setup-deploy-access.sh` (from a checkout of this repo, or download it).
   It creates a dedicated `ih-deploy` user (change with `DEPLOY_USER=name`) with its own SSH key, so other
   projects on the same server can have their own user and key without overwriting this one. It prints the values to add in GitHub.
2. GitHub → repo → Settings → Secrets and variables → Actions: add the printed secrets
   (`DEPLOY_HOST`, `DEPLOY_PORT`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_SSH_KEY`) and variables
   (`DEPLOY_DOMAIN`, `DEPLOY_EMAIL`, `DEPLOY_USER`, optional `DEPLOY_PATH`, `DEPLOY_ARGS`).
3. Actions → **Deploy** → Run workflow. After that, every push to `main` runs the tests, uploads the
   code over SSH (host key pinned), runs `deploy.sh` and checks `https://DOMAIN/healthz`.

Your `.env` on the server is never overwritten by deploys. Revoke access any time:
`sudo rm /home/deploy/.ssh/authorized_keys`.

## Accounts, watched sites and plans

| Feature | How it works |
|---|---|
| Sign in | Passwordless: a one-time link by email (valid 15 min, single use, rate limited), then a signed cookie |
| Watched sites | `/account`: Free = `FREE_SITES` (1, monthly), Pro = `PRO_SITES` (5, daily/weekly/monthly). The worker queues rescans |
| Alerts | After each rescan the owner is emailed only when there are **new** problems or the grade dropped (plus a first report) |
| Pricing | `/pricing`. With no `CHECKOUT_URL` it shows a waitlist (table `waitlist`) |
| Payments | Lemon Squeezy webhook `POST /webhooks/lemonsqueezy` (HMAC-SHA256 `X-Signature`). Subscription events switch the plan by email |

Set these without touching the server: in GitHub add **secrets** `RESEND_API_KEY`, `SECRET_KEY`, `LS_WEBHOOK_SECRET`
and **variables** `MAIL_FROM`, `ADMIN_EMAILS`, `CHECKOUT_URL`, `PRO_PRICE`. Each deploy writes the non-empty ones into
the server's `.env`. Email needs a verified sending domain at Resend (add its DNS records for infoharness.com).

## Deploy manually (DigitalOcean droplet or any Ubuntu server)

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

**Database:** if PostgreSQL is already running on the server, `deploy.sh` detects it and creates a
separate `infoharness` database and login inside it (nothing else on the server is touched). Force a mode
with `--db host` or `--db bundled`.

**Already running nginx on 80/443?** Use `sudo ./deploy.sh infoharness.com "" --proxy none`, then follow
`deploy/nginx.conf.example` (includes the certbot command).

**Small server (2 GB RAM)?** The stack uses roughly 400–700 MB while scanning. If the server also
runs other databases, add 2 GB swap — `deploy.sh` prints the exact command when swap is missing.

## One server, many projects (shared PostgreSQL + ClickHouse)

The `scripts/` folder turns your droplet into a small shared data platform. Every project gets its own
database and login, so projects can't read each other's data, and credentials live in one place:
`/etc/aydexis/projects/<project>.env` (root-only).

| Task | Command |
|---|---|
| New project, PostgreSQL only | `sudo ./scripts/provision-project.sh myproject` |
| New project with ClickHouse too | `sudo ./scripts/provision-project.sh myproject --clickhouse` |
| Prepare ClickHouse once (installs if missing) | `sudo ./scripts/setup-clickhouse.sh` |
| Same, and bind ClickHouse to localhost only | `sudo ./scripts/setup-clickhouse.sh --localhost-only` |
| See a project's connection URLs | `sudo cat /etc/aydexis/projects/myproject.env` |

What each project gets:

| | PostgreSQL | ClickHouse |
|---|---|---|
| Database | `myproject` | `myproject` |
| Login | `myproject` + random password | `myproject` + same password, localhost only |
| Isolation | Only its own role can connect | Grants on its own database only |
| Fair sharing | — | Per-project memory cap (600 MB) and 120 s query timeout |

Scripts are safe to re-run: existing databases are kept and the stored password is re-applied.
`setup-clickhouse.sh` only adds its own files under `/etc/clickhouse-server/{config,users}.d/` and never
edits your existing configuration. It creates an `aydexis_admin` user with access management (needed to
create per-project users) and caps ClickHouse at 35% of RAM so it coexists with PostgreSQL and your apps.

**ClickHouse already running in Docker?** `setup-clickhouse.sh` detects it and stops instead of installing
a second copy. Provision projects with your existing admin login; the script then talks to the HTTP port
(8123) and also allows logins from the Docker bridge range:
`CLICKHOUSE_ADMIN_USER=... CLICKHOUSE_ADMIN_PASSWORD=... sudo -E ./scripts/provision-project.sh myproject --clickhouse --no-postgres`

**PostgreSQL in Docker or a managed database?** Set the admin URL instead of using the local `postgres` user:
`PG_ADMIN_URL=postgresql://postgres:PASS@127.0.0.1:5432/postgres sudo -E ./scripts/provision-project.sh myproject`

**Connecting from Docker:** containers on the default bridge network can't reach `127.0.0.1` on the host.
Use `network_mode: host` (as infoharness does in `docker-compose.host.yml`) or point the app at the
Docker bridge gateway and allow it in `pg_hba.conf`.

infoharness itself only needs PostgreSQL. ClickHouse is there for future analytics-heavy projects
(event tracking, logs, aydex.cloud).

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
