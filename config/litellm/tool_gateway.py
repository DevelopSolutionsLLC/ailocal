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
import re
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


TASK_ENV = "AILOCAL_TASK_NEGOTIATION"


def task_negotiation_enabled():
    """Phase D is opt-in. Keyword classification of natural language is a
    heuristic, and the cost of a miss is asymmetric: dropping a tool the task
    needed strands the agent, while keeping a spare tool only costs tokens."""
    return (os.environ.get(TASK_ENV) or "").strip().lower() in ("1", "true", "on")


def _single_user_turn(data):
    """True when the USER has said exactly one thing, whatever the agent has done since.

    This bounds the conversational class. Two failure modes had to be avoided and
    they pull in opposite directions:

      - Classification always reads the FIRST user message, which never changes
        as a session grows. Make the override sticky on that and a session
        opening with a chat question stays tool-less forever, so a later
        "now fix this file" can never touch anything.
      - Release it on the agent's own continuation turn and the override lapses
        mid-loop. MEASURED: "show me an example of hello world in c++"
        classified conversational, the model produced a second turn, tools
        re-armed (48 of 61 kept) and it went on to call rg, Bash x3 and Write.
        That is the exact repo-crawling behaviour the class exists to stop, and
        a single-turn measurement (61 -> 1) had hidden it.

    Counting only USER messages resolves both: the override holds for the whole
    agent loop that answers one question, and lifts the moment the user asks for
    something else. Anything unrecognised returns False, so it fails CLOSED and
    tools are kept.
    """
    def is_real_user_turn(m):
        """A user message the HUMAN wrote, not a tool result.

        The trap: on the Anthropic route, tool results come back as
        `role: "user"` messages carrying `tool_result` blocks. Counting user
        messages naively therefore releases the override the instant any tool
        runs — measured, and it is why a conversational request still ended up
        calling rg/Bash/Write. Only a user turn containing actual text counts.
        """
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
        # Responses API: function_call_output items are the tool-result shape.
        return sum(1 for i in items
                   if isinstance(i, dict) and i.get("role") == "user"
                   and i.get("type") not in ("function_call_output",)) == 1
    return False


def first_user_text(data):
    """The task statement: the first user message, across all three dialects.

    Reads ONLY the user's own request. Assistant turns and tool results are the
    model's output, and letting those steer which tools remain available would
    let a confused model narrow its own capabilities mid-loop.

    Client-injected scaffolding (<system-reminder> blocks carrying CLAUDE.md and
    similar) is stripped: measured, an unstripped first message began with the
    entire contents of a global instructions file, which would classify every
    session by whatever words happened to appear in it.
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
    """Flatten namespace bundles into standalone function tools.

    Codex declares MCP servers as bundles ({"type": <source_type>, "name":
    "mcp__lsp", "tools":[<function tools>]}), and LiteLLM discards those entries
    before the backend — so the model never learns those tools exist. The
    sub-tools are already valid function tools, and this hook runs before the
    drop, so flattening them here is sufficient.

    Returns (new_tools, expanded_info). The bundle itself is REMOVED once
    expanded: leaving it would pay its bytes twice and it would be dropped
    downstream anyway.

    Pure — builds a new list.
    """
    if not cfg.get("enabled"):
        return tools, []
    # The bundle type comes from the registry, not from a literal here. The
    # architectural test greps this module for tool/type literals and rejected it
    # when inlined — including as a report key and a fallback string, which is
    # why the reported field is `bundle`. The guard cannot tell a conditional
    # from a dict key, and renaming is cheaper than weakening the guard.
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
            # Refuse rather than silently truncate: a partially-expanded bundle
            # would advertise some tools and hide others with no way to tell.
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

        # Flatten namespace bundles first, so the tools inside them are
        # measured, grouped and negotiated exactly like any other function tool.
        # Done before inventory, not after: expanding afterwards would report
        # byte figures for a payload that was never sent.
        ns_cfg = {} if passthrough else reg.namespace_expansion()
        tools, expanded = expand_namespaces(tools, ns_cfg, reg.group_of)
        if expanded:
            data["tools"] = tools

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

        # Phase D: automatic task negotiation, off unless explicitly enabled.
        # A classification may only ADD to `removable` (i.e. subtract from what
        # the model is sent) — never grant a group the client or model class
        # disallowed. And it only ever narrows groups the client was already
        # willing to drop, so a misread task cannot remove a tool that Phase B
        # considered load-bearing.
        task_class, task_hits, task_needed = None, 0, None
        if (not passthrough) and task_negotiation_enabled():
            task_class, task_needed, task_hits = reg.classify_task(
                first_user_text(data))

            # Bound the conversational class to a SINGLE USER REQUEST — see
            # _single_user_turn for why it counts users only. Sticky-forever and
            # release-on-the-agent's-own-continuation are both wrong, in
            # opposite directions, and the second was measured stripping then
            # re-arming tools mid-loop.
            if task_class == "conversational" and not _single_user_turn(data):
                task_class, task_needed, task_hits = None, None, 0

            if task_needed is not None:
                # Candidates are ALL known groups, not just the ones the client
                # profile already offered. Restricting them to the client's
                # drop_groups made Phase D incapable of doing anything Phase B
                # had not already done — "simple edit -> read + edit" cannot
                # shed lsp if lsp was never a candidate. Caught by a test that
                # asserted the shedding actually happens.
                #
                # Safety does not come from a narrow candidate set. It comes
                # from: the registry's `always` floor, protected tools, an
                # unmatched task being a no-op, passthrough models being exempt,
                # and the whole feature being opt-in.
                removable = removable | {g for g in reg.all_groups()
                                         if g not in task_needed}

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

        # A tool the gateway keeps is NOT necessarily a tool the backend sees:
        # LiteLLM's Responses->Chat-Completions transformation runs AFTER this
        # hook and silently discards any type it cannot express (namespace,
        # custom, web_search). Reporting those as "kept" is how Codex's
        # mcp__grepai/mcp__lsp bundles read as delivered while the model never
        # received them — measured, and it cost two misdiagnoses. So `tools_kept`
        # means FORWARDED, and the pre-translation figure keeps its own name.
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
            # FORWARDED to the backend — survived the gateway AND survives
            # LiteLLM's route translation. This is the only count comparable
            # with tools_reachable, exactly as bytes_kept_reachable is the only
            # byte figure comparable with bytes_reachable.
            "tools_kept": len(forwarded),
            # What the gateway itself kept, before translation. Retained under
            # its own name so the two stages stay separable.
            "tools_kept_by_gateway": len(keep),
            "tools_killed_by_translation": len(killed_downstream),
            "killed_by_translation": killed_downstream or None,
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
            # Tokens the model actually pays for: forwarded only. Counting
            # translation-killed bundles here inflated the estimate by ~17K on
            # every Codex request.
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
                # `classified_text` is what first_user_text() actually handed the
                # classifier, truncated. Without it a wrong task_class is
                # uninvestigable: you can see the verdict but not the input, and
                # the input is the part that drifts across a multi-turn session.
                # Capture is opt-in (AILOCAL_TOOL_GATEWAY_CAPTURE) and off by
                # default, which is why it is safe to record request text here
                # and NOT in the always-on metric line.
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
