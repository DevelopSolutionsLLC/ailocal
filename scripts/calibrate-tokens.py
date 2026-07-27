#!/usr/bin/env python3
"""calibrate-tokens.py — measure how wrong the gateway's token estimate is.

tool_gateway.py reports `tokens_est` using litellm's token_counter, which
selects the OpenAI cl100k tokenizer even for an ollama_chat/qwen3-coder
deployment (verified via litellm.utils._select_tokenizer, which returns
'openai_tokenizer' for that model). Qwen's own tokenizer is not present in the
proxy image, so that figure is a proxy, not a measurement.

This script establishes the ratio between the two by asking the model itself.
Ollama returns `prompt_eval_count` — the real number of tokens the real
tokenizer produced for the real prompt. Comparing that against the cl100k
estimate for the same text gives an honest correction factor.

Method:
  1. Take the actual captured tool payloads (data/tool-captures/), so the text
     being measured is the text this project cares about, not lorem ipsum.
  2. Send each as a user message straight to Ollama with num_predict=1, so the
     response is cheap but prompt_eval_count is real.
  3. Subtract a measured baseline: the same request with an empty payload, to
     remove the chat template's own token overhead from the comparison.

Run on the host (Ollama is host-native): python3 scripts/calibrate-tokens.py
"""

import glob
import json
import os
import sys
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("AILOCAL_CALIBRATE_MODEL",
                       "qwen3-coder:30b-a3b-q4_K_M")
CAPTURES = os.environ.get("AILOCAL_CAPTURES", "data/tool-captures")


def ollama_prompt_tokens(text):
    """Real prompt token count from the model's own tokenizer."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "options": {"num_predict": 1},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["prompt_eval_count"]


def cl100k(text):
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except ImportError:
        print("tiktoken is not installed on the host. Run this inside the "
              "proxy container, or `pip install tiktoken`. Refusing to "
              "approximate the approximation.")
        sys.exit(1)


def main():
    files = sorted(glob.glob(os.path.join(CAPTURES, "*.json")),
                   key=os.path.getsize, reverse=True)
    if not files:
        print(f"No captures in {CAPTURES}/ — run a real client with "
              f"AILOCAL_TOOL_GATEWAY_CAPTURE set first.")
        return 1

    print(f"model    {MODEL}")
    print(f"baseline measuring chat-template overhead...", flush=True)
    base_real = ollama_prompt_tokens("")
    print(f"         empty prompt = {base_real} real tokens "
          f"(template overhead, subtracted below)\n")

    print(f"{'CAPTURE':38} {'BYTES':>7} {'cl100k':>7} {'REAL':>7} {'RATIO':>7}")
    ratios = []
    seen = set()
    for path in files:
        doc = json.load(open(path))
        rep = doc.get("report") or {}
        key = (rep.get("client"), rep.get("route"))
        if key in seen:
            continue          # largest per client/route only
        seen.add(key)
        text = json.dumps(doc.get("tools") or [], separators=(",", ":"),
                          ensure_ascii=False, sort_keys=True)
        est = cl100k(text)
        real = ollama_prompt_tokens(text) - base_real
        ratio = real / est if est else float("nan")
        ratios.append(ratio)
        label = f"{rep.get('client')} {rep.get('route')}"
        print(f"{label:38} {len(text.encode()):7} {est:7} {real:7} "
              f"{ratio:7.3f}", flush=True)

    if ratios:
        lo, hi = min(ratios), max(ratios)
        print(f"\nRatio real/cl100k: {lo:.3f}–{hi:.3f}")
        print("Multiply any tokens_est figure by this to get the token count "
              "the model actually sees.")
        if hi > 1.05 or lo < 0.95:
            print("The estimate is NOT interchangeable with the real count. "
                  "Quote tokens_est only alongside this ratio.")
        else:
            print("Within 5% — tokens_est is usable as-is for this payload "
                  "shape, though it remains an estimate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
