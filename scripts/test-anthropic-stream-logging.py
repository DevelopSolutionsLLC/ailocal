#!/usr/bin/env python3
"""
test-anthropic-stream-logging.py — regression cover for the LiteLLM Anthropic
streaming logging bug, and for the guard we backported.

WHAT BROKE
----------
A streamed /v1/messages request carrying a web-search tool produced, per request:

    LiteLLM.Success_Call Error: 1 validation error for AnthropicResponse
    Input should be a valid dictionary or instance of AnthropicResponse
    [input_type=FakeAnthropicMessagesStreamIterator]

Two LiteLLM features colliding: websearch interception converts the stream to
non-streaming, `_maybe_wrap_in_fake_stream` re-wraps the result as a fake stream
iterator, and `_handle_anthropic_messages_response_logging` then tries to
validate that iterator as an AnthropicResponse.

WHY THIS TEST EXISTS
--------------------
The guard lives in config/litellm/anthropic_stream_logging_fix.py and is designed
to self-retire when upstream fixes this. Two failure modes need catching:

  1. The guard stops being applied (config edit, import failure, LiteLLM
     refactor renames the method) and the errors come back silently — they are
     NON-BLOCKING, so nothing else notices.
  2. A LiteLLM upgrade changes the internals enough that the patch no-ops while
     the bug persists.

Both look the same from outside: errors in the log. So this test asserts the
OBSERVABLE property — a streamed request with the triggering tool shape produces
no validation error — rather than asserting the patch is installed.

THE TRIGGER SHAPE MATTERS. Interception refuses a `web_search` that carries an
`input_schema`, so that variant does NOT reproduce the bug. The bare server-tool
shape is required. Two earlier repro attempts came back clean for exactly this
reason, which is why the shape is pinned here with a comment rather than left to
whoever edits this next.

Usage: python3 scripts/test-anthropic-stream-logging.py
Exit 0 = pass. Requires the stack running.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:4000"
CONTAINER = "ailocal-litellm"
failures = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def master_key():
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def proxy_up():
    try:
        urllib.request.urlopen(BASE + "/health/liveliness", timeout=5).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def post_stream(key, tools):
    """Send a STREAMING /v1/messages request and drain it."""
    body = {
        "model": "ailocal-fast",
        "stream": True,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "say OK"}],
    }
    if tools is not None:
        body["tools"] = tools
    req = urllib.request.Request(
        BASE + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")


def errors_since(mark):
    out = subprocess.run(
        ["docker", "logs", "--since", str(mark), CONTAINER],
        capture_output=True, text=True,
    )
    blob = out.stdout + out.stderr
    return blob.count("FakeAnthropicMessagesStreamIterator")


def main():
    print("ANTHROPIC STREAMING LOGGING REGRESSION")
    if not proxy_up():
        print("  SKIP — proxy not reachable; start the stack first")
        return 0
    key = master_key()
    if not key:
        print("  SKIP — LITELLM_MASTER_KEY not readable")
        return 0

    # The exact shape that triggers websearch interception. A `web_search` with
    # an input_schema is REFUSED by interception and will not reproduce the bug —
    # do not "simplify" this into that shape.
    trigger_tools = [{"type": "web_search_20250305", "name": "web_search"}]

    mark = int(time.time())
    payload = post_stream(key, trigger_tools)
    time.sleep(3)
    n = errors_since(mark)

    check(n == 0,
          f"streamed /v1/messages + web_search produces no AnthropicResponse "
          f"validation error (saw {n})")

    # Streaming itself must still work — the guard must not have been "fixed" by
    # disabling the fake-stream wrapper or the interception path.
    check("event:" in payload or "data:" in payload,
          "the response is still a real SSE stream")

    # Control: the same request without the trigger must also be clean. If this
    # fails, something broke streaming generally rather than the guard.
    mark = int(time.time())
    post_stream(key, None)
    time.sleep(2)
    check(errors_since(mark) == 0, "control: streamed request with no tools is clean")

    print()
    if failures:
        print(f"STREAM LOGGING: {len(failures)} FAILED")
        return 1
    print("STREAM LOGGING: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
