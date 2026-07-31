"""
anthropic_stream_logging_fix.py — backport of a missing type guard in LiteLLM's
Anthropic success-logging path.

THE BUG (upstream, LiteLLM 1.93.0; STILL PRESENT in 1.94.1)
----------------------------------
Two LiteLLM features collide. Reproduced deterministically with plain curl, no
client involved:

    POST /v1/messages   stream: true   tools: [{"type": "web_search_20250305",
                                                "name": "web_search"}]
      -> 3 x "LiteLLM.Success_Call Error: 1 validation error for AnthropicResponse"
    same request with stream: false
      -> 0 errors

The chain, read from the installed source rather than inferred:

1. `WebSearchInterceptionLogger.async_filter_deployment_hook` converts a
   web-search tool and, when the caller asked for streaming, flips
   `stream=True -> False` and sets `_websearch_interception_converted_stream`
   (integrations/websearch_interception/handler.py).
2. After the call, `_maybe_wrap_in_fake_stream` wraps the resulting dict in a
   `FakeAnthropicMessagesStreamIterator` so the client still receives SSE
   (llms/custom_httpx/llm_http_handler.py).
3. Success logging then calls
   `LiteLLMLoggingObj._handle_anthropic_messages_response_logging(result=...)`
   with that iterator (litellm_core_utils/litellm_logging.py:1823).
4. That method early-returns for `ModelResponse` and for the
   `ResponseCompletedEvent` / `ResponseIncompleteEvent` / `ResponseFailedEvent`
   family, then falls through to `AnthropicResponse.model_validate(result)`.
   The iterator is none of those, so pydantic raises.

Its own docstring states the contract it is relying on — "For streaming
responses, anthropic_messages handler calls success_handler with a assembled
ModelResponse" — which the websearch path does not honour. Upstream has already
patched this same method once for unhandled types (the ResponseCompletedEvent
family, BerriAI/litellm#27091) and missed this one.

RE-CHECKED on the 1.94.1 image (2026-07-30, security upgrade): the method still
early-returns only for the ResponseCompletedEvent family and ResponsesAPIResponse,
then reaches `AnthropicResponse.model_validate(result)` with no iterator guard.
This shim is still required. Read the installed source again after any upgrade --
do not assume a version bump fixed it.

NOT CAUSED BY OUR HOOKS. The traceback contains no frame from request_trace,
tool_gateway, tool_repair or persona_injector, and the repro needs none of them.

IMPACT: non-blocking. The client still receives the full stream — the failure is
in LiteLLM's own success/spend logging, which is skipped for those requests. Our
`request_trace` and `tool_gateway_metric` lines are emitted by separate callbacks
and are unaffected (verified: both still appear for the failing requests). The
practical cost is an ERROR-level traceback per streamed web-search request, which
buries real errors during a log audit.

THE FIX
-------
Add the missing guard, in the same shape as the guards upstream already has:
if the result is a fake stream iterator, return it unchanged. This is a
BACKPORT, not a local invention and not a suppression — nothing is caught,
nothing is logged away, and the type mismatch itself stops happening. Streaming,
tracing, metrics and every other callback are untouched.

SELF-RETIRING. The patch checks whether the installed method already handles the
type and does nothing if so, so a LiteLLM upgrade that fixes this upstream
silently disables it. Delete this file and its `callbacks:` entry once
`scripts/test-anthropic-stream-logging.py` passes with the patch disabled.
"""

import logging

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("anthropic_stream_logging_fix")

PATCH_MARKER = "_ailocal_fake_stream_guard"


def _fake_stream_types():
    """The iterator classes the websearch path can hand to success logging.

    Imported defensively: these live under `experimental_pass_through`, so a
    LiteLLM refactor may move or remove them. A missing class means there is
    nothing to guard against, not an error worth breaking the proxy over.
    """
    found = []
    try:
        from litellm.llms.anthropic.experimental_pass_through.messages.fake_stream_iterator import (
            FakeAnthropicMessagesStreamIterator,
        )
        found.append(FakeAnthropicMessagesStreamIterator)
    except Exception as exc:  # noqa: BLE001
        log.debug("fake stream iterator not importable (%s) — nothing to patch", exc)
    return tuple(found)


def apply_patch():
    """Install the guard. Idempotent; returns a short status string for tests."""
    try:
        from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLogging
    except Exception as exc:  # noqa: BLE001
        return f"skipped: cannot import LiteLLM logging ({exc})"

    target = getattr(LiteLLMLogging, "_handle_anthropic_messages_response_logging", None)
    if target is None:
        return "skipped: method absent (upstream refactor?)"
    if getattr(target, PATCH_MARKER, False):
        return "already patched"

    types = _fake_stream_types()
    if not types:
        return "skipped: no fake-stream types to guard"

    def patched(self, result, *args, **kwargs):
        # The one added line of behaviour: hand back a fake stream iterator
        # untouched, exactly as upstream already does for ModelResponse. Placed
        # BEFORE the original so the pydantic validation is never reached.
        if isinstance(result, types):
            return result
        return target(self, result, *args, **kwargs)

    setattr(patched, PATCH_MARKER, True)
    patched.__doc__ = (target.__doc__ or "") + "\n\n[ailocal] guarded against fake stream iterators."
    LiteLLMLogging._handle_anthropic_messages_response_logging = patched
    return "patched"


STATUS = apply_patch()
log.info("anthropic_stream_logging_fix: %s", STATUS)
# print(), not log.info(): LiteLLM filters third-party loggers, so this is what
# actually survives to `docker logs` and tells an operator the guard is live.
print(f"anthropic_stream_logging_fix: {STATUS}", flush=True)


class AnthropicStreamLoggingFix(CustomLogger):
    """Registration vehicle only.

    The fix is applied at import. This class exists so the patch can be listed in
    `litellm_settings.callbacks` like every other hook — which means the existing
    "every registered hook imports inside the proxy image" gate covers it, and an
    operator can see it in the config instead of it being an invisible import
    side effect.
    """


proxy_handler_instance = AnthropicStreamLoggingFix()
