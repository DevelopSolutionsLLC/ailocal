#!/usr/bin/env python3
"""profile-config.py — one fail-closed profile resolver, and no second parser.

The failure this guards against is not hypothetical. Profile parsing had four
independent implementations, and every shell entry point shared this shape:

    _TIER="$(cat config/active-profile 2>/dev/null || echo 64gb)"

A missing, empty or unreadable marker silently selected the 64 GB tier — on a
32 GB machine that installs models that do not fit, with no error anywhere. It
is the same shape as the defect the planner benchmark was built around.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import profile_config as P  # noqa: E402

failures: list[str] = []


def check(cond: object, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        failures.append(label)


def sandbox(marker=None, profile_text=None) -> Path:
    """A throwaway repo root. The REAL config/active-profile is never touched."""
    root = Path(tempfile.mkdtemp(prefix="pcfg-"))
    (root / "config" / "profiles").mkdir(parents=True)
    if marker is not None:
        (root / "config" / "active-profile").write_text(marker)
    if profile_text is not None:
        (root / "config" / "profiles" / "64gb.yaml").write_text(profile_text)
    return root


GOOD = "architecture:\n  role: Lead\n  active: m:1\n  context_input: 100\n  max_output: 20\n"


def main() -> int:
    print("ALL PROFILES LOAD")
    for t in P.TIERS:
        prof = P.load_profile(t)
        roles = [r for r in P.ROLES if r in prof]
        check(len(roles) >= 4, f"{t} loads with {len(roles)} capability roles")
    check(P.resolve_active_tier() in P.TIERS,
          f"active tier resolves ({P.resolve_active_tier()})")

    print("\nFAIL CLOSED — NEVER DEFAULTS TO 64gb")
    cases = [
        ("missing marker", sandbox(), P.ACTIVE_PROFILE_MISSING, "tier"),
        ("empty marker", sandbox(marker="   \n"), P.ACTIVE_PROFILE_EMPTY, "tier"),
        ("unknown tier", sandbox(marker="999gb"), P.ACTIVE_PROFILE_INVALID, "tier"),
        ("missing profile file", sandbox(marker="64gb"), P.PROFILE_FILE_MISSING, "load"),
        ("malformed yaml", sandbox(marker="64gb", profile_text="  stray: 1\n"),
         P.PROFILE_YAML_INVALID, "load"),
        ("non-integer context_input",
         sandbox(marker="64gb",
                 profile_text="architecture:\n  role: L\n  active: m\n  context_input: x\n"),
         P.ROLE_CONFIG_INVALID, "load"),
        ("role missing required field",
         sandbox(marker="64gb", profile_text="architecture:\n  role: L\n  context_input: 10\n"),
         P.PROFILE_SCHEMA_INVALID, "load"),
    ]
    for label, root, expect, kind in cases:
        try:
            if kind == "tier":
                P.resolve_active_tier(root)
            else:
                P.load_profile("64gb", root)
            got = "NO ERROR"
        except P.ProfileError as e:
            got = e.code
        check(got == expect, f"{label} ⇒ {expect} (got {got})")
        shutil.rmtree(root, ignore_errors=True)

    root = sandbox(marker="64gb", profile_text=GOOD)
    try:
        P.resolve_role("64gb", "review", root)
        got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.ROLE_MISSING, f"absent role ⇒ ROLE_MISSING (got {got})")
    check(P.resolve_role("64gb", "architecture", root)["model"] == "m:1",
          "a value containing a colon survives parsing")
    shutil.rmtree(root, ignore_errors=True)

    print("\nFAIL CLOSED ON TYPOS, DUPLICATES AND MALFORMED LISTS")
    # Every one of these was ACCEPTED before 2026-08-04. Only recognised keys
    # were copied out, so a misspelled tuning field parsed cleanly, was silently
    # discarded, and the role ran at the default -- a value that reads as set in
    # review and is not. Duplicates were last-wins, so a merge artefact could
    # change the deployed model with nothing to see. An unclosed flow list
    # became the bare string "[a, b".
    BAD = [
        ("temprature: 0.1",     P.PROFILE_SCHEMA_INVALID),
        ("topk: 20",            P.PROFILE_SCHEMA_INVALID),
        ("keepalive: 6h",       P.PROFILE_SCHEMA_INVALID),
        ("num_predict: 512",    P.PROFILE_SCHEMA_INVALID),   # retired by migration
        ("context: 4096",       P.PROFILE_SCHEMA_INVALID),   # retired by migration
        ("repeat-penalty: 1.0", P.PROFILE_YAML_INVALID),     # hyphen is not a key
        ("preferred: [a, b",    P.PROFILE_YAML_INVALID),     # unclosed flow list
    ]
    for frag, expect in BAD:
        root = sandbox(marker="64gb", profile_text=GOOD + f"  {frag}\n")
        try:
            P.load_profile("64gb", root)
            got = "ACCEPTED"
        except P.ProfileError as e:
            got = e.code
        check(got == expect, f"`{frag}` \u21d2 {expect} (got {got})")
        shutil.rmtree(root, ignore_errors=True)

    dup_key = ("architecture:\n  role: L\n  active: m:1\n"
               "  context_input: 100\n  context_input: 999\n  max_output: 20\n")
    dup_sec = GOOD + "architecture:\n  role: X\n  active: other\n  context_input: 55\n"
    for label, text in (("duplicate key", dup_key), ("duplicate section", dup_sec)):
        root = sandbox(marker="64gb", profile_text=text)
        try:
            P.load_profile("64gb", root)
            got = "ACCEPTED"
        except P.ProfileError as e:
            got = e.code
        check(got == P.PROFILE_YAML_INVALID,
              f"{label} \u21d2 PROFILE_YAML_INVALID (got {got})")
        shutil.rmtree(root, ignore_errors=True)

    # And the real profiles must still load -- a stricter parser that rejects
    # shipped configuration is not stricter, it is broken.
    for t in P.TIERS:
        try:
            P.load_profile(t)
            ok = True
        except P.ProfileError:
            ok = False
        check(ok, f"{t} still parses under the stricter rules")

    print("\nERRORS CARRY A CODE, NOT FILE CONTENTS")
    root = sandbox(marker="64gb",
                   profile_text=GOOD + "  secret_token: abc123SECRET\n")
    try:
        P.load_profile("64gb", root)
        msg = ""
    except P.ProfileError as e:
        msg = str(e)
    check("abc123SECRET" not in msg, "profile values never appear in an error string")
    shutil.rmtree(root, ignore_errors=True)

    print("\nNO SECOND PARSER, NO SILENT FALLBACK")
    shells = ["install-models.sh", "start.sh", "validate.sh", "doctor.sh"]
    for name in shells:
        src = (REPO / "scripts" / name).read_text()
        check("echo 64gb" not in src, f"{name} has no hardcoded 64gb fallback")
        check(not re.search(r"cat .*active-profile", src),
              f"{name} does not read active-profile directly")
        check(src.count('profile-config" active-tier') == 1,
              f"{name} resolves the tier exactly once")
    doctor = (REPO / "scripts" / "doctor.sh").read_text()
    check(not re.search(r"sed -n .*profiles/", doctor),
          "doctor.sh no longer parses profile YAML with sed")

    sync = (REPO / "scripts" / "sync-models.py").read_text()
    check("import profile_config" in sync, "sync-models uses the shared resolver")
    check('return "64gb"' not in sync, "sync-models no longer defaults to 64gb")
    bench = (REPO / "scripts" / "lib" / "benchmark.py").read_text()
    check("import profile_config" in bench, "benchmark uses the shared resolver")
    check("re.finditer" not in bench.split("def parse_profile")[1].split("def ")[0],
          "benchmark's parse_profile no longer parses YAML itself")
    check("effective_tiers" in bench,
          "benchmark reads the generated artifact (all tiers)")

    print("\nBENCHMARK AND PRODUCTION AGREE ON EVERY ROLE")
    import benchmark as B
    for t in P.TIERS:
        a = B.parse_profile(t)
        b = {r: {"active": P.resolve_role(t, r)["active"],
                 "context": P.resolve_role(t, r)["context"],
                 "enabled": P.resolve_role(t, r)["enabled"]}
             for r in P.ROLES if r in P.load_profile(t)}
        check(a == b, f"{t}: benchmark and resolver return identical role values")

    print("\nCLI IS USABLE FROM A SHELL AND FAILS NON-ZERO")
    cli = str(REPO / "scripts" / "profile-config")
    r = subprocess.run([cli, "active-tier"], capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() in P.TIERS,
          f"active-tier prints a bare scalar ({r.stdout.strip()})")
    r = subprocess.run([cli, "role", "architecture", "--field", "model"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and ":" in r.stdout,
          "role --field prints a bare scalar")
    r = subprocess.run([cli, "role", "embeddings", "--field", "top_p"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == "",
          "an absent optional field prints empty, not the string 'None'")
    r = subprocess.run([cli, "role", "nosuchrole"], capture_output=True, text=True)
    check(r.returncode != 0 and P.ROLE_MISSING in r.stderr,
          "an unknown role exits non-zero with a code on stderr")
    r = subprocess.run([cli, "validate"], capture_output=True, text=True)
    check(r.returncode == 0, "validate passes for the current repository")





    print("\nCOMPACTION AND PROVIDER HAVE ONE OWNER")
    # Both clients derive from the SAME profile block. Codex additionally clamps
    # to its default model's window — a documented client correction, not a
    # second source: using architecture's window wrote a limit Codex's default
    # model could never reach.
    eff = P.load_effective()
    comp = eff["compaction"]
    check(comp.get("window") and comp.get("pct"),
          f"profile owns compaction ({comp.get('window')} x {comp.get('pct')}%)")
    claude = json.loads((REPO / "config/clients/claude/settings.json").read_text())
    env = claude.get("env", {})
    check(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == str(comp["window"])
          and env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == str(comp["pct"]),
          "Claude compaction is generated from the profile block verbatim")

    codex = (REPO / "config/clients/codex/config.toml").read_text()
    import re as _r
    cw = int(_r.search(r"model_context_window\s*=\s*(\d+)", codex).group(1))
    cl = int(_r.search(r"model_auto_compact_token_limit\s*=\s*(\d+)", codex).group(1))
    impl = P.effective_role("implementation")
    check(cw == impl["total_context"],
          f"Codex context window == implementation total_context ({cw})")
    # Same percentage policy, but capped by ADMISSIBLE INPUT, not total
    # context. Deriving from total_context produced a trigger 2,048 tokens
    # above what the backend admits, so the session 400s before compacting.
    check(cl == min(comp["window"] * comp["pct"] // 100,
                    int(impl["context_input"] * comp["pct"] / 100)),
          f"Codex trigger derives from the SAME percentage policy ({cl})")
    check(cl <= impl["context_input"],
          f"Codex trigger ({cl}) is within admissible input "
          f"({impl['context_input']})")
    # The regression this guards: the codex block read a key the geometry
    # migration deleted, so it silently never regenerated and kept stale values.
    sync = (REPO / "scripts" / "sync-models.py").read_text()
    check('_cx = _geom(' in sync and 'cx_in = _cx["context_input"]' in sync,
          "Codex compaction reads shared geometry and caps on context_input")
    check("codex compaction cannot be derived" in sync,
          "Codex compaction fails closed instead of silently skipping")

    # Provider is a profile value, not a model-name sniff.
    check(P.effective_role("embeddings").get("provider") == "ollama",
          "embeddings declares provider: ollama in the profile")
    # BEHAVIOURAL: a role declaring provider: ollama must get the ollama route
    # even when the model is not called "nomic". A prose mention of the old
    # conditional is not the defect — this is the third test in this suite to
    # trip on its own explanatory comment.
    import importlib.util as _il4
    _s4 = _il4.spec_from_file_location("_sm4", REPO / "scripts" / "sync-models.py")
    _sm4 = _il4.module_from_spec(_s4); _s4.loader.exec_module(_sm4)
    blk = _sm4.gen_role_block("embeddings", {
        "active": "some-other-embedder:1b", "provider": "ollama",
        "context_input": 2048, "role": "E"})
    check("model: ollama/some-other-embedder:1b" in blk,
          "provider comes from the profile, not from the model name")
    blk = _sm4.gen_role_block("fast", {
        "active": "nomic-shaped-name:1b", "context_input": 100,
        "max_output": 10, "role": "F"})
    check("model: ollama_chat/nomic-shaped-name:1b" in blk,
          "a nomic-SHAPED name no longer forces the embedding route")
    cfg = (REPO / "config/litellm/config.yaml").read_text()
    check("model: ollama/nomic-embed-text" in cfg,
          "embeddings still generates the ollama (non-chat) route")
    check(cfg.count("model: ollama_chat/") == 5,
          "the five chat roles still use ollama_chat")

    print("\nEXPLICIT CONTEXT AND OUTPUT GEOMETRY")
    for t in P.TIERS:
        prof = P.load_profile(t)
        for r in P.ROLES:
            if r not in prof:
                continue
            raw = prof[r]
            check("context" not in raw, f"{t}.{r}: legacy `context` is gone")
            check(isinstance(raw.get("context_input"), int),
                  f"{t}.{r}: declares context_input")
            check(raw.get("max_output") != -1, f"{t}.{r}: no num_predict/-1 reserve")
            c = P.resolve_role(t, r)
            check(c["total_context"] == c["context_input"] + (c["max_output"] or 0),
                  f"{t}.{r}: total_context is derived, not configured")
            check(c["num_ctx"] == c["total_context"], f"{t}.{r}: num_ctx == total_context")
            check(c["max_input_tokens"] == c["context_input"],
                  f"{t}.{r}: admission == context_input BY CONSTRUCTION")
            if c["max_output"]:
                check(c["num_predict"] == c["max_output"],
                      f"{t}.{r}: num_predict == max_output")

    # A profile still carrying `context` has not been migrated — that is an
    # error, not a fallback: guessing which of the two old meanings was intended
    # is how both survived side by side.
    box = Path(tempfile.mkdtemp(prefix="legacy-"))
    (box / "config" / "profiles").mkdir(parents=True)
    (box / "config" / "active-profile").write_text("64gb")
    (box / "config" / "profiles" / "64gb.yaml").write_text(
        "architecture:\n  role: L\n  active: m\n  context: 4096\n")
    try:
        P.load_profile("64gb", box); got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.PROFILE_SCHEMA_INVALID, f"legacy `context` fails closed (got {got})")
    (box / "config" / "profiles" / "64gb.yaml").write_text(
        "architecture:\n  role: L\n  active: m\n  context_input: 100\n  max_output: -1\n")
    try:
        P.load_profile("64gb", box); got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.ROLE_CONFIG_INVALID, f"max_output -1 fails closed (got {got})")
    shutil.rmtree(box, ignore_errors=True)

    # Geometry is derived in ONE place.
    g = P.geometry(1000, 200)
    check(g["total_context"] == 1200 and g["num_ctx"] == 1200
          and g["num_predict"] == 200 and g["max_input_tokens"] == 1000,
          "geometry() derives all four values from the two declared ones")
    sync_src = (REPO / "scripts" / "sync-models.py").read_text()
    check("_pc.geometry(" in sync_src,
          "sync-models calls the shared geometry, does not re-derive it")
    # BEHAVIOURAL, not textual: the previous form matched one exact expression
    # (`_geom(info)["max_output"]`) and broke the moment that value was hoisted
    # into a local -- testing the spelling rather than the property.
    _prof = P.load_profile(P.active_tier())
    _emitted = [_sm4.gen_role_block(r, _prof[r])
                for r in ("architecture", "implementation", "review")]
    _declared = [P.resolve_role(P.active_tier(), r)["max_output"]
                 for r in ("architecture", "implementation", "review")]
    check(all(f"num_predict: {d}" in blk for blk, d in zip(_emitted, _declared)),
          f"emitted num_predict equals the profile's max_output {_declared}")

    # Advertised must equal enforced: they disagreed before (fast advertised
    # 12288 while enforcing 4096).
    import re as _re3
    cfg3 = (REPO / "config" / "litellm" / "config.yaml").read_text()
    for blk in cfg3.split("  - model_name: ")[1:]:
        name = blk.split("\n")[0].strip()
        gg = lambda k: (int(m.group(1)) if (m := _re3.search(rf"{k}:\s*(-?\d+)", blk)) else None)
        np_, mo = gg("num_predict"), gg("max_output_tokens")
        if np_ is None or mo is None:
            continue
        check(np_ == mo, f"{name}: advertised max_output_tokens == enforced num_predict")

    print("\nPRODUCTION ADMISSION RESPECTS PHYSICAL GEOMETRY")
    import importlib.util as _il2
    _sp2 = _il2.spec_from_file_location("_sm2", REPO / "scripts" / "sync-models.py")
    _sm2 = _il2.module_from_spec(_sp2); _sp2.loader.exec_module(_sm2)

    # num_ctx holds prompt AND generation. Advertising the whole window as
    # admissible input is KNOWN_ISSUES #19: accepted by pre-call, then trimmed
    # from the FRONT by Ollama with HTTP 200 and no error.
    admit, note = _sm2.admission_for("architecture", 98304, 16384)
    check(admit == 81920 and not note, "finite reserve: 98304-16384 = 81920")
    admit, note = _sm2.admission_for("review", 24576, 4096)
    check(admit == 20480, "finite reserve: 24576-4096 = 20480")

    # -1 is Ollama INFINITE: no arithmetic is possible, so none is invented.
    admit, note = _sm2.admission_for("implementation", 24576, -1)
    check(admit == 24576 and note == _sm2.OUTPUT_RESERVE_UNBOUNDED,
          "num_predict -1 preserves num_ctx and is MARKED unsafe, not silently reserved")

    # Invalid geometry must fail generation, never clamp.
    for ctx, np_, label in ((4096, 4096, "reserve == window"),
                            (4096, 8192, "reserve > window"),
                            (0, 128, "zero window")):
        try:
            _sm2.admission_for("x", ctx, np_); raised = False
        except SystemExit:
            raised = True
        check(raised, f"invalid geometry rejected, not clamped ({label})")

    # Every generated finite-output alias must satisfy the invariant.
    import re as _re
    cfg = (REPO / "config" / "litellm" / "config.yaml").read_text()
    checked = 0
    for blk in cfg.split("  - model_name: ")[1:]:
        name = blk.split("\n")[0].strip()
        g = lambda k: (int(m.group(1)) if (m := _re.search(rf"{k}:\s*(-?\d+)", blk)) else None)
        ctx, np_, mit = g("num_ctx"), g("num_predict"), g("max_input_tokens")
        if ctx is None or mit is None or np_ is None or np_ <= 0:
            continue
        checked += 1
        check(mit <= ctx - np_,
              f"{name}: admits {mit} <= {ctx}-{np_} = {ctx-np_}")
    check(checked >= 3, f"invariant checked on {checked} finite-output aliases")

    print("\nGENERATED ARTIFACT IS AUTHORITATIVE AT RUNTIME")
    eff = P.load_effective()
    check(eff["schema_version"] in P.SUPPORTED_SCHEMA_VERSIONS,
          f"schema_version is supported ({eff['schema_version']})")
    for k in ("tier", "roles", "source_profile", "source_profile_sha256",
              "active_profile_sha256", "config_sha256", "generated_at"):
        check(k in eff, f"artifact records {k}")
    arch = eff["roles"]["architecture"]
    for k in ("model", "context", "num_predict", "reasoning", "temperature",
              "top_p", "top_k", "repeat_penalty", "keep_alive", "persona"):
        check(k in arch, f"role carries {k}")
    check(P.active_tier() == eff["tier"], "runtime tier comes from the artifact")


    print("\nALL TIERS COME FROM GENERATED DATA — NO RUNTIME YAML")
    tiers = P.effective_tiers()
    check(sorted(tiers) == sorted(P.TIERS), f"all four tiers normalized {sorted(tiers)}")
    for t in P.TIERS:
        blk = tiers[t]
        check(blk["source_profile_sha256"], f"{t} records its source hash")
        check(len(blk["roles"]) >= 4, f"{t} carries {len(blk['roles'])} roles")
    check(P.effective_role_for_tier("32gb", "architecture")["model"],
          "a non-active tier resolves from generated data")
    try:
        P.effective_role_for_tier("999gb", "architecture"); got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.EFFECTIVE_PROFILE_SCHEMA_INVALID, "unknown tier fails closed")

    import benchmark as _B
    for t in P.TIERS:
        a = _B.parse_profile(t)
        b = {r: {"active": c["model"], "context": c["context"], "enabled": c["enabled"]}
             for r, c in tiers[t]["roles"].items()}
        check(a == b, f"{t}: benchmark cross-tier == generated data")
    bsrc = (REPO / "scripts" / "lib" / "benchmark.py").read_text()
    check("effective_tiers" in bsrc and "re.finditer" not in
          bsrc.split("def parse_profile")[1].split("def ")[0],
          "benchmark parse_profile reads generated data, parses no YAML")

    # Shell consumers must not parse YAML either.
    for name in ("start.sh", "doctor.sh", "install-models.sh"):
        src = (REPO / "scripts" / name).read_text()
        check("active:" not in src or "grep -E" not in src.split("active:")[0][-80:],
              f"{name} does not grep|sed profile YAML")
    check("effective-profile.json" in (REPO / "scripts" / "install-models.sh").read_text(),
          "install-models.sh consumes the generated artifact")

    print("\nSTALENESS AND CORRUPTION FAIL CLOSED")
    import shutil as _sh
    box = Path(tempfile.mkdtemp(prefix="eff-"))
    (box / "config" / "profiles").mkdir(parents=True)
    _sh.copy(REPO / "config" / "effective-profile.json", box / "config")
    _sh.copy(REPO / "config" / "active-profile", box / "config")
    # ALL tiers: the artifact now normalizes every tier and validates every
    # source hash, so a fixture carrying only the active profile is incomplete.
    for _t in P.TIERS:
        _sh.copy(REPO / "config" / "profiles" / f"{_t}.yaml", box / "config" / "profiles")
    check(P.load_effective(box)["tier"] == eff["tier"], "a faithful copy validates")

    def expect(code, mutate, label):
        b = Path(tempfile.mkdtemp(prefix="eff-"))
        _sh.copytree(box / "config", b / "config")
        mutate(b)
        try:
            P.load_effective(b)
            got = "NO ERROR"
        except P.ProfileError as e:
            got = e.code
        check(got == code, f"{label} ⇒ {code} (got {got})")
        _sh.rmtree(b, ignore_errors=True)

    expect(P.EFFECTIVE_PROFILE_MISSING,
           lambda b: (b / "config" / "effective-profile.json").unlink(),
           "artifact deleted")
    expect(P.EFFECTIVE_PROFILE_STALE_TIER,
           lambda b: (b / "config" / "active-profile").write_text("32gb\n"),
           "active-profile changed")
    expect(P.EFFECTIVE_PROFILE_STALE_SOURCE,
           lambda b: (b / "config" / "profiles" / f"{eff['tier']}.yaml")
                     .write_text("architecture:\n  role: x\n  active: y\n  context: 1\n"),
           "source profile edited")

    def corrupt(b):
        f = b / "config" / "effective-profile.json"
        d = json.loads(f.read_text())
        d["config_sha256"] = "0" * 64
        f.write_text(json.dumps(d))
    expect(P.EFFECTIVE_PROFILE_HASH_INVALID, corrupt, "config hash corrupted")

    def bad_schema(b):
        f = b / "config" / "effective-profile.json"
        d = json.loads(f.read_text())
        d["schema_version"] = 99
        f.write_text(json.dumps(d))
    expect(P.EFFECTIVE_PROFILE_SCHEMA_INVALID, bad_schema, "unsupported schema")
    _sh.rmtree(box, ignore_errors=True)

    print("\nNO RUNTIME YAML FALLBACK, NO REDUNDANT WRAPPER")
    cli_src = (REPO / "scripts" / "profile-config").read_text()
    check("effective_role" in cli_src and "active_tier" in cli_src,
          "the CLI reads the artifact")
    check(not (REPO / "scripts" / "profile-json").exists(),
          "scripts/profile-json is deleted (jq is already a dependency)")
    check("_legacy_load_models_yaml" not in sync,
          "the dead legacy parser is deleted")

    # NO SECOND PARSER IN A SHELL ENTRY POINT. install.sh carried a Python
    # regex heredoc that parsed config/profiles/<tier>.yaml directly and read
    # `context` and `num_predict` -- fields the geometry migration removed --
    # so the install plan printed:
    #     configured context:  None
    #     max output:          None
    # Structural, because the field names are the part that rots: a parser
    # reading CURRENT names would have printed plausible stale numbers instead
    # of an obvious None, and nothing would have caught it.
    # Referencing the path (an existence check) is fine; READING FIELDS out of
    # it is not. These patterns are how a second parser actually reappears.
    FIELD_READS = (
        "re.finditer(r'^([a-z_]+):",          # the heredoc that was here
        "grep -m1 '^status:",                 # the last single-field grep
        "^  context_input:", "^  max_output:", "^  active:",
    )
    for entry in ("install.sh", "install-models.sh", "install-clients.sh",
                  "doctor.sh", "update.sh"):
        path = REPO / "scripts" / entry
        if not path.exists():
            continue
        src = path.read_text()
        hit = [p for p in FIELD_READS if p in src]
        check(not hit,
              f"{entry} does not read profile YAML fields itself (found {hit})")

    # And the plan it prints must use the CURRENT schema, from the resolver.
    inst = (REPO / "scripts" / "install.sh").read_text()
    check("context_input" in inst and "max_output" in inst,
          "install.sh reports context_input/max_output, not the removed fields")
    check("profile-config" in inst and "profile-summary" in inst,
          "install.sh renders its plan from the resolver, not from YAML")
    check(inst.index("sync-models.py") < inst.index("profile-summary"),
          "install.sh generates BEFORE printing a plan derived from generation")
    # Behavioural, not textual: typed values must survive generation without a
    # string round-trip. A prose mention of "reserialize" is not the defect.
    import importlib.util as _il
    _sp = _il.spec_from_file_location("_sm", REPO / "scripts" / "sync-models.py")
    _sm = _il.module_from_spec(_sp)
    _sp.loader.exec_module(_sm)
    _models = _sm.load_models_yaml(REPO / "config" / "profiles" /
                                   f"{P.active_tier()}.yaml")
    # `completion`, not `architecture`: preferred is documentation-only and
    # optional (profile_config defaults it to []), and architecture's list was
    # removed on 2026-08-03 when qwen3-coder:30b stopped being a supported
    # candidate. This assertion only needs SOME typed list to prove values
    # survive generation without a string round-trip; it must not pin a role
    # whose fallback policy is allowed to change.
    _pref = _models["completion"]["preferred"]
    check(isinstance(_pref, list),
          f"lists stay typed through generation (preferred is {type(_pref).__name__})")
    check(_sm.flow_list(_pref) is _pref,
          "flow_list is idempotent — no list→string→list round-trip")
    check(_sm.flow_list("[a, b]") == ["a", "b"],
          "flow_list still parses raw strings for config/clients.yaml")
    check("jq -r" in (REPO / "scripts" / "doctor.sh").read_text(),
          "doctor.sh uses jq, not a bespoke extractor")

    print("\nGENERATION IS ATOMIC AND INSTALL FAILS CLOSED")
    check("flush_stage" in sync and "os.replace" in sync,
          "outputs are staged and swapped atomically")
    inst = (REPO / "scripts" / "install.sh").read_text()
    check("sync-models.py\" || true" not in inst and "|| true" not in
          inst.split("Syncing model config")[1].split("step ")[0],
          "install.sh no longer swallows a generation failure")
    check("stopping before any model is pulled" in inst,
          "install.sh stops before install-models.sh on generation failure")

    print("\nGENERATED FILES ARE OUTPUTS, NOT SOURCES")
    for gen in ("config/capabilities.generated.json", "config/integration-contract.json"):
        check((REPO / gen).exists(), f"{gen} exists")
    caps = json.loads((REPO / "config/capabilities.generated.json").read_text())
    tier = P.resolve_active_tier()
    arch = P.resolve_role(tier, "architecture")
    blob = json.dumps(caps)
    check(arch["model"] in blob,
          "the generated capability file reflects the resolved profile model")

    print()
    if failures:
        print(f"PROFILE CONFIG: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PROFILE CONFIG: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
