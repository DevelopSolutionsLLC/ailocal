# Implementation of `ailocal benchmark planner`. Not executable on its own.
"""benchmarks/planner.py — the authoritative planner comparison driver.

THIS IS ORCHESTRATION ONLY. Every mechanism it uses already exists and is
already tested: worktrees, alias lifecycle, routing, permissions, confinement,
evidence, sessions, restoration. Nothing here reimplements them, because a
second implementation drifts and then two runs of "the same" benchmark are not.

A comparison whose setup is not reproducible cannot be re-run to check its
result. Driving one ad hoc is how a comparison measures one model three times,
lets candidates read an unrelated repository, and leaves the ground truth
readable by absolute path.

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

REPO = Path(__file__).resolve().parent.parent   # benchmarks/ -> repo root
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "benchmarks"))
import suite as B  # noqa: E402

PLANNER = Path.home() / ".local/state/ailocal/benchmark/planner"
RUBRIC = REPO / "benchmarks" / "planner-rubric.md"
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


# ── private mapping (identity boundary) ─────────────────────────────────────
# EVERYTHING BELOW IS PRIVATE. The mapping resolves candidate ids to models so
# aliases can be built; nothing derived from it is printed, logged, serialized
# into the public manifest, or written to a scoring record. Error messages
# deliberately carry a STATE and never a value -- an exception string is the
# easiest place for an identity to escape.
VERIFIED_PRIVATE_MAPPING = "VERIFIED_PRIVATE_MAPPING"
PRIVATE_MAPPING_MISSING = "PRIVATE_MAPPING_MISSING"
PRIVATE_MAPPING_PERMISSIONS_INVALID = "PRIVATE_MAPPING_PERMISSIONS_INVALID"
PRIVATE_MAPPING_SCHEMA_INVALID = "PRIVATE_MAPPING_SCHEMA_INVALID"
PRIVATE_MAPPING_HASH_MISMATCH = "PRIVATE_MAPPING_HASH_MISMATCH"
PRIVATE_MAPPING_CANDIDATES_INVALID = "PRIVATE_MAPPING_CANDIDATES_INVALID"
PRIVATE_MAPPING_MODEL_INVALID = "PRIVATE_MAPPING_MODEL_INVALID"
PRIVATE_MAPPING_DUPLICATE_MODEL = "PRIVATE_MAPPING_DUPLICATE_MODEL"

VERIFIED_ROUTING = "VERIFIED_ROUTING"
ROUTING_ALIAS_MISSING = "ROUTING_ALIAS_MISSING"
ROUTING_DIGEST_MISMATCH = "ROUTING_DIGEST_MISMATCH"
ROUTING_PRODUCTION_FALLBACK = "ROUTING_PRODUCTION_FALLBACK"
INVALID_ROUTING = "INVALID_ROUTING"

MAPPING_PATH = PLANNER / "run2" / "MAPPING.private.json"
#: Locked planner geometry, from HANDOFF.md. NOT a tunable: the output ceiling
#: is 8192, deliberately not the 32768 benchmark ceiling.
PLANNER_CONTEXT, PLANNER_CEILING, PLANNER_MODE = 32768, 8192, "off"


def load_private_mapping(expected_hash: str = None) -> dict:
    """Resolve candidate ids to models. PRIVATE — callers must not print it.

    Fails closed on every axis, and the failure NEVER carries a value: a state
    is returned, the contents are not, and no exception message quotes a model.
    """
    p = MAPPING_PATH
    if not p.exists():
        return {"state": PRIVATE_MAPPING_MISSING}
    mode = p.stat().st_mode
    if mode & 0o022:
        # Group/other WRITABLE is fatal: a mapping anyone can rewrite cannot
        # pin a comparison. Group/other READABLE is recorded as a warning
        # instead -- the locked hash already detects tampering, and refusing to
        # run over a 0644 file on a single-user machine would be theatre.
        return {"state": PRIVATE_MAPPING_PERMISSIONS_INVALID}
    warn = "GROUP_OR_OTHER_READABLE" if (mode & 0o044) else None
    if expected_hash and sha(p) != expected_hash:
        return {"state": PRIVATE_MAPPING_HASH_MISMATCH}
    try:
        raw = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {"state": PRIVATE_MAPPING_SCHEMA_INVALID}
    if not isinstance(raw, dict) or not isinstance(raw.get("mapping"), dict):
        return {"state": PRIVATE_MAPPING_SCHEMA_INVALID}
    m = raw["mapping"]
    if sorted(m) != sorted(CANDIDATES):
        return {"state": PRIVATE_MAPPING_CANDIDATES_INVALID}
    # SCHEMA, read from the file rather than assumed: each candidate maps to
    # {"model": <tag>, "mode": <thinking mode>}. An earlier version of this
    # validator expected a bare string and rejected the real file as
    # MODEL_INVALID -- which is the failure working, but for the wrong reason.
    if any(not isinstance(v, dict) or not isinstance(v.get("model"), str)
           or not isinstance(v.get("mode"), str) or not v["model"].strip()
           for v in m.values()):
        return {"state": PRIVATE_MAPPING_SCHEMA_INVALID}
    models = {c: v["model"] for c, v in m.items()}
    modes = {c: v["mode"] for c, v in m.items()}
    if len(set(models.values())) != len(models):
        return {"state": PRIVATE_MAPPING_DUPLICATE_MODEL}
    installed = B.installed()
    for v in models.values():
        if v not in installed and f"{v}:latest" not in installed:
            return {"state": PRIVATE_MAPPING_MODEL_INVALID}
    digests = {c: (B.model_info(v).get("digest") or "") for c, v in models.items()}
    if any(not d for d in digests.values()):
        return {"state": PRIVATE_MAPPING_MODEL_INVALID}
    return {"state": VERIFIED_PRIVATE_MAPPING, "_mapping": models,
            "_modes": modes, "_digests": digests,
            "shuffle_seed": raw.get("shuffle_seed"),
            "permissions_warning": warn}


PLACEHOLDER = "<alias-for-"


def assert_no_placeholder(*values) -> None:
    """A placeholder alias reaching a client command is how this driver failed
    its first readiness claim. Assert rather than trust."""
    for v in values:
        if v is not None and PLACEHOLDER in str(v):
            raise AssertionError(
                "placeholder alias reached execution — refusing to run")


def build_candidate_aliases(priv: dict) -> dict:
    """One temporary alias per candidate, via the existing builder.

    Alias construction is NOT reimplemented here: build_alias() already encodes
    num_ctx = context + ceiling, the admission ceiling and the model_info block,
    and a second copy would drift from the one every other benchmark uses."""
    out = {}
    for cand in CANDIDATES:
        model = priv["_mapping"][cand]
        # The reasoning mode is part of the LOCKED candidate definition and is
        # taken from the mapping, not from a driver default: overriding it here
        # would silently change what the comparison measures.
        mode = (priv.get("_modes") or {}).get(cand, PLANNER_MODE)
        entry = B.build_alias(model, mode, PLANNER_CONTEXT,
                              PLANNER_CEILING, {})
        assert_no_placeholder(entry["model_name"])
        out[cand] = entry
    names = [e["model_name"] for e in out.values()]
    if len(set(names)) != len(names):
        # Two candidates on one alias is exactly how run 1 measured a single
        # model three times.
        raise AssertionError("two candidates resolved to the same alias")
    return out


def verify_candidate_routing(cand: str, alias: str, model: str, digest: str,
                             wt: Path, extra_args: list) -> dict:
    """Prove THIS candidate's alias served THIS candidate's weights.

    Public fields carry no identity: state plus a hash. The alias, the model and
    the digest stay in the private record."""
    res = B.verify_routing(alias, model, wt, extra_args=extra_args)
    served = set(res.get("served_aliases") or [])
    if not res.get("alias_served"):
        state = (ROUTING_PRODUCTION_FALLBACK
                 if any(a.startswith("ailocal-") for a in served)
                 else ROUTING_ALIAS_MISSING)
    elif digest and res.get("expected_digest") and \
            res["expected_digest"] != digest:
        state = ROUTING_DIGEST_MISMATCH
    else:
        state = VERIFIED_ROUTING
    public = {"candidate": cand, "state": state,
              "routing_evidence_hash": hashlib.sha256(
                  json.dumps(res, sort_keys=True, default=str).encode()
              ).hexdigest()[:16]}
    return {"public": public, "_private": res}


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


_ALIAS_RE = re.compile(r"bench-[a-z0-9.\-]+")
_EMBEDDED = (
    # Matches the KEY and the first nested key name without requiring a
    # balanced-brace match, which failed on one record and left the probe
    # model's name behind. Presence in only one file would itself be a weak
    # asymmetry even though the probe model is public and identical for all.
    re.compile(r'\\?"modelUsage\\?":\s*\{\s*\\?"[^"\\]*\\?"'),
    re.compile(r'\\?"canonicalModel\\?":\s*\\?"[^"\\]*\\?"'),
    re.compile(r'\\?"(?:duration_ms|duration_api_ms|total_cost_usd)\\?":\s*[0-9.]+'),
)


def redact_text(t: str) -> str:
    """Redact identity INSIDE string values, not just in keys.

    The client's terminal result is embedded in `stdout_full` as serialized
    JSON, so key-based stripping never touched it: the first scoring copies
    carried `"canonicalModel":"bench-<model>-<mode>-<ctx>"` as plain text, and a
    bench alias names its model outright. Model FAMILY names are deliberately
    left alone -- the planner task is about model pulling, every candidate
    discusses them, so they carry no signal about who answered."""
    t = _ALIAS_RE.sub("<ALIAS-REDACTED>", t)
    for rx in _EMBEDDED:
        t = rx.sub("<IDENTITY-REDACTED>", t)
    return t


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
    if isinstance(obj, str):
        return redact_text(obj)
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
    ap.add_argument("--validate-private-routing", action="store_true",
                    help="resolve the private mapping and build aliases "
                         "WITHOUT inference or any runtime mutation")
    ap.add_argument("--force-rerun", action="store_true",
                    help="re-run a candidate that already completed")
    a = ap.parse_args()

    if not (a.dry_run or a.all or a.candidate or a.validate_private_routing):
        print("Refusing to do anything by default. Pass --dry-run, --all, or "
              "--candidate <id>. Inference is never implicit.", file=sys.stderr)
        return 2

    out = Path(a.output_dir) if a.output_dir else \
        Path.home() / ".local/state/ailocal/benchmark/planner" / a.run_id
    env = environment_state()
    # Validation and dry-run must not create real git worktrees: neither runs
    # the cleanup path, so both leaked one worktree per candidate per
    # invocation until this was caught.
    wts = prepare_worktrees(a.run_id, a.dry_run or a.validate_private_routing)
    manifest = build_manifest(a.run_id, a.turns, a.probe_model, wts)

    if a.validate_private_routing:
        # Opens the mapping (authorized), builds alias objects, resolves
        # digests -- and mutates nothing. Prints candidate ids and states only:
        # no alias, no model, no digest, no mapping.
        print(f"PRIVATE ROUTING VALIDATION {a.run_id} — no inference, "
              f"no runtime mutation\n")
        priv = load_private_mapping(manifest["private_mapping_hash"])
        rows = []
        if priv["state"] != VERIFIED_PRIVATE_MAPPING:
            for c in CANDIDATES:
                rows.append((c, priv["state"], "SKIPPED", "SKIPPED", "NO", "NO"))
        else:
            try:
                entries = build_candidate_aliases(priv)
                built = True
            except AssertionError:
                entries, built = {}, False
            for c in CANDIDATES:
                e = entries.get(c)
                alias_ok = bool(e and e["model_name"] and PLACEHOLDER
                                not in e["model_name"])
                digest_ok = bool(priv["_digests"].get(c))
                geom_ok = bool(e and e["litellm_params"]["num_predict"]
                               == PLANNER_CEILING
                               and e["litellm_params"]["num_ctx"]
                               == PLANNER_CONTEXT + PLANNER_CEILING)
                gate = "YES" if (alias_ok and digest_ok and geom_ok) else "NO"
                rows.append((c, VERIFIED_PRIVATE_MAPPING,
                             "VALID" if alias_ok and geom_ok else "INVALID",
                             "VERIFIED" if digest_ok else "MISSING",
                             gate, gate))
        print(f"{'Candidate':<13}{'Mapping':<26}{'Alias build':<13}"
              f"{'Digest':<10}{'Routing gate configured':<25}Ready")
        for r in rows:
            print(f"{r[0]:<13}{r[1]:<26}{r[2]:<13}{r[3]:<10}{r[4]:<25}{r[5]}")
        ready = all(r[5] == "YES" for r in rows)
        if priv.get("permissions_warning"):
            print(f"\n  mapping permissions: {priv['permissions_warning']} "
                  f"(hash-locked; not fatal)")
        print(f"\n  geometry: num_ctx {PLANNER_CONTEXT + PLANNER_CEILING}, "
              f"num_predict {PLANNER_CEILING}, mode '{PLANNER_MODE}' (locked)")
        print(f"  aliases/models/digests: WITHHELD")
        print(f"\nREADY: {'YES' if ready else 'NO'}")
        return 0 if ready else 1

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

    # ── lifecycle order (private from here down) ────────────────────────
    priv = load_private_mapping(locked.get("private_mapping_hash"))
    if priv["state"] != VERIFIED_PRIVATE_MAPPING:
        print(priv["state"], file=sys.stderr)          # state only, never values
        return 7
    if priv.get("permissions_warning"):
        print(f"  mapping permissions: {priv['permissions_warning']} "
              f"(hash-locked; not fatal)")
    entries = build_candidate_aliases(priv)
    aliases = {c: e["model_name"] for c, e in entries.items()}
    applied = B.apply_aliases(list(entries.values()))
    if not applied["ok"]:
        B.restore()
        print(f"alias application failed: missing={len(applied['missing'])}",
              file=sys.stderr)
        return 8
    if not B.litellm_healthy():
        B.restore()
        print("runtime unhealthy after alias application", file=sys.stderr)
        return 9
    print(f"  aliases applied: {len(applied['installed'])} temporary "
          f"(names withheld)")

    targets = [a.candidate] if a.candidate else list(CANDIDATES)
    results = {}
    routing_public = {}
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

            # Routing is verified with the SAME override, permissions and
            # confinement the scored turns use -- otherwise it proves routing
            # for a request this candidate never makes.
            gate_args = (B.permission_args(PERMISSIONS)
                         + ["--model", aliases[cand]]
                         + B.confinement_args(
                             wts[cand],
                             deny_extra=[p for c, p in wts.items() if c != cand]))
            rt = verify_candidate_routing(
                cand, aliases[cand], priv["_mapping"][cand],
                priv["_digests"][cand], wts[cand], gate_args)
            routing_public[cand] = rt["public"]
            (out / f"{cand}.routing.private.json").write_text(
                json.dumps(rt["_private"], indent=1, default=str))
            if rt["public"]["state"] != VERIFIED_ROUTING:
                print(f"{cand}: {rt['public']['state']} — stopped before turn 1")
                results[cand] = {"error": rt["public"]["state"],
                                 "turns_completed": 0}
                continue
            if not B.litellm_healthy():
                print(f"{cand}: runtime unhealthy — not started")
                results[cand] = {"error": INVALID_ENVIRONMENT, "turns_completed": 0}
                continue

            res = run_one(cand, wts, a, out, session=session, skip=done,
                          alias=aliases[cand])
            results[cand] = res
            merged = _merge(prior, res) if prior else res
            dst.write_text(json.dumps(merged, indent=1, default=str))
            label = uuid.uuid4().hex[:8]
            proof = write_scoring_copy(merged, out / "scoring", label)
            (out / "scoring" / f"{label}.proof.json").write_text(
                json.dumps(proof, indent=1))
            state = merged.get("error") or (
                "COMPLETE" if merged.get("turns_completed") == a.turns else INCOMPLETE)
            conf = (merged.get("confinement") or {})
            pf = (conf.get("preflight") or {}).get("state", "n/a")
            ev = conf.get("events") or []
            blocked = sum(e.get("escape_attempts_blocked", 0) for e in ev)
            print(f"{cand}: {state} ({merged.get('turns_completed')}/{a.turns}) "
                  f"routing={rt['public']['state']} confinement={pf} "
                  f"turns_internal={sum((r.get('structured') or {}).get('num_turns') or 0 for r in merged.get('records') or [])} "
                  f"escapes_blocked={blocked}")
    finally:
        rest = B.restore()
        sweep = B.sweep_worktree_root()
        for c, wt in wts.items():
            rm = B.remove_worktree(wt)
            if not rm["removed"]:
                print(f"  cleanup {c}: {rm['status']} {rm.get('reason','')}")
        (out / "routing.public.json").write_text(
            json.dumps(routing_public, indent=1, default=str))
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


def run_one(cand: str, wts: dict, a, out: Path, session=None, skip: int = 0,
            alias: str = None) -> dict:
    """One candidate, with every gate wired in and siblings denied.

    A breach or gate failure stops THIS candidate only: the others were
    configured independently and stay eligible."""
    wt = wts[cand]
    siblings = [p for c, p in wts.items() if c != cand]
    prompts = turn_prompts(a.turns)[skip:]
    assert_no_placeholder(alias)
    return B.run_client_scenario(
        "claude-local", prompts, wt, timeout=1800,
        confinement={"model": alias,
                     "permissions": PERMISSIONS,
                     "probe_model": a.probe_model,
                     "sibling_worktrees": siblings,
                     "extra_paths": [str(PLANNER / "HANDOFF.md"),
                                     str(REPO / "README.md"),
                                     str(Path.home() / ".config/ailocal")]})


if __name__ == "__main__":
    sys.exit(main())
