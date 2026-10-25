"""Render profile-owned model facts through native client context surfaces."""
from __future__ import annotations

import argparse
import json
import sys

from . import policy


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
    model = args.model
    unspecified = False
    if args.hook:
        try:
            event = json.load(sys.stdin)
            model = event.get("to_model") or event.get("model")
        except (ValueError, AttributeError):
            print("MODEL_GUIDANCE_INVALID_EVENT", file=sys.stderr)
            return 2
        if not isinstance(model, str) or not model:
            model, unspecified = None, True
    try:
        result = policy.model_guides(args.tier)
        if model:
            models = result["models"]
            # Native Claude slots are resolved through the same authored policy.
            slots = policy.load_client_policy().get("claude", {}).get("slots", {})
            alias = "ailocal-" + slots[model] if model in slots else model
            matches = {name: g for name, g in models.items()
                       if name == alias or g["model"] == model}
            if not matches:
                print("MODEL_GUIDANCE_UNKNOWN_MODEL", file=sys.stderr)
                return 2
            result["models"] = matches
        if unspecified:
            print("Active model was not supplied by the client. These are the active profile's "
                  "guides; apply only the entry matching the selected alias. "
                  "local-agent.model_guide also provides the current limits and sources.")
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
