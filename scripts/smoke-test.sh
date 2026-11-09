#!/usr/bin/env bash
# Week 4: end-to-end smoke test against a running urdnot-api with real backends.
#
#   docker compose up -d --build --wait
#   set -a; . ./.env; set +a          # exports CLAN_SECRET for this script
#   scripts/smoke-test.sh
#
# CI runs the same script (see .github/workflows/ci.yml), so a failure here
# reproduces locally. It checks behavior that the unit tests cannot: the image
# starts, Compose wires DATABASE_URL and REDIS_URL, and data really goes to
# Postgres and Redis instead of the in-memory fallbacks.

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
: "${CLAN_SECRET:?export CLAN_SECRET (e.g. set -a; . ./.env; set +a)}"

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

# status METHOD PATH [curl args...] -> prints the HTTP status, body goes to $body_file
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
status() {
  local method="$1" path="$2"; shift 2
  curl -sS -o "$body_file" -w '%{http_code}' -X "$method" "$@" "$BASE_URL$path"
}

# Liveness and readiness. /readyz is 200 only if Postgres and Redis both answer.
[[ "$(status GET /healthz)" == 200 ]] || fail "/healthz is not 200"
pass "/healthz 200"
code="$(status GET /readyz)"
[[ "$code" == 200 ]] || fail "/readyz returned $code: $(cat "$body_file")"
pass "/readyz 200"

# The home page reports which backends were selected at startup.
status GET / >/dev/null
grep -q 'Storage: postgres' "$body_file" || fail "home page does not report postgres storage"
grep -q 'Counter: redis' "$body_file" || fail "home page does not report redis counter"
pass "storage=postgres, counter=redis"

# Writes need the clan secret.
code="$(status POST /api/hollows -H 'Content-Type: application/json' \
  -d '{"name":"intruder","text":"no secret"}')"
[[ "$code" == 403 ]] || fail "POST without X-Clan-Secret returned $code, want 403"
pass "POST without secret 403"

# A unique name per run, so reruns against a kept volume still prove a new write.
name="smoke-$(date +%s)-$RANDOM"
code="$(status POST /api/hollows -H 'Content-Type: application/json' \
  -H "X-Clan-Secret: $CLAN_SECRET" \
  -d "{\"name\":\"$name\",\"text\":\"written by the smoke test\"}")"
[[ "$code" == 201 ]] || fail "POST with secret returned $code: $(cat "$body_file")"
pass "POST with secret 201"

[[ "$(status GET /api/hollows)" == 200 ]] || fail "GET /api/hollows is not 200"
grep -q "\"$name\"" "$body_file" || fail "record $name not found in GET /api/hollows"
pass "record $name read back"

# The Redis counter must increase between two calls.
status GET /api/headbutts >/dev/null
first="$(tr -dc '0-9' <"$body_file")"
status GET /api/headbutts >/dev/null
second="$(tr -dc '0-9' <"$body_file")"
(( second == first + 1 )) || fail "headbutts went $first -> $second, want +1"
pass "headbutts $first -> $second"

# Prometheus exposition is served and counts requests.
status GET /metrics >/dev/null
grep -q '^http_requests_total{' "$body_file" || fail "/metrics has no http_requests_total"
pass "/metrics exposes http_requests_total"

echo "smoke test passed against $BASE_URL"
