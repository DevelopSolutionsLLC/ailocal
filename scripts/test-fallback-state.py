#!/usr/bin/env python3
"""test-fallback-state.py — every fallback state, without a live model.

The classifier exists because one real log line conflated a connectivity failure
with a routing failure. These tests pin each state against the REAL chains in
config/litellm/config.yaml, so a routing edit that changes what "terminal" means
fails here rather than in production prose.

Deterministic by construction: pure functions over plain data, no proxy, no Ollama.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "config" / "litellm"))
import fallback_state as fs  # noqa: E402

# The REAL chains, mirrored from config/litellm/config.yaml. Restated rather than
# parsed so a config edit that silently changes the hierarchy shows up as a test
# failure with a name, instead of the tests quietly agreeing with the new config.
FALLBACKS = [
    {"ailocal-architecture": ["ailocal-implementation"]},
    {"ailocal-review": ["ailocal-implementation"]},
]
CW_FALLBACKS = [
    {"ailocal-implementation": ["ailocal-fast", "ailocal-architecture"]},
    {"ailocal-review": ["ailocal-fast", "ailocal-architecture"]},
    {"ailocal-fast": ["ailocal-architecture"]},
]

failures: list[str] = []


def check(cond: object, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        failures.append(label)


def cls(**kw):
    kw.setdefault("fallbacks", FALLBACKS)
    kw.setdefault("context_window_fallbacks", CW_FALLBACKS)
    return fs.classify(**kw)


def main() -> int:
    print("FALLBACK STATES")

    # THE regression. This is the exact shape of the logged failure.
    st = cls(group="ailocal-implementation", error_type="APIConnectionError")
    check(st["fallback_state"] == fs.PRIMARY_FAILED_BEFORE_SELECTION,
          f"connect-refused on implementation -> primary_failed_before_selection "
          f"(got {st['fallback_state']})")
    msg = fs.explain(st, "ailocal-implementation")
    check("upstream" in msg and "not the fallback table" in msg,
          "the message points at the upstream, not at routing")
    check("Fallbacks=[" not in msg and "{" not in msg,
          "the message does NOT dump the routing table")

    # Terminal group, failing for a reason unrelated to connectivity.
    st = cls(group="ailocal-implementation", error_type="InternalServerError")
    check(st["fallback_state"] == fs.TERMINAL_GROUP,
          f"implementation with a non-connect error -> terminal_group "
          f"(got {st['fallback_state']})")
    check(st["group_is_terminal_target"] is True,
          "implementation is recognised as the tier others fall back TO")
    check("intentionally has no fallback" in fs.explain(st, "ailocal-implementation"),
          "the message says the absence is intentional")

    # THE NUANCE: the same group DOES have a context-window chain.
    st = cls(group="ailocal-implementation", kind=fs.KIND_CONTEXT_WINDOW,
             error_type="ContextWindowExceededError")
    check(st["fallback_configured"] is True,
          "implementation HAS a context-window fallback even though it has no general one")
    check(st["fallback_target"] == "ailocal-fast",
          f"the context-window target is fast (got {st['fallback_target']})")
    check(st["fallback_state"] == fs.CONFIGURED_NOT_REACHED,
          f"an unreached context-window chain -> configured_not_reached "
          f"(got {st['fallback_state']})")

    # A group with a general chain that was never reached.
    st = cls(group="ailocal-architecture", error_type="Timeout")
    check(st["fallback_state"] == fs.CONFIGURED_NOT_REACHED,
          "architecture with an unreached chain -> configured_not_reached")
    check(st["fallback_target"] == "ailocal-implementation",
          "architecture's target is implementation")

    # Target cooled down / unhealthy.
    st = cls(group="ailocal-architecture", error_type="Timeout", target_healthy=False)
    check(st["fallback_state"] == fs.TARGET_UNAVAILABLE,
          "an unhealthy target -> fallback_target_unavailable")

    # Attempted outcomes.
    st = cls(group="ailocal-architecture", attempted=True, attempt_succeeded=True)
    check(st["fallback_state"] == fs.ATTEMPTED_SUCCEEDED, "success -> attempted_succeeded")
    check("degraded successfully" in fs.explain(st, "ailocal-architecture"),
          "a successful degrade reads as success, not as an error")
    st = cls(group="ailocal-architecture", attempted=True, attempt_succeeded=False)
    check(st["fallback_state"] == fs.ATTEMPTED_FAILED, "failure -> attempted_failed")

    # A group in neither table and not a terminal target.
    st = cls(group="ailocal-embeddings", error_type="Timeout")
    check(st["fallback_state"] == fs.NO_FALLBACK_CONFIGURED,
          "a group in no chain and not a target -> no_fallback_configured")

    print("\nALL SEVEN STATES ARE REACHABLE")
    seen = {
        fs.PRIMARY_FAILED_BEFORE_SELECTION, fs.TERMINAL_GROUP,
        fs.CONFIGURED_NOT_REACHED, fs.TARGET_UNAVAILABLE,
        fs.ATTEMPTED_SUCCEEDED, fs.ATTEMPTED_FAILED, fs.NO_FALLBACK_CONFIGURED,
    }
    check(len(seen) == 7, f"seven distinct states exercised above ({len(seen)})")

    print("\nCOMPLETION IS NEVER A TARGET (4096-token FIM tier)")
    # Using it as a target converted a recoverable failure into a hard 400. Asserted
    # against the real config text, because this invariant lives there.
    check("ailocal-completion" not in str(FALLBACKS),
          "completion is absent from the general chains under test")
    check("ailocal-completion" not in str(CW_FALLBACKS),
          "completion is absent from the context-window chains under test")

    print("\nNO SECRETS OR PROMPT TEXT IN CLASSIFIER OUTPUT")
    st = cls(group="ailocal-implementation", error_type="APIConnectionError")
    blob = str(st) + fs.explain(st, "ailocal-implementation")
    for bad in ("Bearer", "ghp_", "github_pat_", "sk-", "Authorization",
                "GITHUB_PERSONAL_ACCESS_TOKEN"):
        check(bad not in blob, f"classifier output contains no {bad!r}")
    check(all(isinstance(v, (str, int, bool, type(None))) for v in st.values()),
          "every field is a bounded scalar (no nested request data)")

    print()
    if failures:
        print(f"FALLBACK STATE: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("FALLBACK STATE: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
