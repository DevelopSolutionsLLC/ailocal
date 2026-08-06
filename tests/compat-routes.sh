#!/usr/bin/env bash
# test-compat-routes.sh — the client compatibility probe endpoints.
#
# Guards the compat routes in deploy/litellm/hooks/startup.py. Claude Code derives auxiliary
# Anthropic-shaped probes from ANTHROPIC_BASE_URL, so they arrive at this proxy;
# LiteLLM implements none of them and returned 404 for `HEAD /api/hello`.
#
# What is asserted, and why each one:
#   1. HEAD /api/hello -> 200, empty body        the observed regression
#   2. GET  /api/hello -> 200, empty body        the other method the client uses
#   3. no auth header still -> 200               it is a pre-credential probe
#   4. no model was invoked                      a probe must not touch a backend
#   5. /v1/models still 200 WITH auth            no routing/auth side effect
#   6. /v1/models still 401 WITHOUT auth         the unauth'd route did not leak
#   7. /health/liveliness still 200              health routes untouched
#
# 5-7 exist because the failure mode worth catching is not "the probe 404s" but
# "someone made the probe work by loosening auth or routing for everything else".
#
# Usage: ./tests/compat-routes.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
ROOT="$ROOT_DIR"
cd "$ROOT"

PROXY="${AILOCAL_PROXY:-http://127.0.0.1:4000}"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env 2>/dev/null | cut -d= -f2-)"
[ -n "$KEY" ] || { echo "No LITELLM_MASTER_KEY in .env"; exit 1; }


status() { # method path [auth]
  local method="$1" path="$2" auth="${3:-}"
  if [ -n "$auth" ]; then
    curl -s -o /dev/null -w '%{http_code}' -m 15 -X "$method" \
      -H "Authorization: Bearer $KEY" "$PROXY$path"
  else
    curl -s -o /dev/null -w '%{http_code}' -m 15 -X "$method" "$PROXY$path"
  fi
}

echo "==> compatibility probe: /api/hello"

# 1 + 2. Both methods the client is known to issue. `curl -I` is the exact
# invocation from the bug report, so use it verbatim rather than -X HEAD.
code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 -I "$PROXY/api/hello")"
[ "$code" = "200" ] && ok "HEAD /api/hello -> 200" \
                    || bad "HEAD /api/hello -> $code (want 200)"

code="$(status GET /api/hello)"
[ "$code" = "200" ] && ok "GET /api/hello -> 200" \
                    || bad "GET /api/hello -> $code (want 200)"

# Empty body. Asserted because the client treats this as a reachability signal
# only; anything we put in the body is unread payload and possible disclosure.
body="$(curl -s -m 15 "$PROXY/api/hello")"
[ -z "$body" ] && ok "GET /api/hello body is empty" \
               || bad "GET /api/hello returned a body: ${body:0:120}"

# 3. Unauthenticated. The probe is fired before credentials are established, so
# requiring auth here would reintroduce the failure with a different status.
code="$(status HEAD /api/hello)"
[ "$code" = "200" ] && ok "HEAD /api/hello unauthenticated -> 200" \
                    || bad "HEAD /api/hello unauthenticated -> $code (want 200)"

# 4. No model invocation. A probe that woke a backend would be a latency and
# memory regression that no status-code assertion would ever catch.
#
# Count the trace LEDGER, not `docker logs`. Only the failure path prints to
# stdout, so a successful inference adds no stdout line at all and a count of
# those cannot detect a woken backend. Every request — success or failure —
# appends to the JSONL ledger, so that is the signal.
#
# The ledger is shared and written asynchronously after the response, so a
# record from a preceding check can land mid-measurement. Settle first: read
# until the count stops moving, bounded, then measure.
trace_count() {
  docker exec ailocal-litellm sh -c 'cat /app/captures/traces/*.jsonl 2>/dev/null | wc -l' \
    2>/dev/null | tr -d ' \n' || echo 0
}
settle_traces() {   # bounded wait for the ledger to stop growing
  local prev cur i
  prev="$(trace_count)"
  for i in $(seq 1 20); do
    sleep 0.25
    cur="$(trace_count)"
    [ "$cur" = "$prev" ] && { printf '%s' "$cur"; return 0; }
    prev="$cur"
  done
  printf '%s' "$prev"          # unsettled: measure anyway, the diff still reports
}

if [ -z "$(trace_count)" ] || [ "$(trace_count)" = "0" ]; then
  skip "no trace ledger (AILOCAL_TRACE_DIR unset?) — model-invocation check not run"
else
  before="$(settle_traces)"
  curl -s -o /dev/null -m 15 -I "$PROXY/api/hello"
  curl -s -o /dev/null -m 15 "$PROXY/api/hello"
  after="$(settle_traces)"
  [ "$before" = "$after" ] && ok "no model invoked (trace ledger $before unchanged)" \
    || bad "probe invoked a model: trace ledger went $before -> $after"
fi

echo
echo "==> no side effects on existing routes"

# 5 + 6. /v1/models must still work AND must still reject an unauthenticated
# caller. The second half is the one that catches a fix applied too broadly.
code="$(status GET '/v1/models?limit=1000' auth)"
[ "$code" = "200" ] && ok "GET /v1/models (authed) -> 200" \
                    || bad "GET /v1/models (authed) -> $code (want 200)"

# The next request is deliberately unauthenticated, so LiteLLM logs an
# ERROR-level `user_api_key_auth(): Exception occured - No api key passed in.`
# traceback, plus a `request_trace {"phase": "failure"}` entry from our own hook.
# That is the assertion working, not a fault — but read cold in `docker logs` it
# looks exactly like a real failure, and it HAS been mistaken for one.
#
# WHY IT IS MARKED RATHER THAN SUPPRESSED (investigated, not assumed):
#
#   The traceback is emitted by litellm/proxy/auth/auth_exception_handler.py
#   inside `user_api_key_auth`, a FastAPI dependency that runs BEFORE the route
#   handler and before every LiteLLM callback. We own no hook that runs earlier,
#   so a distinctive request header cannot reach it. Silencing it would require
#   one of: editing the installed package (forbidden), a global log-level or
#   auth-handler change (forbidden), or ASGI middleware in the production request
#   path for every request — disproportionate for one test assertion.
#
#   Our own request_trace failure entry COULD be header-suppressed, but that
#   would hide only half the noise while giving any caller a header that
#   suppresses their own failure trace — a real hole in production
#   observability, bought for a test's convenience. Declined deliberately.
#
#   Running the negative-auth case against a throwaway proxy would isolate it
#   fully, but costs a second container start per gate run for one 401.
#
# So: the assertion and production logging are both left exactly as they are,
# and the expected traceback is BRACKETED IN THE PROXY LOG ITSELF — not merely
# in this script's stdout, which is where the previous version fell short. The
# markers are written to the container's stdout (fd 1 of pid 1), so they appear
# inline in `docker logs ailocal-litellm`, immediately around the traceback.
CONTAINER="${AILOCAL_CONTAINER:-ailocal-litellm}"
proxy_log_mark() {  # best-effort: never fail the test over a log annotation
  docker exec "$CONTAINER" sh -c "echo '$1' >> /proc/1/fd/1" >/dev/null 2>&1 || true
}

echo
echo "  vvvv  EXPECTED-AUTH-FAILURE — the next request is INTENTIONALLY unauthenticated."
echo "        LiteLLM will log an ERROR traceback + request_trace failure. This is CORRECT."
proxy_log_mark "=== BEGIN EXPECTED-AUTH-FAILURE (test-compat-routes.sh): the following 'No api key passed in.' ERROR + request_trace failure is an INTENTIONAL negative-auth assertion, not a fault ==="
code="$(status GET /v1/models)"
proxy_log_mark "=== END EXPECTED-AUTH-FAILURE (test-compat-routes.sh) ==="
echo "  ^^^^  END EXPECTED-AUTH-FAILURE (bracketed in the proxy log too)"
echo
case "$code" in
  401|403) ok "GET /v1/models unauthenticated -> $code (still protected)" ;;
  *)       bad "GET /v1/models unauthenticated -> $code (want 401/403; auth may have been loosened)" ;;
esac

# 7. Health routes untouched — and specifically NOT the thing /api/hello was
# wired to, which is why it is asserted separately rather than assumed.
code="$(status GET /health/liveliness)"
[ "$code" = "200" ] && ok "GET /health/liveliness -> 200" \
                    || bad "GET /health/liveliness -> $code (want 200)"

# An unrelated unknown path must STILL 404. Without this, a catch-all handler
# would pass every other assertion in this file.
code="$(status GET /api/definitely-not-a-route)"
[ "$code" = "404" ] && ok "unknown path still 404s (no catch-all)" \
                    || bad "GET /api/definitely-not-a-route -> $code (want 404)"

report "compat routes" || exit 1
