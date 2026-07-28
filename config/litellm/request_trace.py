"""
request_trace.py — one durable record per request, so "Claude said API error" can
be answered with "which component, and when".

Writes JSONL to $AILOCAL_TRACE_DIR. Off unless that is set.

WHY THIS EXISTS
The Claude-local failure was invisible for a long time because every component
reported success: HTTP 200, gateway completed, tool repair clean, SSE well-formed.
The failure was a ~90 s silence before the first byte, which no component
considered its own problem. A per-request timeline makes that class of failure
visible instead of requiring it to be re-derived each time.

WHAT IS ACTUALLY MEASURABLE HERE, AND WHAT IS NOT
This runs inside the proxy, so it sees the proxy's view and no more. Stated
plainly because the gaps matter:

  MEASURABLE   request id, client, model, capability, route, tool count and
               bytes, time to first streamed chunk, total duration, finish/stop
               reason, exception + traceback on failure
  NOT MEASURABLE
               Ollama's prompt_eval_duration / eval_duration. Verified: they do
               not survive into the LiteLLM response — `usage` carries only
               prompt_tokens/completion_tokens/total_tokens. Time-to-first-chunk
               is recorded as `ttfb_ms` and is a PROXY for prompt-eval time, not
               a measurement of it. Getting the real figure needs a direct Ollama
               query, which this hook deliberately does not make.
  NOT VISIBLE AT ALL
               Client-side timeouts and disconnects. If Claude Code gives up at
               60 s, the proxy keeps streaming and records a success. A trace
               showing a large ttfb_ms and a completed response, next to a client
               that reported an error, IS the evidence of a client timeout — but
               the disconnect itself is not observable from here. Never record a
               client-side failure as though it were seen.

Registered via litellm_settings.callbacks: request_trace.proxy_handler_instance
"""

import json
import os
import time
import uuid

from litellm.integrations.custom_logger import CustomLogger


def _classify_event(item) -> str | None:
    """Does this streamed event put TEXT on screen, or carry a TOOL CALL?

    Handles all three dialects because the distinction is dialect-independent and
    the question ("did the user see anything?") is the same in each.
    """
    try:
        # Events arrive as PYDANTIC MODELS on most routes, not dicts. An earlier
        # version read __dict__, which does not expose pydantic fields, so every
        # event classified as None and first_visible_text_ms was always null --
        # a metric that silently measured nothing. model_dump() first.
        if isinstance(item, dict):
            d = item
        elif hasattr(item, "model_dump"):
            d = item.model_dump()
        elif hasattr(item, "dict"):
            d = item.dict()
        else:
            d = getattr(item, "__dict__", {}) or {}
        if not isinstance(d, dict):
            return None
        # `type` is an ENUM on the Responses path (ResponsesAPIStreamEvents), so
        # str() yields "ResponsesAPIStreamEvents.RESPONSE_CREATED" rather than
        # "response.created" and every endswith() check silently failed. Take
        # .value when present. Measured, after two wrong guesses about the shape.
        _t = d.get("type")
        t = str(getattr(_t, "value", _t) or "")
        if t.endswith("output_text.delta") or t == "content_block_delta":
            delta = d.get("delta")
            if isinstance(delta, str) and delta:
                return "text"
            if isinstance(delta, dict):
                return "text" if delta.get("text") or delta.get("type") == "text_delta" else "tool"
        if "function_call" in t or "tool_use" in t:
            return "tool"
        choices = d.get("choices") or []
        if choices:
            delta = getattr(choices[0], "delta", None) or (
                choices[0].get("delta") if isinstance(choices[0], dict) else None) or {}
            get = delta.get if isinstance(delta, dict) else lambda k: getattr(delta, k, None)
            if get("tool_calls"):
                return "tool"
            if get("content"):
                return "text"
    except Exception:  # noqa: BLE001
        return None
    return None


def _load_registry():
    """Reuse capability_registry rather than re-deriving capability/client/route.
    A second implementation would drift from the gateway's, and then a trace and
    a gateway metric for the SAME request could disagree about what it was."""
    import importlib.util
    import sys as _sys
    if "capability_registry" in _sys.modules:
        return _sys.modules["capability_registry"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "capability_registry.py")
    spec = importlib.util.spec_from_file_location("capability_registry", path)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["capability_registry"] = mod
    spec.loader.exec_module(mod)
    return mod


TRACE_DIR = os.environ.get("AILOCAL_TRACE_DIR") or ""
# Correlation id lives here in the request dict. LiteLLM passes `data` through to
# every hook for the same request, so this is the join key across hooks without
# mutating anything the backend sees.
KEY = "_ailocal_trace"


def emit(record):
    try:
        print("request_trace " + json.dumps(record, default=str), flush=True)
    except Exception:
        pass


class RequestTrace(CustomLogger):

    def __init__(self):
        super().__init__()
        try:
            self.registry = _load_registry().Registry()
        except Exception as exc:
            # Tracing must survive a registry problem: a trace with fewer fields
            # is far better than a hook that breaks the request path.
            emit({"event": "registry_unavailable", "error": str(exc)})
            self.registry = None
        self.dir = TRACE_DIR
        if self.dir:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except Exception as exc:
                emit({"event": "trace_dir_failed", "error": str(exc)})
                self.dir = ""

    # ── helpers ─────────────────────────────────────────────────────────────
    def _state(self, data):
        """Per-request scratch space, created on first touch."""
        if not isinstance(data, dict):
            return None
        st = data.get(KEY)
        if st is None:
            st = {"request_id": uuid.uuid4().hex[:16], "t_start": time.time()}
            data[KEY] = st
        return st

    def _write(self, rec):
        if not self.dir:
            return
        try:
            path = os.path.join(self.dir, time.strftime("%Y%m%d") + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception as exc:
            emit({"event": "trace_write_failed", "error": str(exc)})

    @staticmethod
    def _headers(data):
        req = (data or {}).get("proxy_server_request") or {}
        return {str(k).lower(): v for k, v in (req.get("headers") or {}).items()}

    def _base(self, data, call_type=None):
        st = self._state(data) or {}
        call_type = call_type or st.get("call_type")
        headers = self._headers(data)
        ua = str(headers.get("user-agent") or "")
        tools = (data or {}).get("tools") or []
        model = (data or {}).get("model")
        capability = client = model_class = backend = None
        route = None
        if self.registry is not None:
            try:
                capability = self.registry.capability_of(model)
                client = self.registry.detect_client(headers)
                route = self.registry.route_for_call_type(
                    call_type, "input" in (data or {}))
                model_class, _spec = self.registry.model_class(model)
            except Exception:
                pass
        # The REAL backend tag behind the alias. Benchmarking a capability is
        # meaningless without knowing which model actually served it.
        try:
            backend = ((data or {}).get("litellm_params") or {}).get("model") \
                      or (data or {}).get("_backend_model")
        except Exception:
            backend = None
        return {
            "capability": capability,
            "client": client,
            "route": route,
            "model_class": model_class,
            "backend_model": backend,
            "request_id": st.get("request_id"),
            "ts": st.get("t_start"),
            "call_type": call_type,
            "model": (data or {}).get("model"),
            "stream": bool((data or {}).get("stream")),
            "user_agent": ua[:80],
            "tools_declared": len(tools),
            # Message history length matters independently of tools: a long
            # session grows the prompt even when the tool payload is filtered.
            # This is the field that would show a residual latency cause.
            "messages": len((data or {}).get("messages") or []),
            "input_items": len((data or {}).get("input") or [])
                           if isinstance((data or {}).get("input"), list) else None,
        }

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not self.dir:
            return data
        try:
            st = self._state(data)
            # The streaming iterator hook is not given call_type. Without this
            # the route defaulted to /v1/chat/completions and a /v1/messages
            # request was traced as the wrong route — a trace that lies about
            # what it observed is worse than one missing the field.
            if st is not None and call_type:
                st["call_type"] = call_type
        except Exception:
            pass
        return data

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict,
                                                      response, request_data):
        """Wrap the stream to timestamp the FIRST chunk. That is the number the
        Claude failure turned on, so it is measured rather than inferred."""
        if not self.dir:
            async for item in response:
                yield item
            return
        st = self._state(request_data) or {}
        first = None
        n = 0
        # Per-event timeline. ttfb_ms + generation_ms alone actively MISLED a
        # whole investigation: they cannot distinguish "the model was slow", "the
        # backend was busy", "nothing visible was ever produced" and "output was
        # buffered and replayed". These four fields separate them.
        stamps = []
        first_text = None
        first_tool = None
        saw_text = False
        try:
            async for item in response:
                now = time.time()
                if first is None:
                    first = now
                    st["t_first_byte"] = first
                stamps.append(now)
                kind = _classify_event(item)
                if kind == "text":
                    saw_text = True
                    if first_text is None:
                        first_text = now
                elif kind == "tool" and first_tool is None:
                    first_tool = now
                n += 1
                yield item
        finally:
            rec = self._base(request_data)
            total_ms = (round((time.time() - st["t_start"]) * 1000, 1)
                        if st.get("t_start") else None)
            ttfb_ms = (round((first - st["t_start"]) * 1000, 1)
                       if first and st.get("t_start") else None)
            # Decode phase, separated from the wait before the first token. These
            # two are driven by different things — prompt size vs model speed —
            # and a benchmark that merges them cannot tell a slow model from a
            # large prompt.
            gen_ms = (round(total_ms - ttfb_ms, 1)
                      if (total_ms is not None and ttfb_ms is not None) else None)
            # Derived timeline. Every field here answers a question that
            # ttfb/generation could not.
            gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
            window_ms = ((stamps[-1] - stamps[0]) * 1000) if len(stamps) > 1 else 0.0
            eps = (len(stamps) / (window_ms / 1000)) if window_ms > 1 else None
            rec.update({
                "phase": "stream_end",
                "ttfb_ms": ttfb_ms,
                # WHAT THE USER ACTUALLY EXPERIENCES. first_event is when the
                # stream opened; first_visible_text is when the UI started
                # moving. They differ by seconds on tool-call turns, and the gap
                # between them IS the "it looks frozen" complaint.
                "first_event_ms": ttfb_ms,
                "first_visible_text_ms": (round((first_text - st["t_start"]) * 1000, 1)
                                          if first_text and st.get("t_start") else None),
                "first_function_call_event_ms": (round((first_tool - st["t_start"]) * 1000, 1)
                                                 if first_tool and st.get("t_start") else None),
                "event_gap_max_ms": round(max(gaps), 1) if gaps else None,
                "events_per_second": round(eps, 1) if eps else None,
                # MEASURED at the source (2026-07-28): Ollama emits a tool call as
                # ONE atomic chunk with complete arguments -- there are no partial
                # function-call deltas to forward. So a tool-call turn having
                # almost no events is CORRECT, not a buffering fault.
                "tool_call_only": bool(first_tool) and not saw_text,
                # A local model cannot emit >200 events/sec through this stack.
                # Above that, the events were produced earlier and replayed.
                "impossible_flush_detected": bool(eps and eps > 200),
                "buffered_stream": bool(eps and eps > 200 and n > 50),
                # Set by LiteLLM's websearch_interception when it converts a
                # streaming call to non-streaming. Recorded so the question
                # "did interception downgrade this?" is answerable from data
                # instead of from reading source and guessing.
                "stream_downgraded": bool(
                    (request_data or {}).get("_websearch_interception_converted_stream")),
                # NOT MEASURABLE from inside the proxy, and recorded as null
                # rather than omitted so their absence is explicit: Ollama's
                # load_duration / prompt_eval_duration / queue time do not survive
                # into the LiteLLM response (usage carries only token counts).
                # Getting them needs a direct backend query this hook must not make.
                "backend_queue_ms": None,
                "prompt_eval_ms": None,
                "model_load_ms": None,
                "total_ms": total_ms,
                "generation_ms": gen_ms,
                "chunks": n,
                # Chunks/sec is a throughput signal available even when the
                # provider gives no token usage on the streaming path. Labelled
                # as chunks, NOT tokens, because they are not the same thing.
                "chunks_per_sec": (round(n / (gen_ms / 1000.0), 2)
                                   if gen_ms and gen_ms > 0 else None),
                # A stream that produced zero chunks is NOT a success, whatever
                # the HTTP status was.
                "outcome": "streamed" if n else "empty_stream",
            })
            self._write(rec)
            emit(rec)

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        if not self.dir:
            return response
        try:
            st = self._state(data) or {}
            rec = self._base(data)
            finish = None
            stop = None
            try:
                choices = getattr(response, "choices", None) or []
                if choices:
                    finish = getattr(choices[0], "finish_reason", None)
                stop = getattr(response, "stop_reason", None)
            except Exception:
                pass
            usage = getattr(response, "usage", None)
            rec.update({
                "phase": "success",
                "total_ms": round((time.time() - st.get("t_start", time.time()))
                                  * 1000, 1),
                "finish_reason": finish,
                "stop_reason": stop,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "outcome": "success",
            })
            self._write(rec)
        except Exception as exc:
            emit({"event": "trace_success_failed", "error": str(exc)})
        return response

    async def async_post_call_failure_hook(self, request_data, original_exception,
                                           user_api_key_dict, traceback_str=None):
        """The record that matters most. Captures the exception TYPE and message,
        which is what distinguishes a timeout from a context-window rejection
        from a backend refusal — three failures that look identical to a user."""
        if not self.dir:
            return None
        try:
            st = self._state(request_data) or {}
            rec = self._base(request_data)
            rec.update({
                "phase": "failure",
                "total_ms": round((time.time() - st.get("t_start", time.time()))
                                  * 1000, 1),
                "outcome": "failure",
                "error_type": type(original_exception).__name__,
                "error": str(original_exception)[:600],
                "traceback": (traceback_str or "")[:1500],
            })
            self._write(rec)
            emit({k: rec[k] for k in ("request_id", "phase", "error_type",
                                      "total_ms", "model")})
        except Exception as exc:
            emit({"event": "trace_failure_failed", "error": str(exc)})
        return None


proxy_handler_instance = RequestTrace()
