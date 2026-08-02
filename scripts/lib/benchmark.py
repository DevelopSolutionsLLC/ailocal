"""Benchmark orchestrator. ailocal owns orchestration; external tools own scoring.

ailocal does exactly six things here: pick models from a hardware profile, stand
up temporary authenticated LiteLLM aliases carrying vendor presets, invoke an
external benchmark engine against them, capture telemetry, restore the production
runtime, and write a report.

It deliberately does NOT own datasets, prompting, scoring, tokenization or task
definitions. lm-evaluation-harness already does those, correctly, and a previous
attempt to reimplement them grew to 42 files before producing a single ranking.

Every scored request goes through authenticated LiteLLM. Direct Ollama is used
only for residency and metadata, and can never produce a score.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

#: The memory-tier ladder. install.sh owns the canonical thresholds;
#: scripts/tests/test-benchmark.py parses install.sh and asserts these match, so
#: there is one source of truth enforced by a test rather than by convention.
#:
#: NEVER ROUND UP. Selecting at 75% of a tier's name gave a 24 GB machine the
#: 32gb profile and models it could not hold.
TIERS = ("16gb", "32gb", "64gb", "128gb")


def ram_gb() -> int:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                         text=True, timeout=10).stdout.strip()
    return int(out) // 1024 // 1024 // 1024 if out else 0


def tier_for_gb(gb: int):
    if gb >= 128:
        return "128gb"
    if gb >= 64:
        return "64gb"
    if gb >= 32:
        return "32gb"
    if gb >= 16:
        return "16gb"
    return None

CONFIG = REPO / "config" / "benchmark.yaml"
OLLAMA = "http://127.0.0.1:11434"
LITELLM = os.environ.get("AILOCAL_LITELLM_URL", "http://127.0.0.1:4000")
ALIAS_PREFIX = "bench-"

PROFILE, EXPLICIT, PROFILE_PLUS = "PROFILE", "EXPLICIT", "PROFILE_PLUS_EXPLICIT"

#: LiteLLM's pre-call check counts with a GENERIC tokenizer, not the model's.
#: Measured: for one text it returned 7079 for every model while Ollama's true
#: counts ranged 5227-7098 — so it over-counts an efficient tokenizer by up to
#: 35%. This margin stops a valid prompt being rejected on the gate's own error.
PRECALL_MARGIN = 1.40


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    p = Path(base) / "ailocal" / "benchmark"
    p.mkdir(parents=True, exist_ok=True)
    return p


def venv_bin(name: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "ailocal" / "benchmark" / "venv" / "bin" / name


# ── tiny config reader ──────────────────────────────────────────────────────
# The repo has no pyyaml dependency and the benchmark is not the place to add
# one. This reads the shape config/benchmark.yaml actually uses and RAISES on
# anything else rather than guessing.

def load_config(path: Path = CONFIG) -> dict:
    root, stack = {}, [(-1, {})]
    stack[0] = (-1, root)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        body = raw.strip()
        if "#" in body and body.count('"') % 2 == 0:
            body = body.split("#", 1)[0].strip()
        # Block list item: `- "text"`. Client scenarios are ordered turn lists,
        # which is the only place these appear.
        if body.startswith("- "):
            parent = stack[-1][1]
            if not isinstance(parent, list):
                raise ValueError(f"{path}:{lineno}: list item outside a list")
            parent.append(_scalar(body[2:]))
            continue
        if body[0] in "\"'":
            close = body.index(body[0], 1)
            key, rest = body[1:close], body[close + 1:].lstrip()
            if not rest.startswith(":"):
                raise ValueError(f"{path}:{lineno}: quoted key without ':'")
            value = rest[1:].strip()
        elif ":" in body:
            key, _, value = body.partition(":")
            key, value = key.strip(), value.strip()
        else:
            raise ValueError(f"{path}:{lineno}: not a mapping line: {raw!r}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            # A key whose first child is `- ` opens a list, not a mapping.
            nxt = _peek_child(path, lineno, indent)
            child = [] if nxt == "list" else {}
            parent[key] = child
            stack.append((indent, child))
        elif value.startswith("{"):
            parent[key] = _flow(value, f"{path}:{lineno}")
        else:
            parent[key] = _scalar(value)
    return root


def _peek_child(path: Path, lineno: int, indent: int):
    """Is the next more-indented line a list item or a mapping key?"""
    for raw in path.read_text().splitlines()[lineno:]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if len(raw) - len(raw.lstrip()) <= indent:
            return "map"
        return "list" if raw.lstrip().startswith("- ") else "map"
    return "map"


def _flow(raw: str, where: str) -> dict:
    inner = raw.strip()[1:-1]
    if "{" in inner:
        raise ValueError(f"{where}: nested flow maps unsupported")
    out = {}
    # Split on commas OUTSIDE quotes: `modes: "off,on"` is one value, not two.
    parts, buf, q = [], [], None
    for ch in inner:
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == ",":
            parts.append("".join(buf)); buf = []; continue
        buf.append(ch)
    parts.append("".join(buf))
    for pair in parts:
        if not pair.strip():
            continue
        k, sep, v = pair.partition(":")
        if not sep:
            raise ValueError(f"{where}: flow entry without ':': {pair!r}")
        out[k.strip()] = _scalar(v)
    return out


def _scalar(v):
    v = v.strip().strip('"').strip("'")
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v


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


def api_key() -> str:
    for var in ("LITELLM_MASTER_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    env = REPO / "config" / "clients" / "env.sh"
    if env.exists():
        for var in ("LITELLM_MASTER_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            m = re.search(rf'^\s*export\s+{var}=["\']?([^"\'\s]+)',
                          env.read_text(), re.M)
            if m:
                return m.group(1)
    raise RuntimeError("no LiteLLM API key found — is the stack running?")


def litellm_healthy(timeout: int = 5) -> bool:
    try:
        urllib.request.urlopen(f"{LITELLM}/health/liveliness", timeout=timeout)
        return True
    except Exception:
        return False


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
    return {"context_length": ctx,
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


# ── profile / model selection ───────────────────────────────────────────────

def parse_profile(tier: str) -> dict:
    """{capability: {active, context, ...}} from the tier's own YAML."""
    text = (REPO / "config" / "profiles" / f"{tier}.yaml").read_text()
    caps = {}
    for m in re.finditer(r'^([a-z_]+):\n((?:[ \t]+.*\n)+)', text, re.M):
        name, body = m.group(1), m.group(2)
        if name not in ("architecture", "implementation", "review", "fast",
                        "completion", "embeddings"):
            continue
        f = {k: v.strip() for k, v in
             re.findall(r'^\s+([a-z_]+):[ \t]*(.*?)[ \t]*(?:#.*)?$', body, re.M)}
        active = f.get("active", "")
        caps[name] = {
            "active": active,
            "context": int(f["context"]) if f.get("context", "").isdigit() else 0,
            # No backend, or an explicit disable, means the capability does not
            # ship — it must not be benchmarked as though it did.
            "enabled": bool(active) and active.lower() not in
                       ("none", "false", "disabled"),
        }
    return caps


def select(profile: str = None, explicit=None) -> dict:
    """Resolve models. Selection MODE is recorded because a run of candidate
    tags on this machine is not a benchmark of a profile."""
    explicit = [t for t in dict.fromkeys(explicit or []) if t]
    gb = ram_gb()
    detected = tier_for_gb(gb)
    cfg = load_config()
    resolve = cfg.get("resolve", {})

    if profile is None and explicit:
        mode, tier, caps = EXPLICIT, None, {}
    else:
        tier = detected if profile in (None, "auto") else profile
        if tier not in TIERS:
            raise ValueError(f"unknown profile {tier!r}")
        caps = parse_profile(tier)
        mode = PROFILE_PLUS if explicit else PROFILE

    models = {}
    for cap, spec in caps.items():
        if not spec["enabled"] or cap == "embeddings":
            continue
        tag = resolve.get(spec["active"], spec["active"])
        # Deduplicate: profiles deliberately point several roles at one resident
        # model so Ollama keeps a single copy.
        e = models.setdefault(tag, {"capabilities": [], "in_profile": True,
                                    "profile_context": spec["context"]})
        e["capabilities"].append(cap)
    for tag in explicit:
        tag = resolve.get(tag, tag)
        models.setdefault(tag, {"capabilities": [], "in_profile": False,
                                "profile_context": 0})

    have = installed()
    for tag, e in models.items():
        e["installed"] = tag in have or f"{tag}:latest" in have
        e["digest"] = (have.get(tag, {}).get("digest") or "")[:12]
        e["size"] = int(have.get(tag, {}).get("size") or 0)
        e.update(model_info(tag) if e["installed"] else {"context_length": 0})
    return {"mode": mode, "ram_gb": gb, "detected_tier": detected,
            "tier": tier, "models": models}


# ── temporary LiteLLM aliases ───────────────────────────────────────────────

def alias_name(model: str, mode: str, context: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"{ALIAS_PREFIX}{slug}-{mode}-{context // 1024}k"


def build_alias(model: str, mode: str, context: int, ceiling: int,
                preset: dict) -> dict:
    """One deployment carrying VENDOR settings.

    They live in the alias because this stack IGNORES client generation
    parameters — measured, max_tokens of 50 and of 300 both returned 1492
    completion tokens.
    """
    think = {"off": False, "on": True,
             "low": "low", "medium": "medium", "high": "high"}.get(mode)
    params = {"model": f"ollama_chat/{model}",
              "api_base": "os.environ/OLLAMA_URL",
              "num_ctx": context + ceiling,
              "num_predict": ceiling,
              "keep_alive": "10m", **preset}
    if think is not None:
        params["think"] = think
    return {"model_name": alias_name(model, mode, context),
            "litellm_params": params,
            "model_info": {"max_input_tokens": int(context * PRECALL_MARGIN),
                           "max_output_tokens": ceiling,
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


def runtime_dir() -> Path:
    p = state_dir() / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


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
    _compose(["up", "-d", "--force-recreate", "litellm"], [override])
    ok = _wait_healthy()
    got = set(aliases()) if ok else set()
    want = {e["model_name"] for e in entries}
    return {"ok": ok and want <= got, "installed": sorted(want & got),
            "missing": sorted(want - got),
            "production": sorted(a for a in got if a.startswith("ailocal-"))}


def restore() -> dict:
    """Return to production config and PROVE it. Runs on success, error and
    interrupt — a benchmark must never leave aliases in a user's runtime."""
    _compose(["up", "-d", "--force-recreate", "litellm"])
    ok = _wait_healthy()
    got = set(aliases()) if ok else set()
    leaked = sorted(a for a in got if a.startswith(ALIAS_PREFIX))
    for p in (runtime_dir() / "litellm", runtime_dir() / "docker-compose.bench.yml"):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    prod = sorted(a for a in got if a.startswith("ailocal-"))
    return {"restored": ok and not leaked and len(prod) >= 5,
            "production": prod, "leaked": leaked, "healthy": ok}


def _compose(args: list, extra=None):
    files = ["-f", str(REPO / "deploy/litellm/docker-compose.yml"),
             "-f", str(REPO / "deploy/searxng/docker-compose.yml")]
    for f in (extra or []):
        files += ["-f", str(f)]
    env = {**os.environ,
           "AILOCAL_STATE": os.environ.get(
               "AILOCAL_STATE",
               str(Path(state_dir()).parent))}
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


# ── external engine ─────────────────────────────────────────────────────────

def run_lm_eval(alias: str, task: str, limit: int, out_dir: Path,
                timeout: int = 7200, sample_timeout: int = 300) -> dict:
    """Invoke lm-evaluation-harness against an authenticated LiteLLM alias.

    lm-eval owns the dataset, the prompting, the extraction and the scoring.
    ailocal supplies only the endpoint, the credential and the alias.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(venv_bin("lm_eval")),
           "--model", "local-chat-completions",
           "--model_args", (f"base_url={LITELLM}/v1/chat/completions,"
                            f"model={alias},num_concurrent=1,max_retries=1,"
                            f"tokenized_requests=False,"
                            # PER-SAMPLE wall limit. A thinking model with a
                            # 32,768 budget will fill it: one gsm8k question ran
                            # 6 minutes generating to the ceiling. The ceiling is
                            # NOT reduced — this bounds real-world usability so a
                            # single runaway cannot stall the screen.
                            f"timeout={sample_timeout}"),
           "--tasks", task,
           "--apply_chat_template",
           # humaneval_plus/mbpp_plus execute generated code. lm-eval requires
           # explicit consent; execution is the whole point of EvalPlus.
           "--confirm_run_unsafe_code",
           # Per-sample records. Without these a score cannot be audited, and an
           # EXTRACTION failure is indistinguishable from a model failure — the
           # exact confusion that made six models look like they scored 0.000 on
           # humaneval_plus.
           "--log_samples",
           # Our corrected task definitions (extraction only; datasets, tests
           # and metrics remain lm-eval's).
           "--include_path", str(REPO / "config" / "benchmark-tasks"),
           "--output_path", str(out_dir)]
    if limit:
        cmd += ["--limit", str(limit)]
    started = time.time()
    before = telemetry()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "OPENAI_API_KEY": api_key()})
    wall = time.time() - started

    scores = {}
    for f in sorted(out_dir.rglob("results_*.json")):
        try:
            d = json.loads(f.read_text())
            for tname, metrics in (d.get("results") or {}).items():
                scores[tname] = {k: v for k, v in metrics.items()
                                 if isinstance(v, (int, float))}
        except Exception:
            pass
    a = audit(out_dir)
    return {"alias": alias, "task": task, "returncode": r.returncode,
            "audit": a, "confidence": a["confidence"],
            "wall_seconds": round(wall, 1), "scores": scores,
            "telemetry_before": before, "telemetry_after": telemetry(),
            "stderr_tail": r.stderr[-600:] if r.returncode else ""}


def _stream_timed(alias: str, prompt: str, key: str, timeout: int) -> dict:
    """One streamed request. TTFT is the wall time to the FIRST content token.

    Streaming is the only way to separate prefill from decode here: LiteLLM does
    not expose Ollama's prompt_eval_duration, so without TTFT a decode rate would
    silently include the prefill.
    """
    payload = {"model": alias, "messages": [{"role": "user", "content": prompt}],
               "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(
        f"{LITELLM}/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    started = time.time()
    ttft, usage, chunks = None, {}, 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                continue
            if d.get("usage"):
                usage = d["usage"]
            delta = ((d.get("choices") or [{}])[0].get("delta") or {})
            if ttft is None and (delta.get("content") or delta.get("reasoning_content")):
                ttft = time.time() - started
            chunks += 1
    wall = time.time() - started
    return {"ttft_s": round(ttft, 3) if ttft else None,
            "wall_seconds": round(wall, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "chunks": chunks}


#: A real prefill on this hardware runs 200-1,200 tok/s. Anything far above is a
#: cache hit, not a fast machine — measured, a cache-served prefill reported
#: 1,196,380 tok/s while looking entirely normal on token counts alone.
IMPLAUSIBLE_INPUT_RATE = 20_000


def _stats(values: list) -> dict:
    vals = sorted(v for v in values if v)
    if not vals:
        return {"n": 0}
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"median": round(median, 1), "min": round(vals[0], 1),
            "max": round(vals[-1], 1), "n": len(vals),
            "cv": round((var ** 0.5) / mean, 3) if mean else None}


def cold_load_seconds(model: str, alias: str, key: str = None) -> float:
    """Time a genuine cold load: evict, then send a one-token request.

    Kept SEPARATE from the rate probes — folding a model load into a prefill rate
    is how a 30 GB model looks slow at inference when it was actually loading.
    """
    key = key or api_key()
    unload(model)
    time.sleep(3)
    r = _stream_timed(alias, "Reply with one word: ok", key, 900)
    return r["wall_seconds"]


def throughput_probe(alias: str, model: str, reps: int = 3,
                     timeout: int = 900) -> dict:
    """Operational rates, measured through the SAME authenticated path.

    These are END-TO-END: they include HTTP, queueing and the proxy. Ollama's
    internal counters are NOT exposed through LiteLLM, so:

        native_prompt_eval_tps  = unavailable
        native_generation_tps   = unavailable

    Never call these native throughput. They never touch a quality score.
    """
    key = api_key()
    out = {"alias": alias, "model": model,
           "native_prompt_eval_tps": "unavailable",
           "native_generation_tps": "unavailable"}

    out["cold_load_seconds"] = cold_load_seconds(model, alias, key)
    # Everything below runs RESIDENT, so load time cannot pollute a rate.

    prefill, decode, invalid = [], [], []
    for rep in range(reps):
        # A unique nonce AND unique body per repetition: measured, a changed
        # leading nonce alone does NOT bust llama.cpp's cache — only different
        # content forces a real prefill.
        seed = 7 + rep * 13
        body = "\n".join(f"row {i}: value {(i * seed) % 991}" for i in range(4000))
        p = (f"# sample {rep}-{seed}\nRead this table.\n{body}\n"
             f"Reply with exactly one word: ok")
        r = _stream_timed(alias, p, key, timeout)
        rate = (r["prompt_tokens"] / r["wall_seconds"]
                if r["prompt_tokens"] and r["wall_seconds"] else None)
        r["end_to_end_input_rate"] = round(rate, 1) if rate else None
        if rate and rate > IMPLAUSIBLE_INPUT_RATE:
            r["validity"] = "INVALID_CACHE_CONTAMINATION"
            invalid.append(r)
        else:
            r["validity"] = "VALID"
            prefill.append(r)

        # Same task contract for every model, and a NATURAL one — not repeated
        # filler, which some models refuse or truncate.
        d = _stream_timed(
            alias,
            f"Write a clear explanation of how a hash table works, including "
            f"collision handling and resizing. Aim for about 600 words. "
            f"(variant {rep})", key, timeout)
        gen = (d["wall_seconds"] - d["ttft_s"]) if d.get("ttft_s") else None
        if gen and gen > 0 and d.get("completion_tokens"):
            d["end_to_end_output_rate"] = round(d["completion_tokens"] / gen, 1)
        elif d.get("completion_tokens") and d["wall_seconds"]:
            d["conservative_end_to_end_output_rate"] = round(
                d["completion_tokens"] / d["wall_seconds"], 1)
        decode.append(d)

    out["prefill_samples"] = prefill + invalid
    out["decode_samples"] = decode
    out["end_to_end_input_rate"] = _stats(
        [s["end_to_end_input_rate"] for s in prefill])
    out["end_to_end_output_rate"] = _stats(
        [s.get("end_to_end_output_rate") for s in decode])
    out["conservative_end_to_end_output_rate"] = _stats(
        [s.get("conservative_end_to_end_output_rate") for s in decode])
    out["ttft_s"] = _stats([s.get("ttft_s") for s in decode])
    out["invalid_cache_contaminated"] = len(invalid)
    return out


def audit(out_dir: Path, n: int = 4) -> dict:
    """Prove extraction did not destroy the answer, before ranking anyone.

    Three defects in a row were interface faults, not model faults, and each
    looked exactly like a model failing. So no suite is trusted until a few
    samples are checked: raw response vs what was actually executed.

    Returns a CONFIDENCE grade. Only HIGH may influence a recommendation.
    """
    samples = sorted(out_dir.rglob("samples_*.jsonl"))
    if not samples:
        return {"confidence": "LOW", "reason": "no per-sample records to audit",
                "checked": 0}
    rows = [json.loads(l) for l in samples[0].read_text().splitlines() if l.strip()][:n]
    checked, empty, shrunk = [], 0, 0
    for r in rows:
        raw = "".join(x for x in _flatten(r.get("resps")))
        ext = "".join(x for x in _flatten(r.get("filtered_resps")))
        # Extraction legitimately drops prose, but code that vanishes or
        # collapses is the signature of an interface fault.
        has_code = "def " in ext or "return" in ext
        if not ext.strip() or not has_code:
            empty += 1
        elif raw and len(ext) < len(raw) * 0.30:
            shrunk += 1
        checked.append({"raw_chars": len(raw), "extracted_chars": len(ext),
                        "has_code": has_code,
                        "extract_tail": ext[-90:]})
    bad = empty + shrunk
    if empty:
        grade, why = "INVALID_TASK_INTERFACE", (
            f"{empty}/{len(rows)} samples extracted to no executable code — "
            f"the harness discarded the answer")
    elif shrunk:
        grade, why = "LOW", (f"{shrunk}/{len(rows)} samples lost >70% of the "
                             f"response during extraction")
    else:
        grade, why = "HIGH", (f"{len(rows)} samples audited: extraction "
                              f"preserved executable code")
    return {"confidence": grade, "reason": why, "checked": len(rows),
            "samples": checked, "suspect": bad}


def _flatten(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _flatten(x)


def expected_wall_seconds_per_correct_sample(batch_wall: float, samples: int,
                                             success_rate: float):
    """Wall seconds spent per CORRECT solution.

    An earlier report divided the whole batch's wall time by the success rate,
    which yields "seconds to run 40 samples, inflated" — not a per-sample figure,
    and roughly 40x too large. It made qwen3.5:4b look like 127 s per correct
    answer when the true value is 3.18 s.

    Equivalent to batch_wall / (samples * success_rate), i.e. batch wall divided
    by the number of samples that actually passed.
    """
    if not samples or not success_rate:
        return None
    return batch_wall / (samples * success_rate)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_dir(run_id: str) -> Path:
    p = state_dir() / "runs" / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── client integration backend ──────────────────────────────────────────────
# A SECOND execution backend, not a second benchmark system. Everything after
# LiteLLM is unchanged; only the thing issuing the request differs.
#
# Model benchmarks answer "which model performs better". These answer "which
# client provides a working engineering workflow" — session persistence,
# compaction, tool routing, MCP, hooks, retries. lm-eval cannot see any of that
# because it bypasses the client entirely.
#
# Uses each client's OWN resume/continue support rather than wrapping it:
#   claude  -p --resume <id>      (non-interactive print mode)
#   codex   exec resume --last
#: EXACT session identity only. `claude --continue` and `codex exec resume
#: --last` select the most recent session on the machine — which may belong to
#: another terminal, another repository, a manual session, or a concurrent run.
#: For automated benchmarking that is a correctness hazard, not a convenience.
CLIENTS = {
    "claude-local": {
        "bin": "claude-local",
        "start": lambda p: ["-p", "--output-format", "json", p],
        "resume": lambda sid, p: ["-p", "--output-format", "json",
                                  "--resume", sid, p],
    },
    "codex-local": {
        "bin": "codex-local",
        "start": lambda p: ["exec", "--json", p],
        # `codex exec resume <SESSION_ID> <PROMPT>` — positional, verified from
        # `codex exec resume --help`. NEVER --last.
        "resume": lambda sid, p: ["exec", "resume", sid, "--json", p],
    },
}

#: Opt-in, manual debugging only. Never the benchmark path.
IMPLICIT_RESUME = {"claude-local": ["--continue"],
                   "codex-local": ["exec", "resume", "--last"]}


#: Defines claude-local / codex-local. Generated by sync-models.py.
CLIENT_ENV = REPO / "config" / "clients" / "configure.zsh"


def _shq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


class SessionLost(RuntimeError):
    """The exact session id could not be captured or resumed. Fail closed."""


def run_client_turn(client: str, prompt: str, session: str, cwd: Path,
                    timeout: int = 900) -> dict:
    """One turn through a real client, resuming an EXACT session id.

    `session` is None only for the first turn. Any later turn without an id is a
    hard failure — silently falling back to "latest session" could attach the
    benchmark to the user's own conversation.
    """
    spec = CLIENTS[client]
    args = spec["resume"](session, prompt) if session else spec["start"](prompt)
    # claude-local / codex-local are ZSH FUNCTIONS, not binaries: they export the
    # ailocal base URL, API key and capability-alias slots before calling the
    # real CLI. `command -v` finds them, but subprocess cannot exec them — so
    # they are invoked through a shell that sources their definition. Calling
    # `claude` directly would bypass ailocal routing entirely.
    inner = " ".join([spec["bin"]] + [_shq(a) for a in args])
    cmd = ["zsh", "-c", f"source {_shq(str(CLIENT_ENV))} >/dev/null 2>&1; {inner}"]
    before = telemetry()
    started = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(cwd))
        rc, out, err = r.returncode, r.stdout, r.stderr
        timed_out = False
    except subprocess.TimeoutExpired as e:
        rc, out, err, timed_out = None, (e.stdout or b"").decode()[:4000], "", True
    wall = time.time() - started

    session_id, tool_calls, usage = None, 0, {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Codex names it thread_id (emitted on `thread.started`); Claude Code
        # names it session_id. Same concept, different key.
        session_id = (d.get("session_id") or d.get("sessionId")
                      or d.get("thread_id") or session_id)
        if d.get("type") in ("tool_use", "tool_call", "function_call"):
            tool_calls += 1
        for key in ("usage", "token_usage"):
            if isinstance(d.get(key), dict):
                usage = d[key]
    # claude -p --output-format json emits one object, not a stream
    if not session_id:
        try:
            d = json.loads(out)
            session_id = d.get("session_id")
            usage = d.get("usage") or usage
            tool_calls = tool_calls or len(d.get("tool_uses") or [])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "client": client, "requested_session": session,
        "command": " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""),
        "returncode": rc,
        "timed_out": timed_out, "wall_seconds": round(wall, 2),
        "session_id": session_id, "tool_calls": tool_calls,
        "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
        "completion_tokens": (usage.get("output_tokens")
                              or usage.get("completion_tokens")),
        "stdout_tail": out[-1200:], "stderr_tail": err[-600:],
        "telemetry_before": before, "telemetry_after": telemetry(),
        "crashed": rc not in (0, None),
    }


def client_version(client: str) -> str:
    try:
        r = subprocess.run(
            ["zsh", "-c", f"source {_shq(str(CLIENT_ENV))} >/dev/null 2>&1; "
                          f"{CLIENTS[client]['bin']} --version"],
            capture_output=True, text=True, timeout=30)
        return r.stdout.strip()[:60]
    except Exception:
        return "unknown"


def disposable_worktree(run_id: str) -> Path:
    """An isolated git worktree per run.

    Scenarios that mutate code must never touch the live checkout, and two runs
    must never share a tree.
    """
    wt = state_dir() / "worktrees" / run_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                    str(wt), "HEAD"], capture_output=True, text=True, timeout=120)
    return wt


def remove_worktree(wt: Path) -> None:
    """Remove the tree; evidence already lives in the run directory."""
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force",
                    str(wt)], capture_output=True, text=True, timeout=120)


def run_client_scenario(client: str, turns: list, cwd: Path,
                        timeout: int = 900) -> dict:
    """A multi-turn session through one client, on ONE exact session id.

    Fails closed: if the first turn yields no session id, or a later turn
    reports a different one, the scenario stops. Continuing would silently
    measure a different conversation — possibly the user's own.
    """
    records, session = [], None
    error = None
    for i, prompt in enumerate(turns):
        if i and not session:
            error = ("SESSION_LOST: no session id captured from turn 1; "
                     "refusing to fall back to implicit latest-session resume")
            break
        rec = run_client_turn(client, prompt, session, cwd, timeout=timeout)
        rec["turn"] = i + 1
        got = rec.get("session_id")
        if i == 0:
            session = got
            rec["session_continuous"] = bool(session)
            if not session:
                records.append(rec)
                error = ("SESSION_LOST: client returned no session id on turn 1")
                break
        else:
            # An id that changes mid-scenario means the client started a NEW
            # conversation; every later turn would measure something else.
            rec["session_continuous"] = got in (None, session)
            if not rec["session_continuous"]:
                records.append(rec)
                error = (f"SESSION_DIVERGED: expected {session}, got {got}")
                break
        records.append(rec)
        if rec["crashed"] or rec["timed_out"]:
            error = "CLIENT_CRASH" if rec["crashed"] else "CLIENT_TIMEOUT"
            break
    return {
        "client": client, "turns_planned": len(turns),
        "turns_completed": len(records),
        "session_id": session,
        "session_continuous": bool(records) and all(
            r["session_continuous"] for r in records),
        "error": error,
        "cwd": str(cwd),
        "tool_calls": sum(r["tool_calls"] for r in records),
        "crashes": sum(1 for r in records if r["crashed"]),
        "timeouts": sum(1 for r in records if r["timed_out"]),
        "wall_seconds": round(sum(r["wall_seconds"] for r in records), 1),
        "ttft_first_turn": records[0]["wall_seconds"] if records else None,
        "records": records,
    }
