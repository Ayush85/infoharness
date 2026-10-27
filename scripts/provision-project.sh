#!/usr/bin/env bash
# Give a project its own isolated database (and optionally ClickHouse) on this shared server.
#
#   sudo ./scripts/provision-project.sh infoharness                 # PostgreSQL only
#   sudo ./scripts/provision-project.sh analytics --clickhouse      # PostgreSQL + ClickHouse
#   sudo ./scripts/provision-project.sh analytics --clickhouse --no-postgres
#
# Each project gets:  its own DB + login (no access to other projects' data), a random password,
# and a credentials file at /etc/aydexis/projects/<name>.env (root-only). Safe to re-run: existing
# databases are kept and the stored password is re-applied.
#
# PostgreSQL admin access: native install via `sudo -u postgres psql` (default), or set
#   PG_ADMIN_URL=postgresql://postgres:PASSWORD@127.0.0.1:5432/postgres   (e.g. Postgres in Docker)
# ClickHouse admin access: /etc/aydexis/clickhouse-admin.env (written by setup-clickhouse.sh), or set
#   CLICKHOUSE_ADMIN_USER / CLICKHOUSE_ADMIN_PASSWORD
set -euo pipefail

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

NAME="${1:-}"; shift || true
WANT_PG=1; WANT_CH=0
for arg in "$@"; do
  case "$arg" in
    --clickhouse) WANT_CH=1 ;;
    --no-postgres) WANT_PG=0 ;;
    *) die "Unknown option: $arg" ;;
  esac
done
[[ "$NAME" =~ ^[a-z][a-z0-9_]{1,30}$ ]] || die "Usage: $0 <project_name> [--clickhouse] [--no-postgres]   (lowercase letters, digits, _)"

STATE_DIR="${AYDEXIS_STATE_DIR:-/etc/aydexis}"
CRED="$STATE_DIR/projects/$NAME.env"
PG_HOST="${PG_HOST:-127.0.0.1}"; PG_PORT="${PG_PORT:-5432}"
CH_HOST="${CH_HOST:-127.0.0.1}"; CH_HTTP_PORT="${CH_HTTP_PORT:-8123}"; CH_TCP_PORT="${CH_TCP_PORT:-9000}"

mkdir -p "$STATE_DIR/projects"
chmod 700 "$STATE_DIR" "$STATE_DIR/projects"

# Reuse the stored password so re-runs never break a running app.
PASSWORD=""
if [[ -f "$CRED" ]]; then
  PASSWORD="$(grep -E '^PASSWORD=' "$CRED" | cut -d= -f2- || true)"
fi
[[ -n "$PASSWORD" ]] || PASSWORD="$(openssl rand -hex 24)"

# ---------------------------------------------------------------- PostgreSQL
pg_admin() {  # pg_admin <psql args...>
  if [[ -n "${PG_ADMIN_URL:-}" ]]; then
    psql "$PG_ADMIN_URL" -v ON_ERROR_STOP=1 -qtA "$@"
  else
    command -v psql >/dev/null || die "psql not found. Install postgresql-client or set PG_ADMIN_URL."
    sudo -u postgres psql -p "$PG_PORT" -v ON_ERROR_STOP=1 -qtA "$@"
  fi
}

if (( WANT_PG )); then
  say "PostgreSQL: role + database '$NAME'"
  pg_admin -d postgres -c "SELECT 1" >/dev/null || die "Can't connect to PostgreSQL as admin."
  pg_admin -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$NAME') THEN
    CREATE ROLE "$NAME" LOGIN PASSWORD '$PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  ELSE
    ALTER ROLE "$NAME" WITH LOGIN PASSWORD '$PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
\$\$;
SQL
  if [[ "$(pg_admin -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$NAME'")" != "1" ]]; then
    pg_admin -d postgres -c "CREATE DATABASE \"$NAME\" OWNER \"$NAME\" ENCODING 'UTF8'"
  else
    say "  database already exists — kept as is"
  fi
  # Only this project's role may connect to its database.
  pg_admin -d postgres -c "REVOKE CONNECT ON DATABASE \"$NAME\" FROM PUBLIC; GRANT CONNECT, TEMP ON DATABASE \"$NAME\" TO \"$NAME\";"
  pg_admin -d "$NAME" -c "ALTER SCHEMA public OWNER TO \"$NAME\";"
  PGPASSWORD="$PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$NAME" -d "$NAME" -qtAc "SELECT 1" >/dev/null \
    || warn "Created, but a password login on $PG_HOST:$PG_PORT failed — check pg_hba.conf allows 'host $NAME $NAME 127.0.0.1/32 scram-sha-256'."
fi

# ---------------------------------------------------------------- ClickHouse
if (( WANT_CH )); then
  command -v clickhouse-client >/dev/null || die "ClickHouse isn't installed. Run: sudo ./scripts/setup-clickhouse.sh"
  if [[ -z "${CLICKHOUSE_ADMIN_USER:-}" && -f "$STATE_DIR/clickhouse-admin.env" ]]; then
    # shellcheck disable=SC1091
    source "$STATE_DIR/clickhouse-admin.env"
  fi
  CH_ADMIN_USER="${CLICKHOUSE_ADMIN_USER:-default}"
  CH_ADMIN_PASS="${CLICKHOUSE_ADMIN_PASSWORD:-}"
  ch() { clickhouse-client --host "$CH_HOST" --port "$CH_TCP_PORT" --user "$CH_ADMIN_USER" --password "$CH_ADMIN_PASS" --multiquery "$@"; }
  ch -q "SELECT 1" >/dev/null || die "Can't connect to ClickHouse as '$CH_ADMIN_USER'. Run setup-clickhouse.sh first."

  say "ClickHouse: database + user '$NAME' (memory-capped so one project can't starve the others)"
  ch -q "
    CREATE DATABASE IF NOT EXISTS \`$NAME\`;
    CREATE SETTINGS PROFILE IF NOT EXISTS \`${NAME}_profile\` SETTINGS max_memory_usage = ${CH_MAX_MEMORY:-600000000}, max_execution_time = ${CH_MAX_SECONDS:-120};
    CREATE USER IF NOT EXISTS \`$NAME\` IDENTIFIED WITH sha256_password BY '$PASSWORD' HOST LOCAL;
    ALTER USER \`$NAME\` IDENTIFIED WITH sha256_password BY '$PASSWORD' HOST LOCAL DEFAULT DATABASE \`$NAME\` SETTINGS PROFILE '${NAME}_profile';
    GRANT ALL ON \`$NAME\`.* TO \`$NAME\`;
  " || die "ClickHouse provisioning failed. If it says 'Not enough privileges', run setup-clickhouse.sh (it creates an admin with access management)."
  clickhouse-client --host "$CH_HOST" --port "$CH_TCP_PORT" --user "$NAME" --password "$PASSWORD" -q "SELECT 1" >/dev/null \
    || warn "User created, but a test login failed."
fi

# ---------------------------------------------------------------- credentials file
{
  echo "# Credentials for project '$NAME' — generated by provision-project.sh. Keep private."
  echo "PROJECT=$NAME"
  echo "PASSWORD=$PASSWORD"
  if (( WANT_PG )) || grep -q '^POSTGRES_URL=' "$CRED" 2>/dev/null; then
    echo "POSTGRES_URL=postgresql://$NAME:$PASSWORD@$PG_HOST:$PG_PORT/$NAME"
  fi
  if (( WANT_CH )) || grep -q '^CLICKHOUSE_HTTP_URL=' "$CRED" 2>/dev/null; then
    echo "CLICKHOUSE_HTTP_URL=http://$NAME:$PASSWORD@$CH_HOST:$CH_HTTP_PORT/?database=$NAME"
    echo "CLICKHOUSE_NATIVE_URL=clickhouse://$NAME:$PASSWORD@$CH_HOST:$CH_TCP_PORT/$NAME"
  fi
} > "$CRED.tmp"
mv "$CRED.tmp" "$CRED"
chmod 600 "$CRED"
say "Done. Credentials: $CRED"
