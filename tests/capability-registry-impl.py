#!/usr/bin/env python3
"""test-capability-registry.py — tests for deploy/litellm/hooks/capability_registry.py
and the shipped deploy/litellm/registry.yaml.

Two kinds of check here, and the second matters more:

1. The registry answers correctly for the real models, clients and routes.
2. The registry is the ONLY place those facts live. There is a source-level
   assertion that the negotiator contains no model/client/tool literals, because
   "no hard-coded conditionals" is an architectural property that decays the
   moment someone adds one convenient `if`.

Run: python3 tests/capability-registry-impl.py   (needs PyYAML -> container)
"""

import os
import json
import sys
import importlib.util
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG_PY = os.environ.get("AILOCAL_REGISTRY_MODULE",
                        os.path.join(ROOT, "deploy/litellm/hooks/capability_registry.py"))
REG_YAML = os.environ.get("AILOCAL_REGISTRY",
                          os.path.join(ROOT, "deploy/litellm/registry.yaml"))
CAPS = os.environ.get("AILOCAL_CAPABILITIES_JSON",
                      "/app/generated/capabilities.json")
CONF = os.environ.get("AILOCAL_CONFIG_PATH",
                      "/app/generated/config.yaml")
# Overridable: inside the container ROOT resolves from /tmp, so the default
# would not find it. A missing file FAILS rather than passing vacuously —
# this is the check that keeps the architecture honest, so it must run.
GATEWAY_PY = os.environ.get("AILOCAL_GATEWAY_SOURCE",
                            os.path.join(ROOT, "deploy/litellm/hooks/tool_gateway.py"))

_spec = importlib.util.spec_from_file_location("capability_registry", REG_PY)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Suite

_suite = Suite()
check = _suite.check

try:
    import yaml  # noqa: F401
except ImportError:
    print("\nSKIPPED: PyYAML absent on this interpreter. Run inside the proxy "
          "image (tests/in-container.sh). Exiting non-zero so an "
          "incomplete run is never mistaken for a passing one.")
    sys.exit(1)

reg = cr.Registry(path=REG_YAML, caps_json=CAPS, config_path=CONF)

print("\nLOADING")
check(reg.loaded, f"registry.yaml loads ({reg.state}) {reg.error or ''}")
d = reg.describe()
check(d["groups"] >= 8, f"groups present ({d['groups']})")
check(d["model_classes"] >= 5, f"model classes present ({d['model_classes']})")
check(d["routes"] == 3, f"all three routes described ({d['routes']})")
check(d["contexts_from_generated"] >= 5,
      f"context windows come from the GENERATED file, not the registry "
      f"({d['contexts_from_generated']} found)")
check(d["aliases"] > 0, f"model_group_alias loaded ({d['aliases']})")

print("\nMODEL CLASSIFICATION")
name, _ = reg.model_class("ailocal-architecture")
check(name == "local_agentic", f"ailocal-architecture -> local_agentic (got {name})")
name, _ = reg.model_class("ailocal-implementation")
check(name == "local_nonagentic",
      f"ailocal-implementation -> local_nonagentic (got {name})")
name, _ = reg.model_class("ailocal-completion")
check(name == "local_completion", f"ailocal-completion -> local_completion (got {name})")

# The trap this ordering exists to avoid: compat aliases LOOK frontier.
name, _ = reg.model_class("claude-sonnet-4-6")
check(name in ("local_agentic", "local_nonagentic"),
      f"claude-sonnet-4-6 resolves via alias to a LOCAL class, not frontier "
      f"(got {name})")
check(reg.is_passthrough("claude-sonnet-4-6") is False,
      "a compat alias pointing at a local capability is NOT passthrough")
check(reg.capability_of("claude-sonnet-4-6") in
      ("implementation", "architecture", "review"),
      "capability_of resolves the compat alias")

# A genuine frontier id (not in this deployment's alias map) passes through.
name, _ = reg.model_class("claude-3-5-sonnet-2024-10-22")
check(name == "frontier_passthrough",
      f"a dated frontier model id -> frontier_passthrough (got {name})")
check(reg.is_passthrough("claude-3-5-sonnet-2024-10-22") is True,
      "frontier models are passthrough")
check(reg.is_passthrough("some-model-nobody-registered") is True,
      "an unmatched model is passthrough — fail open, never filter blindly")

print("\nCAPABILITY FLAGS")
check(reg.supports("ailocal-architecture", "tools") is True,
      "local_agentic supports tools")
check(reg.supports("ailocal-architecture", "reasoning") is False,
      "local_agentic declares NO reasoning (qwen3-coder emits no <think>)")
check(reg.supports("ailocal-completion", "tools") is False,
      "the FIM tier declares no tool support at all")
check(reg.supports("ailocal-implementation", "mcp") is False,
      "the measured non-agentic tier declares no MCP")
check(reg.supports("ailocal-architecture", "nonexistent_feature") is None,
      "an unknown feature returns None, not False — unknown != unsupported")

print("\nCONTEXT WINDOWS (generated, not restated)")
# INVARIANTS, not literals. This assertion hardcoded 65536 and broke the moment
# the profile changed the window -- under a heading that says "generated, not
# restated". Restating a generated number tests the literal, not the pipeline.
# The durable properties are ordering and positivity, which hold for any profile.
_ctx = {c: reg.max_context(f"ailocal-{c}")
        for c in ("architecture", "fast", "implementation", "review", "completion")}
check(all(isinstance(v, int) and v > 0 for v in _ctx.values()),
      f"every local capability has a positive generated window ({_ctx})")
# `fast > implementation` was NOT a durable invariant. It only held while
# implementation was sized for a smaller model it no longer runs (16,384 input
# on a 26B that architecture already drives at 81,920). Correcting that to
# 65,536 on 2026-08-03 put implementation above fast, which is intended, so the
# assertion was testing the stale policy rather than a property of the pipeline.
# What is actually invariant: architecture is the largest window, because it is
# the role defined as carrying whole-repository context.
check(_ctx["architecture"] == max(_ctx.values()),
      f"architecture has the largest generated window ({_ctx})")
check(_ctx["completion"] == min(_ctx.values()),
      f"completion is the smallest window ({_ctx['completion']})")
check(reg.max_context("claude-3-5-sonnet-2024-10-22") == 200000,
      "a cloud class falls back to its declared max_context")

print("\nROUTES")
check(reg.route_for_call_type("anthropic_messages") == "/v1/messages",
      "anthropic_messages -> /v1/messages")
check(reg.route_for_call_type("acompletion") == "/v1/chat/completions",
      "acompletion -> /v1/chat/completions")
check(reg.route_for_call_type(None, has_input_key=True) == "/v1/responses",
      "an unknown call_type with an `input` key -> /v1/responses")
check(reg.route_drops_type("/v1/responses", "namespace") is True,
      "/v1/responses drops namespace (LiteLLM's own behaviour)")
for t in ("computer_use", "image_generation", "shell"):
    check(reg.route_drops_type("/v1/responses", t) is True,
          f"/v1/responses drops {t}")
check(reg.route_drops_type("/v1/responses", "function") is False,
      "/v1/responses does NOT drop function tools")
check(reg.route_drops_type("/v1/messages", "namespace") is False,
      "the drop rule is route-specific, not global")
check(reg.result_status_mode("/v1/chat/completions") == "unknown",
      "the OpenAI route reports UNKNOWN result status — it has no error flag")
check(reg.result_status_mode("/v1/messages") == "explicit",
      "the Anthropic route has explicit is_error")

print("\nCLIENT DETECTION")
check(reg.detect_client({"user-agent": "claude-cli/2.0.0"}) == "claude-code",
      "claude-cli user-agent")
check(reg.detect_client({"User-Agent": "CLAUDE-CLI/2"}) == "claude-code",
      "header name and value matching is case-insensitive")
check(reg.detect_client({"originator": "codex_cli_rs"}) == "codex", "codex originator")
check(reg.detect_client({"user-agent": "vscode/1.9"}) == "vscode", "vscode")
check(reg.detect_client({"x-app": "cli"}) == "claude-code", "header-value match")
check(reg.detect_client({}) == "unknown", "no headers -> unknown")
check(reg.detect_client({"user-agent": "curl/8"}) == "unknown",
      "an unrecognised agent is unknown, never guessed")

print("\nNEGOTIATION RULE (both sides must agree)")
rm = reg.removable_groups("claude-code", "ailocal-architecture")
check("orchestration" in rm and "scheduling" in rm and "worktree" in rm,
      "claude-code + local_agentic: orchestration/scheduling/worktree removable")
check("edit_and_run" not in rm and "search" not in rm and "lsp" not in rm,
      "the coding surface is never removable")
check(reg.removable_groups("unknown", "ailocal-architecture") == set(),
      "an unknown CLIENT yields nothing removable")
check(reg.removable_groups("claude-code", "totally-unknown-model") == set(),
      "an unknown MODEL yields nothing removable")
# Intersection, not union: claude-code does not list `interactive`, so even
# though local_agentic denies it, it stays.
check("interactive" not in rm,
      "a group only one side objects to is NOT removable (intersection)")

print("\nPROTECTED TOOLS")
check(reg.is_protected("web_search") is True, "web_search is protected")
check(reg.is_protected("WebSearch") is True, "WebSearch is protected")
check(reg.is_protected("<web_search>") is True,
      "an unnamed entry is protected implicitly")
check(reg.is_protected("<namespace>") is True, "so is <namespace>")
check(reg.is_protected("Workflow") is False, "an ordinary tool is not protected")

print("\nGROUP LOOKUP")
check(reg.group_of("Workflow") == "orchestration", "Workflow -> orchestration")
check(reg.group_of("mcp__lsp__get_hover") == "lsp", "prefix pattern -> lsp")
check(reg.group_of("mcp__grepai__grepai_search") == "search", "grepai -> search")
check(reg.group_of("TaskCreate") == "delegation", "Task* prefix -> delegation")
# `Agent` is THE subagent spawn tool (renamed from `Task` in Claude Code 2.1.63).
# It sat in `orchestration` and was therefore dropped for every local model, which
# is what actually prevented delegation -- a payload capture showed no `Task` tool
# and that was misread as "headless mode has no subagents". Verified working once
# Agent reached the model: it called Agent and the reviewer ran on the review tier.
check(reg.group_of("Agent") == "delegation",
      "Agent (the real subagent tool) -> delegation, NOT orchestration")
# Delegation is a SEPARATE group from orchestration on purpose. Grouped together,
# denying orchestration to local models also stripped Task, so claude-local could
# not reach the subagents this repo ships — measured, and initially misread as the
# model declining to delegate. Splitting them keeps the 21,525 B Workflow tool
# dropped while leaving ~1 KB of Task in place.
check(reg.group_of("Workflow") != reg.group_of("TaskCreate"),
      "Workflow and Task are in different groups")
check(reg.group_of("CronList") == "scheduling", "Cron* prefix -> scheduling")
check(reg.group_of("some_unknown_tool") is None, "an unknown tool has no group")

definite, ambiguous = reg.mutating_tools()
check("Edit" in definite and "apply_patch" in definite,
      "mutating tools come from the registry, not the host fallback")
check("Bash" in ambiguous,
      "Bash is AMBIGUOUS — it can legitimately be read-only")
check(not (definite & ambiguous), "the two sets are disjoint")

print("\nTASK CLASSIFICATION")
# The conversational class is the only one allowed below the `always` floor. It
# exists because "show me an example of hello world in c++" arrived holding
# Read/Glob/Grep/Bash and the agent went spelunking through the repo before
# answering a general knowledge question.
name, groups, _ = reg.classify_task("show me an example of hello world in c++")
check(name == "conversational", "a general question classifies as conversational")
check(groups == set(), "conversational sheds EVERY group, including the always-floor")

# Every other class must keep the floor: losing tools mid-task strands an agent,
# which is far worse than carrying a few unnecessary schemas.
floor = reg.task_always_groups()
for text, expected in [("fix the typo in README.md", "simple_edit"),
                       ("where is the persona injector defined", "explore"),
                       ("why does the build keep failing", "debug"),
                       ("refactor the sync-models generator", "architecture")]:
    name, groups, _ = reg.classify_task(text)
    check(name == expected, f"{expected!r} classification")
    check(floor <= groups, f"{expected!r} keeps the always-floor")

name, groups, _ = reg.classify_task("refactor the sync-models generator")
check("delegation" in groups,
      "architecture keeps delegation — handing off is the point of that class")
check("orchestration" not in groups,
      "architecture still sheds heavy orchestration (Workflow is 21,525 B)")

check(reg.classify_task("")[0] is None, "empty text is unclassified, not conversational")

print("\nFAIL-OPEN BEHAVIOUR")
missing = cr.Registry(path="/nonexistent", caps_json="/nonexistent",
                      config_path="/nonexistent")
check(missing.state == "absent", "a missing registry reports 'absent'")
check(missing.is_passthrough("anything") is True,
      "with no registry, EVERY model is passthrough (nothing gets filtered)")
check(missing.removable_groups("claude-code", "ailocal-architecture") == set(),
      "with no registry, nothing is removable")
check(missing.detect_client({"user-agent": "claude-cli"}) == "unknown",
      "with no registry, no client is claimed to be identified")

import tempfile
bad = os.path.join(tempfile.mkdtemp(), "bad.yaml")
open(bad, "w").write("groups: [unclosed\n")
broken = cr.Registry(path=bad, caps_json="/nonexistent", config_path="/nonexistent")
check(broken.state == "error", "a malformed registry reports 'error', not 'absent'")
check(broken.is_passthrough("anything") is True,
      "a malformed registry also fails open")

# ── the architectural assertion ─────────────────────────────────────────────
print("\nNO HARD-CODED CONDITIONALS IN THE NEGOTIATOR")
if os.path.exists(GATEWAY_PY):
    src = open(GATEWAY_PY, encoding="utf-8").read()
    # Strip comments and docstrings: prose may name models freely (and should),
    # it is executable literals that would re-introduce the coupling.
    code = re.sub(r'"""(?:.|\n)*?"""', "", src)
    code = re.sub(r"#.*", "", code)
    forbidden = ["qwen", "deepseek", "claude-cli", "codex_cli", "Workflow",
                 "mcp__lsp", "mcp__grepai", "namespace", "ailocal-architecture"]
    # Match each token only as a COMPLETE quoted string, which is the shape a
    # conditional comparison takes (`type == "namespace"`, `name == "Workflow"`).
    #
    # A bare substring search was too coarse: it flagged the function name
    # expand_namespaces, the config key namespace_expansion, a
    # `template.format(namespace=...)` kwarg and the default template
    # "{namespace}__{tool}" — all legitimate identifiers. Renaming those to
    # satisfy the grep would have made the code worse to read while catching
    # nothing extra. This form still catches the real thing: an inlined
    # `tool.get("type") == "namespace"` fails, and did.
    hits = [tok for tok in forbidden
            if re.search(r"""(?<![A-Za-z0-9_{])['"]%s['"]""" % re.escape(tok), code)]
    check(not hits,
          f"tool_gateway.py executable code contains no model/client/tool "
          f"literal as a quoted value (found: {hits})")

    # Prove the guard still bites, so a future loosening cannot pass silently.
    probe_bad = 'if tool.get("type") == "namespace": pass'
    check(bool(re.search(r"""(?<![A-Za-z0-9_{])['"]namespace['"]""", probe_bad)),
          "the guard still detects an inlined tool-type comparison")
    probe_ok = 'template = "{namespace}__{tool}"'
    check(not re.search(r"""(?<![A-Za-z0-9_{])['"]namespace['"]""", probe_ok),
          "...and does not flag a template placeholder")
    check("registry" in code,
          "tool_gateway.py consults the registry")
else:
    check(False, f"{GATEWAY_PY} missing")

sys.exit(_suite.report())
