#!/usr/bin/env python3
"""test-context-limits.py — configured context vs. what the backend actually serves.

WHY THIS EXISTS

E3 asked whether the `num_ctx` values in config/litellm/config.yaml are backed by
real backend capacity. For five of six capabilities they are. For embeddings they are
not, and the failure mode is the worst kind:

    requested 512  -> prompt_eval_count 514
    requested 2000 -> prompt_eval_count 2002
    requested 2048 -> prompt_eval_count 2048
    requested 3000 -> prompt_eval_count 2048     <- SILENTLY CLIPPED
    requested 4096 -> prompt_eval_count 2048     <- SILENTLY CLIPPED
    requested 8192 -> prompt_eval_count 2048     <- SILENTLY CLIPPED

nomic-embed-text does not reject an over-long input and does not warn. It returns a
valid 768-dimension vector computed from the first 2048 tokens only. So a caller that
declares 8192 gets a successful-looking embedding of the first quarter of its text,
and every downstream similarity score is computed against a truncated document. An
error would be far safer than this.

This matters beyond ailocal: Cadence's semantic index embeds with the same model, so
any chunk longer than 2048 tokens is indexed from its opening fragment while
reporting success.

THE TEST IS THE GUARD. config/litellm/config.yaml currently declares num_ctx 8192 for
`ailocal-embeddings` and is USER-OWNED with uncommitted changes, so this file
deliberately does not edit it. Instead it FAILS when the declared value exceeds the
measured backend limit, which turns a silent data-quality bug into a red gate. The
one-line correction is reported by the test itself.

Skips cleanly when Ollama is unreachable — a machine without a local runtime is not a
broken machine.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config" / "litellm" / "config.yaml"
OLLAMA = "http://127.0.0.1:11434"

failures: list[str] = []
notes: list[str] = []


def check(cond: object, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        failures.append(label)


def curl(path: str, payload: dict | None = None, timeout: int = 60) -> dict | None:
    cmd = ["curl", "-s", "-m", str(timeout), f"{OLLAMA}{path}"]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except ValueError:
        return None


def declared_contexts() -> dict[str, tuple[str, int]]:
    """capability -> (backend model, declared num_ctx), READ ONLY.

    Parsed from the generated model_list block. The file is never written here.
    """
    cfg = CONFIG.read_text()
    found: dict[str, tuple[str, int]] = {}
    for name, backend, ctx in re.findall(
            r'- model_name:\s*(\S+).*?model:\s*(\S+).*?num_ctx:\s*(\d+)', cfg, re.S):
        if name.startswith("ailocal-") and name not in found:
            found[name] = (backend.split("/", 1)[-1], int(ctx))
    return found


def model_max(model: str) -> int | None:
    d = curl("/api/show", {"model": model})
    if not d:
        return None
    info = d.get("model_info") or {}
    vals = [v for k, v in info.items() if k.endswith(".context_length")]
    return vals[0] if vals else None


def embed_eval_count(model: str, approx_tokens: int) -> int | None:
    """Tokens the server ACTUALLY processed, via prompt_eval_count.

    `prompt_eval_count` is the only honest signal here: the response body looks
    identical whether the input was processed whole or clipped.
    """
    d = curl("/api/embed", {"model": model, "input": "word " * approx_tokens}, timeout=90)
    if not d or "error" in (d or {}):
        return None
    return d.get("prompt_eval_count")


def main() -> int:
    if curl("/api/version") is None:
        print("SKIP: Ollama is not reachable at 127.0.0.1:11434")
        return 0

    print("DECLARED num_ctx vs BACKEND MAXIMUM")
    declared = declared_contexts()
    if not declared:
        print("SKIP: could not parse the generated model_list")
        return 0

    over = []
    for cap, (backend, ctx) in sorted(declared.items()):
        mx = model_max(backend)
        verdict = "unknown" if mx is None else ("ok" if ctx <= mx else "OVER-DECLARED")
        print(f"    {cap:<24}{backend:<36}num_ctx={ctx:<8}backend_max={str(mx):<8}{verdict}")
        if mx is not None and ctx > mx:
            over.append((cap, backend, ctx, mx))

    # The assertion, stated positively so the failure message is actionable.
    check(not over,
          "no capability declares more context than its backend serves"
          + (f" (over-declared: {[(c, d, m) for c, _, d, m in over]})" if over else ""))
    for cap, backend, ctx, mx in over:
        notes.append(f"PROPOSED (protected file, not applied): {cap} num_ctx {ctx} -> {mx}"
                     f"  [{backend} maximum]")

    print("\nOVER-LIMIT BEHAVIOUR: reject, error, or SILENT CLIP?")
    emb = declared.get("ailocal-embeddings")
    if emb is None:
        print("    (no embeddings capability declared; skipping)")
    else:
        backend, ctx = emb
        mx = model_max(backend) or 0
        measured = {}
        for n in (512, mx, mx + 1000, ctx):
            if n <= 0:
                continue
            measured[n] = embed_eval_count(backend, n)
            print(f"    requested~{n:<7} prompt_eval_count={measured[n]}")

        under = measured.get(512)
        at_limit = measured.get(mx)
        over_limit = measured.get(mx + 1000)
        at_declared = measured.get(ctx)

        check(under is not None and under <= mx + 8,
              f"a small input is processed whole ({under})")
        # THE finding. Not an error, not a rejection — a success with a truncated
        # document behind it, which no caller can detect from the response body.
        clipped = (over_limit is not None and at_limit is not None
                   and over_limit <= at_limit + 8)
        check(clipped is True,
              f"an over-limit input is CLIPPED at the backend maximum, not rejected "
              f"(requested {mx + 1000} -> processed {over_limit})")
        if clipped:
            notes.append(
                f"MEASURED: {backend} silently clips at {mx} tokens. Requests for "
                f"{mx + 1000} and {ctx} both processed only {over_limit} tokens and "
                f"still returned a valid embedding. Declaring num_ctx {ctx} therefore "
                f"advertises {ctx / mx:.0f}x the real capacity and yields embeddings "
                f"of truncated text with no error.")
        check(at_declared is not None and at_declared <= mx + 8,
              f"the fully-declared size is clipped too ({at_declared} <= {mx})")

    print()
    for n in notes:
        print(f"  note: {n}")
    print()
    if failures:
        print(f"CONTEXT LIMITS: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("CONTEXT LIMITS: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
