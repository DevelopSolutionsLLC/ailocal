"""
session_observer.py — records what a session ASKED FOR and what it ACTUALLY DID.

Purely observational. It reads request payloads and writes a ledger to disk. It
does not alter requests, does not inject turns, and does not touch client
conversation state — that stays off the table until protocol ownership is
settled. If this module fails, the request still goes through.

WHY
---
A local model's report that it finished is not evidence that it finished. The
failure that matters is not a wrong answer, it is a confident summary of work
that never happened — an "I've updated the file" with no write behind it. That
is only detectable by comparing three things that live in three places:

  what was asked       the first user message          (the proxy sees this)
  what was executed    the tool calls in the history   (the proxy sees this)
  what changed         the filesystem and git          (only the HOST sees this)

This module owns the first two. scripts/verify-session.py owns the third and
does the comparison. The split is not incidental: the proxy runs in a container
with no access to the repository, so a verification layer that lived entirely
here could only ever check the model against its own claims.

THE ONE NON-OBVIOUS MECHANISM
-----------------------------
Agent clients are stateless over HTTP: every turn re-sends the entire
conversation. So a single async_pre_call_hook observation carries the whole
history — every tool call the model has made and every result it got back. There
is no need to hook responses, buffer streams, or correlate turns. The last
request of a session contains the complete ledger for that session, and each
write simply supersedes the previous one.

This is why the module is small, and why it cannot miss a tool call that
happened before it was loaded, as long as one request arrives afterwards.

SESSION IDENTITY
----------------
Derived from a hash of the first user message plus the model. Clients do not
send a stable session id on all three routes, and inventing one would mean
mutating the request. The consequence is honest and worth stating: two sessions
that begin with a byte-identical first message and the same model share a
ledger, and the later one wins. Ledgers are a debugging aid, not an audit log.

ENABLEMENT
----------
AILOCAL_SESSION_LEDGER must be set to a writable directory, or the hook returns
immediately. Off unless asked for, like the gateway.
"""

import hashlib
import json
import os
import re
import time

from litellm.integrations.custom_logger import CustomLogger

LEDGER_DIR = os.environ.get("AILOCAL_SESSION_LEDGER") or ""


def emit(record):
    try:
        print("session_observer " + json.dumps(record, default=str), flush=True)
    except Exception:
        pass


def _text_of(content):
    """Flatten a message's content to text. It may be a plain string or a list
    of typed blocks; only text blocks contribute."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _strip_injected(text):
    """Remove the client's own injected context from a user message.

    Claude Code prepends a <system-reminder> block to the first user turn
    carrying AGENTS.md, directory context, and similar. Measured: the raw first
    message began with the entire contents of the user's global AGENTS.md, so
    the ledger recorded that as the "requested change" and stored 2 KB of
    unrelated instructions. Both wrong and needlessly nosy — this ledger should
    hold the ask, not the client's scaffolding."""
    out = []
    depth = 0
    for chunk in re.split(r"(<system-reminder>|</system-reminder>)", text or ""):
        if chunk == "<system-reminder>":
            depth += 1
        elif chunk == "</system-reminder>":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(chunk)
    return "".join(out).strip()


def _digest(value):
    """A short stable fingerprint of tool arguments. The arguments themselves are
    NOT stored: they routinely contain file contents and command lines, and this
    ledger is meant to be safe to leave lying around. A digest is enough to tell
    'called Edit twice with the same input' from 'called it twice differently'."""
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        blob = str(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def extract(data):
    """Pull the ledger out of one request payload.

    Three dialects, three shapes for the same facts:

      /v1/messages          assistant content blocks {"type":"tool_use","name",
                            "input"}; results come back as user blocks
                            {"type":"tool_result","is_error"}
      /v1/chat/completions  assistant {"tool_calls":[{"function":{"name",
                            "arguments"}}]}; results are role:"tool" messages
      /v1/responses         flat data["input"] items typed "function_call" and
                            "function_call_output"

    Returns (requested_change, calls, results).
    """
    requested = ""
    calls = []
    results = []

    messages = data.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")

            if role == "user" and not requested:
                # The first user text is the ask. Tool results also arrive as
                # user messages, so skip anything that is only tool_result
                # blocks — otherwise the "request" becomes a command's output.
                # Injected client scaffolding is stripped first; if that leaves
                # nothing, keep looking rather than recording the scaffolding.
                text = _strip_injected(_text_of(content))
                if text:
                    requested = text

            if role == "assistant":
                # Anthropic: tool_use blocks inside the content list.
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"):
                            calls.append({"name": block.get("name"),
                                          "args": _digest(block.get("input"))})
                # OpenAI: a parallel tool_calls array.
                for tc in msg.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    calls.append({"name": fn.get("name"),
                                  "args": _digest(fn.get("arguments"))})

            if role == "user" and isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_result"):
                        results.append({"error": bool(block.get("is_error"))})

            if role == "tool":
                # OpenAI tool results carry no error flag; the convention is an
                # error string in the content. Recorded as unknown rather than
                # guessed at, so a downstream reader does not treat absence of
                # an error flag as proof of success.
                results.append({"error": None})

    # /v1/responses keeps everything in one flat list.
    items = data.get("input")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call":
                calls.append({"name": item.get("name"),
                              "args": _digest(item.get("arguments"))})
            elif itype == "function_call_output":
                out = item.get("output")
                results.append({"error": None if out is None
                                else "error" in str(out)[:200].lower()})
            elif itype == "message" and item.get("role") == "user":
                if not requested:
                    requested = _text_of(item.get("content"))
    elif isinstance(items, str) and not requested:
        requested = items

    return requested, calls, results


class SessionObserver(CustomLogger):

    def session_id(self, requested, model):
        seed = (requested or "")[:2000] + "|" + str(model)
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def build(self, data):
        requested, calls, results = extract(data)
        model = data.get("model")
        counts = {}
        for c in calls:
            counts[c["name"]] = counts.get(c["name"], 0) + 1
        errors = sum(1 for r in results if r.get("error") is True)
        unknown = sum(1 for r in results if r.get("error") is None)
        return {
            "session": self.session_id(requested, model),
            "observed_at": time.time(),
            "model": model,
            # Truncated: the ledger is a debugging aid, not a transcript store.
            "requested_change": (requested or "")[:2000],
            "tool_calls_total": len(calls),
            "tool_calls_by_name": counts,
            "tool_call_sequence": [c["name"] for c in calls],
            "tool_results_total": len(results),
            "tool_results_errored": errors,
            "tool_results_unknown_status": unknown,
            # Deliberately absent: any verdict on whether the work was done.
            # That requires the filesystem, which this process cannot see.
            # scripts/verify-session.py supplies it.
            "verdict": None,
            "verdict_note": "requires host-side filesystem/git comparison — "
                            "see scripts/verify-session.py",
        }

    def write(self, ledger):
        path = os.path.join(LEDGER_DIR, ledger["session"] + ".json")
        tmp = path + ".tmp"
        # Write-then-rename: a reader never sees a half-written ledger, and each
        # observation supersedes the last (the newest request holds the fullest
        # history, so there is nothing to merge).
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, default=str)
        os.replace(tmp, path)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data,
                                  call_type):
        if not LEDGER_DIR:
            return data
        try:
            os.makedirs(LEDGER_DIR, exist_ok=True)
            ledger = self.build(data)
            # A turn with no tool activity yet carries no information worth a
            # file, and writing one would overwrite a fuller ledger from an
            # earlier session that happened to share the seed.
            if ledger["tool_calls_total"] or ledger["tool_results_total"]:
                self.write(ledger)
        except Exception as exc:
            # Observation must never break the request path.
            emit({"event": "observe_failed", "error": "%s: %s"
                  % (type(exc).__name__, exc)})
        return data


proxy_handler_instance = SessionObserver()
