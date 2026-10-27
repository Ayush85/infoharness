#!/usr/bin/env bash
# Prepare ONE shared ClickHouse server for many projects on this machine.
#
#   sudo ./scripts/setup-clickhouse.sh                  # install if missing + admin user + small-server tuning
#   sudo ./scripts/setup-clickhouse.sh --localhost-only # also bind ClickHouse to 127.0.0.1 only
#
# What it does (idempotent, only adds its own files; never edits your existing config):
#   * installs clickhouse-server/client from the official LTS apt repo if not present
#   * /etc/clickhouse-server/users.d/aydexis-admin.xml  -> admin user with SQL access management
#     (needed so provision-project.sh can create per-project users, databases and memory limits)
#   * /etc/clickhouse-server/config.d/aydexis-small-server.xml -> memory caps + 7-day TTL on system logs
#   * admin credentials in /etc/aydexis/clickhouse-admin.env (root-only)
# Then give each project its own database:  sudo ./scripts/provision-project.sh <name> --clickhouse
set -euo pipefail

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."
LOCALHOST_ONLY=0
[[ "${1:-}" == "--localhost-only" ]] && LOCALHOST_ONLY=1

STATE_DIR="${AYDEXIS_STATE_DIR:-/etc/aydexis}"
CONF_D=/etc/clickhouse-server/config.d
USERS_D=/etc/clickhouse-server/users.d
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"

# ---------------------------------------------------------------- install
if ! command -v clickhouse-server >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -qE ':(8123|9000)\b'; then
    die "Something already listens on 8123/9000 but clickhouse-server isn't installed on the host
   (probably ClickHouse in Docker). Not installing a second copy. Use your existing admin login instead:
   CLICKHOUSE_ADMIN_USER=... CLICKHOUSE_ADMIN_PASSWORD=... sudo -E ./scripts/provision-project.sh <name> --clickhouse --no-postgres"
  fi
  say "Installing ClickHouse (LTS) from packages.clickhouse.com…"
  apt-get install -y apt-transport-https ca-certificates curl gnupg
  curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/clickhouse-keyring.gpg
  ARCH=$(dpkg --print-architecture)
  echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=${ARCH}] https://packages.clickhouse.com/deb lts main" \
    > /etc/apt/sources.list.d/clickhouse.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y clickhouse-server clickhouse-client
  systemctl enable clickhouse-server
else
  say "ClickHouse already installed: $(clickhouse-server --version 2>/dev/null | head -1)"
fi

mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 4000 )); then
  warn "This server has ${mem_mb} MB RAM. ClickHouse is happiest with 4 GB+; this script caps it to stay polite"
  warn "alongside PostgreSQL and your apps, but heavy analytical queries will be slower. Add swap if you haven't."
fi

# ---------------------------------------------------------------- admin user
ADMIN_USER=aydexis_admin
if [[ -f "$STATE_DIR/clickhouse-admin.env" ]]; then
  # shellcheck disable=SC1091
  source "$STATE_DIR/clickhouse-admin.env"
  ADMIN_PASS="$CLICKHOUSE_ADMIN_PASSWORD"
else
  ADMIN_PASS="$(openssl rand -hex 24)"
fi
ADMIN_HASH=$(printf '%s' "$ADMIN_PASS" | sha256sum | cut -d' ' -f1)

mkdir -p "$USERS_D" "$CONF_D"
cat > "$USERS_D/aydexis-admin.xml" <<XML
<clickhouse>
  <users>
    <${ADMIN_USER}>
      <password_sha256_hex>${ADMIN_HASH}</password_sha256_hex>
      <networks><ip>::1</ip><ip>127.0.0.1</ip></networks>
      <profile>default</profile>
      <quota>default</quota>
      <access_management>1</access_management>
      <named_collection_control>1</named_collection_control>
      <show_named_collections>1</show_named_collections>
    </${ADMIN_USER}>
  </users>
</clickhouse>
XML

# ---------------------------------------------------------------- small-server tuning
cat > "$CONF_D/aydexis-small-server.xml" <<'XML'
<clickhouse>
  <!-- Leave room for PostgreSQL and the apps on the same machine -->
  <max_server_memory_usage_to_ram_ratio>0.35</max_server_memory_usage_to_ram_ratio>
  <mark_cache_size>268435456</mark_cache_size>
  <uncompressed_cache_size>0</uncompressed_cache_size>
  <max_concurrent_queries>50</max_concurrent_queries>
  <!-- System log tables grow forever by default; keep one week -->
  <query_log>
    <ttl>event_date + INTERVAL 7 DAY DELETE</ttl>
  </query_log>
  <trace_log remove="1"/>
  <metric_log remove="1"/>
  <asynchronous_metric_log remove="1"/>
  <part_log remove="1"/>
</clickhouse>
XML

if (( LOCALHOST_ONLY )); then
  cat > "$CONF_D/aydexis-listen.xml" <<'XML'
<clickhouse>
  <listen_host replace="1">127.0.0.1</listen_host>
</clickhouse>
XML
  say "ClickHouse will listen on 127.0.0.1 only."
fi
chown -R clickhouse:clickhouse "$CONF_D" "$USERS_D" 2>/dev/null || true

umask 077
cat > "$STATE_DIR/clickhouse-admin.env" <<ENV
CLICKHOUSE_ADMIN_USER=${ADMIN_USER}
CLICKHOUSE_ADMIN_PASSWORD=${ADMIN_PASS}
ENV

# ---------------------------------------------------------------- restart & verify
if systemctl is-active --quiet clickhouse-server; then
  warn "Restarting ClickHouse to apply settings — queries from other projects pause for a few seconds."
fi
say "Restarting ClickHouse…"
systemctl restart clickhouse-server
for _ in $(seq 1 30); do
  if clickhouse-client --user "$ADMIN_USER" --password "$ADMIN_PASS" -q "SELECT 1" >/dev/null 2>&1; then
    say "ClickHouse ready. Admin credentials: $STATE_DIR/clickhouse-admin.env"
    if ss -ltn 2>/dev/null | grep -E ':(8123|9000)\b' | grep -vqE '127\.0\.0\.1|\[::1\]'; then
      warn "ClickHouse ports 8123/9000 are reachable from outside this server."
      warn "Block them in your DigitalOcean firewall, or re-run with --localhost-only."
    fi
    if clickhouse-client --user default --password '' -q "SELECT 1" >/dev/null 2>&1; then
      warn "The built-in 'default' user has no password. Projects should use their own users;"
      warn "consider setting one in /etc/clickhouse-server/users.d/ once nothing depends on it."
    fi
    echo "   Next: sudo ./scripts/provision-project.sh <project> --clickhouse"
    exit 0
  fi
  sleep 2
done
journalctl -u clickhouse-server --no-pager -n 30 || true
die "ClickHouse didn't come back up. See the log above."
