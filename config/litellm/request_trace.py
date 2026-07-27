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
        headers = self._headers(data)
        ua = str(headers.get("user-agent") or "")
        tools = (data or {}).get("tools") or []
        return {
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
            self._state(data)
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
        try:
            async for item in response:
                if first is None:
                    first = time.time()
                    st["t_first_byte"] = first
                n += 1
                yield item
        finally:
            rec = self._base(request_data)
            rec.update({
                "phase": "stream_end",
                "ttfb_ms": round((first - st["t_start"]) * 1000, 1)
                           if first and st.get("t_start") else None,
                "total_ms": round((time.time() - st["t_start"]) * 1000, 1)
                            if st.get("t_start") else None,
                "chunks": n,
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
