"""RULER long-context adapter.

NVIDIA's RULER measures the context length a model can ACTUALLY use, as opposed
to the one its card claims. This module is the smallest thing that lets it score
models through ailocal's authenticated LiteLLM path. It owns no prompts, no
datasets and no metrics.

    official RULER generator  (scripts/data/prepare.py)
      -> authenticated LiteLLM -> bench-* alias -> Ollama -> model
      -> official RULER scorer (scripts/eval/synthetic/constants.py)

Version choice: `main`'s README deprecates its *evaluation pipeline* — the
Docker image, run.sh and config_models.sh — but NOT its data generators. The
current `rulerv1-ns` branch carries no pipeline of its own; it delegates to
NeMo-Skills, which clones this very repository and shells out to
`scripts/data/prepare.py`. So main's generators ARE the current official ones.
What v1-ns replaces is the serving layer, which is exactly the part ailocal
supplies. RULER is pinned by commit and lives outside the repository.

Two behaviours of the upstream generator matter here:

* `prepare.py` shells out to bare `python` and its task scripts import
  `constants` and `tokenizer` as top-level modules — it expects its Docker
  container's environment. `_env()` reproduces that.
* It catches CalledProcessError, prints a success-shaped message and exits 0.
  A failed generation therefore LOOKS fine. Every call verifies the output file.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from benchmark import LITELLM, IMPLAUSIBLE_INPUT_RATE, venv_bin

#: Pinned upstream. Apache 2.0.
RULER_COMMIT = "c3f5e3b4f87f97e048793bb510a3a6b19a46bf3a"
RULER_REPO = "https://github.com/NVIDIA/RULER"

#: RULER v1 default. Changing it changes the samples, so it is recorded in the
#: manifest rather than exposed as a convenience flag.
SEED = 42

#: Model tag -> HuggingFace tokenizer. RULER sizes the haystack with the MODEL's
#: own tokenizer, which is what makes the realized context land near the target.
#: The `openai`/cl100k_base path in the deprecated README is for OpenAI's API and
#: would mis-size every model here. All five resolve without authentication.
TOKENIZERS = {
    "qwen3.5:2b": "Qwen/Qwen3.5-2B",
    "qwen3.5:4b": "Qwen/Qwen3.5-4B",
    "qwen3.5:9b": "Qwen/Qwen3.5-9B",
    "gpt-oss:20b": "openai/gpt-oss-20b",
    # gemma4:26b-mlx is the 26B-A4B mixture-of-experts, not a dense 26B.
    "gemma4:26b-mlx": "google/gemma-4-26B-A4B-it",
    "qwen3-coder:30b-a3b-q4_K_M": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
}

#: Official v1 task names, grouped by the capability they isolate. Kept separate
#: on purpose: upstream's ruler_score.py averages all 13 into a single number,
#: which hides exactly the difference we are trying to measure. A model that
#: retrieves a needle perfectly and cannot aggregate is not "average".
DIMENSIONS = {
    "retrieval": ["niah_single_1", "niah_single_2", "niah_single_3"],
    "multihop": ["vt"],
    "aggregation": ["cwe", "fwe"],
}

#: Generation budget RULER reserves from max_seq_length when sizing input.
#: scripts/data/synthetic/constants.py. NOT an output cap for us — the alias
#: still carries the full ceiling; these answers are simply short.
TOKENS_TO_GENERATE = {"niah": 128, "variable_tracking": 30,
                      "common_words_extraction": 120,
                      "freq_words_extraction": 50, "qa": 32}


def ruler_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(root) / "ailocal" / "benchmark" / "ruler"


def installed() -> bool:
    d = ruler_dir()
    return (d / "scripts" / "data" / "prepare.py").exists()


def head_commit() -> str:
    r = subprocess.run(["git", "-C", str(ruler_dir()), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def datasets_ready() -> dict:
    """The two data files the chosen tasks need, and whether they are REAL.

    english_words.json ships as a git-lfs pointer. Without `git lfs pull` it is
    a 132-byte text stub that json.load happily rejects only at generation time,
    after the model is already resident. Both aggregation tasks depend on it.
    """
    j = ruler_dir() / "scripts" / "data" / "synthetic" / "json"
    out = {}
    for name, need in (("english_words.json", 1 << 20),
                       ("PaulGrahamEssays.json", 1 << 20)):
        p = j / name
        size = p.stat().st_size if p.exists() else 0
        out[name] = {"present": p.exists(), "bytes": size,
                     "lfs_pointer": 0 < size < need}
    return out


def _env() -> dict:
    """RULER's task scripts expect their container's layout."""
    d = ruler_dir()
    env = dict(os.environ)
    env["PATH"] = f"{venv_bin('python').parent}{os.pathsep}{env.get('PATH','')}"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(d / "scripts" / "data"), str(d / "scripts" / "data" / "synthetic")])
    return env


def task_family(task: str) -> str:
    """`niah_single_2` -> `niah`; the family carries the metric."""
    import yaml  # provided by the benchmark venv
    with open(ruler_dir() / "scripts" / "synthetic.yaml") as f:
        return yaml.safe_load(f)[task]["task"]


def generate(task: str, tokenizer: str, seq_len: int, samples: int,
             out_dir: Path, seed: int = SEED) -> Path:
    """Official generation. Returns the jsonl path, or raises.

    `seq_len` is RULER's max_seq_length: input PLUS generation, per its own
    definition of "32K". Input is therefore sized to seq_len minus the task's
    tokens_to_generate, and that is the figure the context band is measured
    against.
    """
    out = out_dir / task / "test.jsonl"
    if out.exists() and sum(1 for _ in open(out)) == samples:
        return out
    d = ruler_dir()
    cmd = [str(venv_bin("python")), "prepare.py",
           "--save_dir", str(out_dir), "--benchmark", "synthetic",
           "--subset", "test", "--task", task,
           "--tokenizer_path", tokenizer, "--tokenizer_type", "hf",
           "--model_template_type", "base", "--prepare_for_ns",
           "--num_samples", str(samples), "--max_seq_length", str(seq_len),
           "--random_seed", str(seed)]
    r = subprocess.run(cmd, cwd=d / "scripts" / "data", env=_env(),
                       capture_output=True, text=True)
    # Upstream swallows failures and still prints "Prepare <task> with lines:".
    # Trust the artefact, never the message.
    if not out.exists():
        tail = (r.stderr or r.stdout or "")[-600:]
        raise RuntimeError(f"RULER generation failed for {task}:\n{tail}")
    n = sum(1 for _ in open(out))
    if n != samples:
        raise RuntimeError(f"RULER produced {n} samples for {task}, want {samples}")
    return out


def metric_fn(task: str):
    """The OFFICIAL metric, imported from the pinned checkout — never copied."""
    p = str(ruler_dir() / "scripts" / "eval" / "synthetic")
    if p not in sys.path:
        sys.path.insert(0, p)
    import constants  # RULER's scripts/eval/synthetic/constants.py
    return constants.TASKS[task_family(task)]["metric_fn"]


def score(task: str, preds: list, refs: list) -> float:
    """Percentage, computed exactly as upstream does."""
    return metric_fn(task)(preds, refs)


def classify(rec: dict, target_input: int, ceiling: int,
             tolerance: float = 0.02) -> str:
    """Validity for one scored sample. Order matters: a truncated or
    cache-served answer is not merely out of band, it is not evidence at all."""
    if rec.get("error"):
        return rec["error"]
    ct = rec.get("completion_tokens") or 0
    if ct >= ceiling:
        return "INVALID_TRUNCATED"
    pt = rec.get("prompt_tokens")
    # Wall time, not TTFT: these requests are not streamed. Wall includes decode,
    # so the implied prefill rate is an UNDER-estimate and cannot false-positive.
    # A cache-served 32K prefill once reported 1,196,380 tok/s here.
    wall = rec.get("wall_seconds")
    if pt and wall and wall > 0 and (pt / wall) > IMPLAUSIBLE_INPUT_RATE:
        return "INVALID_CACHE_CONTAMINATION"
    if pt and target_input:
        if abs(pt - target_input) / target_input > tolerance:
            return "OUTSIDE_CONTEXT_BAND"
    return "VALID"


def _text(alias: str, prompt: str, key: str, timeout: int) -> dict:
    """One scored request. Always LiteLLM; direct Ollama is never scored."""
    payload = {"model": alias, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        f"{LITELLM}/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    usage = d.get("usage") or {}
    return {"text": msg.get("content") or "",
            "wall_seconds": round(time.time() - started, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens")}


def run_task(task: str, alias: str, rows: list, key: str, ceiling: int,
             target_input: int, timeout: int = 1800,
             progress=None) -> dict:
    """Score one task. Invalid samples are recorded and EXCLUDED from the score.

    The prompt is `input` alone. RULER's `answer_prefix` is deliberately not
    sent: upstream's default format puts it in a `generation` field, which is an
    assistant prefill, and chat APIs reject or mangle prefills. That is the same
    mechanism that produced six 0.000 scores on humaneval_plus. NeMo-Skills
    added a `chat` format that drops it for exactly this reason.
    """
    samples, preds, refs = [], [], []
    for i, row in enumerate(rows):
        try:
            rec = _text(alias, row["input"], key, timeout)
            rec["error"] = None
        except urllib.error.HTTPError as e:
            rec = {"error": "ERROR", "detail": f"HTTP {e.code}"}
        except TimeoutError:
            rec = {"error": "TIMEOUT"}
        except Exception as e:  # noqa: BLE001
            rec = {"error": "ERROR", "detail": f"{type(e).__name__}: {e}"[:200]}
        state = classify(rec, target_input, ceiling)
        # `response` is truncated for storage only. Scoring below uses the FULL
        # text — a needle can sit past 500 characters of preamble.
        entry = {"index": row.get("index", i), "validity": state,
                 "prompt_tokens": rec.get("prompt_tokens"),
                 "completion_tokens": rec.get("completion_tokens"),
                 "wall_seconds": rec.get("wall_seconds"),
                 "expected": row["outputs"],
                 "response": (rec.get("text") or "")[:500]}
        samples.append(entry)
        if state == "VALID":
            preds.append(rec["text"])
            refs.append(row["outputs"])
        if progress:
            progress(i + 1, len(rows), state)
    valid = len(preds)
    return {"samples": samples,
            "valid": valid, "invalid": len(rows) - valid,
            "validity_counts": _counts(s["validity"] for s in samples),
            "realized_prompt_tokens": _median(
                s["prompt_tokens"] for s in samples if s["prompt_tokens"]),
            "target_input_tokens": target_input,
            "wall_seconds": round(sum(s["wall_seconds"] or 0 for s in samples), 1),
            "mean_wall_seconds": round(
                sum(s["wall_seconds"] or 0 for s in samples) / len(rows), 1),
            "completion_tokens_total": sum(
                s["completion_tokens"] or 0 for s in samples),
            # Only VALID samples reach the metric. An invalid cell is absence of
            # evidence, not a zero.
            "score": score(task, preds, refs) if preds else None}


def _counts(it) -> dict:
    out = {}
    for v in it:
        out[v] = out.get(v, 0) + 1
    return out


def _median(it):
    vals = sorted(it)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) // 2


def manifest(models: dict, seq_len: int, samples: int, ceiling: int) -> dict:
    """Everything needed to reproduce the run."""
    return {
        "ruler": {"repo": RULER_REPO, "commit": head_commit(),
                  "pinned_commit": RULER_COMMIT,
                  "branch_decision": "v1 generators+scorer from main; the "
                                     "deprecated part is the serving layer, "
                                     "which LiteLLM replaces. rulerv1-ns "
                                     "delegates to NeMo-Skills, which clones "
                                     "and invokes these same generators.",
                  "license": "Apache-2.0"},
        "transport": {"endpoint": f"{LITELLM}/v1/chat/completions",
                      "auth": "bearer (redacted)",
                      "scored_via": "LiteLLM only"},
        "generation": {"seed": SEED, "tokenizer_type": "hf",
                       "model_template_type": "base",
                       "answer_prefix_sent": False,
                       "max_seq_length": seq_len,
                       "samples_per_task": samples},
        "inference": {"output_ceiling": ceiling,
                      "num_ctx": seq_len + ceiling},
        "dimensions": DIMENSIONS,
        "models": models,
    }
