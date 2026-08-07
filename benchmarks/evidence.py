"""evidence.py — where durable benchmark state lives, and how it is
captured without leaking secrets.

MOVED VERBATIM from benchmark.py. This module is the LEAF of the benchmark
layering: it owns the state directories, the evidence bundle, redaction, and
capture of the LiteLLM container log. Nothing here imports another benchmark
module, which is what lets runtime/clients/engines all depend on it without a
cycle.

Capture lives here rather than in runtime because capturing the log IS evidence
collection -- and because apply_aliases/restore must call it, so placing it in
runtime would make runtime and evidence mutually dependent.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent   # benchmarks/ -> repo root

sys.path.insert(0, str(REPO / "lib"))
import policy  # noqa: E402  every root has one owner (ADR 009)


def state_dir() -> Path:
    """Benchmark state, under the one state root."""
    p = policy.state_root() / "benchmark"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tooling_dir() -> Path:
    """Third-party benchmark tooling. policy.py owns the path (ADR 009)."""
    return policy.benchmark_tooling_root()


def runtime_dir() -> Path:
    p = state_dir() / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p
#: Anything that could carry a credential out of the container environment.
_REDACT = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|api[_-]?key\W{1,3}\S+"
    r"|Authorization\W{1,3}\S+|LITELLM_MASTER_KEY\W{1,3}\S+)", re.I)


def redact(text: str) -> str:
    return _REDACT.sub("[REDACTED]", text or "")


def container_id(name: str = "ailocal-litellm") -> dict:
    """Identity of the container whose logs we are about to capture.

    Recorded so a post-restart bundle can never be mistaken for a pre-restart
    one: a different id means a different log buffer.
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}|{{.Created}}|{{.RestartCount}}",
             name], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            cid, created, restarts = r.stdout.strip().split("|")
            return {"id": cid[:12], "created": created, "restarts": restarts}
    except Exception:
        pass
    return {}


def capture_litellm_log(dest: Path, name: str = "ailocal-litellm") -> dict:
    """Persist the CURRENT container's log, then hash it. Call before recreate."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ident = container_id(name)
    text = ""
    try:
        r = subprocess.run(["docker", "logs", name], capture_output=True,
                           text=True, timeout=120)
        text = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001 — never mask the run's own failure
        text = f"[capture failed: {type(e).__name__}: {e}]"
    body = redact(text)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    return {"path": str(dest), "bytes": len(body),
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "container": ident, "captured_epoch": time.time()}


# ── durable evidence ────────────────────────────────────────────────────────
# `docker compose up --force-recreate` REPLACES the LiteLLM container, and the
# replacement starts with an empty log buffer. Both alias installation and
# restoration do that, and every candidate block ends in restore() — so the
# harness deleted the per-request evidence for the run it had just performed.
# `docker logs --since <window>` for a failed candidate returned zero lines,
# and its request-level cause is now permanently unrecoverable.
# Capture therefore happens BEFORE any recreate, never after.
EVIDENCE_COMPLETE, EVIDENCE_PARTIAL, EVIDENCE_MISSING = (
    "EVIDENCE_COMPLETE", "EVIDENCE_PARTIAL", "EVIDENCE_MISSING")


def evidence_state(bundle: dict, need_requests: bool = True) -> str:
    """Fail closed: silence is not proof that nothing happened."""
    logs = bundle.get("litellm_logs") or []
    if not logs:
        return EVIDENCE_MISSING
    if need_requests and all((e.get("bytes") or 0) == 0 for e in logs):
        return EVIDENCE_PARTIAL
    if not bundle.get("checksums"):
        return EVIDENCE_PARTIAL
    return EVIDENCE_COMPLETE


def evidence_dir(run_id: str) -> Path:
    d = run_dir(run_id) / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_dir(run_id: str) -> Path:
    p = state_dir() / "runs" / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p
