#!/usr/bin/env bash
# Load the shop_warehouse dataset into a Postgres database and make a login for
# the parent Metabase that owns the data.
#
#   WAREHOUSE_ADMIN_URL=postgres://admin:pw@host:5432/warehouse ./load.sh
#
# WAREHOUSE_ADMIN_URL: a login that can create roles and alter ownership in the
#   target database (a superuser, or the admin of a managed Postgres).
# PARENT_LOGIN (default metabase_parent): the login the parent Metabase uses.
# PARENT_URL_FILE (default ../.secrets/warehouse.url): where the parent's
#   connection URL goes, mode 600. The URL is never printed. A later run
#   keeps the password in this file. Deleting the file rotates the password,
#   and the parent then needs the new one (Admin -> Databases).
#   pg_service.conf next to it holds the same login for psql.
#
# Needs: psql, python3, docker (to build the dump, and for pg_restore when the
# local pg_restore doesn't match the server's major version).
#
# Why the parent's login owns the data: Metabase's workspace provisioning
# needs the login to own the input tables (or be a member of their owner), and
# its teardown (DROP OWNED; DROP USER) leaves grants behind when another role
# owns them, so End session fails.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

SCHEMAS=(raw_shopify raw_stripe raw_marketing shop_analytics analytics)
EXPECTED_TABLES=26
PARENT_LOGIN="${PARENT_LOGIN:-metabase_parent}"
PARENT_URL_FILE="${PARENT_URL_FILE:-$here/../.secrets/warehouse.url}"
# Resolve a relative path from the caller's directory, before the cd.
case "$PARENT_URL_FILE" in /*) ;; *) PARENT_URL_FILE="$PWD/$PARENT_URL_FILE" ;; esac
PARENT_SERVICE_FILE="$(dirname "$PARENT_URL_FILE")/pg_service.conf"
DUMP=shop_warehouse.dump
cd "$here"

die() {
  echo "load.sh: $*" >&2
  exit 1
}

[ -n "${WAREHOUSE_ADMIN_URL:-}" ] || die "set WAREHOUSE_ADMIN_URL to an admin connection URL for the target database"
[[ "$PARENT_LOGIN" =~ ^[a-z_][a-z0-9_]*$ ]] || die "PARENT_LOGIN must be a lowercase SQL identifier"
for tool in psql python3 docker; do
  command -v "$tool" > /dev/null || die "$tool is not installed"
done

# Split the URL into libpq env vars, so no password reaches a command line.
# The assignment, unlike eval "$(...)", stops the script when python fails.
pg_vars=$(python3 -I -c '
import os, shlex, sys, urllib.parse as u
p = u.urlsplit(os.environ["WAREHOUSE_ADMIN_URL"])
if p.scheme not in ("postgres", "postgresql") or not p.hostname or not p.path.strip("/"):
    sys.exit("load.sh: WAREHOUSE_ADMIN_URL must look like postgres://user:password@host:port/database")
q = dict(u.parse_qsl(p.query))
v = {"PGHOST": p.hostname, "PGPORT": str(p.port or 5432), "PGDATABASE": u.unquote(p.path.strip("/")),
     "PGUSER": u.unquote(p.username or ""), "PGPASSWORD": u.unquote(p.password or ""),
     "PGSSLMODE": q.get("sslmode", "prefer")}
for k, val in v.items():
    print(f"export {k}={shlex.quote(val)}")
')
eval "$pg_vars"
unset pg_vars
unset WAREHOUSE_ADMIN_URL

psql -X -q -c 'select 1' > /dev/null || die "can't connect to the target database"
server_major=$(psql -X -Atc 'show server_version_num' | cut -c1-2)
echo "connected to Postgres $server_major on $PGHOST:$PGPORT/$PGDATABASE as $PGUSER"

# ---------------------------------------------------------------- the dump

if [ -f "$DUMP" ]; then
  echo "reusing $DUMP"
else
  echo "building $DUMP (about 2 minutes)"
  build_container="mbc-dataset-build-$$"
  trap 'docker rm -f "$build_container" > /dev/null 2>&1 || true' EXIT
  docker run -d --name "$build_container" -e POSTGRES_PASSWORD=build -e POSTGRES_DB=warehouse \
    -p 127.0.0.1::5432 postgres:16 > /dev/null
  build_port=$(docker port "$build_container" 5432/tcp | head -1 | sed 's/.*://')
  # Over TCP: the image's init-only server answers on the socket, then restarts.
  ready=no
  for _ in $(seq 60); do
    if docker exec "$build_container" pg_isready -h 127.0.0.1 -U postgres -d warehouse -q 2> /dev/null; then
      ready=yes
      break
    fi
    sleep 1
  done
  [ "$ready" = yes ] || die "the build container's Postgres didn't start within 60 s"
  PGHOST=127.0.0.1 PGPORT="$build_port" PGUSER=postgres PGPASSWORD=build PGDATABASE=warehouse PGSSLMODE=disable ./build.sh
  docker exec "$build_container" pg_dump -U postgres -d warehouse -Fc -Z 9 --no-owner --no-privileges > "$DUMP.tmp"
  mv "$DUMP.tmp" "$DUMP"
  docker rm -f "$build_container" > /dev/null
  trap - EXIT
fi

# ---------------------------------------------------------------- restore

schema_list=$(printf "'%s'," "${SCHEMAS[@]}")
schema_list="${schema_list%,}"
present=$(psql -X -Atc "select count(*) from information_schema.tables where table_schema in ($schema_list)")
if [ "$present" -eq "$EXPECTED_TABLES" ]; then
  echo "the $EXPECTED_TABLES tables are already there; skipping the restore"
elif [ "$present" -ne 0 ]; then
  die "the database has $present of the $EXPECTED_TABLES dataset tables; drop schemas ${SCHEMAS[*]} and run again"
else
  local_major=""
  if command -v pg_restore > /dev/null; then
    local_major=$(pg_restore --version | sed -E 's/[^0-9]*([0-9]+).*/\1/')
  fi
  # pg_restore from a newer major emits settings an older server rejects
  # (17 sends transaction_timeout, which 16 refuses), so match the server.
  if [ "$local_major" = "$server_major" ]; then
    echo "restoring with the local pg_restore $local_major"
    pg_restore --no-owner --no-privileges --exit-on-error -d "$PGDATABASE" "$DUMP"
  else
    echo "restoring with pg_restore from postgres:$server_major (local pg_restore is ${local_major:-missing})"
    docker_host="$PGHOST"
    network_args=()
    if [ "$(uname)" = Linux ]; then
      network_args=(--network host)
    elif [ "$PGHOST" = localhost ] || [ "$PGHOST" = 127.0.0.1 ]; then
      docker_host=host.docker.internal
    fi
    docker run --rm -i ${network_args[@]+"${network_args[@]}"} \
      -e PGHOST="$docker_host" -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE -e PGSSLMODE \
      "postgres:$server_major" pg_restore --no-owner --no-privileges --exit-on-error -d "$PGDATABASE" < "$DUMP"
  fi
  present=$(psql -X -Atc "select count(*) from information_schema.tables where table_schema in ($schema_list)")
  [ "$present" -eq "$EXPECTED_TABLES" ] || die "restore left $present tables, expected $EXPECTED_TABLES"
fi

# ---------------------------------------------------------------- the parent's login

# Reuse the password from an earlier run's URL file, so a parent that already
# connects with it keeps working; otherwise generate one.
parent_password=$(PARENT_LOGIN="$PARENT_LOGIN" PARENT_URL_FILE="$PARENT_URL_FILE" python3 -I -c '
import os, secrets, urllib.parse as u
try:
    p = u.urlsplit(open(os.environ["PARENT_URL_FILE"]).read().strip())
    if u.unquote(p.username or "") == os.environ["PARENT_LOGIN"] and p.password:
        print(u.unquote(p.password))
        raise SystemExit
except OSError:
    pass
print(secrets.token_hex(24))
')
[[ "$parent_password" =~ ^[A-Za-z0-9._~-]+$ ]] || die "the password in $PARENT_URL_FILE has characters this script doesn't pass through psql; delete the file to generate a new one"
# The password goes in through a psql variable read from stdin, never argv.
psql -X -q -v ON_ERROR_STOP=1 -v login="$PARENT_LOGIN" -v schemas="$schema_list" > /dev/null <<SQL
\set password '$parent_password'
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login') AS create_login \gset
\if :create_login
  CREATE ROLE :"login" LOGIN CREATEROLE PASSWORD :'password';
\else
  ALTER ROLE :"login" LOGIN CREATEROLE PASSWORD :'password';
\endif
-- CREATE lets the parent create each session's mb__isolation_* schema.
GRANT CONNECT, CREATE ON DATABASE :"DBNAME" TO :"login";
-- A non-superuser admin can hand objects to a role only when it may SET ROLE
-- to it. On Postgres 16 the creator's automatic membership doesn't allow that,
-- so grant it explicitly (a no-op notice when it is there already).
SELECT NOT rolsuper AS grant_self FROM pg_roles WHERE rolname = current_user \gset
\if :grant_self
  GRANT :"login" TO CURRENT_USER;
\endif
SELECT set_config('mbc.parent_login', :'login', false);
DO \$\$
DECLARE r record; owner text := current_setting('mbc.parent_login');
BEGIN
  FOR r IN SELECT nspname FROM pg_namespace WHERE nspname IN ($schema_list) LOOP
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', r.nspname, owner);
  END LOOP;
  FOR r IN SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname IN ($schema_list) AND c.relkind IN ('r','p','v','m','S') LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO %I',
                   CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                  WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
                   r.nspname, r.relname, owner);
  END LOOP;
END \$\$;
SQL

not_owned=$(psql -X -Atc "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname in ($schema_list) and c.relkind in ('r','p','v','m','S') and pg_get_userbyid(c.relowner) <> '$PARENT_LOGIN'")
[ "$not_owned" -eq 0 ] || die "$not_owned dataset objects aren't owned by $PARENT_LOGIN"

# ---------------------------------------------------------------- the URL file

mkdir -p "$(dirname "$PARENT_URL_FILE")"
(
  umask 077
  # Write and chmod a temp file, then move it, so the secret is never in a
  # file others can read.
  tmp="$PARENT_URL_FILE.tmp.$$"
  tmp_service="$PARENT_SERVICE_FILE.tmp.$$"
  PARENT_LOGIN="$PARENT_LOGIN" PARENT_PASSWORD="$parent_password" python3 -I -c '
import os, urllib.parse as u
e = os.environ
host = e["PGHOST"] if ":" not in e["PGHOST"] else "[" + e["PGHOST"] + "]"
query = "" if e["PGSSLMODE"] in ("prefer", "disable", "allow") else "?sslmode=" + e["PGSSLMODE"]
login, password, db = (u.quote(e[k]) for k in ("PARENT_LOGIN", "PARENT_PASSWORD", "PGDATABASE"))
print("postgres://" + login + ":" + password + "@" + host + ":" + e["PGPORT"] + "/" + db + query)
' > "$tmp"
  # The same login as a pg_service.conf entry, so psql can use it without the
  # password on its command line: PGSERVICEFILE=<this file> psql service=demo_parent
  printf '[demo_parent]\nhost=%s\nport=%s\ndbname=%s\nuser=%s\npassword=%s\nsslmode=%s\n' \
    "$PGHOST" "$PGPORT" "$PGDATABASE" "$PARENT_LOGIN" "$parent_password" "$PGSSLMODE" > "$tmp_service"
  chmod 600 "$tmp" "$tmp_service"
  mv "$tmp" "$PARENT_URL_FILE"
  mv "$tmp_service" "$PARENT_SERVICE_FILE"
)

echo "loaded: $EXPECTED_TABLES tables in ${SCHEMAS[*]}, owned by $PARENT_LOGIN"
echo "the parent's connection URL is in $PARENT_URL_FILE, and a psql service demo_parent in $PARENT_SERVICE_FILE (both mode 600)"
