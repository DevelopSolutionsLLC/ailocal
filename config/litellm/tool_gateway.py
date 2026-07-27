"""
tool_gateway.py — the Local Agent Gateway's capability negotiator.

    incoming request -> inspect client -> inspect model -> inspect tools
                     -> select compatible subset -> forward

This module contains NO knowledge of any specific model, client, tool or route.
Every such fact lives in config/litellm/registry.yaml and is reached through
capability_registry.py. That is enforced, not merely intended:
scripts/test-capability-registry.py greps this file's executable code for model,
client and tool literals and fails if it finds any. If a fact about a model
belongs anywhere, it belongs in the registry.

WHY THE GATEWAY, NOT THE MODEL, IS THE SUBJECT
----------------------------------------------
Earlier revisions were built around "make the local model work", which put model
assumptions into code. This one is built around the boundary. A model the
registry marks `passthrough` — any frontier/cloud model — is measured and
forwarded untouched, no matter what the feature flags say. A local model is
optimised aggressively. Swapping either is a registry edit.

MODES (env AILOCAL_TOOL_GATEWAY, read per-request)
--------------------------------------------------
  off     hook returns immediately. Nothing measured, nothing changed. DEFAULT.
  report  measure and log. NEVER mutates the request.
  filter  measure, then remove the negotiated-away tools.

An unrecognised value is reported and treated as off — never silently coerced,
because a typo'd env var that quietly disables a safety layer is indistinguishable
from the layer working.

WHAT IT REFUSES TO CLAIM
------------------------
`bytes_dropped` counts only tools that would otherwise have REACHED the backend.
LiteLLM discards some tool types itself during dialect translation (which types
is a registry fact, per route). Counting those would credit this gateway with
work already done — measured, that is the difference between a 71% and an 18%
figure on one real client. They are reported separately as
`bytes_prefiltered_by_litellm` and `bytes_dropped_moot`.

Token figures come from litellm's counter, which selects the cl100k tokenizer
even for a non-OpenAI backend. Calibrated against Ollama's real
prompt_eval_count at 1.009-1.021 (scripts/calibrate-tokens.py), so the estimate
under-counts by 1-2% on tool-schema JSON. Labelled `cl100k-proxy` in every
record; re-calibrate after a model change.

FAIL-OPEN, EVERYWHERE
---------------------
No registry, an unparseable registry, an unknown client, an unknown model, an
unnamed tool entry: each results in forwarding the request unchanged. A
capability layer that fails closed turns a config mistake into a client that has
silently lost its tools.

Registered LAST in litellm_settings.callbacks, after websearch_interception, so
that interception always sees the client's full tool list.
Ref: https://docs.litellm.ai/docs/proxy/call_hooks
"""

import json
import os
import time

from litellm.integrations.custom_logger import CustomLogger


def _load_registry_module():
    """Import capability_registry.py as a SIBLING FILE, by path.

    LiteLLM loads callbacks with importlib.spec_from_file_location, which does
    NOT put the module's directory on sys.path. So `from capability_registry
    import Registry` raises ModuleNotFoundError at proxy boot and takes the whole
    container down — measured, by doing exactly that. Neither does a package
    import work: /app/config is not a package.

    Resolving relative to __file__ is the only form that holds under all three
    load paths: the proxy's file loader, a direct `python tool_gateway.py`, and
    a test harness that loads it by path from the repo.
    """
    import importlib.util
    import sys as _sys
    if "capability_registry" in _sys.modules:
        return _sys.modules["capability_registry"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "capability_registry.py")
    spec = importlib.util.spec_from_file_location("capability_registry", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a circular import cannot re-enter this loader.
    _sys.modules["capability_registry"] = module
    spec.loader.exec_module(module)
    return module


Registry = _load_registry_module().Registry

MODE_ENV = "AILOCAL_TOOL_GATEWAY"
VALID_MODES = ("off", "report", "filter")
CAPTURE_DIR = os.environ.get("AILOCAL_TOOL_GATEWAY_CAPTURE") or ""


def encode(obj):
    """The one canonical encoding. Byte counts are only comparable if every call
    site uses this — separators, ensure_ascii and key order all change length."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                      sort_keys=True, default=str)


def emit(record):
    """One metric line to stdout. print(), not logging: LiteLLM filters
    third-party loggers, so this is what survives to `docker logs`. Never
    raises — a measurement layer must not be able to break the request path."""
    try:
        print("tool_gateway_metric " + json.dumps(record, default=str),
              flush=True)
    except Exception:
        pass


def mode():
    raw = (os.environ.get(MODE_ENV) or "off").strip().lower()
    if raw not in VALID_MODES:
        emit({"event": "bad_mode", "value": raw, "using": "off"})
        return "off"
    return raw


# ── Tool-shape normalisation ────────────────────────────────────────────────
# All three dialects put the array at data["tools"] and disagree about the
# contents. The registry documents the shapes; the gateway only needs a logical
# name and a byte count, so this stays dialect-agnostic by construction.

def tool_name(tool):
    """Logical name, whatever envelope the entry arrived in. Entries with no
    name at all (a bare {"type":"web_search"}) get a bracketed pseudo-name so
    they are still counted — they are real payload weight — and the registry
    treats bracketed names as protected."""
    if not isinstance(tool, dict):
        return "<malformed>"
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return fn["name"]
    if tool.get("name"):
        return tool["name"]
    return "<%s>" % (tool.get("type") or "unknown")


def tool_bytes(tool):
    return len(encode(tool).encode("utf-8"))


def _schema_of(tool):
    """(container, key) for a tool's parameter schema, whichever dialect it is.
    Returns (None, None) when there is none to rewrite."""
    if not isinstance(tool, dict):
        return None, None
    fn = tool.get("function")
    if isinstance(fn, dict):
        for key in ("parameters", "input_schema"):
            if isinstance(fn.get(key), dict):
                return fn, key
    for key in ("input_schema", "parameters"):
        if isinstance(tool.get(key), dict):
            return tool, key
    return None, None


def _strip_keys(node, keys):
    """Recursively drop `keys` from a schema. Returns a new structure; the
    caller's original tool dict is never mutated, because REPORT mode must be
    able to measure a rewrite without performing one."""
    if isinstance(node, dict):
        return {k: _strip_keys(v, keys) for k, v in node.items()
                if k not in keys}
    if isinstance(node, list):
        return [_strip_keys(v, keys) for v in node]
    return node


def _truncate_descriptions(node, limit, marker, top_level=True):
    """Recursively shorten `description` fields inside a schema."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if (k == "description" and isinstance(v, str) and limit
                    and len(v) > limit):
                out[k] = v[:limit].rstrip() + marker
            else:
                out[k] = _truncate_descriptions(v, limit, marker, False)
        return out
    if isinstance(node, list):
        return [_truncate_descriptions(v, limit, marker, False) for v in node]
    return node


def rewrite_tool(tool, rules):
    """Apply the registry's rewrite rules to one tool. Pure: returns a new dict.

    Order matters only in that stripping first means truncation never has to
    consider keys that are about to disappear.
    """
    if not rules.get("enabled") or not isinstance(tool, dict):
        return tool
    marker = rules.get("truncation_marker") or " ..."
    out = dict(tool)

    strip = set(rules.get("strip_keys") or [])
    if strip:
        container, key = _schema_of(out)
        if container is not None:
            # Rebuild the containing dict rather than mutating the caller's.
            #
            # The identity test must be against the ORIGINAL container, not the
            # copy: `dict(container)` is always a new object, so comparing the
            # copy was unconditionally true and injected a bogus "function" key
            # into Anthropic-shaped tools — corrupting the schema and inflating
            # the byte count. Caught by the kept+dropped==in accounting test.
            at_top = container is out
            new_container = dict(container)
            new_container[key] = _strip_keys(container[key], strip)
            if at_top:
                out = new_container
            else:
                out["function"] = new_container

    limit = rules.get("max_description_chars")
    if limit and isinstance(out.get("description"), str) \
            and len(out["description"]) > limit:
        out["description"] = out["description"][:limit].rstrip() + marker
    fn = out.get("function")
    if limit and isinstance(fn, dict) and isinstance(fn.get("description"), str) \
            and len(fn["description"]) > limit:
        fn = dict(fn)
        fn["description"] = fn["description"][:limit].rstrip() + marker
        out["function"] = fn

    param_limit = rules.get("max_param_description_chars")
    if param_limit:
        container, key = _schema_of(out)
        if container is not None:
            at_top = container is out
            new_container = dict(container)
            new_container[key] = _truncate_descriptions(
                container[key], param_limit, marker)
            if at_top:
                out = new_container
            else:
                out["function"] = new_container
    return out


def tokens_est(text, model=None):
    """cl100k-proxy estimate. Returns None rather than a fabricated number when
    the counter is unavailable: a missing measurement and a measurement of zero
    are not the same thing."""
    try:
        import litellm
        return litellm.token_counter(model=model or "gpt-4o", text=text)
    except Exception:
        return None


class ToolGateway(CustomLogger):

    def __init__(self, registry=None):
        super().__init__()
        self.registry = registry if registry is not None else Registry()
        emit({"event": "gateway_init", "registry": self.registry.describe()})

    # ── negotiation (pure: takes a request, returns a decision) ─────────────
    def negotiate(self, data, call_type=None):
        """Decide which tools to forward. Mutates nothing; the caller applies.

        Returns (report, keep) where keep is the list of surviving tool dicts in
        declaration order.
        """
        reg = self.registry
        tools = (data or {}).get("tools") or []
        model = (data or {}).get("model")

        headers = ((data or {}).get("proxy_server_request") or {}).get("headers")
        client = reg.detect_client(headers)
        route = reg.route_for_call_type(call_type, "input" in (data or {}))
        capability = reg.capability_of(model)
        model_class, _ = reg.model_class(model)
        passthrough = reg.is_passthrough(model)

        names = [tool_name(t) for t in tools]
        sizes = [tool_bytes(t) for t in tools]
        total_bytes = sum(sizes)

        def reaches(tool):
            """Whether LiteLLM forwards this entry to the backend on this route."""
            ttype = tool.get("type") if isinstance(tool, dict) else None
            return not reg.route_drops_type(route, ttype)

        reachable_bytes = sum(s for t, s in zip(tools, sizes) if reaches(t))
        prefiltered = total_bytes - reachable_bytes

        # The decision. A tool is removed only when the model declares no tool
        # support at all, or when its group is removable for this (client, model)
        # pair AND it is not protected. Passthrough short-circuits everything.
        removable = set() if passthrough else reg.removable_groups(client, model)
        supports_tools = reg.supports(model, "tools")

        keep, drop = [], []
        for tool, name, size in zip(tools, names, sizes):
            if passthrough:
                verdict = True
            elif supports_tools is False:
                # A model that cannot use tools should not be sent schemas at
                # all. Protected entries still survive: web search is rewritten
                # by another layer before the model is involved.
                verdict = reg.is_protected(name)
            elif reg.is_protected(name):
                verdict = True
            else:
                verdict = reg.group_of(name) not in removable
            (keep if verdict else drop).append((name, size, tool))

        # Schema rewrites (Phase C): shrink what survives, without removing it.
        # Never applied to a passthrough model or a protected tool — a frontier
        # model must be untouched, and a protected tool is one whose exact shape
        # another layer depends on.
        rules = {} if passthrough else reg.rewrite_rules(client)
        rewritten = []
        for name, size, tool in keep:
            if rules.get("enabled") and not reg.is_protected(name):
                new_tool = rewrite_tool(tool, rules)
                rewritten.append((name, tool_bytes(new_tool), new_tool))
            else:
                rewritten.append((name, size, tool))

        kept_bytes = sum(s for _, s, _ in keep)
        rewritten_bytes = sum(s for _, s, _ in rewritten)
        # What the MODEL actually receives: kept tools minus the ones LiteLLM
        # discards on this route anyway. bytes_kept alone is not comparable with
        # bytes_reachable — on /v1/responses it counts namespace bundles that
        # never reach a backend, which made a real measurement read as a -133%
        # "reduction". Any before/after ratio must use this field.
        kept_reachable = sum(s for _, s, t in rewritten if reaches(t))
        dropped_bytes = sum(s for _, s, t in drop if reaches(t))
        dropped_moot = sum(s for _, s, t in drop if not reaches(t))
        keep = rewritten

        report = {
            "route": route,
            "client": client,
            "model": model,
            "capability": capability,
            "model_class": model_class,
            "passthrough": passthrough,
            "registry": reg.state,
            "max_context": reg.max_context(model),
            "removable_groups": sorted(removable),
            "tools_in": len(tools),
            "bytes_in": total_bytes,
            "tools_reachable": sum(1 for t in tools if reaches(t)),
            "bytes_reachable": reachable_bytes,
            "bytes_prefiltered_by_litellm": prefiltered,
            "tools_kept": len(keep),
            "tools_dropped": len(drop),
            "bytes_kept": rewritten_bytes,
            "bytes_kept_reachable": kept_reachable,
            # Kept-before-rewrite vs after, so the two reductions (dropping a
            # tool, shrinking a tool) are never conflated into one figure.
            "bytes_kept_before_rewrite": kept_bytes,
            "bytes_saved_by_rewrite": kept_bytes - rewritten_bytes,
            "rewrite_enabled": bool(rules.get("enabled")),
            "rewrite_rules": {k: v for k, v in rules.items()
                              if k != "truncation_marker"} or None,
            "bytes_dropped": dropped_bytes,
            "bytes_dropped_moot": dropped_moot,
            "tokens_est_in": tokens_est(encode(tools), model),
            "tokens_est_kept": tokens_est(encode([t for _, _, t in keep]), model),
            "tokenizer": "cl100k-proxy",
            "dropped_names": sorted(n for n, _, _ in drop),
            "dropped_groups": sorted({reg.group_of(n) or "ungrouped"
                                      for n, _, _ in drop}),
            "largest": sorted(zip(names, sizes), key=lambda p: -p[1])[:10],
        }
        if passthrough:
            report["savings_claim"] = (
                "none — model class is passthrough; measured and forwarded "
                "unchanged by design")
        elif not reg.loaded:
            report["savings_claim"] = "none — registry %s" % reg.state
        elif not removable and supports_tools is not False:
            report["savings_claim"] = (
                "none — no group is removable for this client/model pair")
        return report, keep

    # ── capture ────────────────────────────────────────────────────────────
    def capture(self, data, report):
        """Dump the raw tool payload for offline analysis. This is how real
        client payloads enter the test corpus — captured from the live path,
        never hand-written to look plausible."""
        if not CAPTURE_DIR:
            return
        try:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            stamp = "%d-%s-%s" % (time.time() * 1000, report["client"],
                                  report["route"].strip("/").replace("/", "-"))
            with open(os.path.join(CAPTURE_DIR, stamp + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump({"report": report, "tools": data.get("tools") or [],
                           "model": data.get("model")}, f, indent=2, default=str)
        except Exception as exc:
            emit({"event": "capture_failed", "error": str(exc)})

    # ── LiteLLM entrypoint ─────────────────────────────────────────────────
    async def async_pre_call_hook(self, user_api_key_dict, cache, data,
                                  call_type):
        current = mode()
        if current == "off":
            return data
        if not (data or {}).get("tools"):
            return data

        started = time.perf_counter()
        try:
            report, keep = self.negotiate(data, call_type)
        except Exception as exc:
            # Negotiation failing must not fail the request.
            emit({"event": "negotiate_failed",
                  "error": "%s: %s" % (type(exc).__name__, exc)})
            return data

        report["mode"] = current
        report["overhead_ms"] = round((time.perf_counter() - started) * 1000, 3)

        applied = (current == "filter" and report["tools_dropped"] > 0
                   and not report["passthrough"])
        if applied:
            data["tools"] = [t for _, _, t in keep]
        report["applied"] = applied

        emit(report)
        self.capture(data, report)
        return data


proxy_handler_instance = ToolGateway()
