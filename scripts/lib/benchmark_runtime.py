"""benchmark_runtime.py — Ollama state, temporary aliases, and the LiteLLM
container lifecycle.

MOVED VERBATIM from benchmark.py. Owns everything that touches a running
service: model discovery, health, temporary alias construction and
installation, and restoration. Depends only on benchmark_evidence.

The restoration contract is unchanged and load-bearing: restore() runs on
success, error and interrupt, and PROVES the runtime came back.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from checks.services import (  # noqa: E402  service access has one owner
    OLLAMA, PROXY as LITELLM, _key_from, master_key as api_key,
    proxy_healthy as litellm_healthy,
)
from benchmark_evidence import REPO, capture_litellm_log, runtime_dir, state_dir


ALIAS_PREFIX = "bench-"
#: LiteLLM's pre-call check counts with a GENERIC tokenizer, not the model's.
#: Measured: for one text it returned 7079 for every model while Ollama's true
#: counts ranged 5227-7098 — so it over-counts an efficient tokenizer by up to
#: 35%. This margin stops a valid prompt being rejected on the gate's own error.
PRECALL_MARGIN = 1.40
#: NOT USED FOR ADMISSION, and must never be again. The over-counting above is
#: real, but the remedy cannot be to admit more tokens than the window
#: physically holds: between num_ctx and context*1.40 Ollama accepts the request
#: and silently discards the front of the prompt. A margin may widen a WARNING;
#: it may not grant permission to exceed the window. Retained so the measured
#: over-counting is not rediscovered, and so the multiplication is not re-added.


# ── HTTP ────────────────────────────────────────────────────────────────────


def _json(url: str, payload=None, key: str = None, timeout: int = 60,
          method: str = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())








def aliases(key: str = None) -> list:
    d = _json(f"{LITELLM}/v1/models", key=key or api_key(), timeout=30)
    return [m["id"] for m in d.get("data", [])]


# ── Ollama: diagnostics ONLY, never a score ─────────────────────────────────


def installed() -> dict:
    try:
        return {m["name"]: m for m in _json(f"{OLLAMA}/api/tags")["models"]}
    except Exception:
        return {}


def model_info(tag: str) -> dict:
    try:
        d = _json(f"{OLLAMA}/api/show", {"model": tag}, timeout=60)
    except Exception:
        return {}
    ctx = 0
    for k, v in (d.get("model_info") or {}).items():
        if k.endswith(".context_length"):
            ctx = int(v)
            break
    # /api/show carries no digest; /api/tags does. The digest is what pins a
    # result to an exact set of weights, so a run manifest without it cannot be
    # reproduced after a model is re-pulled under the same tag.
    entry = installed().get(tag) or {}
    return {"context_length": ctx,
            "digest": entry.get("digest", ""),
            "size_bytes": entry.get("size"),
            "ollama_capabilities": d.get("capabilities") or []}


def resident() -> list:
    try:
        return [m.get("name") or m.get("model")
                for m in _json(f"{OLLAMA}/api/ps").get("models", [])]
    except Exception:
        return []


def unload(tag: str) -> None:
    try:
        _json(f"{OLLAMA}/api/generate",
              {"model": tag, "prompt": "", "keep_alive": 0}, timeout=120)
    except Exception:
        pass


# ── temporary LiteLLM aliases ───────────────────────────────────────────────


def alias_name(model: str, mode: str, context: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"{ALIAS_PREFIX}{slug}-{mode}-{context // 1024}k"


#: Reasoning-mode vocabulary. ONE mapping, here, because the benchmark speaks
#: modes ("off"/"on"/"low"/"medium"/"high") while the profile speaks a boolean
#: `reasoning`. Both reach the same `think` parameter; two mappings would drift.
THINK_MODES = {"off": False, "on": True,
               "low": "low", "medium": "medium", "high": "high"}


def build_alias(model: str, mode: str, context: int, ceiling: int,
                preset: dict, keep_alive: str = None) -> dict:
    """A temporary benchmark deployment: production geometry + explicit overlay.

    GEOMETRY IS NOT COMPUTED HERE. It comes from policy.geometry(), the
    same function sync-models calls, so a benchmark alias and a production alias
    cannot disagree about what num_ctx, num_predict or admission mean. This
    function previously did `num_ctx = context + ceiling` itself, which is how
    build_alias enforced the admission invariant that production did not.

    `context` is the INPUT budget and `ceiling` the OUTPUT reserve — the same
    context_input / max_output the profiles now declare.

    Vendor generation settings live in the alias because this stack ignores
    client generation parameters: measured, a per-request max_tokens of 512
    against an alias declaring num_predict 32768 returned 4,199 tokens.

    keep_alive defaults to the caller's choice rather than a hardcoded literal;
    benchmarks that want production behaviour pass the profile's value.
    """
    import policy as _pc
    g = _pc.geometry(context, ceiling)
    think = THINK_MODES.get(mode)
    params = {"model": f"ollama_chat/{model}",
              "api_base": "os.environ/OLLAMA_URL",
              "num_ctx": g["num_ctx"],
              "num_predict": g["num_predict"],
              "keep_alive": keep_alive or "10m", **preset}
    if think is not None:
        params["think"] = think
    return {"model_name": alias_name(model, mode, context),
            "litellm_params": params,
            # Admission is g["max_input_tokens"], which IS context_input by
            # construction. It was `context * PRECALL_MARGIN`, which admitted
            # 45,875 tokens into a 40,960-token window. MEASURED consequence
            # (repro-context-admission.py, since deleted; qwen3.5:2b): 43,645 tokens
            # ADMITTED, then Ollama silently truncated to 20,482 — HTTP 200,
            # finish_reason=stop, system prompt gone. Failing closed costs a
            # loud rejection; failing open costs a corrupted run that looks fine.
            "model_info": {"max_input_tokens": g["max_input_tokens"],
                           "max_output_tokens": g["max_output"],
                           "input_cost_per_token": 0, "output_cost_per_token": 0,
                           "supports_reasoning": think is not None}}


def _emit(entry: dict) -> str:
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v) if isinstance(v, (int, float)) else str(v)
    lines = [f"  - model_name: {entry['model_name']}", "    litellm_params:"]
    lines += [f"      {k}: {val(v)}" for k, v in entry["litellm_params"].items()]
    lines.append("    model_info:")
    lines += [f"      {k}: {val(v)}" for k, v in entry["model_info"].items()]
    return "\n".join(lines)


def apply_aliases(entries: list) -> dict:
    """Install temporary aliases and restart LiteLLM.

    LiteLLM has no model database here (`/model/new` -> "No DB Connected"), and
    its docs confirm that without one the proxy relies entirely on config.yaml at
    startup — no hot-reload, no includes. A copied config plus one restart is the
    documented path. Production config is never edited.
    """
    dst = runtime_dir() / "litellm"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(REPO / "config" / "litellm", dst,
                    ignore=shutil.ignore_patterns("__pycache__"))
    cfg = dst / "config.yaml"
    text = cfg.read_text()
    m = re.search(r"^model_list:\s*$", text, re.M)
    after = text[m.end():]
    nxt = re.search(r"^(?!\s|#)\S", after, re.M)
    cut = m.end() + (nxt.start() if nxt else len(after))
    block = "\n".join(["  # >>> TEMPORARY BENCHMARK ALIASES <<<",
                       *(_emit(e) for e in entries), ""])
    cfg.write_text(text[:cut] + "\n" + block + "\n" + text[cut:])

    override = runtime_dir() / "docker-compose.bench.yml"
    override.write_text("services:\n  litellm:\n    volumes:\n"
                        f"      - {dst}:/app/config:ro\n")
    # BEFORE the recreate: the current buffer is about to be discarded.
    pre = capture_litellm_log(runtime_dir() / "litellm.pre-apply.log")
    _compose(["up", "-d", "--force-recreate", "litellm"], [override])
    ok = _wait_healthy()
    got = set(aliases()) if ok else set()
    want = {e["model_name"] for e in entries}
    return {"ok": ok and want <= got, "evidence": pre,
            "installed": sorted(want & got),
            "missing": sorted(want - got),
            "production": sorted(a for a in got if a.startswith("ailocal-"))}


def restore() -> dict:
    """Return to production config and PROVE it. Runs on success, error and
    interrupt — a benchmark must never leave aliases in a user's runtime."""
    # BEFORE the recreate: this is the ONLY moment the run's own request
    # logs still exist. Capturing after restore returns an empty buffer.
    pre = capture_litellm_log(runtime_dir() / "litellm.pre-restore.log")
    _compose(["up", "-d", "--force-recreate", "litellm"])
    ok = _wait_healthy()
    got = set(aliases()) if ok else set()
    leaked = sorted(a for a in got if a.startswith(ALIAS_PREFIX))
    for p in (runtime_dir() / "litellm", runtime_dir() / "docker-compose.bench.yml"):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    prod = sorted(a for a in got if a.startswith("ailocal-"))
    return {"restored": ok and not leaked and len(prod) >= 5,
            "evidence": pre,
            "production": prod, "leaked": leaked, "healthy": ok}


def _compose(args: list, extra=None):
    files = ["-f", str(REPO / "deploy/litellm/docker-compose.yml"),
             "-f", str(REPO / "deploy/searxng/docker-compose.yml")]
    for f in (extra or []):
        files += ["-f", str(f)]
    import policy
    env = {**os.environ, "AILOCAL_STATE": str(policy.runtime_root())}
    return subprocess.run(["docker", "compose", "--project-directory", str(REPO),
                           *files, *args], capture_output=True, text=True,
                          timeout=300, env=env)


def _wait_healthy(timeout: float = 240) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if litellm_healthy():
            return True
        time.sleep(2)
    return litellm_healthy()


# ── telemetry ───────────────────────────────────────────────────────────────


def telemetry() -> dict:
    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except Exception:
            return ""
    out = {"resident": resident()}
    mp = sh(["memory_pressure"])
    m = re.search(r"System-wide memory free percentage:\s*(\d+)", mp)
    if m:
        out["free_percent"] = int(m.group(1))
    sw = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)([MG])", sw)
    if m:
        out["swap_mb"] = round(float(m.group(1)) * (1024 if m.group(2) == "G" else 1), 1)
    therm = sh(["pmset", "-g", "therm"])
    out["thermal"] = ("nominal" if "No thermal warning" in therm or not therm
                      else "non-nominal")
    return out
