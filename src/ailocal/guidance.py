"""Render profile-owned model facts through native client context surfaces."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from pathlib import Path

from . import policy

#: Claude Code's id for an unresolved gateway model: claude-router-<hex name>
#: (seen 2.1.310, 2026-10-26). Undocumented → fallback, never first lookup.
_ROUTER_ID = re.compile(r"^claude-router-([0-9a-f]+)$")


def hook_model_candidates(event: dict) -> list[str]:
    """Return model ids a PostModelSwitch/SessionStart event names, docs first.

    `to_model`, then `requested_model` (code.claude.com/docs/en/hooks); a
    router-encoded id is followed by its decoded id. Order kept, dupes dropped.
    """
    seen, out = set(), []
    for key in ("to_model", "requested_model", "model"):
        value = event.get(key)
        if not isinstance(value, str) or not value:
            continue
        routed = _ROUTER_ID.match(value)
        expanded = [value, _decode_router(routed.group(1))] if routed else [value]
        for candidate in expanded:
            if candidate and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _decode_router(hex_id: str) -> str | None:
    try:
        return bytes.fromhex(hex_id).decode("utf-8") or None
    except ValueError:
        return None


#: What the always-on hook says instead of the reference data it leaves out.
REFERENCE_POINTER = ("Quantization, inference preset, MTP draft depth and evaluation evidence: "
                     "the agent MCP's model_guide tool, or `ailocal profile guide <alias>`.")


def render(model: str, guide: dict, brief: bool = False) -> str:
    """Return the guide for one model.

    `brief` is the always-on hook form (#60): limits, prompting advice and
    limitations only; the rest cost over half the instruction budget.
    """
    return "\n".join([
        f"Local model guide: {model} ({guide.get('model', model)})",
        *([] if brief else [guide["summary"]]),
        *([f"Profile limits: {guide['context_input']} input tokens, "
           f"{guide['max_output']} maximum output tokens; native thinking={guide['reasoning']}."]
          if "context_input" in guide else []),
        *(["Configured inference preset: " + ", ".join(
            f"{key}={value}" for key, value in guide["sampling"].items()) +
            ". These are routing defaults, not instructions to alter the task."]
          if guide.get("sampling") and not brief else []),
        *(["Current MTP draft tokens: " + str(guide['sampling']['draft_num_predict']) +
           ". Use this configured value when asked about draft depth; historical trial values below do not override it."]
          if guide.get('sampling', {}).get('draft_num_predict') is not None and not brief else []),
        *[f"Prompting advice: {s}" for s in guide["prompting"]],
        *[f"Known limitation: {s}" for s in guide["limitations"]],
        REFERENCE_POINTER if brief else
        "Historical evaluation evidence (not current routing settings): " + guide["evidence"],
        "Capability names describe routing, not permission to ignore the requested task. "
        "This guidance is not proof of correctness; report actual checks and failures.",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ailocal profile guide")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--tier", choices=policy.TIERS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hook", action="store_true")
    args = parser.parse_args(argv)
    candidates = [args.model] if args.model else []
    unspecified = False
    hook_cwd = None
    if args.hook:
        try:
            event = json.load(sys.stdin)
            candidates = hook_model_candidates(event)
            cwd = event.get('cwd')
            if isinstance(cwd, str) and Path(cwd).is_absolute():
                hook_cwd = cwd
        except (ValueError, AttributeError):
            print("MODEL_GUIDANCE_INVALID_EVENT", file=sys.stderr)
            return 2
        if not candidates:
            unspecified = True
    try:
        result = policy.model_guides(args.tier)
        if candidates:
            models = result["models"]
            # Native slots resolve via the same policy and omitted-role rule.
            slots = policy.load_client_policy().get("claude", {}).get("slots", {})
            roles = [name.removeprefix("ailocal-") for name in models]
            matches = {}
            for model in candidates:
                alias = ("ailocal-" + policy.resolve_capability(slots[model], roles)
                         if model in slots else model)
                matches = {name: g for name, g in models.items()
                           if name == alias or g["model"] == model}
                if matches:
                    break
            if not matches:
                print("MODEL_GUIDANCE_UNKNOWN_MODEL", file=sys.stderr)
                return 2
            result["models"] = matches
        if hook_cwd and not args.json:
            # Hook input has the current dir on resume; a launch prompt may not.
            print('Task working directory: ' + json.dumps(hook_cwd) + '. '
                  'Resolve relative task files here unless the task chooses another directory. '
                  'The client already loaded its instruction files; its configuration directory '
                  'is not the task directory.')
        if unspecified:
            # One limits line per alias, not full guides (#60: ~7 KB) nor a bare
            # lookup instruction (Claude 2.1.308 then omitted the guide).
            # PostModelSwitch adds the brief guide once a model is named.
            print("Active model was not supplied by the client; apply only the line matching the "
                  "selected alias. Its prompting advice and limitations: the agent MCP's model_guide "
                  "tool, or `ailocal profile guide <alias>`.")
            for name, g in result["models"].items():
                print(f"{name}: {g['model']}; input {g['context_input']}, output {g['max_output']}, "
                      f"thinking {g['reasoning']}.")
        else:
            print(json.dumps(result, indent=2) if args.json else "\n\n".join(
                render(name, guide, brief=args.hook) for name, guide in result["models"].items()))
        return 0
    except policy.ProfileError as error:
        print(error.code, file=sys.stderr)
        return 2


def hook_bytes(event: dict, tier: str | None = None) -> int | None:
    """Return bytes `--hook` prints for `event`, or None when it fails.

    Claude Code adds it to context beside CLAUDE.md, so the budget counts it
    (#39, ADR 017).
    """
    stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(event))
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = main(["--hook", *(["--tier", tier] if tier else [])])
    finally:
        sys.stdin = stdin
    return len(out.getvalue().encode("utf-8")) if rc == 0 else None


def launch_default_hook_bytes(tier: str | None = None) -> tuple[int | None, int | None]:
    """Return guide-hook bytes: (launch default, model unspecified).

    Shared by the integration contract and `ailocal check` → same event.
    """
    default = (policy.load_client_policy().get("claude") or {}).get("launch_default")
    if default:
        default = policy.resolve_capability(default, policy.enabled_roles(tier or policy.active_tier()))
    event = {"hook_event_name": "SessionStart", "cwd": "/"}
    named = hook_bytes({**event, "model": f"ailocal-{default}"}, tier) if default else None
    return named, hook_bytes(event, tier)
