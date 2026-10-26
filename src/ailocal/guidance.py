"""Render profile-owned model facts through native client context surfaces."""
from __future__ import annotations

import argparse
import json
import re
import sys

from . import policy

#: Model ids Claude Code sends for a model it did not resolve to a Claude
#: family. [REAL] 2026-10-26, Claude Code 2.1.310 behind the local gateway:
#: selecting `ailocal-cyber` from the /model picker's gateway-discovered rows
#: ran PostModelSwitch with the hook name `claude-router-61696c6f63616c2d6379626572`,
#: persisted that same id as the saved `model`, and reported
#: `[claude-code:unrecognized_model] {"model":"claude-router-6169…"}` on the
#: next launch; the hex decodes to `ailocal-cyber`. The encoding is not in
#: the hooks or model-config documentation, so it is a fallback after the
#: documented fields, never the first lookup.
_ROUTER_ID = re.compile(r"^claude-router-([0-9a-f]+)$")


def hook_model_candidates(event: dict) -> list[str]:
    """Model ids a PostModelSwitch/SessionStart event may name, documented first.

    `to_model` is the id the session switched to and `requested_model` the
    alias or id the request named (code.claude.com/docs/en/hooks, PreModelSwitch
    input, shared by PostModelSwitch). A gateway id Claude Code does not
    recognise reaches `to_model` as the undocumented router encoding above, so
    each candidate that matches it also contributes its decoded id, after the
    verbatim value. Order is preserved and duplicates dropped; the caller takes
    the first candidate the profile knows.
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


def render(model: str, guide: dict) -> str:
    return "\n".join([
        f"Local model guide: {model} ({guide.get('model', model)})",
        guide["summary"],
        *([f"Profile limits: {guide['context_input']} input tokens, "
           f"{guide['max_output']} maximum output tokens; native thinking={guide['reasoning']}."]
          if "context_input" in guide else []),
        *[f"Prompting advice: {s}" for s in guide["prompting"]],
        *[f"Known limitation: {s}" for s in guide["limitations"]],
        guide["evidence"],
        "Capability names describe routing, not permission to ignore the requested task. "
        "This guidance is not proof of correctness; report actual checks and failures.",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ailocal profile guide")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--tier", choices=policy.TIERS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hook", action="store_true")
    args = parser.parse_args(argv)
    candidates = [args.model] if args.model else []
    unspecified = False
    if args.hook:
        try:
            event = json.load(sys.stdin)
            candidates = hook_model_candidates(event)
        except (ValueError, AttributeError):
            print("MODEL_GUIDANCE_INVALID_EVENT", file=sys.stderr)
            return 2
        if not candidates:
            unspecified = True
    try:
        result = policy.model_guides(args.tier)
        if candidates:
            models = result["models"]
            # Native Claude slots are resolved through the same authored policy.
            slots = policy.load_client_policy().get("claude", {}).get("slots", {})
            matches = {}
            for model in candidates:
                alias = "ailocal-" + slots[model] if model in slots else model
                matches = {name: g for name, g in models.items()
                           if name == alias or g["model"] == model}
                if matches:
                    break
            if not matches:
                print("MODEL_GUIDANCE_UNKNOWN_MODEL", file=sys.stderr)
                return 2
            result["models"] = matches
        if unspecified:
            print("Active model was not supplied by the client. These are the active profile's "
                  "guides; apply only the entry matching the selected alias. "
                  "The agent MCP's model_guide tool also provides the current limits and sources.")
            # No guessed default after resume, and no duplicate advice for weights
            # shared by several roles. Each alias still names its own limits.
            grouped = {}
            for name, guide in result["models"].items():
                grouped.setdefault(guide["model"], []).append((name, guide))
            for entries in grouped.values():
                label = "; ".join(f"{n} (input {g['context_input']}, output {g['max_output']}, "
                                  f"thinking {g['reasoning']})" for n, g in entries)
                guide = {k: v for k, v in entries[0][1].items()
                         if k not in ("context_input", "max_output", "reasoning")}
                print("\n" + render(label, guide))
        else:
            print(json.dumps(result, indent=2) if args.json else "\n\n".join(
                render(name, guide) for name, guide in result["models"].items()))
        return 0
    except policy.ProfileError as error:
        print(error.code, file=sys.stderr)
        return 2
