"""request_trace.py — one durable record per request, so "Claude said API error"
can be answered with "which component, and when".

Writes JSONL to $AILOCAL_TRACE_DIR. Off unless that is set.

The failure class this exists for: every component reports success (HTTP 200,
gateway completed, repair clean, SSE well-formed) while the user sees a long
silence before the first byte, which no single component owns. ttfb_ms beside
outcome makes that visible.

Two gaps are load-bearing when reading a record. ttfb_ms is [APPROX] for
prompt-eval time, not a measurement of it: Ollama's prompt_eval_duration does
not survive into the LiteLLM response. Client timeouts and disconnects are NOT
VISIBLE — if a client gives up at 60 s the proxy keeps streaming and records a
success, so a large ttfb_ms beside a client that reported an error IS the
evidence, but never record a client-side failure as though it were observed
here.

Records are read with .get(): a reader tolerates fields that older records
carry and this version no longer writes.
"""

import json
import os
import time
import uuid

from litellm.integrations.custom_logger import CustomLogger


def _load_registry():
    """Reuse capability_registry rather than re-deriving capability/client/route:
    a second implementation would drift, and a trace and a gateway metric for the
    SAME request could then disagree about what it was. Loader duplicated from
    tool_gateway.py by necessity — see the note there."""
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

# Bumped when a field's meaning changes OR a field is withdrawn, so a consumer
# can tell what a record is entitled to carry. A null is "unknown", never a
# measurement.
#
# READING HISTORY (files are never rewritten): v1-v3 carry an overloaded `model`
# — the requested alias early, the resolved backend tag on completion. v4 split
# that into `requested_alias` and `resolved_backend_model`, and also carried
# estimated token components, a context budget and per-field availability
# reasons that no reader ever consumed; v5 stops writing them.
EVENT_VERSION = 5

# A stable identity for THIS proxy process. The startup connect-refused burst was
# only diagnosable because every failing record shared one container lifetime; with
# no generation marker, a restart is indistinguishable from a mid-life failure.
_PROCESS_GENERATION = f"pg-{int(time.time())}-{os.getpid()}"


def _resolved_backend(st, data):
    """The backend tag, ONLY when it is genuinely distinguishable.

    By the time the success callback fires, LiteLLM has rewritten `model` from
    the requested alias to the tag that served it. Before that it still holds
    the alias. So `model` is the backend model exactly when it differs from the
    alias we recorded at pre-call time; when they are equal we know nothing and
    return None rather than repeating the alias under a second name.
    """
    alias = (st or {}).get("requested_alias")
    current = (data or {}).get("model")
    return current if (alias and current and current != alias) else None


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
        d = data or {}
        call_type = call_type or st.get("call_type")
        capability = client = model_class = route = None
        if self.registry is not None:
            try:
                capability = self.registry.capability_of(d.get("model"))
                client = self.registry.detect_client(self._headers(data))
                route = self.registry.route_for_call_type(call_type, "input" in d)
                model_class, _ = self.registry.model_class(d.get("model"))
            except Exception:
                pass
        try:
            backend = (d.get("litellm_params") or {}).get("model")
        except Exception:
            backend = None
        return {
            "request_id": st.get("request_id"),
            "ts": st.get("t_start"),
            "event_version": EVENT_VERSION,
            "process_generation": _PROCESS_GENERATION,
            "capability": capability,
            "client": client,
            "route": route,
            "model_class": model_class,
            "call_type": call_type,
            # The alias the CLIENT asked for, carried from the pre-call hook
            # through the shared per-request state so it is identical on every
            # record of one request -- including the completion record, where
            # LiteLLM has already rewritten `model` to the backend tag.
            "requested_alias": st.get("requested_alias") or d.get("model"),
            # What actually served it. Never inferred from the alias.
            "resolved_backend_model": backend or _resolved_backend(st, data),
            "stream": bool(d.get("stream")),
            "user_agent": str(self._headers(data).get("user-agent") or "")[:80],
            "tools_declared": len(d.get("tools") or []),
            # Message history length matters independently of tools: a long
            # session grows the prompt even when the tool payload is filtered.
            "messages": len(d.get("messages") or []),
        }

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
            # Capture the REQUESTED alias here and nowhere else: this is the only
            # hook that sees it before LiteLLM resolves it.
            if st is not None and (data or {}).get("model"):
                st.setdefault("requested_alias", data["model"])
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
        # Anything leaving the loop other than exhaustion -- client disconnect or
        # a backend fault -- is an interruption. Record and re-raise: swallowing
        # here would convert a real failure into a silent truncation, the exact
        # class of bug this record exists to catch.
        interrupted = False
        interrupt_type = None
        try:
            async for item in response:
                if first is None:
                    first = time.time()
                n += 1
                yield item
        except BaseException as exc:  # noqa: BLE001
            interrupted = True
            interrupt_type = type(exc).__name__
            raise
        finally:
            rec = self._base(request_data)
            total_ms = (round((time.time() - st["t_start"]) * 1000, 1)
                        if st.get("t_start") else None)
            ttfb_ms = (round((first - st["t_start"]) * 1000, 1)
                       if first and st.get("t_start") else None)
            rec.update({
                "phase": "stream_end",
                "ttfb_ms": ttfb_ms,
                # Decode phase, separated from the wait before the first token.
                # These two are driven by different things -- prompt size vs
                # model speed -- and merging them cannot tell a slow model from
                # a large prompt.
                "generation_ms": (round(total_ms - ttfb_ms, 1)
                                  if (total_ms is not None and ttfb_ms is not None)
                                  else None),
                "total_ms": total_ms,
                "chunks": n,
                # A stream that produced zero chunks is NOT a success, whatever
                # the HTTP status was.
                "outcome": "streamed" if n else "empty_stream",
                "stream_interrupted": interrupted,
                "stream_interrupt_type": interrupt_type,
            })
            self._write(rec)
            emit(rec)

    async def async_log_success_event(self, kwargs, response_obj, start_time,
                                      end_time):
        """The completion record — the one that says how generation ENDED.

        WHY THIS HOOK AND NOT THE STREAM ITERATOR. The iterator hook sees raw
        SSE `bytes` on /v1/messages, so extracting usage there would mean
        maintaining a second SSE parser next to LiteLLM's own. LiteLLM already
        assembles the finished response and passes it here — measured on the
        installed 1.93.0, for streamed requests, on both /v1/messages
        (call_type=anthropic_messages) and /v1/chat/completions
        (call_type=acompletion), with usage populated in both.

        `kwargs` also carries the resolved provider params, which the iterator
        hook cannot see — that is what the output-limit question turns on.

        Failure here must never affect the request: this runs after the response
        has been delivered, and every exception is contained.
        """
        if not self.dir:
            return
        try:
            kw = kwargs if isinstance(kwargs, dict) else {}
            rec = self._base(kw, call_type=kw.get("call_type"))
            usage = getattr(response_obj, "usage", None)
            finish = None
            try:
                choices = getattr(response_obj, "choices", None) or []
                if choices:
                    finish = getattr(choices[0], "finish_reason", None)
            except Exception:  # noqa: BLE001
                pass
            opt = kw.get("optional_params") or {}
            rec.update({
                "phase": "completion",
                "outcome": "completed",
                "stream": bool(kw.get("stream")),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                # Why generation stopped. Without a termination reason beside a
                # token count, "did this hit a ceiling" is unanswerable.
                "finish_reason": finish,
                "stop_reason": getattr(response_obj, "stop_reason", None),
                "requested_output_tokens": kw.get("max_tokens"),
                # What LiteLLM actually resolved for the provider, after mapping
                # max_tokens -> num_predict. Recording both this and the
                # requested value is the point.
                "effective_num_predict": opt.get("num_predict",
                                                 kw.get("num_predict")),
                "effective_num_ctx": opt.get("num_ctx", kw.get("num_ctx")),
                "llm_api_duration_ms": kw.get("llm_api_duration_ms"),
            })
            self._write(rec)
        except Exception as exc:  # noqa: BLE001
            emit({"event": "trace_completion_failed", "error": str(exc)[:200]})

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
            # .get so a future field removal degrades one value rather than
            # raising inside the failure path and hiding the failure itself.
            emit({k: rec.get(k) for k in ("request_id", "phase", "error_type",
                                          "total_ms", "requested_alias")})
        except Exception as exc:
            emit({"event": "trace_failure_failed", "error": str(exc)})
        return None


proxy_handler_instance = RequestTrace()
