#!/usr/bin/env bash
# One-command deploy / update for infoharness.
#
#   First deploy:   sudo ./deploy.sh infoharness.com you@example.com
#   Behind nginx:   sudo ./deploy.sh infoharness.com you@example.com --proxy none
#   Update:         sudo ./deploy.sh            (re-builds and restarts with the existing .env)
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN_ARG="${1:-}"
EMAIL_ARG="${2:-}"
PROXY_ARG=""
if [[ "${3:-}" == "--proxy" ]]; then PROXY_ARG="${4:-caddy}"; fi

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

set_env() {  # set_env KEY VALUE  -> replaces or appends in .env
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    echo "${key}=${val}" >> .env
  fi
}
get_env() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- ; }

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

DOMAIN="$(get_env DOMAIN)"
PROXY_MODE="$(get_env PROXY_MODE)"; PROXY_MODE="${PROXY_MODE:-caddy}"
APP_PORT="$(get_env APP_PORT)"; APP_PORT="${APP_PORT:-8000}"
[[ "$(get_env POSTGRES_PASSWORD)" != "change-me" ]] || die "Set a real POSTGRES_PASSWORD in .env"

# ---------------------------------------------------------------- pre-flight checks
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 3000 && swap_mb == 0 )); then
  warn "This server has ${mem_mb} MB RAM and no swap. Recommended: add 2 GB swap:"
  warn "  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab"
fi

if ss -ltn "( sport = :${APP_PORT} )" 2>/dev/null | grep -q LISTEN && ! docker compose ps app 2>/dev/null | grep -q running; then
  die "Port ${APP_PORT} is already in use. Set APP_PORT to a free port in .env."
fi

if [[ "$PROXY_MODE" == "caddy" ]]; then
  for p in 80 443; do
    if ss -ltnp "( sport = :$p )" 2>/dev/null | grep LISTEN | grep -vq docker; then
      die "Port $p is used by another program (nginx/apache?). Stop it, or deploy with: ./deploy.sh $DOMAIN \"\" --proxy none"
    fi
  done
  public_ip=$(curl -fsS --max-time 5 https://api.ipify.org || true)
  dns_ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
  if [[ -n "$public_ip" && "$dns_ip" != "$public_ip" ]]; then
    warn "DNS for $DOMAIN points to '${dns_ip:-nothing}', but this server is $public_ip."
    warn "Add an A record  $DOMAIN -> $public_ip  (and www). HTTPS certificates are issued once DNS is correct."
  fi
fi

# ---------------------------------------------------------------- build & start
say "Building image…"
docker compose build --pull app

profile_args=()
[[ "$PROXY_MODE" == "caddy" ]] && profile_args=(--profile caddy)

say "Starting services…"
docker compose "${profile_args[@]}" up -d --remove-orphans

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
    echo "   Update:  copy new files here and run ./deploy.sh again"
    exit 0
  fi
  sleep 2
done
docker compose logs --tail 50 app
die "The app didn't become healthy. See the logs above."
