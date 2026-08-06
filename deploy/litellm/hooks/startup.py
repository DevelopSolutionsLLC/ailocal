"""startup.py — proxy patches applied at import, before the first request.

Three corrections that share one lifecycle: they run when LiteLLM imports this
module during config load, mutate proxy state once, and observe no request. None
implements a request callback. `proxy_handler_instance` is a no-op CustomLogger
that exists only because a `litellm_settings.callbacks` entry is what makes
LiteLLM import the file.

  model_registrar   restore context-window validation for local deployments
  compat_routes     serve the client compatibility probes LiteLLM lacks
  stream_log_guard  backport a missing type guard in Anthropic success logging

Each reports its status on one boot line via print(), not log.info(): the proxy
filters third-party loggers, so print is what survives to `docker logs` and tells
an operator which corrections are live. Nothing here may raise — a failure must
degrade one correction, never take the container down.
"""

import logging
import os
import sys

import litellm
from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("startup")

#: The GENERATED config — sync-models.py writes it to $AILOCAL_STATE and Compose
#: mounts it at /app/generated. It is NOT in the authored /app/config mount.
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/generated/config.yaml")


def _say(msg):
    """One boot line, visible in `docker logs`."""
    print(msg, flush=True)


# ── model registration ──────────────────────────────────────────────────────
# LiteLLM does not register context limits for locally added deployments: it
# stores model_info under the provider-stripped key while the router's pre-call
# check looks it up WITH the provider prefix. The lookup raises, the router
# swallows it, and the whole max_input_tokens validation block is skipped — so an
# oversized prompt is forwarded unvalidated and silently truncated. The config's
# own `model_info:` block cannot fix this; the router merges it after the lookup
# that throws.
#
# Register generated models under the exact prefixed key at startup so pre-call
# admission checks stay active. Public litellm namespace only — no fork, no
# vendored patch. The key is whatever `litellm_params.model` says, verbatim, so
# nothing here is provider-specific; deployments LiteLLM already maps are left
# alone and genuine upstream pricing is never overridden.
#
# Remove when upstream resolves the registration/lookup mismatch: the self-check
# below then reports every model "already mapped".


def _load_model_list():
    """The generated model_list. sync-models.py owns that file, so new
    capabilities are picked up with no change here."""
    try:
        import yaml

        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("model_list") or []
    except Exception as exc:  # noqa: BLE001 - never block proxy startup
        log.error("model_registrar: could not read %s: %s", CONFIG_PATH, exc)
        _say(f"model_registrar: could not read {CONFIG_PATH}: {exc}")
        return []


def _has_exact_key(model_key):
    """True if the EXACT key is present in litellm.model_cost.

    Deliberately not `litellm.get_model_info(model_key)`: that helper falls back
    to the prefix-stripped key, so it reports success for models the router still
    cannot resolve. The router needs the prefixed key itself.
    """
    return model_key in litellm.model_cost


def _router_can_resolve(model_key):
    """Replicate the router's own lookup from outside, for the self-check."""
    try:
        litellm.get_model_info(model=model_key)
        return model_key in litellm.model_cost
    except Exception:  # noqa: BLE001 - a raise here IS the "not mapped" signal
        return False


def _cost_entry(model_key, model_info):
    """Build a cost-map entry from the deployment's own model_info block.

    litellm_provider is derived from the key's prefix so the entry survives
    LiteLLM's provider-match filtering (_check_provider_match drops entries whose
    provider disagrees with the resolved one)."""
    provider = model_key.split("/", 1)[0] if "/" in model_key else None
    entry = {
        # Local backends are free; explicit zeros keep cost tracking coherent
        # rather than leaving the fields absent.
        "input_cost_per_token": model_info.get("input_cost_per_token", 0),
        "output_cost_per_token": model_info.get("output_cost_per_token", 0),
        "mode": model_info.get("mode", "chat"),
    }
    if provider:
        entry["litellm_provider"] = provider
    # These two are the whole point: the admission check compares counted input
    # tokens against them.
    for field in ("max_input_tokens", "max_output_tokens", "max_tokens"):
        if model_info.get(field) is not None:
            entry[field] = model_info[field]
    return entry


def register_local_models():
    """Inject every configured deployment LiteLLM cannot already map, then
    verify. Returns (registered, already_mapped, failed)."""
    registered, already, failed = [], [], []

    for deployment in _load_model_list():
        params = deployment.get("litellm_params") or {}
        model_key = params.get("model")
        if not model_key:
            continue
        if _has_exact_key(model_key):
            already.append(model_key)
            continue
        # Assign directly rather than via litellm.register_model(): that helper is
        # what strips the provider prefix, which is the defect being worked around.
        litellm.model_cost[model_key] = _cost_entry(
            model_key, deployment.get("model_info") or {})
        registered.append(model_key)

    # Invalidate LiteLLM's case-insensitive lookup cache so the new keys are seen.
    try:
        from litellm.utils import _invalidate_model_cost_lowercase_map

        _invalidate_model_cost_lowercase_map()
    except Exception:  # noqa: BLE001 - private helper; absence is not fatal
        pass

    # get_model_info is LRU-cached; clear it or a lookup made before this
    # injection keeps returning the stale prefix-stripped answer.
    try:
        from litellm.utils import _cached_get_model_info

        _cached_get_model_info.cache_clear()
    except Exception:  # noqa: BLE001 - private helper; absence is not fatal
        pass

    # Fail LOUDLY rather than silently losing context-window validation if
    # LiteLLM's internals shift. Re-runs the real lookup, not a dict membership
    # test, because the lookup is what the router actually performs.
    for model_key in registered:
        if not _router_can_resolve(model_key):
            failed.append(model_key)

    _say(f"model_registrar: config={CONFIG_PATH} models={len(registered)+len(already)}")
    if registered:
        _say("model_registrar: REGISTERED " + ", ".join(registered))
    if already:
        _say("model_registrar: already mapped " + ", ".join(already))
    if failed:
        _say("model_registrar: FAILED " + ", ".join(failed))
        log.error(
            "model_registrar: %d model(s) STILL unmapped after registration: %s. "
            "Context-window validation is DISABLED for these — the router will "
            "skip max_input_tokens enforcement. LiteLLM internals may have "
            "changed; re-check get_router_model_info().",
            len(failed), ", ".join(failed))
    return registered, already, failed


# ── client compatibility routes ─────────────────────────────────────────────
# Claude Code is pointed here through ANTHROPIC_BASE_URL and sends auxiliary
# probes built against the shape of api.anthropic.com. LiteLLM has no such route,
# so it 404s:
#
#     INFO: 172.18.0.1:58848 - "HEAD /api/hello HTTP/1.1" 404 Not Found
#
# The probe originates in the client. claude-cli has three `<base>/api/hello`
# call sites:
#
#   a. HEAD, base = `ANTHROPIC_BASE_URL || BASE_API_URL`. The one that arrives
#      here. Skipped entirely when a proxy / unix socket / client cert /
#      Bedrock-style env var is set, and its result is discarded
#      (`.catch(()=>{})`, no status inspection).
#   b. GET, base = BASE_API_URL — connectivity telemetry, classified `http_<status>`.
#   c. GET, base = BASE_API_URL — `preflight_endpoint`, which treats any status
#      != 200 as a connection failure.
#
# (b) and (c) read the BUILT-IN base, so they do not reach this proxy. GET is
# served anyway: it costs nothing, and a future client build that redirects either
# onto ANTHROPIC_BASE_URL gets a 200 rather than a fresh 404.
#
# HONEST SCOPE. This removes the 404 and makes the probe succeed. Call site (a)
# discards its result, so this route is NOT proven to be what silences any
# particular in-terminal API warning; treat that as a separate claim.
#
# CONSTRAINTS. No model invocation, no router lookup, no backend call. No auth
# dependency: it is a reachability probe sent before/without credentials, and it
# discloses nothing — a fixed empty 200. Deliberately NOT a redirect to
# /health/liveliness: a probe answer must not be confused with real health state.
# Idempotent, and touches nothing under /v1/*.

#: Path -> methods. Keep this table tiny and boring: every entry is a static 200
#: with an empty body, and anything needing real logic does not belong here.
COMPAT_ROUTES = {
    "/api/hello": ["GET", "HEAD"],
}


def _empty_ok():
    """Endpoint body. Fixed 200, no content, no side effects."""
    from fastapi import Response

    return Response(status_code=200)


def _get_app():
    """The running proxy's FastAPI app, or None.

    Prefer the already-imported module: LiteLLM loads callbacks from its own
    startup path, so `litellm.proxy.proxy_server` is normally in sys.modules and
    this is a dict lookup. The import fallback covers the standalone case (the
    gate's hook-importability check), where nothing has imported the proxy yet.
    """
    mod = sys.modules.get("litellm.proxy.proxy_server")
    if mod is None:
        try:
            from litellm.proxy import proxy_server as mod  # type: ignore[no-redef]
        except Exception as exc:  # pragma: no cover - depends on litellm layout
            log.error("compat_routes: cannot import litellm.proxy.proxy_server "
                      "(%s: %s). Routes NOT registered; %s keeps returning 404.",
                      type(exc).__name__, exc, ", ".join(sorted(COMPAT_ROUTES)))
            return None
    return getattr(mod, "app", None)


def register_compat_routes():
    """Add the compatibility routes to the proxy app. Returns the paths added.

    Never raises: a failure must not take the proxy down over a probe endpoint.
    It logs at ERROR, because a silent no-op would leave the 404 in place with
    nothing to say so.
    """
    app = _get_app()
    if app is None:
        log.error("compat_routes: no FastAPI app found. Routes NOT registered.")
        return []

    existing = {getattr(r, "path", None) for r in app.routes}
    added = []
    for path, methods in COMPAT_ROUTES.items():
        if path in existing:
            # Either we already ran, or LiteLLM grew its own implementation.
            # Both mean: leave it alone. Self-retiring by construction.
            log.info("compat_routes: %s already registered, leaving it alone", path)
            continue
        try:
            app.add_api_route(path, _empty_ok, methods=methods,
                              include_in_schema=False)
        except Exception as exc:
            log.error("compat_routes: failed to register %s (%s: %s)",
                      path, type(exc).__name__, exc)
            continue
        added.append(path)
    return added


# ── Anthropic streaming success-logging guard ───────────────────────────────
# THE BUG (upstream; present in LiteLLM 1.93.0 and 1.94.1). A streamed
# /v1/messages request carrying a web-search tool makes LiteLLM's own success
# logging raise. Reproduces with plain curl, no client involved:
#
#     POST /v1/messages   stream: true   tools: [{"type": "web_search_20250305",
#                                                 "name": "web_search"}]
#       -> 3 x "LiteLLM.Success_Call Error: 1 validation error for AnthropicResponse"
#     the same request with stream: false -> 0 errors
#
# The websearch path converts the stream and hands a fake stream iterator to
# _handle_anthropic_messages_response_logging, which validates it as an
# AnthropicResponse and raises. Upstream patched that method once before for
# unhandled types (BerriAI/litellm#27091) and missed this one.
#
# Non-blocking: the client still receives the full stream, only LiteLLM's own
# success/spend logging is skipped. The cost is an ERROR traceback per streamed
# web-search request, which buries real errors in a log audit.
#
# SELF-RETIRING. The guard no-ops if the installed method already handles the
# type, so an upstream fix disables it and the boot line reports
# `skipped:`/`already patched`. Re-read the installed method after any upgrade.
# Delete this section once the curl repro above is clean with the module removed
# from `litellm_settings.callbacks`.

PATCH_MARKER = "_ailocal_fake_stream_guard"


def _fake_stream_types():
    """The iterator classes the websearch path can hand to success logging.

    Imported defensively: these live under `experimental_pass_through`, so a
    LiteLLM refactor may move or remove them. A missing class means there is
    nothing to guard against, not an error worth breaking the proxy over.
    """
    found = []
    try:
        from litellm.llms.anthropic.experimental_pass_through.messages.fake_stream_iterator import (
            FakeAnthropicMessagesStreamIterator,
        )
        found.append(FakeAnthropicMessagesStreamIterator)
    except Exception as exc:  # noqa: BLE001
        log.debug("fake stream iterator not importable (%s) — nothing to patch", exc)
    return tuple(found)


def apply_stream_log_guard():
    """Install the guard. Idempotent; returns a short status string for tests."""
    try:
        from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLogging
    except Exception as exc:  # noqa: BLE001
        return f"skipped: cannot import LiteLLM logging ({exc})"

    target = getattr(LiteLLMLogging, "_handle_anthropic_messages_response_logging", None)
    if target is None:
        return "skipped: method absent (upstream refactor?)"
    if getattr(target, PATCH_MARKER, False):
        return "already patched"

    types = _fake_stream_types()
    if not types:
        return "skipped: no fake-stream types to guard"

    def patched(self, result, *args, **kwargs):
        # The one added line of behaviour: hand back a fake stream iterator
        # untouched, exactly as upstream already does for ModelResponse. Placed
        # BEFORE the original so the pydantic validation is never reached.
        if isinstance(result, types):
            return result
        return target(self, result, *args, **kwargs)

    setattr(patched, PATCH_MARKER, True)
    patched.__doc__ = (target.__doc__ or "") + "\n\n[ailocal] guarded against fake stream iterators."
    LiteLLMLogging._handle_anthropic_messages_response_logging = patched
    return "patched"


class ProxyStartup(CustomLogger):
    """Registration vehicle only. The corrections above are applied at import;
    listing this in litellm_settings.callbacks is what triggers that import, and
    it means the gate's hook-import check covers them. Observes no request."""


# ── run, in dependency order ────────────────────────────────────────────────
# Registration must precede traffic. LiteLLM loads callbacks during config load,
# before the server accepts requests, and Starlette resolves app.routes per
# request — so a route added here is served from the first request onward.
REGISTERED, ALREADY_MAPPED, FAILED = register_local_models()
COMPAT_REGISTERED = register_compat_routes()
if COMPAT_REGISTERED:
    _say("compat_routes: registered " + ", ".join(
        f"{p} [{'/'.join(COMPAT_ROUTES[p])}]" for p in COMPAT_REGISTERED))
STREAM_LOG_GUARD = apply_stream_log_guard()
_say(f"anthropic_stream_logging_fix: {STREAM_LOG_GUARD}")

proxy_handler_instance = ProxyStartup()
