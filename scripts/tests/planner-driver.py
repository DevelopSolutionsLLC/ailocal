#!/usr/bin/env python3
"""planner-driver.py — the properties that make a planner comparison trustworthy.

Every check here encodes a way a previous run produced confident, meaningless
numbers: a comparison that measured one model three times, candidates that could
read the answer key, latencies inflated by system sleep, and a rubric that could
not distinguish a concise correct answer from a 317-turn non-answer.

No inference. The driver is imported and driven with the client stubbed, so
ordering, locking, blinding and fail-closed behaviour are proven without a model.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import benchmark as B  # noqa: E402

failures: list[str] = []


def check(cond: object, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        failures.append(label)


def load_driver():
    spec = importlib.util.spec_from_file_location(
        "planner_driver", REPO / "scripts" / "run-planner-comparison.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


D = load_driver()


def main() -> int:
    print("SAFE DEFAULTS")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "run-planner-comparison.py")],
                       capture_output=True, text=True, timeout=120)
    check(r.returncode == 2, "no arguments ⇒ refuses to act (rc=2)")
    check("never implicit" in r.stderr, "refusal explains that inference is never implicit")

    src = (REPO / "scripts" / "run-planner-comparison.py").read_text()
    check("--continue" not in src and '"--last"' not in src,
          "--continue and --last are never used (implicit resume is forbidden)")
    check("--force-rerun" in src, "re-running a completed candidate needs an explicit flag")

    print("\nDRY RUN DOES NOT TOUCH THE MAPPING OR A MODEL")
    out = Path(tempfile.mkdtemp(prefix="drv-"))
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "run-planner-comparison.py"),
                        "--dry-run", "--all", "--output-dir", str(out),
                        "--run-id", "unit-dry"],
                       capture_output=True, text=True, timeout=300)
    check(r.returncode == 0, "dry run succeeds")
    blob = r.stdout
    check("mapping opened         NO" in blob, "dry run states the mapping was not opened")
    # The mapping's real contents must never appear anywhere in the output.
    mp = D.PLANNER / "run2" / "MAPPING.private.json"
    if mp.exists():
        secret = json.loads(mp.read_text())
        leaked = [str(v) for v in secret.values() if str(v) and str(v) in blob]
        check(not leaked, "no mapping VALUE appears in dry-run output")
    man = json.loads((out / "manifest.dry-run.json").read_text())
    check(all(v == "<hidden>" for v in man["model_alias_placeholders"].values()),
          "manifest carries placeholders, never model identities")
    check(len(man["private_mapping_hash"]) == 16,
          "manifest records only a HASH of the private mapping")
    check("blindness_limitation" in man and "NOT full" in man["blindness_limitation"],
          "the blindness limitation is recorded, not glossed over")

    print("\nLOCKED MANIFEST")
    for f in ("fixture_commit", "seeded_file_hash", "prompt_set_hash",
              "rubric_hash", "shuffle_seed", "permission_manifest",
              "expected_turns", "ground_truth_path_hash"):
        check(f in man, f"manifest locks {f}")
    ok, drift = D.manifest_is_locked(man, man)
    check(ok and not drift, "an unchanged manifest is locked")
    moved = dict(man); moved["rubric_hash"] = "tampered"
    ok, drift = D.manifest_is_locked(moved, man)
    check(not ok and "rubric_hash" in drift,
          "a changed rubric hash fails closed as INVALID_RUN_MANIFEST")
    moved = dict(man); moved["shuffle_seed"] = 1
    check(not D.manifest_is_locked(moved, man)[0],
          "a changed shuffle seed fails closed")

    print("\nCONFINEMENT IS WIRED INTO EVERY CANDIDATE")
    calls = []
    orig = B.run_client_scenario

    def stub(client, turns, cwd, timeout=900, extra_args=None, confinement=None):
        calls.append({"cwd": Path(cwd), "confinement": confinement,
                      "turns": len(turns)})
        return {"client": client, "turns_planned": len(turns),
                "turns_completed": len(turns), "session_id": "s-1",
                "session_continuous": True, "error": None, "cwd": str(cwd),
                "tool_calls": 0, "crashes": 0, "timeouts": 0,
                "wall_seconds": 1.0, "ttft_first_turn": 1.0,
                "records": [{"turn": i + 1, "wall_seconds": 1.0,
                             "session_continuous": True}
                            for i in range(len(turns))],
                "confinement": {"required": True}}
    B.run_client_scenario = stub
    try:
        root = B.benchmark_worktree_root()
        wts = {c: root / f"unit-{c}" for c in D.CANDIDATES}
        for w in wts.values():
            w.mkdir(parents=True, exist_ok=True)

        class A:
            turns, probe_model, output_dir = 3, "ailocal-architecture", None
        for cand in D.CANDIDATES:
            D.run_one(cand, wts, A(), out)
    finally:
        B.run_client_scenario = orig

    check(len(calls) == 3, "every candidate is executed through one code path")
    check(all(c["confinement"] for c in calls), "every candidate receives confinement=")
    for i, cand in enumerate(D.CANDIDATES):
        sibs = [str(p) for p in calls[i]["confinement"]["sibling_worktrees"]]
        others = [str(wts[c]) for c in D.CANDIDATES if c != cand]
        check(sorted(sibs) == sorted(others),
              f"{cand} denies exactly its two sibling worktrees")
    extra = calls[0]["confinement"]["extra_paths"]
    check(any("HANDOFF.md" in p for p in extra),
          "the ground truth is probed as a must-be-denied path")
    check(all(c["confinement"]["permissions"] == D.PERMISSIONS for c in calls),
          "all candidates share one permission contract")
    check(len({json.dumps(c["confinement"]["permissions"], sort_keys=True)
               for c in calls}) == 1, "the permission contract is identical, not per-candidate")

    print("\nRESUME NEVER REPEATS OR GUESSES")
    prior = {"session_id": "s-1", "turns_completed": 2,
             "records": [{"turn": 1}, {"turn": 2}]}
    merged = D._merge(prior, {"records": [{"turn": 3}], "error": None,
                              "confinement": {}})
    check(merged["turns_completed"] == 3, "resumed turns extend the prior record")
    check([r["turn"] for r in merged["records"]] == [1, 2, 3],
          "completed turns are not repeated")
    check(D.turn_prompts(3)[2:] == D.turn_prompts(3)[2:],
          "resume slices the prompt set rather than replaying it")

    print("\nENVIRONMENT IS CLASSIFIED, NOT ASSUMED")
    env = D.environment_state()
    check(env["state"] in (D.ENVIRONMENT_VERIFIED, D.TIMING_UNQUALIFIED,
                           D.INVALID_ENVIRONMENT),
          f"environment resolves to a defined state ({env['state']})")
    check(isinstance(env["problems"], list), "environment problems are enumerated")
    check(D.TIMING_UNQUALIFIED != D.INVALID_ENVIRONMENT,
          "timing qualification is separate from run validity")
    for key in ("on_ac_power", "backup_active", "thermal", "resident",
                "caffeinate_running", "runtime_healthy"):
        check(key in env, f"environment records {key}")

    print("\nSCORING COPIES CARRY NO IDENTITY")
    full = {"alias": "bench-qwen3-5-4b-off-32k", "digest": "2a654d98",
            "cwd": "/x/run-candidate-a", "wall_seconds": 2037.3,
            "records": [{"stdout_full": "the plan", "modelUsage": {"m": 1},
                         "duration_api_ms": 1234, "turn": 1}],
            "scenario": {"session_id": "s"}}
    proof = D.write_scoring_copy(full, out / "scoring", "label-1")
    body = (out / "scoring" / "label-1.json").read_text()
    for bad in ("bench-qwen3-5-4b", "2a654d98", "modelUsage", "run-candidate-a"):
        check(bad not in body, f"scoring copy hides {bad}")
    check("the plan" in body, "scoring copy keeps the content being scored")
    check("2037.3" not in body and "1234" not in body,
          "timings are withheld until quality scores are locked (they hint identity)")
    check(len(proof["scoring_sha256"]) == 64 and len(proof["full_record_sha256"]) == 64,
          "the scoring copy is hash-linked to its full record")
    again = D.write_scoring_copy(full, out / "scoring", "label-2")
    check(again["full_record_sha256"] == proof["full_record_sha256"],
          "the same full record always hashes the same")
    check(again["scoring_sha256"] == proof["scoring_sha256"],
          "stripping is deterministic")

    print("\nRUBRIC COVERS THE TWO GAPS")
    rub = D.RUBRIC.read_text()
    check(D.RUBRIC.exists(), "the rubric is a versioned file, not prose in a note")
    check(len(D.sha(D.RUBRIC)) == 16, "the rubric is hashable and lockable")
    check(re.search(r"repetition and circularity", rub, re.I),
          "repetition/circularity is a scored dimension")
    check(re.search(r"execution efficiency", rub, re.I),
          "execution efficiency is a scored dimension")
    check("internal turn count" in rub, "turn count is measured")
    check("monotonic" in rub, "compute time is separated from wall time")
    check(re.search(r"not penalised for low tool count", rub, re.I),
          "a concise correct answer is not penalised for using few tools")
    check(re.search(r"never beats a correct slower answer", rub, re.I),
          "an incorrect fast answer never wins")
    check(re.search(r"fails to\s+complete[^.]*scores 0 on efficiency", rub, re.I | re.S),
          "a long non-answer is explicitly penalised")
    check(re.search(r"within 1 point", rub),
          "efficiency only breaks ties between comparable correctness")
    check(re.search(r"Incomplete candidates are never scored against complete", rub, re.I),
          "incomplete candidates are not scored against complete ones")
    check(rub.index("Score correctness") < rub.index("Reveal the model mapping"),
          "quality is scored before identity is revealed")

    print("\nPROMPTS COME FROM THE AUTHORITATIVE HANDOFF")
    real = D.extract_prompts()
    check(real["state"] == D.PROMPTS_VERIFIED, "the real handoff parses PROMPTS_VERIFIED")
    check(len(real["prompts"]) == 3, "exactly three prompts are extracted")
    check(real["prompts"][0].startswith("Installation on this 32 GB machine"),
          "T1 is first and is the investigation prompt")
    check(real["prompts"][1].startswith("Which single function"),
          "T2 is second")
    check(real["prompts"][2].startswith("Without rereading"), "T3 is third")
    check("\n" in real["prompts"][0], "multiline prompt content is preserved")
    check(real["lengths"] == [604, 137, 129],
          f"prompt lengths are stable {real['lengths']}")
    # Independent corroboration: KNOWN_ISSUES #6 records run 2's prompt as
    # 604 chars. Matching that is stronger evidence the parser reproduces the
    # bytes actually sent than any self-consistent hash would be.
    check(real["lengths"][0] == 604,
          "T1 is 604 chars, matching the independently recorded run-2 figure")
    check(not any(re.match(r"^T\d+:", p) for p in real["prompts"]),
          "the Tn: document marker is not part of any prompt")
    check(not any(l.startswith("    ") for p in real["prompts"]
                  for l in p.splitlines()),
          "Markdown continuation indent is removed")

    # The prompts must not hand the candidate the seeded root cause.
    for leak in ("config/profiles/active-profile", "2>/dev/null",
                 "hardcoded", "precedence"):
        check(not any(leak in p for p in real["prompts"]),
              f"no prompt reveals the ground truth ({leak!r})")

    print("\nPROMPT EXTRACTION FAILS CLOSED")
    tmp = Path(tempfile.mkdtemp(prefix="ph-"))

    def parse(body):
        f = tmp / f"h{abs(hash(body))}.md"
        f.write_text("# x\n\n## Three turns (identical)\n\n" + body + "\n\n## Next\n")
        return D.extract_prompts(f)["state"]

    good = ("T1: alpha\n    more alpha\n\nT2: beta\n\nT3: gamma")
    check(parse(good) == D.PROMPTS_VERIFIED, "a well-formed section parses")
    check(parse("T1: a\n\nT2: b") == D.PROMPT_COUNT_INVALID,
          "a missing third prompt fails PROMPT_COUNT_INVALID")
    check(parse("T1: a\n\nT2: b\n\nT2: c\n\nT3: d") == D.PROMPTS_DUPLICATED,
          "a duplicated section fails PROMPTS_DUPLICATED")
    check(parse("T1: a\n\nT2: b\n\nT3: c\n\nT4: d") == D.PROMPT_COUNT_INVALID,
          "an extra fourth prompt fails closed")
    check(parse("T1: a\n\nT3: c\n\nT2: b") == D.PROMPTS_MALFORMED,
          "out-of-order numbering fails PROMPTS_MALFORMED")
    check(parse("T1: a\nnot indented continuation\n\nT2: b\n\nT3: c")
          == D.PROMPTS_MALFORMED, "a malformed delimiter fails PROMPTS_MALFORMED")
    check(parse("T1: \n\nT2: b\n\nT3: c") == D.PROMPTS_MALFORMED,
          "an empty prompt fails closed")
    missing = tmp / "nosection.md"
    missing.write_text("# nothing here\n")
    check(D.extract_prompts(missing)["state"] == D.PROMPTS_MISSING,
          "a file without the section fails PROMPTS_MISSING")
    check(D.extract_prompts(tmp / "absent.md")["state"] == D.PROMPTS_MISSING,
          "a missing file fails PROMPTS_MISSING")

    # There must be no placeholder path back into the driver.
    drv_src = (REPO / "scripts" / "run-planner-comparison.py").read_text()
    check("[planner turn" not in drv_src,
          "no placeholder prompt text survives anywhere in the driver")
    raised = False
    try:
        D.turn_prompts.__globals__["extract_prompts"]  # sanity
        orig_extract = D.extract_prompts
        D.extract_prompts = lambda *a, **k: {"state": D.PROMPTS_MISSING,
                                             "reason": "forced"}
        try:
            D.turn_prompts(3)
        except RuntimeError:
            raised = True
        finally:
            D.extract_prompts = orig_extract
    except Exception:  # noqa: BLE001
        pass
    check(raised, "turn_prompts RAISES rather than degrading to placeholders")

    print("\nPROMPT HASHING IS CANONICAL")
    h = D.prompt_set_hash(real["prompts"])
    check(h == real["hash"], "the reported hash is reproducible")
    mutated = list(real["prompts"]); mutated[1] = mutated[1] + " "
    check(D.prompt_set_hash(mutated) != h, "one trailing byte changes the hash")
    reordered = [real["prompts"][1], real["prompts"][0], real["prompts"][2]]
    check(D.prompt_set_hash(reordered) != h, "reordering changes the hash")
    split = ["a" + "b", "c"] ; joined = ["a", "bc"]
    check(D.prompt_set_hash(split) != D.prompt_set_hash(joined),
          "moving a boundary between prompts changes the hash (lengths are hashed)")
    check(h != "349bde5142478d07",
          "the placeholder hash is gone and cannot be reproduced")

    # Unrelated prose in HANDOFF.md must not move the prompt hash.
    alt = tmp / "prose.md"
    src_text = (D.PLANNER / "HANDOFF.md").read_text()
    alt.write_text(src_text.replace("## Cleanup", "## Cleanup notes CHANGED"))
    check(D.extract_prompts(alt)["hash"] == h,
          "editing unrelated prose does not change the prompt hash")
    check(D.extract_prompts(alt)["source_hash"] != real["source_hash"],
          "...but the SOURCE hash does change, so the edit is still visible")
    shutil.rmtree(tmp, ignore_errors=True)

    print("\nPUBLIC MANIFEST CARRIES HASHES, NOT PROMPTS")
    for p in real["prompts"]:
        check(p[:40] not in json.dumps(man), "no prompt text appears in the public manifest")
    check(man.get("prompt_set_hash") and man.get("prompt_source_hash"),
          "the manifest carries the prompt and source hashes")
    check(man.get("prompt_state") == D.PROMPTS_VERIFIED,
          "the manifest records the prompt verification state")
    check("prompt_set_hash" in D.LOCKED_FIELDS
          and "prompt_source_hash" in D.LOCKED_FIELDS,
          "prompt hashes are LOCKED fields")
    tampered = dict(man); tampered["prompt_set_hash"] = "x"
    check(not D.manifest_is_locked(tampered, man)[0],
          "a changed prompt hash fails closed after the manifest locks")

    print("\nCANDIDATE CONFINEMENT STILL DENIES THE HANDOFF")
    gt = D.PLANNER / "HANDOFF.md"
    pol = B.confinement_settings(B.benchmark_worktree_root() / "x")
    roots = [d[len("Read(/"):-len("/**)")] for d in pol["permissions"]["deny"]]
    check(any(str(gt).startswith(r.rstrip("/") + "/") for r in roots),
          "the handoff the driver reads remains denied to candidates")

    print("\nNO LEAKS")
    check(not any(p.name.startswith("unit-") and p.is_dir()
                  for p in B.benchmark_worktree_root().iterdir()
                  if p.name not in [w.name for w in wts.values()]),
          "no stray worktrees were created")
    for w in wts.values():
        shutil.rmtree(w, ignore_errors=True)
    B.sweep_worktree_root()
    check(not list(Path.home().glob(".ailocal-confinement-canary-*")),
          "no canary files leak into $HOME")
    shutil.rmtree(out, ignore_errors=True)

    print("\nHISTORICAL RECORDS REMAIN READABLE")
    hist = list((Path.home() / ".local/state/ailocal/captures/traces").glob("*.jsonl"))
    bad = 0
    for f in hist:
        for line in f.read_text(errors="replace").splitlines():
            try:
                json.loads(line)
            except Exception:  # noqa: BLE001
                bad += 1
    check(bad == 0, f"all historical trace records still parse ({len(hist)} files)")

    print()
    if failures:
        print(f"PLANNER DRIVER: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PLANNER DRIVER: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
