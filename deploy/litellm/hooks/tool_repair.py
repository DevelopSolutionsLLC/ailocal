"""
tool_repair.py — generic recovery of malformed tool calls from local models.

Local models routinely "drift" out of the structured tool-call format their
runtime expects, and the runtime's parser then hands the whole call back as
plain assistant text. The agent client is waiting for a tool_call that never
arrives, so the loop stalls (this is the "Codex hangs until I type continue"
symptom).

Measured on this stack with real Claude Code Read/Edit/Write/Bash schemas:

    model                        native      + repair
    qwen3-coder:30b              2/8  (25%)   8/8 (100%)
    qwen2.5-coder:14b            0/8   (0%)   8/8 (100%)
    qwen2.5-coder:32b            0/8   (0%)   8/8 (100%)
    devstral:24b                 8/8 (100%)   8/8 (100%)   <- hook never fires

Upstream references (this is a known, UNFIXED runtime bug):
  ollama/ollama#16686  parser drops tool calls when the model omits the opening
                       <tool_call> tag — describes our exact failure
  ollama/ollama#16693  proposed fix, OPEN and stale since 2026-06-12
  ollama/ollama#16732  alternative fix, CLOSED unmerged
  block/goose#6883     same bug from another agent framework; they shipped the
                       same client-side fallback (PR #6882)

NOT A QWEN HACK. Deliberately model-agnostic: it recognises *formats*, never
model names. Keep this module even if Ollama fixes #16686 — it is a general
compatibility layer, and the next model will drift in some other direction.
It costs ~0.1-0.2 ms and does nothing at all when native tool calls work.

SAFETY MODEL
------------
The dangerous failure is fabricating a tool call that the model never made, so
every rule below biases toward doing nothing:
  - only runs when the response carries NO native tool_calls
  - fenced/inline code is stripped first, so documentation examples that merely
    *show* tool syntax can never become executable calls
  - incomplete calls are rejected (a closing boundary is required) so a
    truncated command is never executed
  - the tool name must appear in the tools the caller actually declared
  - arguments are validated against the declared JSON schema; unknown keys are
    dropped and missing REQUIRED keys cause the call to be rejected outright
  - arguments are never invented or defaulted
"""

import collections
import json
import logging
import os
import re
import uuid

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("tool_repair")

# Set AILOCAL_TOOL_REPAIR_DEBUG=1 to emit attribution records to container stdout.
# print(), not log.info(): LiteLLM filters third-party loggers, so log.info() is
# invisible in `docker logs` — which previously made it impossible to tell whether
# a working tool call came from NATIVE parsing or from this repair layer.
AILOCAL_DEBUG = os.environ.get("AILOCAL_TOOL_REPAIR_DEBUG") == "1"


def attribute(**fields):
    """One machine-parseable line per tool-bearing response.

    Answers the question behavioural tests cannot: how often is the model
    producing native tool calls vs. how often is this layer rescuing it?
    Silent unless AILOCAL_TOOL_REPAIR_DEBUG=1, so normal logs stay clean.
    """
    if not AILOCAL_DEBUG:
        return
    try:
        print("AILOCAL_ATTRIB " + json.dumps(fields, default=str), flush=True)
    except Exception:  # noqa: BLE001 - telemetry must never break a response
        pass


# ── Counters ────────────────────────────────────────────────────────────────
# Deliberately NOT gated behind AILOCAL_TOOL_REPAIR_DEBUG. Gating the only signal
# behind a flag that production does not set meant "repair never fired" and "repair
# fired and the client ignored it" looked identical in the logs — a real incident
# that cost two container swaps to disambiguate. Counters are cheap; emit always.
#
# `reason` is the diagnostic that was missing entirely: a rejected candidate used to
# vanish silently, so a false negative was invisible.
_COUNTS = collections.Counter()


def record(outcome, reason=None, tool=None, route=None, model=None, turn=None):
    """Count one repair decision. outcome: attempted|repaired|rejected|native."""
    try:
        _COUNTS[(outcome, reason or "-", tool or "-")] += 1
        # print(), NOT log.info() — LiteLLM filters third-party loggers, so log.info is
        # invisible in `docker logs`. The module docstring already records this; routing
        # the new counters through log.info reproduced the exact blindness they exist to
        # remove, and the first smoke run surfaced zero metrics because of it.
        print("tool_repair_metric " + json.dumps({
            "outcome": outcome, "reason": reason or "-", "tool": tool or "-",
            "route": route or "-", "model": model or "-", "turn": turn,
            "total": _COUNTS[(outcome, reason or "-", tool or "-")],
        }, default=str), flush=True)
    except Exception:  # noqa: BLE001 - telemetry must never break a response
        pass


def _turn_of(request_data):
    """Best-available turn index: how many assistant replies are already in play.

    LiteLLM does not hand the hook a turn counter, but every agent round-trip appends
    to the conversation, so the assistant-message count IS the turn number. Works for
    all three routes because each carries its history under a known key.
    """
    try:
        d = request_data or {}
        msgs = d.get("messages") or d.get("input") or []
        if isinstance(msgs, str):
            return 1
        n = sum(1 for m in msgs
                if isinstance(m, dict) and m.get("role") == "assistant")
        return n + 1
    except Exception:  # noqa: BLE001
        return None


def _route_of(request_data, hint=None):
    """Which API dialect this request arrived on. `hint` wins when the caller knows."""
    if hint:
        return hint
    d = request_data or {}
    if "input" in d:
        return "/v1/responses"
    return "/v1/chat/completions"


def _ctx(request_data, route=None):
    """Dimensions attached to every metric: route, model, turn."""
    d = request_data or {}
    return {"route": _route_of(d, route), "model": d.get("model"),
            "turn": _turn_of(d)}


def _reject(reason, tool=None, ctx=None):
    """Record a rejected repair candidate and return None (the caller's sentinel)."""
    record("rejected", reason=reason, tool=tool, **(ctx or {}))
    return None


def counters():
    """Snapshot for tests/introspection: {(outcome, reason, tool): n}."""
    return dict(_COUNTS)

# ── Format recognisers ──────────────────────────────────────────────────────
FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_RE = re.compile(r"`[^`\n]*`")
# Closing tags are REQUIRED — this is what rejects truncated calls.
# The closing tag is an ALTERNATION because models mix the two dialects: observed
# output opened `<function=exec_command>` and closed `</tool_call>`. The old
# pattern required `</function>`, matched nothing, and the call was printed to the
# user as literal text instead of being repaired or rejected. `$` terminates a
# truncated emission so a cut-off call is still recoverable rather than silently
# dropped.
QWEN_FUNC_RE = re.compile(
    r"<function=([A-Za-z0-9_\-]+)\s*>(.*?)(?:</function>|</tool_call>|$)", re.S)
QWEN_PARAM_RE = re.compile(r"<parameter=([A-Za-z0-9_\-]+)\s*>(.*?)</parameter>", re.S)
JSON_BLOB_RE = re.compile(
    r'\{\s*"name"\s*:\s*"([A-Za-z0-9_\-]+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', re.S
)
# Cheap pre-filter: if none of these appear, skip all work.
HINTS = ("<function=", '"name"')


def _maybe_marker(text):
    """Streaming guard: could `text` still be growing into a tool marker?

    Markers arrive token-by-token ("<function", "=B", "ash", ">"), so a plain
    `"<function=" in text` test is too late — the first chunk is emitted as
    visible text before the "=" ever arrives, leaking a fragment to the user.
    Return True while the accumulated text either contains a marker OR is still
    a viable prefix of one, so the chunk is held back rather than shown.
    """
    t = text.lstrip()
    if not t:
        return False
    if any(h in t for h in HINTS):
        return True
    # A marker may begin mid-string (e.g. '{"na' growing into '{"name"'), so test
    # whether any TAIL of the text is still a viable prefix of a marker. Only the
    # last len(longest marker) characters can matter, so this stays O(1).
    window = max(len(h) for h in HINTS)
    tail = t[-window:]
    for i in range(len(tail)):
        suffix = tail[i:]
        if any(h.startswith(suffix) for h in HINTS):
            return True
    return False


def _strip_code(text):
    return INLINE_RE.sub("", FENCE_RE.sub("", text))


def _coerce(value, spec):
    """Coerce to the schema's declared type; never fail a call over coercion.

    Accepts ALREADY-TYPED values, not just strings. The Responses branch used to
    stringify every argument before validation, which corrupted arrays/bools/ints
    into their repr; it now passes raw JSON values through, so a bool arrives as a
    bool. A value that already matches its declared type is returned untouched.
    """
    t = (spec or {}).get("type")
    if not isinstance(value, str):
        # Already structured (from parsed JSON). Trust it if the type lines up,
        # and never call string methods on it.
        py = {"integer": int, "number": (int, float), "boolean": bool,
              "object": dict, "array": list, "string": str}.get(t)
        if py is None or isinstance(value, py):
            return value
        return value  # mismatched but structured: let the client reject it, don't mangle
    v = value.strip()
    try:
        if t == "integer":
            return int(v)
        if t == "number":
            return float(v) if "." in v else int(v)
        if t == "boolean":
            return v.lower() == "true"
        if t in ("object", "array"):
            return json.loads(v)
    except Exception:  # noqa: BLE001
        return v
    return v


def _index_tools(declared):
    """name -> function schema, from the tools the caller actually sent."""
    index = {}
    for t in declared or []:
        fn = t.get("function") if isinstance(t, dict) and "function" in t else t
        if isinstance(fn, dict) and fn.get("name"):
            index[fn["name"]] = fn
    return index


def _validate(name, args, tool_index):
    """Return an OpenAI tool_call dict, or None if the candidate must be rejected."""
    if name not in tool_index:
        return None  # hallucinated / unknown tool
    spec = tool_index[name]
    # OpenAI tools carry the schema under "parameters"; Anthropic /v1/messages
    # tools carry the SAME schema under "input_schema". Claude Code uses the
    # latter, so missing this key silently rejected every call on that route.
    params = spec.get("parameters") or spec.get("input_schema") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    clean = {k: _coerce(v, props.get(k)) for k, v in args.items() if k in props}
    if not required.issubset(clean.keys()):
        return None  # would require inventing arguments
    return {
        "id": "call_" + uuid.uuid4().hex[:8],
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(clean)},
    }


def recover(content, declared_tools, ctx=None):
    """(tool_calls | None, leftover_text). Pure function — unit-testable.

    `ctx` is metrics-only (route/model/turn) and never affects the decision.
    Without it the Anthropic and chat routes report nothing, making a rejection
    on those routes invisible.
    """
    if not content or not declared_tools:
        return None, content
    if not any(h in content for h in HINTS):
        # No tool syntax at all -> the model never attempted a call. Distinct from a
        # rejected attempt, and the two were previously indistinguishable.
        record("no_attempt", **(ctx or {}))
        return None, content
    tool_index = _index_tools(declared_tools)
    if not tool_index:
        return _reject("no_declared_tools", ctx=ctx), content

    scan = _strip_code(content)
    calls, spans = [], []

    for m in QWEN_FUNC_RE.finditer(scan):
        args = dict(QWEN_PARAM_RE.findall(m.group(2)))
        call = _validate(m.group(1), args, tool_index)
        if call:
            calls.append(call)
            spans.append(m.group(0))

    if not calls:  # second format: bare JSON blob
        for m in JSON_BLOB_RE.finditer(scan):
            try:
                raw = json.loads(m.group(2))
            except Exception:  # noqa: BLE001
                continue
            # Raw values, not str() — same corruption bug fixed in the Responses
            # branch: stringifying here turned arrays/bools/ints into their repr.
            call = _validate(m.group(1), raw, tool_index)
            if call:
                calls.append(call)
                spans.append(m.group(0))

    if not calls:
        # THIRD format: the whole reply is ONE fenced JSON tool call.
        #
        # _strip_code() deletes fenced blocks before scanning, deliberately —
        # fences usually hold tutorial examples, and executing those would run
        # commands the model never intended. That is still right.
        #
        # But it also deleted the case observed in a real Claude Code session,
        # where qwen3-coder's ENTIRE response was:
        #     ```json
        #     {"name": "Read", "arguments": {"file_path": "..."}}
        #     ```
        # A genuine call, dropped because of where it sat. The session stalled:
        # the model described a tool call and the client never executed one.
        #
        # The distinction that makes this safe is CONTEXT, not content: a
        # tutorial fence is surrounded by prose, whereas a real call IS the whole
        # reply. So this accepts a fenced call only when
        #   - there is exactly ONE fenced block, and
        #   - removing it leaves nothing but whitespace, and
        #   - the blob validates against a DECLARED tool.
        # An explanation with an example fence fails the second test and is left
        # alone, which is the behaviour the multi-fence support was rejected for.
        fences = FENCE_RE.findall(content)
        if len(fences) == 1 and not _strip_code(content).strip():
            body = fences[0].strip("`")
            if body.lower().startswith("json"):
                body = body[4:]
            for m in JSON_BLOB_RE.finditer(body):
                try:
                    raw = json.loads(m.group(2))
                except Exception:  # noqa: BLE001
                    continue
                call = _validate(m.group(1), raw, tool_index)
                if call:
                    calls.append(call)
            if calls:
                record("repaired", reason="sole_fenced_json",
                       tool=calls[0]["function"]["name"], **(ctx or {}))
                return calls, None

    if not calls:
        # Tool syntax was present but nothing survived validation.
        _reject("no_valid_call_in_text", ctx=ctx)
        return None, content

    record("repaired", tool=calls[0]["function"]["name"], **(ctx or {}))
    leftover = scan
    for s in spans:
        leftover = leftover.replace(s, "")
    leftover = leftover.replace("</tool_call>", "").replace("<tool_call>", "").strip()
    return calls, (leftover or None)



# ── /v1/responses (Codex) ───────────────────────────────────────────────────
# This route is NOT byte-oriented. Measured: the streaming iterator hook receives
# LiteLLM Pydantic events (OutputTextDeltaEvent, OutputItemDoneEvent, ...) and the
# SSE serializer runs AFTER the hook. So this branch mutates typed objects and
# constructs LiteLLM's own event classes — never hand-built dicts, never
# reconstructed `data:` lines. Porting the Anthropic byte approach here would have
# parsed nothing.
#
# The observed failure: Codex asks for a tool, the local model answers with a
# fenced JSON blob inside output_text, and zero function_call events are emitted,
# so the call never executes.

# The fence must be TERMINAL — nothing but whitespace may follow it. Leading prose
# is permitted and deliberately NOT inspected (see responses_fence_candidate).
TERMINAL_FENCE_RE = re.compile(
    r'```(?:json)?[ \t]*\n((?:(?!```).)*?)\n?[ \t]*```[ \t\r\n]*\Z', re.S)


def responses_fence_candidate(text, declared_tools, ctx=None):
    """Recover a tool call from output_text — Responses route only.

    Deliberately NOT `recover()`: that strips fenced blocks as a safety measure,
    which would delete the very content we need here. This is the narrow inverse,
    and it stays narrow on purpose — the whole output must be one fence and
    nothing else.

    ALLOW   leading prose followed by a terminal fence holding one complete,
            declared, schema-valid tool invocation
    REJECT  trailing content after the fence, multiple fences, multiple objects,
            malformed JSON, undeclared tools, invalid or missing required args

    Prose is NOT inspected. An earlier draft required a bare fence on the theory
    that "a fence wrapped in prose is an illustration" — but the two are
    structurally identical ("I will execute a command to read the file." vs
    "Here is what this would look like:"), so separating them means classifying
    intent from natural language. This layer is a capability adapter, not an
    intent classifier, and no reference implementation does that:

      vLLM's Llama tool parser "only extracts JSON content and ignores any
      surrounding plain text" (docs.vllm.ai/en/stable/features/tool_calling/)
      llama.cpp prevents the problem instead, via GBNF grammar-constrained
      decoding — the better answer, unavailable to us through Ollama+Responses
      llm-toolcall-proxy uses model-specific structured tags

    We remain stricter than vLLM: it needs neither a fence nor a single object.

    ACCEPTED TRADEOFF: a model that *illustrates* a declared tool with valid
    arguments in a terminal fence will have it executed. Bounded by the checks
    below — declared tools only, schema-valid, never invented arguments.

    Argument validation is delegated to _validate(), so the shared safety rules
    (declared tools only, schema-checked, never invent a required argument)
    remain authoritative.
    """
    if not text or not declared_tools:
        return _reject("no_text_or_tools", ctx=ctx)
    if text.count("```") > 2:
        return _reject("multiple_fences", ctx=ctx)
    m = TERMINAL_FENCE_RE.search(text)
    if not m:
        return _reject("no_terminal_fence", ctx=ctx)  # no fence, or text follows
    inner = m.group(1).strip()
    if not inner.startswith("{") or not inner.endswith("}"):
        return _reject("fence_not_json_object", ctx=ctx)
    try:
        obj = json.loads(inner)          # must be exactly one JSON object
    except Exception:                    # noqa: BLE001
        return _reject("malformed_json", ctx=ctx)
    if not isinstance(obj, dict):
        return _reject("not_an_object", ctx=ctx)
    name = obj.get("name")
    args = obj.get("arguments")
    if not isinstance(name, str) or not isinstance(args, dict):
        return _reject("missing_name_or_arguments", ctx=ctx)
    # Pass values RAW. _validate() -> _coerce() knows each argument's declared type;
    # stringifying here first turned arrays/bools/ints into their repr (prefix_rule
    # ["rg","grep"] became the string "['rg', 'grep']"), which the schema then had to
    # un-corrupt and could not. Measured on a real Codex exec_command payload.
    index = _index_tools(declared_tools)
    if name not in index:
        # Distinguished from a schema failure on purpose: an undeclared name means
        # the model invented a tool, a schema failure means it called a real one
        # wrongly. They need different responses.
        return _reject("undeclared_tool", tool=name, ctx=ctx)
    call = _validate(name, args, index)
    if not call:
        return _reject("schema_validation_failed", tool=name, ctx=ctx)
    record("repaired", tool=name, **(ctx or {}))
    return [call]


def _responses_function_call_events(call, output_index):
    """Build the event sequence LiteLLM emits for a genuine function call.

    Mirrors responses/streaming_iterator.py's function_call branch so Codex
    receives the same object stream it would have gotten natively.
    """
    from litellm.types.llms.openai import (
        FunctionCallArgumentsDeltaEvent,
        FunctionCallArgumentsDoneEvent,
        OutputItemAddedEvent,
        OutputItemDoneEvent,
        ResponsesAPIStreamEvents,
    )
    from litellm.types.llms.openai import BaseLiteLLMOpenAIResponseObject as Base

    fn = call["function"]
    args = fn["arguments"]
    item_id = "fc_" + uuid.uuid4().hex[:24]
    item = Base(**{
        "id": item_id,
        "call_id": call["id"],
        "type": "function_call",
        "name": fn["name"],
        "arguments": args,
        "status": "completed",
    })
    return [
        OutputItemAddedEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
            output_index=output_index,
            item=item,
        ),
        FunctionCallArgumentsDeltaEvent(
            type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DELTA,
            item_id=item_id, output_index=output_index, delta=args,
        ),
        FunctionCallArgumentsDoneEvent(
            type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DONE,
            item_id=item_id, output_index=output_index, arguments=args,
        ),
        OutputItemDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
            output_index=output_index, item=item,
        ),
    ]


def _responses_event_name(chunk):
    """Event discriminator, tolerant of enum-or-str typing."""
    t = getattr(chunk, "type", None)
    return getattr(t, "value", t) if t is not None else None


class ToolRepair(CustomLogger):
    """Registered via litellm_settings.callbacks: tool_repair.proxy_handler_instance"""

    @staticmethod
    def _repair_anthropic(data, response):
        """Repair the Anthropic /v1/messages shape.

        This is the route Claude Code actually uses. Its response has top-level
        `content` blocks and `stop_reason` — no `.choices` — so the OpenAI branch
        below never sees it. Missing this was why raw XML still reached the user
        in a real claude-local run even though /v1/chat/completions was clean.
        """
        blocks = getattr(response, "content", None)
        if blocks is None and isinstance(response, dict):
            blocks = response.get("content")
        if not isinstance(blocks, list):
            return False
        # Native tool_use already present -> leave the response alone.
        for b in blocks:
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype == "tool_use":
                return False

        new_blocks, found = [], False
        for b in blocks:
            is_dict = isinstance(b, dict)
            btype = b.get("type") if is_dict else getattr(b, "type", None)
            text = b.get("text") if is_dict else getattr(b, "text", None)
            if btype != "text" or not text:
                new_blocks.append(b)
                continue
            calls, leftover = recover(text, data.get("tools"), _ctx(data, "/v1/messages"))
            if not calls:
                new_blocks.append(b)
                continue
            found = True
            if leftover:
                new_blocks.append({"type": "text", "text": leftover})
            for c in calls:
                new_blocks.append({
                    "type": "tool_use",
                    "id": c["id"],
                    "name": c["function"]["name"],
                    "input": json.loads(c["function"]["arguments"]),
                })
        if not found:
            return False
        if isinstance(response, dict):
            response["content"] = new_blocks
            response["stop_reason"] = "tool_use"
        else:
            response.content = new_blocks
            try:
                response.stop_reason = "tool_use"
            except Exception:  # noqa: BLE001
                pass
        attribute(route="/v1/messages", stream=False, native=False, repaired=True)
        log.info("tool_repair: recovered tool_use block(s) on /v1/messages")
        return True

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        try:
            if AILOCAL_DEBUG:
                print(f"tool_repair: HOOK resp={type(response).__name__} "
                      f"tools={len(data.get('tools') or [])} "
                      f"has_choices={hasattr(response, 'choices')} "
                      f"has_content={hasattr(response, 'content') or (isinstance(response, dict) and 'content' in response)}",
                      flush=True)
            if self._repair_anthropic(data, response):
                return response
            choices = getattr(response, "choices", None) or []
            for choice in choices:
                msg = getattr(choice, "message", None)
                if msg is None or getattr(msg, "tool_calls", None):
                    continue  # native tool calls present -> never touch
                calls, leftover = recover(getattr(msg, "content", None), data.get("tools"),
                                      _ctx(data, "/v1/chat/completions"))
                if not calls:
                    continue
                msg.tool_calls = calls
                msg.content = leftover
                choice.finish_reason = "tool_calls"
                log.info("tool_repair: recovered %d tool call(s) from content", len(calls))
        except Exception as exc:  # noqa: BLE001 - never break a response
            log.error("tool_repair: non-streaming repair failed: %s", exc)
        return response

    @staticmethod
    def _sse(event_type, payload):
        return (f"event: {event_type}\ndata: {json.dumps(payload)}\n\n").encode()

    @classmethod
    def _anthropic_sse_repair(cls, raw, state, tools):
        """Recover malformed tool calls on the Anthropic SSE byte stream.

        WHY BYTES: there is no structured callback seam before SSE serialization.
        adapters/handler.py:600 goes straight from litellm.acompletion() into
        ANTHROPIC_ADAPTER.translate_completion_output_params_streaming(), which
        returns async_anthropic_sse_wrapper() — already encoded. The proxy's
        async_post_call_streaming_iterator_hook then receives bytes. Repairing
        here is therefore the only option that does not fork LiteLLM internals.
        (Upstream fix: add a normalization seam on `completion_stream` BEFORE the
        adapter, so callbacks can operate on ModelResponseStream chunks.)

        STRATEGY: buffer a text content block start->stop, then decide. If the
        accumulated text recovers into tool calls, emit synthetic tool_use events
        in its place; otherwise flush the buffered events verbatim so ordinary
        prose streams untouched. Each yielded chunk is exactly one complete SSE
        event (streaming_iterator.py:793), so no partial-event handling is needed.

        Returns a list of byte chunks to emit (possibly empty while buffering).
        """
        try:
            text = raw.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return [raw]

        # A real tool_use block means the runtime parsed it correctly -> stand down
        # for the rest of the stream.
        if '"type": "tool_use"' in text or '"type":"tool_use"' in text:
            if not state.get("native"):
                attribute(route="/v1/messages", stream=True, native=True, repaired=False,
                          model=state.get("model"), tools=state.get("ntools"))
            state["native"] = True
            state["saw_tool_use"] = True
            return [raw]
        if state.get("native"):
            return [raw] if "message_delta" not in text else [raw]

        try:
            payload = json.loads(text.split("data:", 1)[1].strip())
        except Exception:  # noqa: BLE001
            return [raw]
        etype = payload.get("type")

        if etype == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "text":
                state["buffering"] = True
                state["buf"] = [raw]
                state["accum"] = ""
                state["index"] = payload.get("index", 0)
                return []
            return [raw]

        if etype == "content_block_delta" and state.get("buffering"):
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                state["accum"] += delta.get("text") or ""
            state["buf"].append(raw)
            return []

        if etype == "content_block_stop" and state.get("buffering"):
            state["buffering"] = False
            buf = state["buf"] + [raw]
            calls, leftover = recover(state.get("accum") or "", tools, state.get("ctx"))
            if not calls:
                return buf  # ordinary text — emit exactly as received
            idx = state.get("index", 0)
            out = []
            if leftover:  # preserve any genuine prose alongside the call
                out.append(cls._sse("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""}}))
                out.append(cls._sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": leftover}}))
                out.append(cls._sse("content_block_stop",
                                    {"type": "content_block_stop", "index": idx}))
                idx += 1
            for c in calls:
                fn = c["function"]
                out.append(cls._sse("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "tool_use", "id": c["id"],
                                      "name": fn["name"], "input": {}}}))
                out.append(cls._sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "input_json_delta",
                              "partial_json": fn["arguments"]}}))
                out.append(cls._sse("content_block_stop",
                                    {"type": "content_block_stop", "index": idx}))
                idx += 1
            state["saw_tool_use"] = True
            state["injected"] = True
            attribute(route="/v1/messages", stream=True, native=False, repaired=True,
                      model=state.get("model"), tools=state.get("ntools"),
                      tool_names=[c["function"]["name"] for c in calls], n_calls=len(calls))
            log.info("tool_repair: injected %d tool_use block(s) into SSE stream", len(calls))
            if AILOCAL_DEBUG:
                print(f"tool_repair: SSE injected {len(calls)} tool_use block(s)", flush=True)
            return out

        return [raw]

    @staticmethod
    def _fix_anthropic_sse_stop_reason(raw, state):
        """Repair stop_reason on the Anthropic SSE byte stream.

        THE BUG (LiteLLM, 100% reproducible on Ollama streaming):

            ollama chunk 0:  done=false, tool_calls=[...]
            ollama chunk 1:  done=true,  done_reason="stop", tool_calls=None

        llms/ollama/chat/transformation.py only applies its tool_calls override
        when tool_calls are present in the SAME chunk as done=True:

            if chunk["done"] is True:
                finish_reason = chunk.get("done_reason") or "stop"
                if tool_calls is not None:          # <- None here, always
                    finish_reason = "tool_calls"

        Ollama never puts them there, so finish_reason stays "stop", the Anthropic
        adapter faithfully maps stop -> end_turn, and the client sees a turn that
        contains a tool_use block but claims to be finished. Claude Code therefore
        does NOT execute the tool — the "hang until I type continue" symptom.

        The existing override cites LiteLLM #18922, which it only fixes for the
        same-chunk case; it misses Ollama's chunk ordering entirely.

        This is provider-agnostic: it keys off a tool_use block actually appearing
        in the stream, not off any model or provider name. It rewrites exactly one
        field, on exactly one event, and only when a tool_use block was seen.
        Each yielded chunk is one complete SSE event (see
        adapters/streaming_iterator.py:793), so no cross-chunk buffering is needed.

        Returns replacement bytes, or None to pass the chunk through untouched.
        """
        try:
            text = raw.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
        if '"type": "tool_use"' in text or '"type":"tool_use"' in text:
            state["saw_tool_use"] = True
            return None
        if "message_delta" not in text or not state.get("saw_tool_use"):
            return None
        # Only rewrite a terminal stop_reason that contradicts the tool_use block.
        for wrong in ('"stop_reason": "end_turn"', '"stop_reason":"end_turn"'):
            if wrong in text:
                fixed = text.replace(wrong, wrong.replace("end_turn", "tool_use"))
                log.info("tool_repair: corrected stop_reason end_turn -> tool_use")
                if AILOCAL_DEBUG:
                    print("tool_repair: STOP_REASON end_turn -> tool_use", flush=True)
                return fixed.encode("utf-8")
        return None

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        """Buffer suspected tool syntax rather than streaming it as visible text.

        Buffering is correct, not a compromise: a tool call is EXECUTED, not read,
        so emitting it token-by-token has no user value and leaking half a call as
        assistant text is exactly the bug we are fixing.

        Also repairs stop_reason on the Anthropic SSE byte path — see
        _fix_anthropic_sse_stop_reason. That path delivers pre-serialized bytes,
        so structured repair is impossible here, but a targeted field rewrite is
        both possible and safe.
        """
        tools = (request_data or {}).get("tools")
        # Responses-route state. Kept separate from the byte-oriented sse_state so
        # the two branches cannot interfere.
        rs = {"buf": [], "text": "", "native": False, "index": 0, "done": False}
        sse_state = {"saw_tool_use": False,
                     "ctx": _ctx(request_data, "/v1/messages"),
                     "model": (request_data or {}).get("model"),
                     "ntools": len(tools or [])}
        if AILOCAL_DEBUG:
            print(f"tool_repair: STREAM hook entered tools={len(tools or [])}", flush=True)
        _dbg_shapes = set()
        buffered = []          # chunks held back while a call may be forming
        text = ""              # accumulated visible text
        suspect = False
        native_seen = False
        # LiteLLM calls this hook expecting a stream, but for some /v1/responses
        # requests it hands back an already-materialized ResponsesAPIResponse
        # (not async-iterable). `async for` on that raised immediately, the
        # broad except below swallowed it, and the caller got an EMPTY stream —
        # no content, no completion signal — every single turn. That silently
        # stalled agentic clients (codex-local looped, re-issuing the same
        # request every ~10-30s, forever). Nothing here needs repairing on an
        # already-complete object; pass it through unchanged.
        if not hasattr(response, "__aiter__"):
            log.warning("tool_repair: got non-streaming %s in streaming hook — passthrough", type(response).__name__)
            yield response
            return
        try:
            async for chunk in response:
                # ── /v1/responses: typed events, handled before anything else ──
                ev = _responses_event_name(chunk)
                if isinstance(ev, str) and ev.startswith("response."):
                    if rs["done"]:
                        yield chunk
                        continue
                    # A genuine function call means the runtime got it right.
                    item = getattr(chunk, "item", None)
                    if getattr(item, "type", None) == "function_call" or "function_call" in ev:
                        rs["native"] = True
                        attribute(route="/v1/responses", stream=True, native=True,
                                  repaired=False, model=sse_state.get("model"),
                                  tools=sse_state.get("ntools"))
                    if rs["native"] or not tools:
                        yield chunk
                        continue

                    if ev == "response.output_text.delta":
                        rs["text"] += getattr(chunk, "delta", "") or ""
                        rs["buf"].append(chunk)
                        continue
                    if ev in ("response.output_item.added", "response.content_part.added",
                              "response.content_part.done"):
                        rs["buf"].append(chunk)
                        rs["index"] = getattr(chunk, "output_index", rs["index"]) or 0
                        continue
                    if ev == "response.output_text.done":
                        rs["buf"].append(chunk)
                        calls = responses_fence_candidate(rs["text"], tools,
                                                          _ctx(request_data, "/v1/responses"))
                        if calls:
                            rs["done"] = True
                            attribute(route="/v1/responses", stream=True, native=False,
                                      repaired=True, model=sse_state.get("model"),
                                      tools=sse_state.get("ntools"),
                                      tool_names=[c["function"]["name"] for c in calls],
                                      n_calls=len(calls))
                            log.info("tool_repair: recovered %d Responses tool call(s)", len(calls))
                            if AILOCAL_DEBUG:
                                print(f"tool_repair: RESPONSES repaired {len(calls)} call(s)", flush=True)
                            for out in _responses_function_call_events(calls[0], rs["index"]):
                                yield out
                        else:
                            for c in rs["buf"]:      # ordinary text — release verbatim
                                yield c
                        rs["buf"] = []
                        continue
                    # anything else (in_progress, completed, ...) passes through,
                    # after flushing whatever text was buffered.
                    for c in rs["buf"]:
                        yield c
                    rs["buf"] = []
                    yield chunk
                    continue

                delta = None
                try:
                    delta = chunk.choices[0].delta
                except Exception:  # noqa: BLE001
                    # Anthropic SSE route delivers pre-serialized bytes. Structured
                    # repair is impossible here, but the stop_reason field can be
                    # corrected — and that is the bug that actually stalls the loop.
                    if isinstance(chunk, (bytes, bytearray)):
                        for out in self._anthropic_sse_repair(bytes(chunk), sse_state, tools):
                            fixed = self._fix_anthropic_sse_stop_reason(out, sse_state)
                            yield fixed if fixed is not None else out
                        continue
                    if AILOCAL_DEBUG:
                        t = type(chunk).__name__
                        ev = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
                        if (t, ev) not in _dbg_shapes:
                            _dbg_shapes.add((t, ev))
                            print(f"tool_repair: STREAM passthrough chunk={t} event={ev}", flush=True)
                    yield chunk
                    continue

                if getattr(delta, "tool_calls", None):
                    if not native_seen:
                        attribute(route="/v1/chat/completions", stream=True, native=True,
                                  repaired=False, model=sse_state.get("model"),
                                  tools=sse_state.get("ntools"))
                    native_seen = True  # runtime parsed it correctly; stay out of the way
                    sse_state["saw_tool_use"] = True

                # Same finish_reason bug on the OpenAI streaming shape: Ollama emits
                # tool_calls in an earlier chunk than done=True, so the transformation's
                # same-chunk override never fires and the terminal chunk says "stop".
                # Measured directly: a streamed Bash call arrived with finish_reason=stop.
                try:
                    ch0 = chunk.choices[0]
                    if (
                        sse_state["saw_tool_use"]
                        and getattr(ch0, "finish_reason", None) == "stop"
                        and not getattr(delta, "content", None)
                    ):
                        ch0.finish_reason = "tool_calls"
                        log.info("tool_repair: corrected finish_reason stop -> tool_calls")
                except Exception:  # noqa: BLE001
                    pass

                piece = getattr(delta, "content", None) or ""
                if native_seen or not tools:
                    yield chunk
                    continue

                text += piece
                buffered.append(chunk)
                if _maybe_marker(text):
                    suspect = True           # hold back — may be (part of) a tool call
                else:
                    # Provably not a tool marker: release everything held so far
                    # and resume normal pass-through streaming.
                    suspect = False
                    for c in buffered:
                        yield c
                    buffered = []

            if suspect and not native_seen:
                calls, leftover = recover(text, tools, _ctx(request_data, "/v1/chat/completions"))
                if calls:
                    last = buffered[-1]
                    d = last.choices[0].delta
                    d.content = leftover
                    d.tool_calls = calls
                    last.choices[0].finish_reason = "tool_calls"
                    attribute(route="/v1/chat/completions", stream=True, native=False,
                              repaired=True, model=sse_state.get("model"),
                              tools=sse_state.get("ntools"),
                              tool_names=[c["function"]["name"] for c in calls], n_calls=len(calls))
                    log.info("tool_repair: recovered %d tool call(s) from stream", len(calls))
                    yield last
                else:
                    for c in buffered:       # not a tool call after all — release verbatim
                        yield c
        except Exception as exc:  # noqa: BLE001
            log.error("tool_repair: streaming repair failed: %s", exc)
            for c in buffered:
                yield c


proxy_handler_instance = ToolRepair()
