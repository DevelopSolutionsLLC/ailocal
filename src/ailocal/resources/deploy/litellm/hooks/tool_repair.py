"""
tool_repair.py — generic recovery of malformed tool calls from local models.

When a model omits the opening <tool_call> tag, Ollama's parser hands the whole
call back as plain assistant text; the client waits for a tool_call that never
arrives and the loop stalls. UNFIXED upstream: ollama/ollama#16686, with #16693
open and stale and #16732 closed unmerged. block/goose#6883 is the same bug in
another framework, fixed the same client-side way.

Model-agnostic by construction: it recognises FORMATS, never model names. Keep
it even if #16686 lands — it costs ~0.1-0.2 ms and never fires when native tool
calls work. [REAL] affected models go from mostly-failing to 8/8 tool calls.

SAFETY MODEL
------------
Fabricating a call the model never made is the dangerous failure, so every rule
biases toward doing nothing:
  - only runs when the response carries NO native tool_calls
  - fenced/inline code is stripped first, so documentation examples that merely
    *show* tool syntax can never become executable calls
  - incomplete calls are rejected (a closing boundary is required) so a
    truncated command is never executed
  - the tool name must appear in the tools the caller actually declared
  - arguments are validated against the declared JSON schema; unknown keys are
    dropped and missing REQUIRED keys cause the call to be rejected outright
  - arguments are never invented or defaulted

SECOND DEFECT: a NATIVE call missing one UI-metadata argument
-------------------------------------------------------------
Everything above concerns a call the runtime failed to parse at all. There is a
second, disjoint defect: the runtime parses the call perfectly and the MODEL
omits a required argument. See complete_ui_metadata() for the measurement, the
rule, and why exactly one (tool, argument) pair is eligible.

The "arguments are never invented" rule above still holds for recover(): that
path may not fabricate, because there the whole call is in doubt. This one is
narrower in every dimension — the call is native and already trusted, the tool
is named explicitly, and the fabricated value is derived from an argument the
model DID supply — so it is a schema-compatibility normalisation, not inference.
"""

import collections
import json
import logging
import os
import re
import uuid

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("tool_repair")

# print(), not log.info(): LiteLLM filters third-party loggers, so log.info() is
# invisible in `docker logs`. AILOCAL_TOOL_REPAIR_DEBUG=1 enables it.
AILOCAL_DEBUG = os.environ.get("AILOCAL_TOOL_REPAIR_DEBUG") == "1"


def attribute(**fields):
    """One machine-parseable line per tool-bearing response: native tool call,
    or rescued here? Silent unless AILOCAL_TOOL_REPAIR_DEBUG=1."""
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


# ── Missing UI-metadata argument on a NATIVE tool call ──────────────────────
# Eligible (tool, argument) pairs. Deliberately a hardcoded allowlist and not a
# heuristic over the schema: "which required arguments are safe to fabricate" is
# not answerable from a JSON schema, so it is answered here, per tool, by a human
# who checked what the argument does.
FABRICABLE = {("Agent", "description")}

_WS_RE = re.compile(r"\s+")
LABEL_WORDS = 5
LABEL_CHARS = 60


def _short_label(prompt):
    """First few words of the prompt, as a bounded, deterministic task label.

    Chosen over a fixed per-subagent string ("Explore repository") because
    subagent types are user-defined and open-ended, so any fixed mapping is
    wrong for the types it has never heard of. Chosen over the whole prompt
    because the schema asks for 3-5 words.

    Discloses nothing new: the prompt is already rendered in the client's UI
    next to this label, so no content moves anywhere it was not already going.
    """
    words = _WS_RE.sub(" ", prompt).strip().split(" ")
    label = " ".join(words[:LABEL_WORDS])[:LABEL_CHARS].strip()
    return label or "Delegated agent task"


def complete_ui_metadata(name, args_text, declared_tools, ctx=None):
    """Add ONE missing UI-metadata argument to a native tool call's arguments.

    Returns the patched arguments as a JSON string, or None to leave the call
    exactly as the model emitted it. `args_text` is the raw JSON the runtime
    parsed out of the model's call.

    THE DEFECT. gemma4 receives Claude Code's `Agent` schema, can recite that
    `description` is required, and omits it anyway. Measured against Ollama
    0.32.13 (latest stable) with the production sampling parameters and the
    real captured schema: 10/10 calls omitted it on gemma4:26b-mlx AND 10/10 on
    non-MLX gemma4:26b, while a control tool with two required string arguments
    omitted 0/10. In real claude-local sessions: 8 of 17 Agent calls omitted it,
    and those 8 are 8 of the 9 DISTINCT sessions that delegated at all — the
    remaining 9 calls are one session that got it right and then repeated its
    own correct example. So it is close to once per fresh delegation.
    Independently reproduced off Ollama entirely, on vLLM: vllm-project/vllm
    #39072 (open, gemma4 omits a required `path`). So the defect is the model,
    not the runtime — confirmed by asking gemma4 to recite the schema it was
    given, which it does correctly, `description` and all.

    WHY REPAIR RATHER THAN LET THE CLIENT RETRY. Claude Code does recover: it
    returns an InputValidationError and the model retries correctly. Measured
    cost of one such recovery: 18.0 s wall, a discarded 14,474-token prefill,
    252 discarded output tokens, and a user-visible "Invalid tool parameters"
    line — on every fresh delegation, deterministically at temperature 0.1.

    WHY `description` IS FABRICABLE. It is UI metadata: "A short (3-5 word)
    description of the task". `subagent_type` selects the agent and `prompt`
    is the work; `description` steers nothing. Fabricating it cannot change
    what the agent does. No other argument of any other tool has been shown to
    have that property, so no other argument is eligible.

    WHY IT CANNOT DRIFT. The missing-required set must be EXACTLY the one
    eligible name. If Claude Code adds a required `Agent` field tomorrow and
    gemma4 omits that too, the missing set becomes two names, no rule matches,
    and the call goes to the client unrepaired — a visible error, which is the
    correct outcome for a field nobody has vetted.

    RETIREMENT. Deliberately not tied to a version number — measure, do not
    assume a release fixed it. Delete this function, its call sites and the
    `native-args` suite when this returns `description` on every attempt:

        POST http://127.0.0.1:11434/api/chat
        {"model": <the configured model>, "stream": false,
         "options": {"temperature": 0.1, "top_p": 0.9, "top_k": 20},
         "tools": [<Claude Code's Agent tool, verbatim>],
         "messages": [{"role": "user", "content":
             "Use the Explore subagent to find where retry logic lives in this
              repository. Delegate it, do not search yourself."}]}

    Baseline for comparison: 0/10 attempts included `description` on both
    gemma4:26b-mlx and gemma4:26b under ollama 0.32.13. The same result also
    follows if the runtime gains schema-constrained tool arguments, since the
    omission then becomes impossible to emit.
    """
    if not args_text or not declared_tools or not isinstance(name, str):
        return None
    eligible = {arg for tool, arg in FABRICABLE if tool == name}
    if not eligible:
        return None                       # not a tool we will ever complete
    try:
        args = json.loads(args_text)
    except Exception:                     # noqa: BLE001 - malformed JSON is not ours
        return _reject("native_args_not_json", tool=name, ctx=ctx)
    if not isinstance(args, dict):
        return _reject("native_args_not_object", tool=name, ctx=ctx)

    index = _index_tools(declared_tools)
    spec = index.get(name)
    if spec is None:
        return _reject("undeclared_tool", tool=name, ctx=ctx)
    params = spec.get("parameters") or spec.get("input_schema") or {}
    props = params.get("properties") or {}
    missing = set(params.get("required") or []) - set(args)
    # EXACTLY the eligible argument — never a superset, never a different one.
    if missing != eligible:
        return None
    field = next(iter(missing))
    if (props.get(field) or {}).get("type") != "string":
        return _reject("field_not_a_string", tool=name, ctx=ctx)

    # Every OTHER argument the model did supply must itself be well-formed.
    # A call that is broken in some second way is not a call we complete.
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _reject("no_usable_prompt", tool=name, ctx=ctx)
    subagent = args.get("subagent_type", None)
    if subagent is not None and (not isinstance(subagent, str) or not subagent.strip()):
        return _reject("malformed_subagent_type", tool=name, ctx=ctx)

    patched = dict(args)
    patched[field] = _short_label(prompt)
    record("repaired", reason="missing_ui_metadata", tool=name, **(ctx or {}))
    log.info("tool_repair: completed missing `%s` on native %s call", field, name)
    return json.dumps(patched)


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

    Prose is NOT inspected. "I will execute a command" and "here is what this
    would look like" are structurally identical, so separating them means
    classifying intent from natural language — this is a capability adapter, not
    an intent classifier. vLLM's Llama parser does the same and is looser still
    (it needs neither a fence nor a single object).

    ACCEPTED TRADEOFF: a model that ILLUSTRATES a declared tool with valid
    arguments in a terminal fence will have it executed. Bounded by _validate():
    declared tools only, schema-checked, never an invented argument.
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


def _responses_stream_from_complete(response, request_data=None):
    """Yield a valid /v1/responses event stream for an already-completed response.

    The event sequence is BUILT BY LITELLM, not by us. _build_synthetic_response_events
    is the same function behind MockResponsesAPIStreamingIterator (o1-pro and other
    non-streaming models) and CachedResponsesAPIStreamingIterator (cache hits), so
    de-streamed responses leave here in exactly the shape every other de-streamed
    LiteLLM path already produces -- including the content_part, output_text.delta,
    function_call_arguments.delta/done and reasoning_summary events that a
    hand-rolled created/completed pair silently drops. Hand-rolling this was the
    first attempt and it broke tool calls.

    That function is private, so its absence is handled rather than assumed: the
    fallback emits the minimal created/item/completed sequence, which is degraded
    (no deltas, no tool-call arguments) but still a valid stream.

    The iterator classes wrapping it are deliberately NOT reused. Both label the
    call with custom_llm_provider="cached_response" and set
    _completed_response_cache_hit, which would report a real, freshly executed
    web-search turn as a cache hit and corrupt spend tracking.
    """
    logging_obj = (request_data or {}).get("litellm_logging_obj")
    status = getattr(response, "status", None)

    def _terminal_override():
        """LiteLLM always terminates with response.completed, even for a failed
        response. Announcing a failure as a completion is wrong in a way a client
        cannot detect, so the terminal event is corrected here. This is the only
        deliberate divergence from the vendor sequence."""
        if status not in ("failed", "incomplete"):
            return None
        from litellm.types.llms.openai import (
            ResponseFailedEvent,
            ResponseIncompleteEvent,
        )
        if status == "failed":
            return ResponseFailedEvent(type="response.failed", response=response)
        return ResponseIncompleteEvent(type="response.incomplete", response=response)

    try:
        from litellm.responses.streaming_iterator import (
            _build_synthetic_response_events,
            MockResponsesAPIStreamingIterator,
        )
        events = _build_synthetic_response_events(
            transformed=response,
            logging_obj=logging_obj,
            chunk_size=MockResponsesAPIStreamingIterator.CHUNK_SIZE,
        )
        override = _terminal_override()
        if override is not None and events:
            events = list(events[:-1]) + [override]
        for event in events:
            yield event
        return
    except Exception as exc:  # pragma: no cover - depends on the installed LiteLLM
        log.warning(
            "tool_repair: LiteLLM synthetic event builder unavailable (%s) — "
            "emitting the minimal fallback sequence", exc,
        )

    def _plain(obj):
        return obj.model_dump() if hasattr(obj, "model_dump") else obj

    try:
        opening = response.model_copy(update={"output": []})
    except Exception:  # pragma: no cover - non-pydantic payload
        opening = response
    yield {"type": "response.created", "response": _plain(opening)}
    for index, item in enumerate(getattr(response, "output", None) or []):
        for name in ("added", "done"):
            yield {"type": f"response.output_item.{name}",
                   "output_index": index, "item": _plain(item)}
    terminal = "completed" if status not in ("failed", "incomplete") else status
    yield {"type": f"response.{terminal}", "response": _plain(response)}


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
        # Native tool_use already present -> the runtime parsed the call, so the
        # text-recovery path below has nothing to do. The arguments may still be
        # missing a UI-metadata field; that is the only thing touched here.
        native = False
        for b in blocks:
            is_dict = isinstance(b, dict)
            btype = b.get("type") if is_dict else getattr(b, "type", None)
            if btype != "tool_use":
                continue
            native = True
            args = b.get("input") if is_dict else getattr(b, "input", None)
            name = b.get("name") if is_dict else getattr(b, "name", None)
            if not isinstance(args, dict):
                continue
            patched = complete_ui_metadata(name, json.dumps(args), data.get("tools"),
                                           _ctx(data, "/v1/messages"))
            if patched is None:
                continue
            if is_dict:
                b["input"] = json.loads(patched)
            else:
                try:
                    b.input = json.loads(patched)
                except Exception:  # noqa: BLE001
                    pass
        if native:
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
    def _native_tool_use_events(cls, ts, args_text):
        """Re-emit one buffered tool_use block, with `args_text` as its input."""
        idx = ts.get("index", 0)
        return [
            cls._sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": ts.get("id"),
                                  "name": ts.get("name"), "input": {}}}),
            cls._sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": args_text}}),
            cls._sse("content_block_stop", {"type": "content_block_stop", "index": idx}),
        ]

    @classmethod
    def _anthropic_native_arg_repair(cls, text, state, tools):
        """Complete a missing UI-metadata argument on a NATIVE streamed tool_use.

        Returns the byte chunks to emit, or None for "not my event" so the
        caller's own handling runs unchanged.

        Buffers one tool_use block start->stop because the arguments arrive as
        input_json_delta fragments and the decision needs the whole object. That
        costs nothing a user can perceive: a tool call is executed, not read —
        the same reasoning the streaming hook already documents for text.
        """
        try:
            payload = json.loads(text.split("data:", 1)[1].strip())
        except Exception:  # noqa: BLE001 - not an SSE data event
            return None
        if not isinstance(payload, dict):
            return None
        etype = payload.get("type")
        ts = state.setdefault("native_tool", {})

        if etype == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") != "tool_use":
                return None               # text block: caller's buffering owns it
            ts.clear()
            ts.update(active=True, index=payload.get("index", 0), id=block.get("id"),
                      name=block.get("name"), args="")
            # The runtime parsed a call: the text-recovery path must stand down,
            # exactly as it would have on seeing this event itself.
            state["native"] = True
            state["saw_tool_use"] = True
            return []                     # hold until the arguments are known

        if not ts.get("active"):
            return None

        if etype == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") != "input_json_delta":
                return None
            ts["args"] += delta.get("partial_json") or ""
            return []

        if etype == "content_block_stop":
            ts["active"] = False
            patched = complete_ui_metadata(ts.get("name"), ts["args"], tools,
                                           state.get("ctx"))
            if patched is not None:
                state["completed_args"] = True
                if AILOCAL_DEBUG:
                    print(f"tool_repair: COMPLETED missing arg on {ts.get('name')}",
                          flush=True)
            return cls._native_tool_use_events(ts, patched or ts["args"])

        return None

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

        # A native tool_use block may still be missing a required argument. That
        # is a different defect from everything below, so it is decided first;
        # the helper returns None for every event it does not own.
        completed = cls._anthropic_native_arg_repair(text, state, tools)
        if completed is not None:
            return completed

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

        UPSTREAM BUG, 100% reproducible on Ollama streaming. Ollama emits
        tool_calls in one chunk and done=true in the NEXT, but
        llms/ollama/chat/transformation.py only applies its tool_calls override
        when both land in the same chunk (the LiteLLM #18922 fix). So
        finish_reason stays "stop", the Anthropic adapter maps it to end_turn,
        and the client sees a turn holding a tool_use block that claims to be
        finished — Claude Code does not execute the tool.

        Provider-agnostic: it keys off a tool_use block appearing in the stream,
        never a model or provider name, and rewrites exactly one field on
        exactly one event. Each yielded chunk is one complete SSE event, so no
        cross-chunk buffering is needed.

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
        # websearch_interception (LiteLLM's own callback, enabled in our config)
        # converts stream=True -> stream=False internally, runs its agentic search
        # loop, and returns a materialized response. Its ARCHITECTURE.md states
        # this outright: "Converts stream=True -> stream=False internally ...
        # Final response returned to user (non-streaming)".
        #
        # On /v1/messages LiteLLM re-wraps that result in a
        # FakeAnthropicMessagesStreamIterator, so the client still receives a
        # stream. On /v1/responses that re-wrap is MISSING: the hook is handed a
        # bare ResponsesAPIResponse that is not async-iterable.
        #
        # Neither previous behaviour was correct. `async for` on it raised, the
        # broad except swallowed the error, and the caller got an EMPTY stream;
        # yielding it unchanged emits a single SSE frame containing a whole
        # `"object": "response"` instead of typed events. Either way no client
        # ever sees response.completed, so agentic clients retry forever
        # (measured: codex-local, 10/10 reconnects, exponential backoff).
        #
        # REPRO   curl /v1/responses -d '{"stream":true,
        #         "tools":[{"type":"web_search"}], ...}'  -- the built-in tool
        #         type is the whole trigger; function and custom tools stream fine.
        # RETIRE  when websearch_interception re-wraps on the responses path as
        #         it already does on the Anthropic one. Re-run the REPRO: if it
        #         emits response.completed with this branch removed, delete it.
        #
        # Supply the re-wrap LiteLLM omits, using LiteLLM's OWN event builder so
        # the emitted stream matches every other de-streamed path it serves.
        # Nothing here needs repairing on an already-complete object, so no repair
        # is attempted.
        if not hasattr(response, "__aiter__"):
            log.warning(
                "tool_repair: %s is not async-iterable in the streaming hook "
                "(websearch_interception de-streamed this request) — "
                "synthesizing created/completed events",
                type(response).__name__,
            )
            for event in _responses_stream_from_complete(response, request_data):
                yield event
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

            # A stream that ended mid tool_use block leaves the completion
            # buffer holding events nobody will ever close. Emit them as
            # received: a truncated call the client rejects is a visible
            # failure, and swallowing it silently would be a worse one.
            ts = sse_state.get("native_tool") or {}
            if ts.get("active"):
                ts["active"] = False
                log.warning("tool_repair: stream ended inside a tool_use block — "
                            "flushing it unrepaired")
                for out in self._native_tool_use_events(ts, ts.get("args") or ""):
                    yield out

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
