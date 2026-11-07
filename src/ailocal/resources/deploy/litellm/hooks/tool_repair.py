"""Recover malformed tool calls from local models, by format, never by name.

Ollama returns a call missing its <tool_call> tag as plain text, stalling the
loop (ollama/ollama#16686, unfixed). Fabricating a call is the dangerous
failure, so: native calls win, code fences are stripped, closing boundaries
are required, tools must be declared, and arguments are schema-checked and
never invented. complete_ui_metadata() is a separate, narrower native repair.
"""

from __future__ import annotations

import collections
import copy
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("tool_repair")

# print(), not log.info(): LiteLLM filters third-party loggers.
AILOCAL_DEBUG = os.environ.get("AILOCAL_TOOL_REPAIR_DEBUG") == "1"


def attribute(**fields: object) -> None:
    """Print one parseable native-or-rescued line; only if debug is on."""
    if not AILOCAL_DEBUG:
        return
    try:
        print("AILOCAL_ATTRIB " + json.dumps(fields, default=str), flush=True)
    except Exception:  # noqa: BLE001 - telemetry must never break a response
        pass


# Never debug-gated: else "never fired" and "fired, ignored" look identical.
_COUNTS = collections.Counter()


def record(outcome: str, reason: str | None = None, tool: str | None = None,
           route: str | None = None, model: object = None,
           turn: int | None = None) -> None:
    """Count one repair decision: attempted|repaired|rejected|native."""
    try:
        _COUNTS[(outcome, reason or "-", tool or "-")] += 1
        print("tool_repair_metric " + json.dumps({
            "outcome": outcome, "reason": reason or "-", "tool": tool or "-",
            "route": route or "-", "model": model or "-", "turn": turn,
            "total": _COUNTS[(outcome, reason or "-", tool or "-")],
        }, default=str), flush=True)
    except Exception:  # noqa: BLE001 - telemetry must never break a response
        pass


def _turn_of(request_data: dict | None) -> int | None:
    """Return the turn index: assistant messages in the history, plus one."""
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


def _route_of(request_data: dict | None, hint: str | None = None) -> str:
    """Return the API route of the request; `hint` wins when given."""
    if hint:
        return hint
    d = request_data or {}
    if "input" in d:
        return "/v1/responses"
    return "/v1/chat/completions"


def _ctx(request_data: dict | None, route: str | None = None) -> dict[str, Any]:
    """Return the metric dimensions: route, model, turn."""
    d = request_data or {}
    return {"route": _route_of(d, route), "model": d.get("model"),
            "turn": _turn_of(d)}


def _reject(reason: str, tool: str | None = None,
            ctx: dict[str, Any] | None = None) -> None:
    """Record a rejected repair candidate and return None."""
    record("rejected", reason=reason, tool=tool, **(ctx or {}))
    return None


def counters() -> dict[tuple[str, str, str], int]:
    """Return a snapshot {(outcome, reason, tool): n}."""
    return dict(_COUNTS)

FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_RE = re.compile(r"`[^`\n]*`")
# Closer alternation: models mix dialects (`<function=…>` … `</tool_call>`).
QWEN_FUNC_RE = re.compile(
    r"<function=([A-Za-z0-9_\-]+)\s*>(.*?)(?:</function>|</tool_call>|$)", re.S)
QWEN_PARAM_RE = re.compile(r"<parameter=([A-Za-z0-9_\-]+)\s*>(.*?)</parameter>", re.S)
JSON_BLOB_RE = re.compile(
    r'\{\s*"name"\s*:\s*"([A-Za-z0-9_\-]+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', re.S
)
# Cheap pre-filter: if none of these appear, skip all work.
HINTS = ("<function=", '"name"')


def _maybe_marker(text: str) -> bool:
    """Return whether `text` contains or may still grow into a tool marker.

    Markers stream token by token; holding viable prefixes stops a fragment
    leaking to the user.
    """
    t = text.lstrip()
    if not t:
        return False
    if any(h in t for h in HINTS):
        return True
    # Any tail may be a marker prefix; only the last len(longest) chars matter.
    window = max(len(h) for h in HINTS)
    tail = t[-window:]
    for i in range(len(tail)):
        suffix = tail[i:]
        if any(h.startswith(suffix) for h in HINTS):
            return True
    return False


def _strip_code(text: str) -> str:
    return INLINE_RE.sub("", FENCE_RE.sub("", text))


def _coerce(value: object, spec: dict | None) -> object:
    """Coerce to the schema's declared type; never fail a call over coercion.

    Already-typed values pass through untouched.
    """
    t = (spec or {}).get("type")
    if not isinstance(value, str):
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


def _index_tools(declared: list | None) -> dict[str, dict]:
    """Map name -> function schema for the tools the caller declared."""
    index = {}
    for t in declared or []:
        fn = t.get("function") if isinstance(t, dict) and "function" in t else t
        if isinstance(fn, dict) and fn.get("name"):
            index[fn["name"]] = fn
    return index


def _validate(name: str, args: dict, tool_index: dict[str, dict]) -> dict | None:
    """Return an OpenAI tool_call dict, or None to reject the candidate."""
    if name not in tool_index:
        return None  # hallucinated / unknown tool
    spec = tool_index[name]
    # OpenAI: "parameters"; Anthropic /v1/messages: "input_schema".
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


# Human-vetted allowlist: safety to fabricate is not derivable from a schema.
FABRICABLE = {("Agent", "description")}

_WS_RE = re.compile(r"\s+")
LABEL_WORDS = 5
LABEL_CHARS = 60


def _short_label(prompt: str) -> str:
    """Return the prompt's first words as a bounded, deterministic label.

    Subagent types are open-ended, so no fixed label fits; the prompt is
    already shown beside it, so nothing new is disclosed.
    """
    words = _WS_RE.sub(" ", prompt).strip().split(" ")
    label = " ".join(words[:LABEL_WORDS])[:LABEL_CHARS].strip()
    return label or "Delegated agent task"


def complete_ui_metadata(name: object, args_text: str, declared_tools: list | None,
                         ctx: dict[str, Any] | None = None) -> str | None:
    """Add ONE missing UI-metadata argument to a native tool call's arguments.

    Returns:
        The patched arguments as JSON, or None to leave the call as emitted.

    gemma4 omits Claude Code's required `Agent.description` on most fresh
    delegations (also vllm-project/vllm#39072); the client retry costs ~18 s.
    `description` is UI metadata that steers nothing, so it alone is eligible,
    and only when it is EXACTLY the missing set. Retire when a direct Ollama
    /api/chat with the Agent tool returns `description` on every attempt.
    """
    if not args_text or not declared_tools or not isinstance(name, str):
        return None
    eligible = {arg for tool, arg in FABRICABLE if tool == name}
    if not eligible:
        return None
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
    # Exactly the eligible argument; an unvetted extra → visible client error.
    if missing != eligible:
        return None
    field = next(iter(missing))
    if (props.get(field) or {}).get("type") != "string":
        return _reject("field_not_a_string", tool=name, ctx=ctx)

    # A call broken in a second way is not completed.
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


def recover(content: str | None, declared_tools: list | None,
            ctx: dict[str, Any] | None = None) -> tuple[list[dict] | None, str | None]:
    """Return (tool_calls | None, leftover_text); pure.

    `ctx` is metrics-only and never affects the decision.
    """
    if not content or not declared_tools:
        return None, content
    if not any(h in content for h in HINTS):
        # No tool syntax: no attempt, distinct from a rejected one.
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
            # Raw values: str() would turn arrays/bools/ints into their repr.
            call = _validate(m.group(1), raw, tool_index)
            if call:
                calls.append(call)
                spans.append(m.group(0))

    if not calls:
        # Third format: the WHOLE reply is one fenced JSON call. Safe by
        # context: a tutorial fence has prose around it; a call is the reply.
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
        _reject("no_valid_call_in_text", ctx=ctx)
        return None, content

    record("repaired", tool=calls[0]["function"]["name"], **(ctx or {}))
    leftover = scan
    for s in spans:
        leftover = leftover.replace(s, "")
    leftover = leftover.replace("</tool_call>", "").replace("<tool_call>", "").strip()
    return calls, (leftover or None)


# /v1/responses: the hook sees typed LiteLLM events (SSE serialises after), so
# this branch builds LiteLLM's own event classes, never `data:` lines.

# The fence must be terminal; leading prose is allowed and not inspected.
TERMINAL_FENCE_RE = re.compile(
    r'```(?:json)?[ \t]*\n((?:(?!```).)*?)\n?[ \t]*```[ \t\r\n]*\Z', re.S)


def responses_fence_candidate(text: str, declared_tools: list | None,
                              ctx: dict[str, Any] | None = None) -> list[dict] | None:
    """Recover a tool call from output_text — Responses route only.

    Not `recover()`, which strips fences. Accepts only one terminal fence with
    one declared, schema-valid call. Prose is not inspected (no intent
    classifying), so an illustrated valid call executes: accepted, bounded by
    _validate().
    """
    if not text or not declared_tools:
        return _reject("no_text_or_tools", ctx=ctx)
    if text.count("```") > 2:
        return _reject("multiple_fences", ctx=ctx)
    m = TERMINAL_FENCE_RE.search(text)
    if not m:
        return _reject("no_terminal_fence", ctx=ctx)
    inner = m.group(1).strip()
    if not inner.startswith("{") or not inner.endswith("}"):
        return _reject("fence_not_json_object", ctx=ctx)
    try:
        obj = json.loads(inner)
    except Exception:                    # noqa: BLE001
        return _reject("malformed_json", ctx=ctx)
    if not isinstance(obj, dict):
        return _reject("not_an_object", ctx=ctx)
    name = obj.get("name")
    args = obj.get("arguments")
    if not isinstance(name, str) or not isinstance(args, dict):
        return _reject("missing_name_or_arguments", ctx=ctx)
    # Raw values: _coerce() knows each type; str() would corrupt arrays.
    index = _index_tools(declared_tools)
    if name not in index:
        # Invented tool, distinct from a real tool called wrongly.
        return _reject("undeclared_tool", tool=name, ctx=ctx)
    call = _validate(name, args, index)
    if not call:
        return _reject("schema_validation_failed", tool=name, ctx=ctx)
    record("repaired", tool=name, **(ctx or {}))
    return [call]


def _responses_function_call_events(call: dict, output_index: int) -> list[object]:
    """Build the event sequence LiteLLM emits for a genuine function call.

    Mirrors responses/streaming_iterator.py's function_call branch.
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


def _responses_event_name(chunk: object) -> object:
    """Return the event type, tolerant of enum-or-str typing."""
    t = getattr(chunk, "type", None)
    return getattr(t, "value", t) if t is not None else None


def _responses_stream_from_complete(response: Any,
                                    request_data: dict | None = None) -> Iterator[object]:
    """Yield a valid /v1/responses event stream for a completed response.

    LiteLLM's own (_)build_synthetic_response_events builds the sequence; a
    hand-rolled one drops deltas and broke tool calls. Without the builder the
    minimal fallback breaks Codex web search: re-run the w7 probe on upgrade.
    The wrapping iterators are not reused: they would log a cache hit.
    """
    logging_obj = (request_data or {}).get("litellm_logging_obj")
    status = getattr(response, "status", None)

    def _terminal_override() -> object | None:
        """Return a failed/incomplete terminal event in place of `completed`.

        LiteLLM always ends with response.completed; the only divergence.
        """
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
        from litellm.responses import streaming_iterator as _si
        build = (getattr(_si, "build_synthetic_response_events", None)
                 or getattr(_si, "_build_synthetic_response_events"))
        MockResponsesAPIStreamingIterator = _si.MockResponsesAPIStreamingIterator
        events = build(
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

    def _plain(obj: Any) -> object:
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


_REASONING_TEXT_PARTS = ("output_text", "text", "reasoning_text")


def _summarised_reasoning(item: Any) -> Any:
    """Reshape a de-streamed reasoning item as LiteLLM's streaming path does.

    Codex drops reasoning without `summary`, so Qwen3.5 answers inside an open
    think block (#38). The text moves to `summary`, never to visible content.
    """
    payload = item.model_dump() if hasattr(item, "model_dump") else item
    if not isinstance(payload, dict) or payload.get("type") != "reasoning" or payload.get("summary"):
        return item
    texts = [part["text"] for part in payload.get("content") or ()
             if isinstance(part, dict) and part.get("type") in _REASONING_TEXT_PARTS
             and isinstance(part.get("text"), str) and part["text"].strip()]
    if not texts:
        return item
    update = {"summary": [{"type": "summary_text", "text": "\n".join(texts)}], "content": []}
    if hasattr(item, "model_copy"):
        # Validate nested types, else a serializer warning leaks the reasoning.
        validated = type(item).model_validate({**payload, **update})
        return item.model_copy(update={key: getattr(validated, key) for key in update})
    return {**item, **update}


def _with_reasoning_summaries(response: Any) -> Any:
    """Return the response with reasoning items summarised, or it unchanged.

    Upstream: BerriAI/litellm#41262, #45552. Retire when the non-streaming
    bridge emits `summary` itself.
    """
    output = list(getattr(response, "output", None) or ())
    summarised = [_summarised_reasoning(item) for item in output]
    if all(new is old for new, old in zip(summarised, output)):
        return response
    clone = response.model_copy() if hasattr(response, "model_copy") else copy.copy(response)
    clone.output = summarised
    return clone


class _CompletedResponseStream:
    """Serve a completed Responses result as the stream the client asked for.

    `_hidden_params` feeds response headers; `completed_response` feeds the
    proxy's container-ownership pass.
    """

    def __init__(self, response: Any, request_data: dict | None) -> None:
        self._events = iter(list(_responses_stream_from_complete(response, request_data)))
        self._hidden_params = getattr(response, "_hidden_params", None) or {}
        self.completed_response = response

    def __aiter__(self) -> _CompletedResponseStream:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


def _destreamed_responses_result(request_data: dict | None, response: object) -> bool:
    """Return whether websearch_interception de-streamed this response."""
    return (bool((request_data or {}).get("_websearch_interception_converted_stream"))
            and type(response).__name__ == "ResponsesAPIResponse")


class ToolRepair(CustomLogger):
    """Registered via litellm_settings.callbacks."""

    @staticmethod
    def _repair_anthropic(data: dict, response: Any) -> bool:
        """Repair the Anthropic /v1/messages shape.

        Claude Code's route: top-level `content`, no `.choices`, so the OpenAI
        branch never sees it.
        """
        blocks = getattr(response, "content", None)
        if blocks is None and isinstance(response, dict):
            blocks = response.get("content")
        if not isinstance(blocks, list):
            return False
        # Native tool_use → no text recovery; only UI metadata may be completed.
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

    async def async_post_call_success_deployment_hook(
            self, request_data: dict, response: object,
            call_type: object) -> _CompletedResponseStream | None:
        """Re-stream a /v1/responses web search that was de-streamed.

        LiteLLM re-wraps it on /v1/messages but not /v1/responses, so clients
        never see response.completed and retry forever (BerriAI/litellm#42770).
        Retire when a streamed /v1/responses with tools=[{"type":"web_search"}]
        completes without this hook.
        """
        if not _destreamed_responses_result(request_data, response):
            return None
        log.info("tool_repair: re-streaming a de-streamed /v1/responses web search")
        return _CompletedResponseStream(_with_reasoning_summaries(response), request_data)

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: object,
                                           response: Any) -> Any:
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
                    continue  # native tool calls: never touch
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
    def _sse(event_type: str, payload: dict) -> bytes:
        return (f"event: {event_type}\ndata: {json.dumps(payload)}\n\n").encode()

    @classmethod
    def _native_tool_use_events(cls, ts: dict, args_text: str) -> list[bytes]:
        """Re-emit one buffered tool_use block with `args_text` as input."""
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
    def _anthropic_native_arg_repair(cls, text: str, state: dict,
                                     tools: list | None) -> list[bytes] | None:
        """Complete a missing UI-metadata arg on a native streamed tool_use.

        Buffers the block start→stop: the decision needs the whole arguments.

        Returns:
            Byte chunks to emit, or None for an event this does not own.
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
                return None               # text block: caller owns it
            ts.clear()
            ts.update(active=True, index=payload.get("index", 0), id=block.get("id"),
                      name=block.get("name"), args="")
            # The runtime parsed a call: text recovery stands down.
            state["native"] = True
            state["saw_tool_use"] = True
            return []

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
    def _anthropic_sse_repair(cls, raw: bytes, state: dict,
                              tools: list | None) -> list[bytes]:
        """Recover malformed tool calls on the Anthropic SSE byte stream.

        Bytes, because the adapter encodes SSE before any callback seam. Buffers
        a text block start→stop, then emits tool_use events or flushes verbatim.
        Each chunk is one complete SSE event.

        Returns:
            Byte chunks to emit (empty while buffering).
        """
        try:
            text = raw.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return [raw]

        # Native missing-argument repair first; None for events it does not own.
        completed = cls._anthropic_native_arg_repair(text, state, tools)
        if completed is not None:
            return completed

        # Native tool_use → stand down for the rest of the stream.
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
                return buf  # ordinary text, verbatim
            idx = state.get("index", 0)
            out = []
            if leftover:
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
    def _fix_anthropic_sse_stop_reason(raw: bytes, state: dict) -> bytes | None:
        """Repair stop_reason after a tool_use block was injected.

        Claude Code will not run a tool in an end_turn turn. Injected streams
        only; native ones were fixed upstream in 1.101.0 (#39010).

        Returns:
            Replacement bytes, or None to pass the chunk through untouched.
        """
        if not state.get("injected"):
            return None
        try:
            text = raw.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
        if "message_delta" not in text:
            return None
        for wrong in ('"stop_reason": "end_turn"', '"stop_reason":"end_turn"'):
            if wrong in text:
                fixed = text.replace(wrong, wrong.replace("end_turn", "tool_use"))
                log.info("tool_repair: corrected stop_reason end_turn -> tool_use")
                if AILOCAL_DEBUG:
                    print("tool_repair: STOP_REASON end_turn -> tool_use", flush=True)
                return fixed.encode("utf-8")
        return None

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: object, response: AsyncIterable[Any],
        request_data: dict | None
    ) -> AsyncIterator[object]:
        """Buffer suspected tool syntax instead of streaming it as text.

        A tool call is executed, not read, so buffering costs the user nothing.
        Also fixes stop_reason after an injected call on the SSE byte path.
        """
        tools = (request_data or {}).get("tools")
        # Separate from sse_state so the two branches cannot interfere.
        rs = {"buf": [], "text": "", "native": False, "index": 0, "done": False}
        sse_state = {"saw_tool_use": False,
                     "ctx": _ctx(request_data, "/v1/messages"),
                     "model": (request_data or {}).get("model"),
                     "ntools": len(tools or [])}
        if AILOCAL_DEBUG:
            print(f"tool_repair: STREAM hook entered tools={len(tools or [])}", flush=True)
        _dbg_shapes = set()
        buffered = []
        text = ""
        suspect = False
        native_seen = False
        try:
            async for chunk in response:
                # /v1/responses: typed events, handled first.
                ev = _responses_event_name(chunk)
                if isinstance(ev, str) and ev.startswith("response."):
                    if rs["done"]:
                        yield chunk
                        continue
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
                            for c in rs["buf"]:
                                yield c
                        rs["buf"] = []
                        continue
                    # Anything else passes through after flushing the buffer.
                    for c in rs["buf"]:
                        yield c
                    rs["buf"] = []
                    yield chunk
                    continue

                delta = None
                try:
                    delta = chunk.choices[0].delta
                except Exception:  # noqa: BLE001
                    # Anthropic SSE bytes: fix stop_reason or the loop stalls.
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
                    native_seen = True
                    sse_state["saw_tool_use"] = True

                # Ollama sends tool_calls before done=True; end says "stop".
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
                    suspect = True
                else:
                    # Provably no marker: release what was held.
                    suspect = False
                    for c in buffered:
                        yield c
                    buffered = []

            # Ended mid tool_use: flush unrepaired; a visible failure.
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
                    for c in buffered:
                        yield c
        except Exception as exc:  # noqa: BLE001
            log.error("tool_repair: streaming repair failed: %s", exc)
            for c in buffered:
                yield c


proxy_handler_instance = ToolRepair()
