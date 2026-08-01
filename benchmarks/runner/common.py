"""common.py — shared plumbing for the benchmark harness.

Design rules this file exists to enforce:

  * Native counters beat wall-clock. Ollama reports prompt_eval_count/duration
    and eval_count/duration; tok/s is computed from those, never from elapsed
    time, which would silently fold load and queueing into throughput.
  * Token counts are MEASURED, never estimated from characters. A fixture is
    only accepted when the backend's own prompt_eval_count lands within +/-1%
    of target.
  * The machine is protected. This runs on a 64 GB laptop; a pathological cell
    (two resident 26B models plus a 64K generation) must abort, not swap the
    box to death.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = os.path.join(ROOT, "benchmarks")
RESULTS = os.path.join(BENCH, "reports")
SCHEMA_VERSION = 1


def manifest():
    with open(os.path.join(BENCH, "manifest.json")) as f:
        return json.load(f)


def post(url, payload, timeout=1800):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


# ── machine state ───────────────────────────────────────────────────────────
def swap_used_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=10).stdout
        return float(out.split("used =")[1].split("M")[0].strip()) / 1024
    except Exception:
        return None


def free_memory_pct():
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True,
                             text=True, timeout=15).stdout
        for line in out.splitlines():
            if "free percentage" in line:
                return int(line.rsplit(":", 1)[1].strip().rstrip("%"))
    except Exception:
        pass
    return None


def free_disk_gb(path="/Users/Shared"):
    try:
        u = shutil.disk_usage(path)
        return u.free / 2 ** 30
    except Exception:
        return None


def thermal_state():
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if "CPU_Speed_Limit" in line:
                return line.strip()
    except Exception:
        pass
    return None


def machine_snapshot():
    return {"swap_used_gb": swap_used_gb(), "free_memory_pct": free_memory_pct(),
            "free_disk_gb": free_disk_gb(), "thermal": thermal_state()}


def safety_check(limits, baseline_swap):
    """Returns a reason string when the machine is too loaded to continue."""
    fm = free_memory_pct()
    if fm is not None and fm < limits["min_free_memory_pct"]:
        return f"free memory {fm}% below {limits['min_free_memory_pct']}%"
    sw = swap_used_gb()
    if sw is not None and baseline_swap is not None:
        if sw - baseline_swap > limits["max_swap_growth_gb"]:
            return f"swap grew {sw - baseline_swap:.1f} GB beyond baseline"
    dk = free_disk_gb()
    if dk is not None and dk < limits["min_free_disk_gb"]:
        return f"free disk {dk:.1f} GB below {limits['min_free_disk_gb']} GB"
    return None


# ── model residency ─────────────────────────────────────────────────────────
def loaded_models(ollama):
    try:
        return {m["name"]: m for m in get(f"{ollama}/api/ps").get("models", [])}
    except Exception:
        return {}


def unload(ollama, tag):
    """keep_alive 0 evicts. Cold runs are meaningless without this."""
    try:
        post(f"{ollama}/api/chat",
             {"model": tag, "keep_alive": 0, "messages": []}, timeout=120)
    except Exception:
        pass
    for _ in range(20):
        if tag not in loaded_models(ollama):
            return True
        time.sleep(1)
    return tag not in loaded_models(ollama)


def unload_all_except(ollama, keep=()):
    for name in list(loaded_models(ollama)):
        if name not in keep and not name.startswith("nomic"):
            unload(ollama, name)


# ── native-counter extraction ───────────────────────────────────────────────
def timings_from_ollama(r):
    """tok/s from NATIVE counters only. Returns None where the backend did not
    report, rather than substituting a wall-clock estimate."""
    pe, ped = r.get("prompt_eval_count"), r.get("prompt_eval_duration")
    ec, ed = r.get("eval_count"), r.get("eval_duration")
    out = {
        "load_duration_s": (r.get("load_duration") or 0) / 1e9,
        "prompt_eval_count": pe,
        "prompt_eval_duration_s": (ped / 1e9) if ped else None,
        "eval_count": ec,
        "eval_duration_s": (ed / 1e9) if ed else None,
        "total_duration_s": (r.get("total_duration") or 0) / 1e9,
        "prompt_tok_s": None,
        "gen_tok_s": None,
    }
    if pe and ped:
        out["prompt_tok_s"] = pe / (ped / 1e9)
    if ec and ed:
        out["gen_tok_s"] = ec / (ed / 1e9)
    return out


# ── checkpointed result store ───────────────────────────────────────────────
def results_path(name="runs.jsonl"):
    os.makedirs(RESULTS, exist_ok=True)
    return os.path.join(RESULTS, name)


def append_result(rec, name="runs.jsonl"):
    """Append-and-flush after EVERY run. A benchmark that loses hours of work to
    an interrupt is not resumable, whatever its --resume flag claims."""
    with open(results_path(name), "a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())


def completed_keys(name="runs.jsonl"):
    """Identity of a finished run, so resume skips exactly what is done."""
    done = set()
    p = results_path(name)
    if not os.path.exists(p):
        return done
    with open(p) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("errors"):
                continue
            done.add(run_key(r))
    return done


def run_key(r):
    return "|".join(str(r.get(k)) for k in (
        "task_suite", "task_id", "model_tag", "requested_context_tokens",
        "reasoning_mode_requested", "cold_or_warm", "repetition"))


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None


def software_versions(ollama):
    v = {"git_commit": git_commit()}
    try:
        v["ollama"] = get(f"{ollama}/api/version").get("version")
    except Exception:
        v["ollama"] = None
    try:
        v["litellm"] = subprocess.run(
            ["docker", "exec", "ailocal-litellm", "python", "-c",
             "import importlib.metadata as m;print(m.version('litellm'))"],
            capture_output=True, text=True, timeout=60).stdout.strip() or None
    except Exception:
        v["litellm"] = None
    return v
