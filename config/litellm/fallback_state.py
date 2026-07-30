#!/usr/bin/env python3
"""fallback_state.py — classify what actually happened to a request's fallback.

WHY THIS EXISTS

A real failure logged this:

    litellm.APIConnectionError: Ollama_chatException - Cannot connect to host
    host.docker.internal:11434 ... No fallback model group found for
    original model_group=ailocal-implementation.
    Fallbacks=[{'ailocal-architecture': ['ailocal-implementation']},
               {'ailocal-review': ['ailocal-implementation']}]

Every word is true and the sentence is misleading. "No fallback model group found"
reads as a lookup that malfunctioned, and printing the whole `Fallbacks=[...]` table
next to it invites the reader to hunt for the bug in that table. There is no bug:
`ailocal-implementation` is the TERMINAL tier that the other groups fall back TO, so
it intentionally has no chain of its own. The real fault was upstream connectivity,
and the fallback prose buried it.

A SECOND DISTINCTION THE OLD MESSAGE ERASED. `implementation` has no entry in
`fallbacks`, but it DOES have one in `context_window_fallbacks`
(implementation 32768 -> fast 65536 -> architecture 131072). So "does this group
have a fallback?" has two different answers depending on the failure kind, and a
single boolean cannot express that. Classification therefore takes the error kind
into account rather than only the group name.

STATES, and what each one tells an operator to go look at:

  no_fallback_configured               nothing declared for this group + kind
  terminal_group_intentionally_no_fallback
                                       this group is the bottom of the chain by
                                       design; look upstream, not at routing
  fallback_configured_not_reached      a chain exists; the request never got there
  fallback_target_unavailable          the chain exists and its target was cooled
                                       down / unhealthy
  primary_failed_before_selection      the primary failed so early that no target
                                       was ever selected (connect refused, config
                                       error)
  fallback_attempted_succeeded         a target served the request
  fallback_attempted_failed            a target was tried and also failed

Pure functions over plain data. No LiteLLM import, no network, no model — so every
state is unit-testable without a backend.
"""
from __future__ import annotations

NO_FALLBACK_CONFIGURED = "no_fallback_configured"
TERMINAL_GROUP = "terminal_group_intentionally_no_fallback"
CONFIGURED_NOT_REACHED = "fallback_configured_not_reached"
TARGET_UNAVAILABLE = "fallback_target_unavailable"
PRIMARY_FAILED_BEFORE_SELECTION = "primary_failed_before_selection"
ATTEMPTED_SUCCEEDED = "fallback_attempted_succeeded"
ATTEMPTED_FAILED = "fallback_attempted_failed"

# Failure kinds, mapped to WHICH chain is relevant. A context overflow is served by
# `context_window_fallbacks`; everything else by `fallbacks`. Consulting the wrong
# table is how a group looks fallback-less when it is not.
KIND_GENERAL = "general"
KIND_CONTEXT_WINDOW = "context_window"

# Error classes that fail before any target can be selected. These are NOT routing
# problems and must never be reported as fallback problems.
PRE_SELECTION_ERRORS = frozenset({
    "APIConnectionError",       # the upstream was unreachable
    "AuthenticationError",
    "ConfigError",
})


def chain_for(group: str, *, kind: str, fallbacks: list[dict] | None,
              context_window_fallbacks: list[dict] | None) -> list[str]:
    """The declared fallback targets for `group` under `kind`, in order.

    LiteLLM declares these as a LIST OF SINGLE-KEY DICTS rather than one mapping,
    so a linear scan is correct; the first matching entry wins, mirroring the
    router's own behaviour.
    """
    table = context_window_fallbacks if kind == KIND_CONTEXT_WINDOW else fallbacks
    for entry in table or []:
        if not isinstance(entry, dict):
            continue
        targets = entry.get(group)
        if targets:
            return list(targets)
    return []


def is_terminal_target(group: str, *, fallbacks: list[dict] | None) -> bool:
    """True when other groups fall back TO this one.

    That is what makes "no chain" INTENTIONAL rather than an omission: the bottom of
    a degradation hierarchy has nowhere to degrade to, by construction.
    """
    for entry in fallbacks or []:
        if isinstance(entry, dict):
            for targets in entry.values():
                if group in (targets or []):
                    return True
    return False


def classify(*, group: str, kind: str = KIND_GENERAL,
             error_type: str | None = None,
             fallbacks: list[dict] | None = None,
             context_window_fallbacks: list[dict] | None = None,
             attempted: bool = False, attempt_succeeded: bool | None = None,
             target_healthy: bool = True) -> dict:
    """Classify one request's fallback outcome.

    Returns the state plus the evidence behind it, so a log line can be precise
    without the reader having to re-derive anything from a routing table.
    """
    chain = chain_for(group, kind=kind, fallbacks=fallbacks,
                      context_window_fallbacks=context_window_fallbacks)

    if attempted:
        state = ATTEMPTED_SUCCEEDED if attempt_succeeded else ATTEMPTED_FAILED
    elif chain and not target_healthy:
        state = TARGET_UNAVAILABLE
    elif error_type in PRE_SELECTION_ERRORS:
        # Checked BEFORE the chain-exists branches: when the primary never
        # connected, whether a chain exists is irrelevant to the diagnosis, and
        # leading with routing sends the reader to the wrong layer.
        state = PRIMARY_FAILED_BEFORE_SELECTION
    elif chain:
        state = CONFIGURED_NOT_REACHED
    elif is_terminal_target(group, fallbacks=fallbacks):
        state = TERMINAL_GROUP
    else:
        state = NO_FALLBACK_CONFIGURED

    return {
        "fallback_state": state,
        "fallback_kind": kind,
        "fallback_configured": bool(chain),
        "fallback_target": chain[0] if chain else None,
        "fallback_chain_len": len(chain),
        "fallback_attempted": bool(attempted),
        "group_is_terminal_target": is_terminal_target(group, fallbacks=fallbacks),
    }


def explain(state: dict, group: str) -> str:
    """A one-line operator-facing message. Names the layer to investigate.

    Deliberately does NOT print the whole routing table. Dumping `Fallbacks=[...]`
    beside a connectivity error is what made the original message send people to
    read routing config while the upstream was down.
    """
    s = state["fallback_state"]
    if s == TERMINAL_GROUP:
        return (f"{group} is the terminal tier of the {state['fallback_kind']} "
                f"fallback hierarchy and intentionally has no fallback of its own; "
                f"the failure is upstream of routing")
    if s == PRIMARY_FAILED_BEFORE_SELECTION:
        return (f"{group} failed before any fallback target could be selected; "
                f"investigate the upstream/backend, not the fallback table")
    if s == NO_FALLBACK_CONFIGURED:
        return f"no {state['fallback_kind']} fallback is configured for {group}"
    if s == CONFIGURED_NOT_REACHED:
        return (f"{group} has a {state['fallback_kind']} fallback "
                f"({state['fallback_target']}) that was never reached")
    if s == TARGET_UNAVAILABLE:
        return (f"{group}'s fallback target {state['fallback_target']} was "
                f"unavailable (cooled down or unhealthy)")
    if s == ATTEMPTED_SUCCEEDED:
        return f"{group} degraded successfully to {state['fallback_target']}"
    return f"{group}'s fallback {state['fallback_target']} was attempted and failed"
