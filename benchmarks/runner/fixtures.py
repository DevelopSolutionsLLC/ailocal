"""fixtures.py — deterministic repository-like prompts, calibrated to a token target.

Two requirements drive this file.

TOKEN COUNTS ARE MEASURED, NOT ESTIMATED. Characters-per-token varies by model
and by content, so a fixture sized from character count is not the size it
claims. Each fixture is calibrated against the backend's own
prompt_eval_count until it lands within +/-1% of target. Tokenizers differ
between models, so calibration is PER MODEL and cached.

THE CONTENT MUST BE REPOSITORY-LIKE. A prompt of one repeated token measures
memory bandwidth, not comprehension. These fixtures carry source, interfaces,
tests, configuration, documentation, issue history, distractors and cross-file
dependencies, so a long-context run exercises retrieval rather than capacity.

EVIDENCE PLACEMENT is controlled. Two retrieval variants exist:
  * "late"        — the fact needed to answer sits near the end
  * "distributed" — the fact is split across several files and positions
A model that accepts 64K but cannot use it scores differently on these two.
"""
import hashlib
import json
import os

SEED = 42  # fixtures are deterministic; a run is reproducible from this alone


def _rng(seed):
    """Tiny deterministic PRNG. Avoids depending on random module global state,
    which other harness code could perturb between calls."""
    x = seed & 0xFFFFFFFF
    while True:
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        yield x


def _service(idx, g):
    name = f"svc_{idx}"
    return f'''# ── {name}/handler.py ─────────────────────────────────────────────
from typing import Optional
from .config import {name.upper()}_TIMEOUT, RETRY_BUDGET

class {name.title().replace("_", "")}Handler:
    """Handles inbound requests for {name}. See docs/{name}.md."""

    def __init__(self, session, cache=None):
        self.session = session
        self.cache = cache
        self._budget = RETRY_BUDGET

    def fetch(self, key: str) -> Optional[dict]:
        if self.cache and key in self.cache:
            return self.cache[key]
        for attempt in range({next(g) % 3 + 2}):
            resp = self.session.get(f"/{name}/{{key}}", timeout={name.upper()}_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                self._budget -= 1
                if self._budget <= 0:
                    raise RuntimeError("retry budget exhausted for {name}")
        return None

# ── {name}/config.py ──────────────────────────────────────────────────────
{name.upper()}_TIMEOUT = {next(g) % 20 + 10}
RETRY_BUDGET = {next(g) % 4 + 3}

# ── tests/test_{name}.py ──────────────────────────────────────────────────
def test_{name}_returns_none_on_miss(handler):
    assert handler.fetch("absent") is None

def test_{name}_honours_budget(handler):
    handler._budget = 1
    # exhausting the budget must raise, not silently return None
    ...

# ── docs/{name}.md ────────────────────────────────────────────────────────
# {name} retries on 429 only. A 5xx is NOT retried, by decision ADR-{idx:03d}.
'''


def build(target_tokens, variant="late", secret=None, scale=1.0, nonce=0):
    """Deterministic text of roughly `target_tokens`, scaled by `scale`.
    `secret` is the fact a retrieval question depends on.

    `nonce` MAKES THE PROMPT NOVEL WITHOUT CHANGING ITS SIZE, and it is not
    optional for warm runs. Re-sending an identical prompt reuses the KV cache:
    prompt_eval_count stays the same while prompt_eval_duration collapses, so
    throughput reads as ~840,000 tok/s -- measuring cache lookup, not prompt
    processing. This repository has already published a wrong 1705 tok/s figure
    from exactly that mistake (see config/profiles/64gb.yaml). Warm means the
    MODEL is resident, never that the PROMPT was already evaluated.
    """
    g = _rng(SEED + target_tokens + nonce * 7919)
    secret = secret or f"BUDGET-{(target_tokens // 1024)}-{SEED}"
    n = max(1, int(target_tokens * scale / 220))
    parts = [_service(i + nonce * 100000, g) for i in range(n)]

    marker = (f"\n# OPERATIONAL NOTE: the effective retry budget in production "
              f"is overridden to {secret} by deploy/override.yaml.\n")
    if variant == "late":
        parts.append(marker)
    else:  # distributed — split the fact so it cannot be found by reading the tail
        half = len(parts) // 2
        parts.insert(max(0, half - 1),
                     "\n# OPERATIONAL NOTE part 1: the effective retry budget is overridden\n")
        parts.append(f"\n# OPERATIONAL NOTE part 2: ...to {secret} by deploy/override.yaml.\n")
    return "".join(parts), secret


def calibrate(measure, target, variant="late", tol=0.01, max_iter=8):
    """Find a scale whose MEASURED token count is within tol of target.

    `measure(text) -> int` must return the backend's own prompt_eval_count.
    Returns (text, measured_tokens, scale, iterations). Raises if it cannot
    converge, because silently shipping a mis-sized fixture would make every
    downstream comparison a lie.
    """
    scale = 1.0
    last = None
    for i in range(1, max_iter + 1):
        text, _ = build(target, variant, scale=scale)
        got = measure(text)
        last = (text, got, scale, i)
        if got and abs(got - target) / target <= tol:
            return last
        if not got:
            raise RuntimeError("backend reported no prompt_eval_count")
        scale *= target / got
    return last  # caller records the miss rather than pretending it hit


def cache_path(bench_dir):
    return os.path.join(bench_dir, "fixtures", "calibration.json")


def load_cache(bench_dir):
    p = cache_path(bench_dir)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def save_cache(bench_dir, cache):
    p = cache_path(bench_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]
