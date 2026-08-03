#!/usr/bin/env python3
"""run-planner-comparison.py — the authoritative planner comparison driver.

THIS IS ORCHESTRATION ONLY. Every mechanism it uses already exists and is
already tested: worktrees, alias lifecycle, routing, permissions, confinement,
evidence, sessions, restoration. Nothing here reimplements them, because a
second implementation drifts and then two runs of "the same" benchmark are not.

WHY IT EXISTS. Run 2 was driven ad hoc. That is how a comparison ended up
measuring one model three times, how candidates read an unrelated repository,
and how the ground truth stayed readable by absolute path. A comparison whose
setup is not reproducible cannot be re-run to check a result.

SAFE BY DEFAULT. With no arguments it does nothing: inference requires --all or
an explicit --candidate. --dry-run never opens the private mapping and never
starts a model.

BLINDNESS, STATED HONESTLY. This produces identity-stripped scoring copies, but
the operator who runs it can read the mapping file on disk. That is a real
limitation and is recorded in the manifest rather than described as blindness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import benchmark as B  # noqa: E402

PLANNER = Path.home() / ".local/state/ailocal/benchmark/planner"
RUBRIC = REPO / "config" / "planner-rubric.md"
CANDIDATES = ("candidate-a", "candidate-b", "candidate-c")
PERMISSIONS = {"allowed": "Read,Glob,Grep",
               "denied": "Bash,Write,Edit,Task,WebFetch,WebSearch",
               "mode": "default"}

INVALID_RUN_MANIFEST = "INVALID_RUN_MANIFEST"
INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
TIMING_UNQUALIFIED = "TIMING_UNQUALIFIED"
ENVIRONMENT_VERIFIED = "ENVIRONMENT_VERIFIED"
INCOMPLETE = "INCOMPLETE"


# ── hashing ─────────────────────────────────────────────────────────────────
def sha(path_or_text) -> str:
    if isinstance(path_or_text, Path):
        if not path_or_text.exists():
            return "MISSING"
        return hashlib.sha256(path_or_text.read_bytes()).hexdigest()[:16]
    return hashlib.sha256(str(path_or_text).encode()).hexdigest()[:16]


PROMPTS_VERIFIED = "PROMPTS_VERIFIED"
PROMPTS_MISSING = "PROMPTS_MISSING"
PROMPTS_DUPLICATED = "PROMPTS_DUPLICATED"
PROMPTS_MALFORMED = "PROMPTS_MALFORMED"
PROMPT_COUNT_INVALID = "PROMPT_COUNT_INVALID"
PROMPT_HASH_MISMATCH = "PROMPT_HASH_MISMATCH"

#: Bumped when the PARSER changes, so a hash computed by an older parser is
#: never silently compared against one computed by a newer one.
PROMPT_SCHEMA_VERSION = "handoff-Tn-v1"

_SECTION = "## Three turns"
_TURN = re.compile(r"^T(\d+):[ \t]?(.*)$")


def extract_prompts(source: Path = None) -> dict:
    """The three turn prompts, verbatim, from the authoritative handoff.

    HANDOFF.md is the source of truth and the driver reads it from the
    GROUND-TRUTH side of the confinement boundary -- candidates cannot, and the
    confinement policy still denies it. There is deliberately NO hard-coded
    copy: a second copy drifts, and then the prompt hash certifies a prompt set
    nobody actually sent.

    FILE STRUCTURE (measured, not assumed):
        ## Three turns (identical for every candidate, one session)
        T1: <first line>
            <continuation lines, indented four spaces>
        <blank>
        T2: ...
        T3: ...
        ## <next heading>   <- section ends here

    WHAT IS AND IS NOT PROMPT CONTENT. The `Tn:` label is a document marker, and
    the four-space continuation indent is Markdown wrapping; neither was ever
    sent to a model. Both are removed. Everything else -- line breaks, internal
    spacing, punctuation, backticks, parenthesised numbering -- is preserved
    byte for byte. This is the only normalisation performed and it is stated
    rather than done quietly.

    Fails closed with a specific reason; never returns placeholders.
    """
    src = Path(source) if source else (PLANNER / "HANDOFF.md")
    if not src.exists():
        return {"state": PROMPTS_MISSING, "prompts": [], "reason": "no HANDOFF.md"}
    text = src.read_text()
    if _SECTION not in text:
        return {"state": PROMPTS_MISSING, "prompts": [],
                "reason": f"no '{_SECTION}' section"}

    body = text.split(_SECTION, 1)[1]
    # The section ends at the next heading; anything after it is other content.
    end = body.find("\n## ")
    body = body[:end] if end != -1 else body

    blocks, order, current = {}, [], None
    for line in body.splitlines():
        m = _TURN.match(line)
        if m:
            n = int(m.group(1))
            if n in blocks:
                return {"state": PROMPTS_DUPLICATED, "prompts": [],
                        "reason": f"T{n} appears more than once"}
            blocks[n] = [m.group(2)]
            order.append(n)
            current = n
            continue
        if current is None:
            continue
        if line.startswith("    "):
            blocks[current].append(line[4:])       # uniform continuation indent
        elif not line.strip():
            current = None                          # blank line closes a block
        else:
            # A non-indented, non-blank line inside the section that is not a
            # new Tn: marker means the delimiters are not what this parser was
            # written against. Refuse rather than guess where the prompt ends.
            return {"state": PROMPTS_MALFORMED, "prompts": [],
                    "reason": f"unexpected line in section: {line[:60]!r}"}

    if not blocks:
        return {"state": PROMPTS_MISSING, "prompts": [], "reason": "no Tn: blocks"}
    if order != sorted(order) or order != list(range(1, len(order) + 1)):
        return {"state": PROMPTS_MALFORMED, "prompts": [],
                "reason": f"turn numbering is not 1..n in order: {order}"}
    if len(blocks) != 3:
        return {"state": PROMPT_COUNT_INVALID, "prompts": [],
                "reason": f"expected exactly 3 prompts, found {len(blocks)}"}

    prompts = ["\n".join(blocks[n]).strip() for n in order]
    if any(not p for p in prompts):
        return {"state": PROMPTS_MALFORMED, "prompts": [],
                "reason": "an extracted prompt is empty"}
    return {"state": PROMPTS_VERIFIED, "prompts": prompts,
            "source": str(src), "source_hash": sha(src),
            "schema": PROMPT_SCHEMA_VERSION,
            "lengths": [len(p) for p in prompts],
            "hash": prompt_set_hash(prompts)}


def prompt_set_hash(prompts: list) -> str:
    """One canonical hash over ORDER, LENGTH, BYTES and parser version.

    Lengths are hashed alongside the bytes so that a prompt boundary moving --
    the same characters split differently between two turns -- changes the hash
    even though the concatenation would not."""
    h = hashlib.sha256()
    h.update(PROMPT_SCHEMA_VERSION.encode())
    for i, p in enumerate(prompts):
        h.update(f"|{i}|{len(p)}|".encode())
        h.update(p.encode())
    return h.hexdigest()[:16]


def turn_prompts(n: int) -> list:
    """The prompts actually sent. Raises rather than degrading to placeholders."""
    res = extract_prompts()
    if res["state"] != PROMPTS_VERIFIED:
        raise RuntimeError(f"{res['state']}: {res.get('reason')}")
    return res["prompts"][:n]


# ── environment ─────────────────────────────────────────────────────────────
def environment_state() -> dict:
    """Measure the conditions that silently invalidate a latency comparison.

    Run 2's wall times were inflated by system sleep and a Time Machine backup,
    and nothing recorded that until the numbers were already in a report."""
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=20).stdout
        except Exception:  # noqa: BLE001
            return ""

    batt = run(["pmset", "-g", "batt"])
    on_ac = "AC Power" in batt
    tm = run(["tmutil", "status"])
    backing_up = '"Running" = 1' in tm or "Running = 1" in tm
    tel = B.telemetry()
    resident = tel.get("resident") or []
    caffeinated = bool(run(["pgrep", "-x", "caffeinate"]).strip())

    problems = []
    if not on_ac:
        problems.append("ON_BATTERY")
    if backing_up:
        problems.append("BACKUP_ACTIVE")
    if str(tel.get("thermal", "")).lower() not in ("nominal", ""):
        problems.append("THERMAL_ELEVATED")
    if len(resident) > 1:
        problems.append("CO_RESIDENT_MODELS")
    if not caffeinated:
        problems.append("NO_CAFFEINATE")

    healthy = B.litellm_healthy()
    state = (ENVIRONMENT_VERIFIED if healthy and not problems
             else INVALID_ENVIRONMENT if not healthy
             else TIMING_UNQUALIFIED)
    return {"state": state, "on_ac_power": on_ac, "backup_active": backing_up,
            "thermal": tel.get("thermal"), "swap_mb": tel.get("swap_mb"),
            "free_percent": tel.get("free_percent"), "resident": resident,
            "caffeinate_running": caffeinated, "runtime_healthy": healthy,
            "problems": problems}


# ── run manifest ────────────────────────────────────────────────────────────
def build_manifest(run_id: str, turns: int, probe_model: str,
                   worktrees: dict) -> dict:
    """Everything that must not move once candidate A starts.

    The private candidate->model mapping is NEVER included; only a hash of the
    mapping file, which proves it did not change without revealing it."""
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()[:12]
    branch = subprocess.run(["git", "-C", str(REPO), "rev-parse",
                             "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    seeded_head = subprocess.run(["git", "-C", str(PLANNER / "seeded"),
                                  "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()[:12]
    mapping = PLANNER / "run2" / "MAPPING.private.json"
    _p = extract_prompts()
    return {
        "run_id": run_id,
        "branch": branch, "commit": head,
        "fixture_commit": seeded_head,
        "seeded_file_hash": sha(PLANNER / "seeded" / "scripts" / "install-models.sh"),
        # Hashes only. The prompts themselves stay OUT of the public manifest:
        # T1 states the symptom, and while it does not reveal the seeded root
        # cause, the public record has no need to carry it.
        "prompt_state": _p["state"],
        "prompt_count": len(_p.get("prompts") or []),
        "prompt_set_hash": _p.get("hash", "UNAVAILABLE"),
        "prompt_source_hash": _p.get("source_hash", "UNAVAILABLE"),
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "prompt_lengths": _p.get("lengths", []),
        "rubric_hash": sha(RUBRIC),
        "rubric_path": str(RUBRIC),
        "candidate_ids": list(CANDIDATES),
        "shuffle_seed": 20260802,
        "client_version": B.client_version("claude-local"),
        "litellm_version": os.environ.get("AILOCAL_LITELLM_VERSION", "1.93.0"),
        "ollama_version": _ollama_version(),
        "model_alias_placeholders": {c: "<hidden>" for c in CANDIDATES},
        "permission_manifest": B.permission_manifest_hash(PERMISSIONS),
        "confinement_policy_version": "permissions.deny/Read-absolute-v2",
        "worktree_root": str(B.benchmark_worktree_root()),
        "evidence_root": str(B.state_dir().resolve()),
        "ground_truth_path_hash": sha(PLANNER / "HANDOFF.md"),
        "expected_turns": turns,
        "probe_model": probe_model,
        # Hash only. Opening this file is what the blind protocol forbids.
        "private_mapping_hash": sha(mapping),
        "worktrees": {c: str(p) for c, p in worktrees.items()},
        "blindness_limitation": (
            "The operator running this driver can read the mapping file on "
            "disk. Scoring copies are identity-stripped, but this is NOT full "
            "blindness and must not be described as such."),
    }


def _ollama_version() -> str:
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version",
                                    timeout=5) as r:
            return json.load(r).get("version", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


LOCKED_FIELDS = ("fixture_commit", "seeded_file_hash", "prompt_set_hash",
                 "prompt_source_hash", "prompt_schema_version", "prompt_count",
                 "rubric_hash", "candidate_ids", "shuffle_seed",
                 "permission_manifest", "confinement_policy_version",
                 "expected_turns", "ground_truth_path_hash")


def manifest_is_locked(current: dict, locked: dict) -> tuple:
    """A locked field that moved means the comparison changed under itself."""
    drift = {k: (locked.get(k), current.get(k))
             for k in LOCKED_FIELDS if locked.get(k) != current.get(k)}
    return (not drift), drift


# ── blind scoring copies ────────────────────────────────────────────────────
IDENTITY_KEYS = ("alias", "digest", "modelUsage", "canonicalModel", "model",
                 "requested_alias", "resolved_backend_model", "routing",
                 "probe_model", "candidate_model", "worktree", "cwd")
TIMING_KEYS = ("wall_seconds", "duration_ms", "duration_api_ms", "total_ms",
               "ttft_first_turn", "telemetry", "llm_api_duration_ms")


def strip_identity(obj, drop_timing: bool = True):
    """Remove anything that names the model, or that hints at it.

    Timings are stripped too, and that is not excessive: a 2B and a 26B model do
    not produce similar latencies, so a timing column is an identity column. The
    rubric therefore scores quality first and reveals operational metrics after
    those scores are locked."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in IDENTITY_KEYS:
                continue
            if drop_timing and k in TIMING_KEYS:
                continue
            out[k] = strip_identity(v, drop_timing)
        return out
    if isinstance(obj, list):
        return [strip_identity(v, drop_timing) for v in obj]
    return obj


def write_scoring_copy(full: dict, out_dir: Path, label: str) -> dict:
    """One identity-stripped copy per candidate, under a random label, with a
    hash proving which full record it came from."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stripped = strip_identity(full)
    body = json.dumps(stripped, indent=1, sort_keys=True, default=str)
    dst = out_dir / f"{label}.json"
    dst.write_text(body)
    return {"label": label, "path": str(dst),
            "scoring_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "full_record_sha256": hashlib.sha256(
                json.dumps(full, sort_keys=True, default=str).encode()).hexdigest()}


# ── driver ──────────────────────────────────────────────────────────────────
def prepare_worktrees(run_id: str, dry_run: bool) -> dict:
    """ALL candidate worktrees are created before ANY candidate runs, because a
    candidate's policy must deny its siblings and they have to exist to be
    named."""
    root = B.benchmark_worktree_root()
    wts = {}
    for c in CANDIDATES:
        wt = root / f"{run_id}-{c}"
        if not dry_run:
            wt = B.disposable_worktree(f"{run_id}-{c}")
        wts[c] = wt
    return wts


def gate_report(cand: str, wt: Path, env: dict, manifest_ok: bool) -> dict:
    """Non-inference gate status, for the dry-run matrix."""
    return {
        "candidate": cand,
        "worktree_class": "<owned-temp-root>/<run-id>-" + Path(wt).name.split("-")[-1],
        "confinable": B.worktree_is_confinable(wt),
        "routing": "DEFERRED (verified at run time)",
        "permissions": "DECLARED " + B.permission_manifest_hash(PERMISSIONS)[:12],
        "confinement": "POLICY READY" if B.worktree_is_confinable(wt) == B.CONFINABLE
                       else "BLOCKED",
        "manifest": "LOCKED" if manifest_ok else INVALID_RUN_MANIFEST,
        "environment": env["state"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--candidate")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--probe-model", default="ailocal-architecture")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--run-id", default=time.strftime("planner-%Y%m%dT%H%M%SZ",
                                                      time.gmtime()))
    ap.add_argument("--output-dir")
    ap.add_argument("--force-rerun", action="store_true",
                    help="re-run a candidate that already completed")
    a = ap.parse_args()

    if not (a.dry_run or a.all or a.candidate):
        print("Refusing to do anything by default. Pass --dry-run, --all, or "
              "--candidate <id>. Inference is never implicit.", file=sys.stderr)
        return 2

    out = Path(a.output_dir) if a.output_dir else \
        Path.home() / ".local/state/ailocal/benchmark/planner" / a.run_id
    env = environment_state()
    wts = prepare_worktrees(a.run_id, a.dry_run)
    manifest = build_manifest(a.run_id, a.turns, a.probe_model, wts)

    if a.dry_run:
        print(f"DRY RUN {a.run_id} — no inference, no aliases, mapping NOT opened\n")
        hdr = ("Candidate", "Worktree class", "Turns", "Routing", "Permissions",
               "Confinement", "Manifest", "Environment", "Ready")
        print(f"{hdr[0]:<12}{hdr[1]:<40}{hdr[2]:<6}{hdr[3]:<10}{hdr[4]:<22}"
              f"{hdr[5]:<15}{hdr[6]:<9}{hdr[7]:<22}{hdr[8]}")
        ready_all = True
        for c in CANDIDATES:
            g = gate_report(c, wts[c], env, True)
            ready = (g["confinement"] == "POLICY READY"
                     and env["state"] != INVALID_ENVIRONMENT
                     and manifest["prompt_state"] == PROMPTS_VERIFIED)
            ready_all &= ready
            print(f"{c:<12}{g['worktree_class']:<40}{a.turns:<6}"
                  f"{'DEFERRED':<10}{g['permissions']:<22}{g['confinement']:<15}"
                  f"{'LOCKED':<9}{g['environment']:<22}{'YES' if ready else 'NO'}")
        print()
        for k in ("prompt_state", "prompt_count", "prompt_set_hash",
                  "prompt_source_hash", "prompt_schema_version", "prompt_lengths",
                  "fixture_commit", "seeded_file_hash",
                  "rubric_hash", "ground_truth_path_hash", "private_mapping_hash",
                  "worktree_root", "evidence_root", "client_version",
                  "litellm_version", "ollama_version"):
            print(f"  {k:<24} {manifest[k]}")
        print(f"\n  environment            {env['state']}"
              f"{' problems=' + ','.join(env['problems']) if env['problems'] else ''}")
        print(f"  mapping opened         NO (hash only)")
        print(f"  blindness              PARTIAL — see manifest.blindness_limitation")
        print(f"\nREADY: {'YES' if ready_all else 'NO'}")
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.dry-run.json").write_text(
            json.dumps(manifest, indent=1, default=str))
        print(f"manifest -> {out / 'manifest.dry-run.json'}")
        return 0

    # ── real execution ──────────────────────────────────────────────────
    if env["state"] == INVALID_ENVIRONMENT:
        print(f"{INVALID_ENVIRONMENT}: {env['problems']}", file=sys.stderr)
        return 4
    out.mkdir(parents=True, exist_ok=True)
    locked_path = out / "manifest.json"
    if locked_path.exists():
        locked = json.loads(locked_path.read_text())
        ok, drift = manifest_is_locked(manifest, locked)
        if not ok:
            print(f"{INVALID_RUN_MANIFEST}: {json.dumps(drift)}", file=sys.stderr)
            return 5
    else:
        locked_path.write_text(json.dumps(manifest, indent=1, default=str))
        locked = manifest

    # Recomputed HERE, after the manifest locked, and again on every resume:
    # a prompt edited between locking and turn 1 would otherwise be certified by
    # a hash of the prompts it replaced.
    live = extract_prompts()
    if live["state"] != PROMPTS_VERIFIED:
        print(f"{live['state']}: {live.get('reason')}", file=sys.stderr)
        return 6
    if live["hash"] != locked.get("prompt_set_hash"):
        print(f"{INVALID_RUN_MANIFEST}: {PROMPT_HASH_MISMATCH} "
              f"{locked.get('prompt_set_hash')} -> {live['hash']}", file=sys.stderr)
        return 5

    targets = [a.candidate] if a.candidate else list(CANDIDATES)
    results = {}
    try:
        for cand in targets:
            dst = out / f"{cand}.full.json"
            if dst.exists() and not (a.resume or a.force_rerun):
                print(f"{cand}: already completed; pass --force-rerun to repeat")
                continue
            prior = json.loads(dst.read_text()) if (dst.exists() and a.resume) else None
            done = len((prior or {}).get("records") or []) if prior else 0
            if done >= a.turns:
                print(f"{cand}: all {done} turns already recorded; nothing to do")
                continue
            session = (prior or {}).get("session_id")
            if done and not session:
                # Resuming without the exact id would attach to whatever session
                # the client last used -- possibly the operator's own.
                results[cand] = {"error": "SESSION_LOST", "turns_completed": done}
                continue

            res = run_one(cand, wts, a, out, session=session, skip=done)
            results[cand] = res
            merged = _merge(prior, res) if prior else res
            dst.write_text(json.dumps(merged, indent=1, default=str))
            label = uuid.uuid4().hex[:8]
            proof = write_scoring_copy(merged, out / "scoring", label)
            (out / "scoring" / f"{label}.proof.json").write_text(
                json.dumps(proof, indent=1))
            state = merged.get("error") or (
                "COMPLETE" if merged.get("turns_completed") == a.turns else INCOMPLETE)
            print(f"{cand}: {state} ({merged.get('turns_completed')}/{a.turns})")
    finally:
        rest = B.restore()
        sweep = B.sweep_worktree_root()
        for c, wt in wts.items():
            rm = B.remove_worktree(wt)
            if not rm["removed"]:
                print(f"  cleanup {c}: {rm['status']} {rm.get('reason','')}")
        print(f"restored={rest['restored']} leaked={rest['leaked']} "
              f"swept={len(sweep['orphans_removed'])}")
    return 0


def _merge(prior: dict, new: dict) -> dict:
    """Resumed turns extend the prior record; completed turns are never redone."""
    out = dict(prior)
    out["records"] = (prior.get("records") or []) + (new.get("records") or [])
    out["turns_completed"] = len(out["records"])
    out["error"] = new.get("error")
    out["confinement"] = new.get("confinement")
    return out


def run_one(cand: str, wts: dict, a, out: Path, session=None, skip: int = 0) -> dict:
    """One candidate, with every gate wired in and siblings denied.

    A breach or gate failure stops THIS candidate only: the others were
    configured independently and stay eligible."""
    wt = wts[cand]
    siblings = [p for c, p in wts.items() if c != cand]
    prompts = turn_prompts(a.turns)[skip:]
    return B.run_client_scenario(
        "claude-local", prompts, wt, timeout=1800,
        confinement={"model": f"<alias-for-{cand}>",
                     "permissions": PERMISSIONS,
                     "probe_model": a.probe_model,
                     "sibling_worktrees": siblings,
                     "extra_paths": [str(PLANNER / "HANDOFF.md"),
                                     str(REPO / "README.md"),
                                     str(Path.home() / ".config/ailocal")]})


if __name__ == "__main__":
    sys.exit(main())
