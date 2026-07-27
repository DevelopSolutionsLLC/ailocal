#!/usr/bin/env python3
"""Memory and load-time probes for the model benchmark.

WHY MEMORY IS A FIRST-CLASS METRIC HERE
A larger model that pages is slower than a smaller one that fits, and the
symptom of paging on this stack is the failure class we already fixed once: a
long silence before the first token. So the benchmark must be able to say "the
bigger model won on quality and lost on memory" rather than reporting quality
alone.

WHAT EACH NUMBER ACTUALLY IS — read these labels literally:

  size_vram_bytes   Ollama's own figure for the loaded model. REAL.
  cold_first_ms     wall time for a trivial request issued right after evicting
                    the model. This is LOAD + PROMPT EVAL + FIRST TOKEN, not a
                    pure load time. Ollama exposes no separate load duration, so
                    the name says "first", not "load".
  load_ms_est       cold_first_ms minus warm_first_ms. An ESTIMATE of the load
                    cost by subtraction. Labelled _est because it inherits both
                    measurements' noise and assumes the only difference is
                    residency.
  pageouts_delta    change in the OS cumulative pageout counter across the run.
                    Non-zero means the system paged while the model was
                    resident. This is the number that decides whether a bigger
                    model is affordable.
  free_pct          system-wide free memory percentage, before and after.

Usage:
    bench_memory.py snapshot                 -> JSON of current memory state
    bench_memory.py evict  <ollama-tag>      -> unload a model (keep_alive 0)
    bench_memory.py model  <ollama-tag>      -> that model's residency + size
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")


def _get(path):
    with urllib.request.urlopen(OLLAMA + path, timeout=10) as r:
        return json.load(r)


def pageouts_and_free():
    """(pageouts, free_pct). Either may be None — a missing reading is reported
    as None, never as zero, because zero pageouts is a meaningful result and
    'could not read' is not the same thing."""
    pageouts = free = None
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True,
                             timeout=15).stdout
        m = re.search(r"Pageouts:\s+(\d+)", out)
        if m:
            pageouts = int(m.group(1))
        m = re.search(r"free percentage:\s+(\d+)", out)
        if m:
            free = int(m.group(1))
    except Exception:
        pass
    return pageouts, free


def snapshot():
    po, free = pageouts_and_free()
    loaded = []
    try:
        for m in (_get("/api/ps").get("models") or []):
            loaded.append({"name": m.get("name"),
                           "size_vram_bytes": m.get("size_vram"),
                           "context_length": m.get("context_length")})
    except Exception as exc:
        loaded = [{"error": f"{type(exc).__name__}: {exc}"}]
    return {"pageouts": po, "free_pct": free,
            "loaded_models": loaded,
            "loaded_vram_total_bytes": sum(
                (m.get("size_vram_bytes") or 0) for m in loaded
                if isinstance(m, dict) and "error" not in m)}


def evict(tag):
    """Unload a model so the next request is genuinely cold.

    ASSERTS the eviction instead of assuming it. Measured on this stack: a
    keep_alive:0 request does NOT reliably evict a model the config pins with
    keep_alive:-1 — the model stayed in /api/ps immediately afterwards. When
    that happens any "cold load" figure derived from the next request is
    confounded (it may be a partial reload, or contention with other models
    being evicted under pressure), so the caller must treat load_ms_est as
    UNRELIABLE rather than reporting it as a clean measurement.

    Returns {"evicted": bool|None, "still_loaded": [...], "reliable_cold": bool}.
    """
    body = json.dumps({"model": tag, "keep_alive": 0,
                       "messages": [{"role": "user", "content": "x"}],
                       "stream": False}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"content-type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=300).read()
    except Exception:
        pass
    try:
        still = [m.get("name") for m in (_get("/api/ps").get("models") or [])]
    except Exception:
        return {"evicted": None, "note": "could not read /api/ps after evicting"}
    gone = tag not in still
    return {"evicted": gone, "still_loaded": still,
            # The only condition under which a following cold-start measurement
            # means what it says.
            "reliable_cold": gone,
            "note": None if gone else
                    ("model still resident after keep_alive:0 — it is pinned "
                     "(keep_alive:-1 in the model_list). Any cold-load figure "
                     "from the next request is CONFOUNDED. Stop Ollama or "
                     "temporarily unpin the capability to measure load time.")}


def model_state(tag):
    try:
        for m in (_get("/api/ps").get("models") or []):
            if m.get("name") == tag:
                return {"resident": True,
                        "size_vram_bytes": m.get("size_vram"),
                        "context_length": m.get("context_length")}
    except Exception as exc:
        return {"resident": None, "error": str(exc)}
    return {"resident": False}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "snapshot":
        print(json.dumps(snapshot()))
    elif cmd == "evict":
        print(json.dumps(evict(sys.argv[2])))
    elif cmd == "model":
        print(json.dumps(model_state(sys.argv[2])))
    else:
        print(__doc__)
        sys.exit(1)
