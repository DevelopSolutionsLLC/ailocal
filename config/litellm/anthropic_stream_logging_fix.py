"""
anthropic_stream_logging_fix.py — backport of a missing type guard in LiteLLM's
Anthropic success-logging path.

THE BUG (upstream; present in LiteLLM 1.93.0 and 1.94.1)
A streamed /v1/messages request carrying a web-search tool makes LiteLLM's own
success logging raise. Reproduces with plain curl, no client involved:

    POST /v1/messages   stream: true   tools: [{"type": "web_search_20250305",
                                                "name": "web_search"}]
      -> 3 x "LiteLLM.Success_Call Error: 1 validation error for AnthropicResponse"
    the same request with stream: false
      -> 0 errors

The websearch interception hook flips `stream=True -> False`, then
`_maybe_wrap_in_fake_stream` wraps the result in a
`FakeAnthropicMessagesStreamIterator` so the client still receives SSE. Success
logging hands that iterator to
`LiteLLMLoggingObj._handle_anthropic_messages_response_logging`, which
early-returns for `ModelResponse`, `ResponsesAPIResponse` and the
`ResponseCompletedEvent` family, then falls through to
`AnthropicResponse.model_validate(result)`. The iterator is none of those, so
pydantic raises. Upstream patched this same method once before for unhandled
types (BerriAI/litellm#27091) and missed this one.

IMPACT: non-blocking. The client still receives the full stream — only LiteLLM's
own success/spend logging is skipped. `request_trace` and `tool_gateway_metric`
are separate callbacks and still emit. The cost is an ERROR-level traceback per
streamed web-search request, which buries real errors during a log audit.

THE FIX
Add the missing guard in the shape upstream already uses: a fake stream iterator
is returned unchanged. Nothing is caught and nothing is logged away — the type
mismatch stops happening. Streaming, tracing and every other callback are
untouched.

SELF-RETIRING. The patch checks whether the installed method already handles the
type and does nothing if so, so a LiteLLM upgrade that fixes this upstream
disables it — the boot line below reports `skipped:`/`already patched` rather
than `patched`. Re-read the installed method after any upgrade; do not assume a
version bump fixed it. Once the curl repro above is clean with this hook removed
from `litellm_settings.callbacks`, delete this file and that entry.
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
