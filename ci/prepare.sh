#!/usr/bin/env bash
# Set up a fresh Metabase for `mb ci run`: admin, API key, warehouse, transforms on, import content.
# Env: MB_URL, WH_NAME, WH_URL (postgresql://user:password@host:port/dbname[?sslmode=require]),
#      CONTENT_DIR (optional: skip import)
# Prints `export MB_API_KEY=...` on stdout's last line.
set -euo pipefail
api() { curl -fsS -H 'content-type: application/json' ${SESSION:+-H "X-Metabase-Session: $SESSION"} "$@"; }

until curl -fsS "$MB_URL/api/health" >/dev/null 2>&1; do sleep 2; done
setup_token=$(api "$MB_URL/api/session/properties" | jq -r '."setup-token"')
password="ci-$(openssl rand -hex 12)"
SESSION=$(api -X POST "$MB_URL/api/setup" -d "$(jq -n --arg t "$setup_token" --arg p "$password" \
  '{token:$t, user:{email:"ci@example.com", password:$p, first_name:"CI", last_name:"Runner"}, prefs:{site_name:"mb-ci"}}')" | jq -r .id)

api -X PUT "$MB_URL/api/setting/transforms-enabled" -d '{"value":true}' >/dev/null

[[ "$WH_URL" =~ ^postgres(ql)?://([^:]+):([^@]+)@([^:/]+):([0-9]+)/([^?]+)(\?sslmode=require)?$ ]] \
  || { echo "WH_URL is not postgresql://user:password@host:port/dbname[?sslmode=require]" >&2; exit 1; }
db_id=$(api -X POST "$MB_URL/api/database" -d "$(jq -n --arg n "$WH_NAME" --arg u "${BASH_REMATCH[2]}" \
  --arg pw "${BASH_REMATCH[3]}" --arg h "${BASH_REMATCH[4]}" --argjson port "${BASH_REMATCH[5]}" \
  --arg d "${BASH_REMATCH[6]}" --argjson ssl "$([ -n "${BASH_REMATCH[7]}" ] && echo true || echo false)" \
  '{name:$n, engine:"postgres", details:{host:$h, port:$port, dbname:$d, user:$u, password:$pw, ssl:$ssl, "ssl-mode":"require"}}')" | jq -r .id)
until [ "$(api "$MB_URL/api/database/$db_id" | jq -r .initial_sync_status)" = complete ]; do sleep 2; done
echo "warehouse $WH_NAME synced as database $db_id" >&2

if [ -n "${CONTENT_DIR:-}" ]; then
  archive=$(mktemp -d)/content.tgz
  tar czf "$archive" -C "$CONTENT_DIR" --exclude .git .
  curl -fsS -H "X-Metabase-Session: $SESSION" -F "file=@$archive" \
    "$MB_URL/api/ee/serialization/import?continue_on_error=false" >&2
  stubs=$(api "$MB_URL/api/database?include_stubs=true" | jq -r '[.data[] | select(.is_stub) | .name] | join(", ")')
  if [ -n "$stubs" ]; then
    echo "content refers to databases this Metabase does not have: $stubs" >&2
    exit 1
  fi
fi

key=$(api -X POST "$MB_URL/api/api-key" -d '{"name":"mb-ci","group_id":2}' | jq -r .unmasked_key)
echo "export MB_API_KEY=$key"
