"""
request_trace.py — one durable record per request, so "Claude said API error" can
be answered with "which component, and when".

Writes JSONL to $AILOCAL_TRACE_DIR. Off unless that is set.

The failure class this exists for: every component reports success (HTTP 200,
gateway completed, repair clean, SSE well-formed) while the user sees a long
silence before the first byte, which no single component owns. A per-request
timeline makes that visible instead of requiring it to be re-derived.

This runs inside the proxy and sees only the proxy's view. Two gaps are
load-bearing when reading a record:

  ttfb_ms is [APPROX] for prompt-eval time, not a measurement of it. Ollama's
  prompt_eval_duration does not survive into the LiteLLM response; `usage`
  carries token counts only.

  Client timeouts and disconnects are NOT VISIBLE. If a client gives up at 60 s
  the proxy keeps streaming and records a success. A large ttfb_ms beside a
  client that reported an error IS the evidence — but never record a
  client-side failure as though it were observed here.
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
        # /v1/messages delivers RAW SSE FRAMES (`bytes`), not objects.
        # MARKER DETECTION, not SSE parsing: the frame is tested for two event
        # signatures and nothing is decoded, framed or reassembled. A second SSE
        # parser beside LiteLLM's own is what must not be built here.
        if isinstance(item, (bytes, bytearray)):
            if b'"type": "text_delta"' in item or b'"type":"text_delta"' in item:
                return "text"
            if b"tool_use" in item or b"input_json_delta" in item:
                return "tool"
            return None
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


def _resolved_backend(st, data):
    """The backend tag, ONLY when it is genuinely distinguishable.

    By the time the success callback fires, LiteLLM has rewritten `model` from
    the requested alias to the tag that served it. Before that it still holds
    the alias. So `model` is the backend model exactly when it differs from the
    alias we recorded at pre-call time; when they are equal we know nothing and
    return None rather than repeating the alias under a second name.
    """
    d = data or {}
    alias = (st or {}).get("requested_alias")
    current = d.get("model")
    return current if (alias and current and current != alias) else None


def _completion_fields(acc, saw_any_event):
    """Render the accumulator as trace fields, with an explicit reason for every
    null. `provider_done_reason` is deliberately NOT defaulted from
    finish_reason: on the ollama_chat streaming path finish_reason IS the raw
    done_reason, but on other providers it is a mapped value, and silently
    conflating the two would manufacture provider evidence we do not have."""
    def field(name, unavailable_reason):
        val = acc.get(name)
        if val is not None:
            return val, None
        if acc.get("extraction_error"):
            return None, EXTRACTION_FAILED
        return None, unavailable_reason

    no_reply = UNAVAILABLE_NO_BACKEND_REPLY if not saw_any_event else NOT_SENT_BY_PROVIDER

    ct, ct_av = field("completion_tokens", no_reply)
    pt, pt_av = field("prompt_tokens", no_reply)
    fr, fr_av = field("finish_reason", no_reply)
    sr, sr_av = field("stop_reason", no_reply)
    pdr, pdr_av = field("provider_done_reason", no_reply)
    pec, pec_av = field("provider_eval_count", no_reply)

    # COMPLETE requires a count AND a termination reason. One without the other
    # cannot answer "did this stop because it hit a ceiling", which is the only
    # question this record exists to answer.
    has_count = ct is not None or pec is not None
    has_reason = fr is not None or sr is not None or pdr is not None
    if has_count and has_reason:
        completeness = EVIDENCE_COMPLETE
    elif has_count or has_reason:
        completeness = EVIDENCE_PARTIAL
    else:
        completeness = EVIDENCE_NONE

    return {
        "completion_tokens": ct,
        "completion_tokens_availability": ct_av,
        "prompt_tokens": pt,
        "prompt_tokens_availability": pt_av,
        "finish_reason": fr,
        "finish_reason_availability": fr_av,
        "stop_reason": sr,
        "stop_reason_availability": sr_av,
        "provider_done": acc.get("provider_done"),
        "provider_done_reason": pdr,
        "provider_done_reason_availability": pdr_av,
        "provider_eval_count": pec,
        "provider_eval_count_availability": pec_av,
        "completion_extraction_error": acc.get("extraction_error"),
        "completion_evidence": completeness,
    }


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



# Bumped when a FIELD'S MEANING changes, never when one is added, so a consumer
# can trust older records. A null is "unknown", not a measurement.
#
# READING HISTORY (files are never rewritten): v1-v3 carry an overloaded `model`
# — the requested alias on pre_call/stream_end, the resolved backend tag on
# completion. v4 emits `requested_alias` and `resolved_backend_model` instead.
EVENT_VERSION = 4

# A stable identity for THIS proxy process. The startup connect-refused burst was
# only diagnosable because every failing record shared one container lifetime; with
# no generation marker, a restart is indistinguishable from a mid-life failure.
# Derived from the boot timestamp of this interpreter, so it survives log rotation
# and needs no Docker introspection from inside the container.
_PROCESS_GENERATION = f"pg-{int(time.time())}-{os.getpid()}"

# Availability reasons, so a null is never ambiguous. "Not measured" and "measured
# as zero" must never look alike.
UNAVAILABLE_NO_HOOK = "not_exposed_by_litellm_hook"
UNAVAILABLE_NO_BACKEND_REPLY = "no_backend_response"

# Completion-side availability. A null completion field has four distinct causes
# and collapsing them is how the planner investigation lost three days: "the
# provider reported nothing", "this dialect cannot carry it", "the hook never
# looked" and "parsing threw" are different facts with different owners.
NOT_SENT_BY_PROVIDER = "not_sent_by_provider"
EXTRACTION_FAILED = "extraction_failed"

# Evidence completeness for a streamed record, so a partially-observed stream can
# never be mistaken for a fully-observed one. EVIDENCE_COMPLETE requires BOTH a
# token count and a termination reason; anything less is PARTIAL by construction.
EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"
EVIDENCE_PARTIAL = "EVIDENCE_PARTIAL"
EVIDENCE_NONE = "EVIDENCE_NONE"


def _upstream_host(data) -> str | None:
    """The configured upstream, WITHOUT credentials.

    api_base only; any user-info or query string is dropped rather than trimmed,
    because a base URL is one of the places a key legitimately appears.
    """
    try:
        base = ((data or {}).get("litellm_params") or {}).get("api_base") or ""
        if not base:
            return None
        rest = base.split("://", 1)[-1]
        host = rest.split("/", 1)[0]
        return host.rsplit("@", 1)[-1].split("?", 1)[0][:120] or None
    except Exception:
        return None


def _est_tokens(text: str) -> int:
    """A deliberately crude, CONSISTENT estimate: ~4 chars per token.

    Estimated, never exact, and labelled as such in the record. The point is that
    every component uses the SAME method so they reconcile with each other; swapping
    in a real tokenizer for one component would make the parts stop summing.
    """
    return max(0, len(text) // 4)


def _text_len_of(content) -> int:
    """Character count of a message's content, WITHOUT retaining the text."""
    if isinstance(content, str):
        return len(content)
    total = 0
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict):
                for key in ("text", "content"):
                    v = part.get(key)
                    if isinstance(v, str):
                        total += len(v)
    return total


def _token_components(data) -> dict:
    """Estimated input-token components, measured by LENGTH only.

    Reads message content to MEASURE it and keeps none of it — the record carries
    integers. Components are disjoint by construction so they reconcile:

      schema        tool DEFINITIONS (serialized tool list)
      system        system prompt, including any injected persona
      history       user + assistant turns
      tool_result   tool RESULTS returned into the conversation
      other         anything else structural

    Tool definitions and tool results are counted separately and never both — the
    trap being that a filtered tool payload and a large tool result are different
    problems with different fixes.
    """
    d = data or {}
    out = {
        "schema_tokens_estimated": None,
        "system_instruction_tokens_estimated": None,
        "conversation_history_tokens_estimated": None,
        "tool_result_tokens_estimated": None,
        "other_input_tokens_estimated": None,
        "input_tokens_estimated_total": None,
        "token_estimate_method": "chars_div_4",
        "token_estimate_exactness": "estimated",
    }
    try:
        schema = _est_tokens(json.dumps(d.get("tools") or []))

        system = 0
        sys_field = d.get("system")
        if isinstance(sys_field, str):
            system += _est_tokens(sys_field)
        elif isinstance(sys_field, list):
            system += _est_tokens(str(_text_len_of(sys_field) * "x"))

        history = tool_result = other = 0
        for msg in (d.get("messages") or []):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            chars = _text_len_of(msg.get("content"))
            if role == "system":
                system += chars // 4
            elif role in ("user", "assistant"):
                # A tool RESULT arrives as a user-role message carrying
                # tool_result blocks; attributing it to history would hide the
                # single largest controllable contributor.
                content = msg.get("content")
                is_result = isinstance(content, list) and any(
                    isinstance(p, dict) and p.get("type") in
                    ("tool_result", "tool_use") for p in content)
                if is_result or role == "tool":
                    tool_result += chars // 4
                else:
                    history += chars // 4
            elif role == "tool":
                tool_result += chars // 4
            else:
                other += chars // 4

        out.update({
            "schema_tokens_estimated": schema,
            "system_instruction_tokens_estimated": system,
            "conversation_history_tokens_estimated": history,
            "tool_result_tokens_estimated": tool_result,
            "other_input_tokens_estimated": other,
            "input_tokens_estimated_total": schema + system + history + tool_result + other,
        })
    except Exception:
        pass
    return out


def _context_budget(self_registry, data, components) -> dict:
    """Declared context, requested output reserve, and the resulting headroom.

    `effective_backend_context_tokens` is deliberately NULL with a reason: the
    declared window is routing metadata, and what Ollama actually sustains is a
    separate measurement this hook cannot make. Reporting the declared number as
    "effective" is exactly the conflation that makes an overflow look impossible.
    """
    d = data or {}
    declared = None
    try:
        if self_registry is not None:
            # Registry.max_context, NOT max_context_for, which does not exist.
            # The bare `except` below turns a typo here into a permanent null
            # budget — the one field that can attribute an overflow.
            declared = self_registry.max_context(d.get("model"))
    except Exception:
        declared = None
    # model_info is a SIBLING of litellm_params in the deployment config, not
    # nested inside it. Both locations are tried, in the order LiteLLM populates
    # them; looking only inside litellm_params leaves every temporary bench-*
    # alias with a null budget.
    if declared is None:
        for src in (d.get("model_info"),
                    (d.get("litellm_params") or {}).get("model_info")):
            try:
                if isinstance(src, dict) and src.get("max_input_tokens"):
                    declared = src["max_input_tokens"]
                    break
            except Exception:  # noqa: BLE001
                continue

    requested_out = d.get("max_tokens") or d.get("max_completion_tokens")
    total_in = components.get("input_tokens_estimated_total")
    headroom = None
    if isinstance(declared, int) and isinstance(total_in, int):
        headroom = declared - total_in - (requested_out if isinstance(requested_out, int) else 0)
    # CONFIGURED geometry, read straight from the deployment. These do not
    # depend on the production registry, which is why they are the only context
    # numbers a temporary bench-* alias can report at all. They are CONFIGURED
    # values and are named as such: none of them is a provider measurement, and
    # the physical window is num_ctx, never the admission threshold.
    lp = d.get("litellm_params") or {}
    num_ctx = lp.get("num_ctx")
    num_predict = lp.get("num_predict")
    usable_in = None
    if isinstance(num_ctx, int) and isinstance(num_predict, int) and num_predict > 0:
        # num_predict -1 means INFINITE generation in Ollama and -2 fill-context;
        # neither reserves a knowable amount, so usable input is not computable
        # rather than silently equal to the whole window.
        usable_in = num_ctx - num_predict
    elif isinstance(num_ctx, int) and num_predict in (None, -1, -2):
        usable_in = None

    return {
        "requested_output_tokens": requested_out if isinstance(requested_out, int) else None,
        "declared_context_tokens": declared if isinstance(declared, int) else None,
        "effective_backend_context_tokens": None,
        "effective_backend_context_availability": "not_measured_by_this_hook",
        "context_headroom_tokens": headroom,
        "configured_num_ctx": num_ctx if isinstance(num_ctx, int) else None,
        "configured_num_predict": num_predict if isinstance(num_predict, int) else None,
        "usable_input_tokens": usable_in,
        "usable_input_availability": (
            None if usable_in is not None else "num_predict_unbounded_or_absent"),
        # What the pre-call guard will ADMIT. Recorded next to usable_input so
        # admission > physical capacity is visible in the record itself rather
        # than needing to be recomputed by a reader.
        "admission_limit_tokens": declared if isinstance(declared, int) else None,
        "admission_exceeds_usable_input": (
            bool(isinstance(declared, int) and usable_in is not None
                 and declared > usable_in)),
        # The model's own trained window. NOT visible from inside the proxy --
        # it needs an Ollama /api/show call this hook must not make.
        "model_native_context_tokens": None,
        "model_native_context_availability": "not_exposed_by_litellm_hook",
    }


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
        components = _token_components(data)
        return {
            "capability": capability,
            "client": client,
            "route": route,
            "model_class": model_class,
            "request_id": st.get("request_id"),
            "ts": st.get("t_start"),
            "call_type": call_type,
            # The alias the CLIENT asked for, carried from the pre-call hook
            # through the shared per-request state so it is identical on every
            # record of one request -- including the completion record, where
            # LiteLLM has already rewritten `model` to the backend tag.
            "requested_alias": st.get("requested_alias") or (data or {}).get("model"),
            # What actually served it. On pre-call this is litellm_params.model
            # ("ollama_chat/<tag>"); on completion LiteLLM has resolved it into
            # `model` itself. Never inferred from the alias.
            "resolved_backend_model": backend or _resolved_backend(st, data),
            "stream": bool((data or {}).get("stream")),
            "user_agent": ua[:80],
            "tools_declared": len(tools),
            # Message history length matters independently of tools: a long
            # session grows the prompt even when the tool payload is filtered.
            # This is the field that would show a residual latency cause.
            "messages": len((data or {}).get("messages") or []),
            "input_items": len((data or {}).get("input") or [])
                           if isinstance((data or {}).get("input"), list) else None,
            "event_version": EVENT_VERSION,
            "process_generation": _PROCESS_GENERATION,
            "upstream_host": _upstream_host(data),
            # Connection timing is NOT exposed to a pre/post-call hook, so these are
            # explicit nulls with a reason rather than the request timestamps
            # relabelled — which would have looked like a measurement and been one.
            "upstream_connect_started_at": None,
            "upstream_connect_completed_at": None,
            "upstream_connect_ms": None,
            "upstream_connect_availability": UNAVAILABLE_NO_HOOK,
            "last_chunk_at": st.get("last_chunk_at"),
            "disconnect_owner": st.get("disconnect_owner"),
            "disconnect_owner_availability": (
                None if st.get("disconnect_owner") else "not_determinable"),
            **components,
            **_context_budget(self.registry, data, components),
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
            # Capture the REQUESTED alias here and nowhere else. This is the
            # only hook that sees it before LiteLLM resolves it, and the state
            # dict travels with the request into every later hook.
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
        # Per-event timeline. ttfb_ms + generation_ms alone actively MISLED a
        # whole investigation: they cannot distinguish "the model was slow", "the
        # backend was busy", "nothing visible was ever produced" and "output was
        # buffered and replayed". These four fields separate them.
        stamps = []
        first_text = None
        first_tool = None
        saw_text = False
        # Completion evidence is NOT extracted here: on /v1/messages this hook
        # sees RAW SSE bytes, so reading usage would mean a second SSE parser.
        # async_log_success_event owns that record. Anything leaving the loop
        # other than exhaustion -- client disconnect or a backend fault -- is an
        # interruption, and its evidence is partial BY CAUSE.
        interrupted = False
        interrupt_type = None
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
        except BaseException as exc:  # noqa: BLE001
            # Record and re-raise. Swallowing here would convert a real failure
            # into a silent truncation, which is the exact class of bug this
            # whole record exists to catch.
            interrupted = True
            interrupt_type = type(exc).__name__
            raise
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
                # Ollama emits a tool call as ONE atomic chunk with complete
                # arguments -- there are no partial function-call deltas to
                # forward. So a tool-call turn having
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
                "stream_completed_normally": not interrupted,
                "stream_interrupted": interrupted,
                "stream_interrupt_type": interrupt_type,
                # The CONFIGURED alias ceiling, read from litellm_params. It is
                # NOT called effective_num_predict: this hook cannot see what
                # Ollama actually applied, and naming a configured value
                # "effective" is precisely the conflation that made an overflow
                # look impossible in the context-budget fields.
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
        installed 1.93.0, for STREAMED requests, on BOTH routes:

            /v1/messages          call_type=anthropic_messages -> ModelResponse
            /v1/chat/completions  call_type=acompletion        -> ModelResponse
            usage.completion_tokens / usage.prompt_tokens populated in both

        Correlation needs no new plumbing: our KEY survives into `kwargs`, so
        the completion record carries the SAME request_id as the pre-call and
        stream_end records (measured: has_trace_key=true on both routes).

        `kwargs` also carries the resolved provider params (num_predict, num_ctx,
        max_tokens), which the iterator hook could not see at all — that is the
        field the output-limit question actually turns on.

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

            acc = {
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "finish_reason": finish,
                "stop_reason": getattr(response_obj, "stop_reason", None),
            }

            opt = kw.get("optional_params") or {}
            # The value LiteLLM actually resolved for the provider, after mapping
            # max_tokens -> num_predict and after any static alias value. This is
            # the effective ceiling; `requested_output_tokens` is what the client
            # asked for. Recording both is the entire point.
            eff_np = opt.get("num_predict", kw.get("num_predict"))

            rec.update({
                "phase": "completion",
                "outcome": "completed",
                "stream": bool(kw.get("stream")),
                "requested_output_tokens": kw.get("max_tokens"),
                "effective_num_predict": eff_np,
                "effective_num_predict_availability": (
                    None if eff_np is not None else NOT_SENT_BY_PROVIDER),
                "effective_num_ctx": opt.get("num_ctx", kw.get("num_ctx")),
                "response_type": type(response_obj).__name__,
                "llm_api_duration_ms": kw.get("llm_api_duration_ms"),
                **_completion_fields(acc, saw_any_event=True),
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
            # Keys must exist in _base()'s output. `model` was removed at
            # event_version 4 (it was overloaded); asking for it here raised
            # KeyError on EVERY failure, so the operator saw trace_failure_failed
            # instead of the failure summary. Use .get so a future field removal
            # degrades one value rather than the whole record.
            emit({k: rec.get(k) for k in ("request_id", "phase", "error_type",
                                          "total_ms", "requested_alias")})
        except Exception as exc:
            emit({"event": "trace_failure_failed", "error": str(exc)})
        return None


proxy_handler_instance = RequestTrace()
