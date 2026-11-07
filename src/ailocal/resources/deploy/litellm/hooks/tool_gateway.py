"""Negotiate which tools reach the model (the Local Agent Gateway).

No model, client, tool or route literal lives here; tests/gateway-impl.py
enforces it, and facts go in registry.yaml. AILOCAL_TOOL_GATEWAY, per request:
off (default), report (never mutates), filter. An unknown value is reported and
treated as off. Fails open everywhere. `bytes_dropped` counts only tools that
would reach the backend; token figures are approximate (cl100k, ~1-2% low).
Registered after websearch_interception so it sees the full tool list.
Ref: https://docs.litellm.ai/docs/proxy/call_hooks
"""

import json
import os
import re
import time

from litellm.integrations.custom_logger import CustomLogger


def _load_registry_module():
    """Import sibling capability_registry.py by path.

    LiteLLM's file loader puts no hooks dir on sys.path, so a plain import fails
    at boot; sharing this loader would need the very import it works around.
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
    """Encode canonically; byte counts compare only if every site uses this."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                      sort_keys=True, default=str)


def emit(record):
    """Print one metric line (LiteLLM filters loggers); never raises."""
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


# Dialects share data["tools"] but not entry shape; only name and bytes matter.

def tool_name(tool):
    """Return a tool's logical name; unnamed ones get a protected `<type>`."""
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


TASK_ENV = "AILOCAL_TASK_NEGOTIATION"


def task_negotiation_enabled():
    """Return whether opt-in task negotiation is on; a miss strands an agent."""
    return (os.environ.get(TASK_ENV) or "").strip().lower() in ("1", "true", "on")


def _single_user_turn(data):
    """Return whether the user has spoken exactly once, whatever the agent did.

    Bounds the conversational class: holds for the agent loop answering one
    question, lifts on the next user ask. Unrecognised shapes → False (keep).
    """
    def is_real_user_turn(m):
        """Return whether a message is human text (tool results are `user`)."""
        if not isinstance(m, dict) or m.get("role") != "user":
            return False
        c = m.get("content")
        if isinstance(c, str):
            return bool(c.strip())
        if isinstance(c, list):
            return any(isinstance(b, dict) and b.get("type") == "text"
                       and str(b.get("text") or "").strip() for b in c)
        return False

    msgs = (data or {}).get("messages")
    if isinstance(msgs, list) and msgs:
        return sum(1 for m in msgs if is_real_user_turn(m)) == 1

    items = (data or {}).get("input")
    if isinstance(items, str):
        return True          # Responses API with a bare prompt string
    if isinstance(items, list) and items:
        # Responses API: function_call_output items are tool results.
        return sum(1 for i in items
                   if isinstance(i, dict) and i.get("role") == "user"
                   and i.get("type") not in ("function_call_output",)) == 1
    return False


def first_user_text(data):
    """Return the first user message in any dialect, minus client scaffolding.

    User text only, so a confused model cannot narrow its own tools;
    <system-reminder> blocks would otherwise classify every session.
    """
    def text_of(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(b.get("text") or "" for b in content
                              if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def strip_injected(text):
        out, depth = [], 0
        for chunk in re.split(r"(<system-reminder>|</system-reminder>)", text or ""):
            if chunk == "<system-reminder>":
                depth += 1
            elif chunk == "</system-reminder>":
                depth = max(0, depth - 1)
            elif depth == 0:
                out.append(chunk)
        return "".join(out).strip()

    for msg in (data or {}).get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            got = strip_injected(text_of(msg.get("content")))
            if got:
                return got
    items = (data or {}).get("input")
    if isinstance(items, str):
        return strip_injected(items)
    for item in items or []:
        if isinstance(item, dict) and item.get("role") == "user":
            got = strip_injected(text_of(item.get("content")))
            if got:
                return got
    return ""


def expand_namespaces(tools, cfg, group_of=None):
    """Flatten namespace bundles into standalone function tools; pure.

    LiteLLM drops Codex's MCP bundles before the backend; this runs first.

    Returns:
        (new_tools, expanded_info); an expanded bundle itself is removed.
    """
    if not cfg.get("enabled"):
        return tools, []
    # From the registry: the literal guard flags dict keys too (hence `bundle`).
    source_type = cfg.get("source_type")
    template = cfg.get("name_template") or "{namespace}__{tool}"
    limit = int(cfg.get("max_tools_per_namespace") or 40)
    only = set(cfg.get("only_groups") or [])

    out, info = [], []
    for tool in tools or []:
        if not (isinstance(tool, dict) and tool.get("type") == source_type):
            out.append(tool)
            continue
        ns = tool.get("name") or "bundle"
        subs = [t for t in (tool.get("tools") or []) if isinstance(t, dict)]
        if not subs:
            out.append(tool)          # nothing to expand; leave it be
            continue
        if len(subs) > limit:
            # Refuse, not truncate: a partial bundle hides tools undetectably.
            info.append({"bundle": ns, "expanded": 0, "sub_tools": len(subs),
                         "skipped": "exceeds max_tools_per_namespace"})
            out.append(tool)
            continue
        made = []
        for sub in subs:
            sub_name = sub.get("name")
            if not sub_name:
                continue
            flat = dict(sub)
            flat["name"] = template.format(namespace=ns, tool=sub_name)
            flat["type"] = "function"
            if only and group_of is not None and group_of(flat["name"]) not in only:
                continue
            made.append(flat)
        if made:
            out.extend(made)
            info.append({"bundle": ns, "expanded": len(made),
                         "sub_tools": len(subs)})
        else:
            out.append(tool)
    return out, info


def _schema_of(tool):
    """Return (container, key) of a tool's parameter schema, or (None, None)."""
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
    """Return a copy of a schema without `keys`; report mode must not mutate."""
    if isinstance(node, dict):
        return {k: _strip_keys(v, keys) for k, v in node.items()
                if k not in keys}
    if isinstance(node, list):
        return [_strip_keys(v, keys) for v in node]
    return node


def _truncate_descriptions(node, limit, marker, top_level=True):
    """Return a copy of a schema with `description` fields shortened."""
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
    """Return one tool rewritten by registry rules; strips before truncating."""
    if not rules.get("enabled") or not isinstance(tool, dict):
        return tool
    marker = rules.get("truncation_marker") or " ..."
    out = dict(tool)

    strip = set(rules.get("strip_keys") or [])
    if strip:
        container, key = _schema_of(out)
        if container is not None:
            # Rebuild, never mutate. Compare the original container: the copy
            # is always new, so testing it injected a bogus "function" key.
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
    """Return a cl100k token estimate, or None when no counter (not zero)."""
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

    def negotiate(self, data, call_type=None):
        """Decide which tools to forward; mutates nothing.

        Returns:
            (report, keep), keep being surviving tools in declaration order.
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

        # Flatten before inventory so bundled tools are measured like others.
        ns_cfg = {} if passthrough else reg.namespace_expansion()
        tools, expanded = expand_namespaces(tools, ns_cfg, reg.group_of)
        if expanded:
            data["tools"] = tools

        names = [tool_name(t) for t in tools]
        sizes = [tool_bytes(t) for t in tools]
        total_bytes = sum(sizes)

        def reaches(tool):
            """Return whether LiteLLM forwards this entry on this route."""
            ttype = tool.get("type") if isinstance(tool, dict) else None
            return not reg.route_drops_type(route, ttype)

        reachable_bytes = sum(s for t, s in zip(tools, sizes) if reaches(t))
        prefiltered = total_bytes - reachable_bytes

        # Remove if the model has no tools, or group removable and unprotected.
        removable = set() if passthrough else reg.removable_groups(client, model)
        supports_tools = reg.supports(model, "tools")

        # Opt-in task negotiation may only add to `removable`, never grant.
        task_class, task_hits, task_needed = None, 0, None
        if (not passthrough) and task_negotiation_enabled():
            task_class, task_needed, task_hits = reg.classify_task(
                first_user_text(data))

            # Conversational holds for one user request; see _single_user_turn.
            if task_class == "conversational" and not _single_user_turn(data):
                task_class, task_needed, task_hits = None, None, 0

            if task_needed is not None:
                # All groups are candidates, else this adds nothing; safety is
                # the `always` floor, protection, no-op on miss, and opt-in.
                removable = removable | {g for g in reg.all_groups()
                                         if g not in task_needed}

        keep, drop = [], []
        for tool, name, size in zip(tools, names, sizes):
            if passthrough:
                verdict = True
            elif supports_tools is False:
                # No schemas to a toolless model; protected ones are rewritten
                # by another layer first.
                verdict = reg.is_protected(name)
            elif reg.is_protected(name):
                verdict = True
            else:
                verdict = reg.group_of(name) not in removable
            (keep if verdict else drop).append((name, size, tool))

        # Shrink survivors; never a passthrough model or protected tool.
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
        # Bytes the model receives; the only figure comparable to reachable.
        kept_reachable = sum(s for _, s, t in rewritten if reaches(t))
        dropped_bytes = sum(s for _, s, t in drop if reaches(t))
        dropped_moot = sum(s for _, s, t in drop if not reaches(t))
        keep = rewritten

        # LiteLLM's later translation drops types it cannot express, so
        # `tools_kept` means forwarded; pre-translation keeps its own name.
        forwarded = [(n, s, t) for n, s, t in keep if reaches(t)]
        killed_downstream = [
            {"name": n, "type": (t.get("type") if isinstance(t, dict) else None),
             "bytes": s,
             "reason": "dropped by litellm %s translation: unsupported type"
                       % route.strip("/")}
            for n, s, t in keep if not reaches(t)]

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
            "namespace_expansion": bool(ns_cfg.get("enabled")),
            "namespaces_expanded": expanded or None,
            "task_negotiation": task_negotiation_enabled(),
            "task_class": task_class,
            "task_pattern_hits": task_hits,
            "task_needed_groups": sorted(task_needed) if task_needed else None,
            "tools_in": len(tools),
            "bytes_in": total_bytes,
            "tools_reachable": sum(1 for t in tools if reaches(t)),
            "bytes_reachable": reachable_bytes,
            "bytes_prefiltered_by_litellm": prefiltered,
            # Forwarded: the only count comparable with tools_reachable.
            "tools_kept": len(forwarded),
            # Kept by the gateway, before translation.
            "tools_kept_by_gateway": len(keep),
            "tools_killed_by_translation": len(killed_downstream),
            "killed_by_translation": killed_downstream or None,
            "tools_dropped": len(drop),
            "bytes_kept": rewritten_bytes,
            "bytes_kept_reachable": kept_reachable,
            # Separates dropping from shrinking.
            "bytes_kept_before_rewrite": kept_bytes,
            "bytes_saved_by_rewrite": kept_bytes - rewritten_bytes,
            "rewrite_enabled": bool(rules.get("enabled")),
            "rewrite_rules": {k: v for k, v in rules.items()
                              if k != "truncation_marker"} or None,
            "bytes_dropped": dropped_bytes,
            "bytes_dropped_moot": dropped_moot,
            "tokens_est_in": tokens_est(encode(tools), model),
            # Forwarded tools only; dropped bundles would inflate the estimate.
            "tokens_est_kept": tokens_est(
                encode([t for _, _, t in forwarded]), model),
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

    def capture(self, data, report):
        """Dump the raw tool payload for the test corpus."""
        if not CAPTURE_DIR:
            return
        try:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            stamp = "%d-%s-%s" % (time.time() * 1000, report["client"],
                                  report["route"].strip("/").replace("/", "-"))
            with open(os.path.join(CAPTURE_DIR, stamp + ".json"), "w",
                      encoding="utf-8") as f:
                # Classifier input, so a wrong task_class is investigable; safe
                # only because capture is opt-in, unlike the metric line.
                json.dump({"report": report, "tools": data.get("tools") or [],
                           "model": data.get("model"),
                           "classified_text": (first_user_text(data) or "")[:400],
                           "user_turns": sum(
                               1 for m in (data.get("messages") or [])
                               if isinstance(m, dict) and m.get("role") == "user"),
                           "single_user_turn": _single_user_turn(data)},
                          f, indent=2, default=str)
        except Exception as exc:
            emit({"event": "capture_failed", "error": str(exc)})

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
