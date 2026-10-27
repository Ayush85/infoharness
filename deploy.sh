#!/usr/bin/env bash
# One-command deploy / update for infoharness.
#
#   sudo ./deploy.sh infoharness.com you@example.com                 # auto-detects your server's PostgreSQL
#   sudo ./deploy.sh infoharness.com you@example.com --db host       # use the server's PostgreSQL (shared)
#   sudo ./deploy.sh infoharness.com you@example.com --db bundled    # run a private Postgres container
#   sudo ./deploy.sh infoharness.com "" --proxy none                 # nginx already owns ports 80/443
#   sudo ./deploy.sh infoharness.com "" --proxy none --port 8730     # app on another local port
#   sudo ./deploy.sh                                                 # update: rebuild + restart, same settings
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- arguments
DOMAIN_ARG=""; EMAIL_ARG=""; PROXY_ARG=""; DB_ARG=""; PORT_ARG=""
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy) PROXY_ARG="${2:-}"; shift 2 ;;
    --db)    DB_ARG="${2:-}"; shift 2 ;;
    --port)  PORT_ARG="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) positional+=("$1"); shift ;;
  esac
done
DOMAIN_ARG="${positional[0]:-}"; EMAIL_ARG="${positional[1]:-}"
[[ -z "$PROXY_ARG" || "$PROXY_ARG" =~ ^(caddy|none)$ ]] || die "--proxy must be caddy or none"
[[ -z "$DB_ARG" || "$DB_ARG" =~ ^(host|bundled)$ ]] || die "--db must be host or bundled"
[[ -z "$PORT_ARG" || "$PORT_ARG" =~ ^[0-9]{2,5}$ ]] || die "--port must be a number"

set_env() {  # set_env KEY VALUE  -> replaces or appends in .env
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    echo "${key}=${val}" >> .env
  fi
}
get_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# ---------------------------------------------------------------- docker
if ! command -v docker >/dev/null 2>&1; then
  [[ $EUID -eq 0 ]] || die "Docker is not installed. Re-run with sudo to install it automatically."
  say "Installing Docker…"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || die "The Docker Compose plugin is missing. Install docker-compose-plugin."

# ---------------------------------------------------------------- .env
if [[ ! -f .env ]]; then
  [[ -n "$DOMAIN_ARG" ]] || die "First deploy needs a domain: ./deploy.sh infoharness.com you@example.com"
  [[ -n "$EMAIL_ARG" || "$PROXY_ARG" == "none" ]] || die "Caddy mode needs an email for HTTPS certificates: ./deploy.sh $DOMAIN_ARG you@example.com"
  say "Creating .env"
  cp .env.example .env
  set_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  set_env IP_HASH_SALT "$(openssl rand -hex 24)"
  chmod 600 .env
fi
if [[ -n "$DOMAIN_ARG" ]]; then
  set_env DOMAIN "$DOMAIN_ARG"
  set_env PUBLIC_BASE_URL "https://$DOMAIN_ARG"
fi
[[ -n "$EMAIL_ARG" ]] && set_env ACME_EMAIL "$EMAIL_ARG"
[[ -n "$PROXY_ARG" ]] && set_env PROXY_MODE "$PROXY_ARG"
[[ -n "$PORT_ARG" ]] && set_env APP_PORT "$PORT_ARG"

DOMAIN="$(get_env DOMAIN)"
PROXY_MODE="$(get_env PROXY_MODE)"; PROXY_MODE="${PROXY_MODE:-caddy}"
APP_PORT="$(get_env APP_PORT)"; APP_PORT="${APP_PORT:-8000}"

# ---------------------------------------------------------------- database mode
DB_MODE="${DB_ARG:-$(get_env DB_MODE)}"
if [[ -z "$DB_MODE" ]]; then
  if { ss -ltnp 2>/dev/null | grep -E ':5432\b' | grep -q postgres; } || \
     { command -v pg_isready >/dev/null && pg_isready -q -h 127.0.0.1 -p 5432; }; then
    DB_MODE=host
    say "Found PostgreSQL running on this server — infoharness will get its own database in it."
  else
    DB_MODE=bundled
    say "No local PostgreSQL found — running a private Postgres container."
  fi
fi
set_env DB_MODE "$DB_MODE"

if [[ "$DB_MODE" == "host" ]]; then
  if [[ -z "$(get_env DATABASE_URL)" ]]; then
    [[ $EUID -eq 0 ]] || die "Creating the database needs sudo."
    say "Provisioning database 'infoharness' in the server's PostgreSQL…"
    ./scripts/provision-project.sh infoharness
    pg_url="$(grep -E '^POSTGRES_URL=' "${AYDEXIS_STATE_DIR:-/etc/aydexis}/projects/infoharness.env" | cut -d= -f2-)"
    [[ -n "$pg_url" ]] || die "Provisioning didn't produce a connection URL."
    set_env DATABASE_URL "${pg_url/postgresql:\/\//postgresql+psycopg://}"
  fi
  set_env COMPOSE_FILE "docker-compose.yml:docker-compose.host.yml"
  profiles=""
else
  [[ "$(get_env POSTGRES_PASSWORD)" != "change-me" ]] || die "Set a real POSTGRES_PASSWORD in .env"
  set_env COMPOSE_FILE "docker-compose.yml"
  profiles="bundled-db"
fi
[[ "$PROXY_MODE" == "caddy" ]] && profiles="${profiles:+$profiles,}caddy"
# docker compose reads COMPOSE_FILE / COMPOSE_PROFILES from .env, so day-2 commands
# (docker compose logs / ps / restart) automatically use the same setup.
set_env COMPOSE_PROFILES "$profiles"

# ---------------------------------------------------------------- pre-flight checks
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 3000 && swap_mb == 0 )); then
  warn "This server has ${mem_mb} MB RAM and no swap. Recommended: add 2 GB swap:"
  warn "  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab"
fi

if ss -ltnp "( sport = :${APP_PORT} )" 2>/dev/null | grep LISTEN | grep -vqE 'docker|uvicorn|python'; then
  die "Port ${APP_PORT} is already used by another program. Set APP_PORT to a free port in .env."
fi

if [[ "$PROXY_MODE" == "caddy" ]]; then
  for p in 80 443; do
    if ss -ltnp "( sport = :$p )" 2>/dev/null | grep LISTEN | grep -vqE 'docker|caddy'; then
      die "Port $p is used by another program (nginx/apache?). Stop it, or deploy with: ./deploy.sh $DOMAIN \"\" --proxy none"
    fi
  done
  public_ip=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
  dns_ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
  if [[ -n "$public_ip" && "$dns_ip" != "$public_ip" ]]; then
    warn "DNS for $DOMAIN points to '${dns_ip:-nothing}', but this server is $public_ip."
    warn "Add an A record  $DOMAIN -> $public_ip  (and www). HTTPS certificates are issued once DNS is correct."
  fi
fi

# ---------------------------------------------------------------- build & start
say "Building image…"
docker compose build --pull app

say "Starting services (database: $DB_MODE, proxy: $PROXY_MODE)…"
docker compose up -d --remove-orphans

say "Waiting for the app to become healthy…"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null 2>&1; then
    say "infoharness is running."
    if [[ "$PROXY_MODE" == "caddy" ]]; then
      echo "   Open: https://$DOMAIN   (certificate is issued on first visit once DNS points here)"
    else
      echo "   App listens on 127.0.0.1:${APP_PORT}. Point your nginx at it — see deploy/nginx.conf.example"
    fi
    echo "   Logs:    docker compose logs -f app worker"
    echo "   Scale:   docker compose up -d --scale worker=2"
    echo "   Update:  git pull && sudo ./deploy.sh"
    exit 0
  fi
  sleep 2
done
docker compose logs --tail 50 app worker
die "The app didn't become healthy. See the logs above."
