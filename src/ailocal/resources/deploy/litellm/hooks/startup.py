"""Proxy patches applied once at import, before the first request.

Each reports one boot line via print() (the proxy filters third-party loggers)
and never raises: a failure degrades one correction, never the container.
"""

import logging
import os
import sys

import litellm
from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("startup")

#: The generated config, mounted at /app/generated (not authored /app/config).
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/generated/config.yaml")


def _say(msg):
    """Print one boot line, visible in `docker logs`."""
    print(msg, flush=True)


# LiteLLM stores local model_info under the provider-stripped key, the router
# looks it up prefixed, and max_input_tokens validation is silently skipped.
# Register under the exact prefixed key. DELETE when all are "already mapped".


def _load_model_list():
    """Return the generated model_list, or [] if unreadable."""
    try:
        import yaml

        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("model_list") or []
    except Exception as exc:  # noqa: BLE001 - never block proxy startup
        log.error("model_registrar: could not read %s: %s", CONFIG_PATH, exc)
        _say(f"model_registrar: could not read {CONFIG_PATH}: {exc}")
        return []


def _has_exact_key(model_key):
    """Return whether the exact key is in litellm.model_cost.

    Not get_model_info: it falls back to the stripped key the router cannot use.
    """
    return model_key in litellm.model_cost


def _router_can_resolve(model_key):
    """Replicate the router's own lookup, for the self-check."""
    try:
        litellm.get_model_info(model=model_key)
        return model_key in litellm.model_cost
    except Exception:  # noqa: BLE001 - a raise here IS the "not mapped" signal
        return False


def _cost_entry(model_key, model_info):
    """Build a cost-map entry from the deployment's own model_info block.

    litellm_provider (from the key prefix) survives _check_provider_match.
    """
    provider = model_key.split("/", 1)[0] if "/" in model_key else None
    entry = {
        "input_cost_per_token": model_info.get("input_cost_per_token", 0),
        "output_cost_per_token": model_info.get("output_cost_per_token", 0),
        "mode": model_info.get("mode", "chat"),
    }
    if provider:
        entry["litellm_provider"] = provider
    # The admission check compares counted input tokens against these.
    for field in ("max_input_tokens", "max_output_tokens", "max_tokens"):
        if model_info.get(field) is not None:
            entry[field] = model_info[field]
    return entry


def register_local_models():
    """Inject every deployment LiteLLM cannot already map, then verify.

    Returns:
        (registered, already_mapped, failed) model keys.
    """
    registered, already, failed = [], [], []

    for deployment in _load_model_list():
        params = deployment.get("litellm_params") or {}
        model_key = params.get("model")
        if not model_key:
            continue
        if _has_exact_key(model_key):
            already.append(model_key)
            continue
        # Not register_model(): it strips the prefix, the defect worked around.
        litellm.model_cost[model_key] = _cost_entry(
            model_key, deployment.get("model_info") or {})
        registered.append(model_key)

    try:
        from litellm.utils import _invalidate_model_cost_lowercase_map

        _invalidate_model_cost_lowercase_map()
    except Exception:  # noqa: BLE001 - private helper; absence is not fatal
        pass

    # get_model_info is LRU-cached; clear stale prefix-stripped answers.
    try:
        from litellm.utils import _cached_get_model_info

        _cached_get_model_info.cache_clear()
    except Exception:  # noqa: BLE001 - private helper; absence is not fatal
        pass

    # Fail loudly via the router's real lookup if LiteLLM internals shift.
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


# Claude Code probes api.anthropic.com-shaped routes LiteLLM 404s. Answer with
# a fixed empty 200: no auth, no backend, not health state, nothing under /v1.

#: Path -> methods; every entry is a static empty 200, nothing with logic.
COMPAT_ROUTES = {
    "/api/hello": ["GET", "HEAD"],
}


def _empty_ok():
    """Return a fixed empty 200 with no side effects."""
    from fastapi import Response

    return Response(status_code=200)


def _get_app():
    """Return the running proxy's FastAPI app, or None.

    Imports the proxy only when standalone (the gate's importability check).
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
    """Add the compatibility routes; return the paths added.

    Never raises; logs at ERROR so a no-op does not hide the remaining 404.
    """
    app = _get_app()
    if app is None:
        log.error("compat_routes: no FastAPI app found. Routes NOT registered.")
        return []

    existing = {getattr(r, "path", None) for r in app.routes}
    added = []
    for path, methods in COMPAT_ROUTES.items():
        if path in existing:
            # Ours already, or LiteLLM grew one: self-retiring.
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


# Upstream bug (LiteLLM ≥ 1.93; cf. BerriAI/litellm#27091): websearch streams
# hand a fake iterator to success logging, which raises an ERROR per request.
# Repro: streamed /v1/messages w/ web_search tool. DELETE when clean w/o this.

PATCH_MARKER = "_ailocal_fake_stream_guard"


def _fake_stream_types():
    """Return the fake-stream iterator classes success logging can receive.

    Both module paths are tried: 1.105.1 moved it while the bug persisted.
    """
    import importlib

    found = []
    for module in (
        "litellm.llms.anthropic.experimental_pass_through.messages.fake_stream_iterator",
        "litellm.llms.anthropic.pass_through.messages.fake_stream_iterator",
    ):
        try:
            cls = importlib.import_module(module).FakeAnthropicMessagesStreamIterator
        except Exception as exc:  # noqa: BLE001
            log.debug("fake stream iterator not importable from %s (%s)", module, exc)
            continue
        if cls not in found:
            found.append(cls)
    return tuple(found)


def apply_stream_log_guard():
    """Install the guard idempotently; return a short status string."""
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
        # Before the original, so pydantic validation is never reached.
        if isinstance(result, types):
            return result
        return target(self, result, *args, **kwargs)

    setattr(patched, PATCH_MARKER, True)
    patched.__doc__ = (target.__doc__ or "") + "\n\n[ailocal] guarded against fake stream iterators."
    LiteLLMLogging._handle_anthropic_messages_response_logging = patched
    return "patched"


# LiteLLM 1.106.5's Ollama transform drops `tool_name` and call `id`, so Gemma4
# renders results as `response:unknown` (#84; id fix: BerriAI/litellm#49278).
# Copies both from the request's own calls; DELETE when upstream forwards both.

TOOL_IDENTITY_MARKER = "_ailocal_tool_identity"
_IDENTITY_FIXTURE = [
    {"role": "user", "content": "fixture"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_fixture", "type": "function",
         "function": {"name": "fixture_tool", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_fixture", "content": "ok"},
]


def _plain(value):
    """Return a message or tool call as a dict, as the transform reads it."""
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value if isinstance(value, dict) else {}


def add_tool_identity(messages, request):
    """Set `tool_name` and tool-call `id` on a transformed request, in place.

    Any shape other than one message per input (and one call per function call)
    means upstream changed; the request is then left as built.
    """
    sent = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(sent, list) or len(sent) != len(messages):
        return
    plain = [_plain(m) for m in messages]
    names = {}
    for message in plain:
        for call in message.get("tool_calls") or []:
            call = _plain(call)
            name = (call.get("function") or {}).get("name")
            if call.get("id") and name:
                names[call["id"]] = name
    for source, out in zip(plain, sent):
        if not isinstance(out, dict):
            continue
        if out.get("role") == "tool" and not out.get("tool_name"):
            name = names.get(source.get("tool_call_id"))
            if name:
                out["tool_name"] = name
        calls = [c for c in map(_plain, source.get("tool_calls") or []) if c.get("type") == "function"]
        built = out.get("tool_calls") or []
        if len(calls) != len(built):
            continue
        for call, emitted in zip(calls, built):
            if call.get("id") and isinstance(emitted, dict) and not emitted.get("id"):
                emitted["id"] = call["id"]


def _upstream_forwards(target, config_cls):
    """Return which of (tool_name, id) upstream already emits, or None."""
    try:
        out = target(config_cls(), "fixture", [dict(m) for m in _IDENTITY_FIXTURE], {}, {}, {})
        sent = out["messages"]
        return bool(sent[2].get("tool_name")), bool(sent[1]["tool_calls"][0].get("id"))
    except Exception as exc:  # noqa: BLE001
        log.debug("tool identity self-check failed (%s)", exc)
        return None


def apply_ollama_tool_identity():
    """Install the correction idempotently; return a short status string."""
    try:
        from litellm.llms.ollama.chat.transformation import OllamaChatConfig
    except Exception as exc:  # noqa: BLE001
        return f"skipped: cannot import OllamaChatConfig ({exc})"

    target = getattr(OllamaChatConfig, "transform_request", None)
    if target is None:
        return "skipped: transform_request absent (upstream refactor?)"
    if getattr(target, TOOL_IDENTITY_MARKER, False):
        return "already patched"
    forwarded = _upstream_forwards(target, OllamaChatConfig)
    if forwarded is None:
        return "skipped: self-check could not run the transform"
    if all(forwarded):
        return "not needed: upstream forwards tool_name and id"

    def patched(self, model, messages, *args, **kwargs):
        request = target(self, model, messages, *args, **kwargs)
        try:
            add_tool_identity(messages, request)
        except Exception as exc:  # noqa: BLE001 — never fail a request over a label
            log.debug("tool identity not applied (%s)", exc)
        return request

    setattr(patched, TOOL_IDENTITY_MARKER, True)
    patched.__doc__ = (target.__doc__ or "") + "\n\n[ailocal] keeps tool_name and tool-call ids."
    OllamaChatConfig.transform_request = patched
    missing = [f for f, have in zip(("tool_name", "id"), forwarded) if not have]
    return "patched: adds " + ", ".join(missing)


class ProxyStartup(CustomLogger):
    """Registration vehicle only: a callbacks entry is what triggers import."""


# Callbacks load before traffic, so these apply from the first request.
REGISTERED, ALREADY_MAPPED, FAILED = register_local_models()
COMPAT_REGISTERED = register_compat_routes()
if COMPAT_REGISTERED:
    _say("compat_routes: registered " + ", ".join(
        f"{p} [{'/'.join(COMPAT_ROUTES[p])}]" for p in COMPAT_REGISTERED))
STREAM_LOG_GUARD = apply_stream_log_guard()
_say(f"anthropic_stream_logging_fix: {STREAM_LOG_GUARD}")
TOOL_IDENTITY = apply_ollama_tool_identity()
_say(f"ollama_tool_identity: {TOOL_IDENTITY}")

proxy_handler_instance = ProxyStartup()
