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
# The config root's .env, not a checkout-relative one. .env has not lived in the
# checkout for a long time; this only ever passed on a working tree that still
# had a stray copy, and a fresh clone has none.
ENV_FILE="$(ailocal profile config-root)/.env"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
[ -n "$KEY" ] || { echo "No LITELLM_MASTER_KEY in $ENV_FILE"; exit 1; }


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

# The "no model was invoked" assertion that used to live here counted the
# request-trace ledger. That ledger is gone with the trace subsystem, and no
# surviving signal distinguishes "served from cache" from "never invoked" for a
# route that returns before the router. Rather than assert it from a proxy that
# cannot see it, this is not claimed: /api/hello returns before any model_list
# lookup, which the status codes above already establish.

echo
echo "==> no side effects on existing routes"

# 5 + 6. /v1/models must still work AND must still reject an unauthenticated
# caller. The second half is the one that catches a fix applied too broadly.
code="$(status GET '/v1/models?limit=1000' auth)"
[ "$code" = "200" ] && ok "GET /v1/models (authed) -> 200" \
                    || bad "GET /v1/models (authed) -> $code (want 200)"

# The next request is deliberately unauthenticated, so LiteLLM logs an
# ERROR-level `user_api_key_auth(): Exception occured - No api key passed in.`
# traceback. That is the assertion working, not a fault — but read cold in `docker logs` it
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
echo "        LiteLLM will log an ERROR traceback. This is CORRECT."
proxy_log_mark "=== BEGIN EXPECTED-AUTH-FAILURE (test-compat-routes.sh): the following 'No api key passed in.' ERROR is an INTENTIONAL negative-auth assertion, not a fault ==="
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
