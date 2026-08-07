#!/usr/bin/env python3
"""Profile and configuration invariants.

Two sections, kept separately addressable so the gate reports them as distinct
behaviours:

  resolver   the single constrained parser — fail-closed loading, unknown and
             duplicate field rejection, active-tier selection, and the
             authoritative geometry contract (num_ctx, num_predict, admission).
  hardware   tier design — capability coverage, model sharing and disk cost,
             memory-to-tier selection, compaction thresholds, and the README
             claims that must track the profiles.

Geometry is asserted once, by the resolver. The hardware section checks tier
*design*, not field-level geometry, so the two do not restate each other.

Usage: profiles.py [resolver|hardware]   (default: both)
"""
from __future__ import annotations

import os
import re
import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
from harness import RESOURCES, REPO, Suite, load_module  # noqa: E402
from ailocal import policy as P

PROFILES = ("16gb", "32gb", "64gb", "128gb")
CAPABILITIES = ("architecture", "implementation", "review",
                "fast", "completion", "embeddings")

_suite = Suite()
check = _suite.check


def parse(tier):
    """Capability -> field map, straight from the policy owner's reader."""
    path = REPO / "profiles" / f"{tier}.toml"
    return P.load_profile_file(path), path.read_text()


PARSED = {t: parse(t) for t in PROFILES}


def load_sync():
    """Fresh generation module: each sandbox needs unshared state."""
    return load_module("generation", REPO / "src" / "ailocal" / "generation.py")


def sandbox(marker=None, profile_text=None) -> Path:
    """A throwaway repo root. The real runtime state is never touched.

    Paths come from the policy owner, so relocating them needs no fixture edit.
    """
    root = Path(tempfile.mkdtemp(prefix="pcfg-"))
    (root / "profiles").mkdir(parents=True)
    if marker is not None:
        _write_marker(root, marker)
    if profile_text is not None:
        (root / "profiles" / "64gb.toml").write_text(profile_text)
    return root


def _state(root: Path) -> Path:
    """A sandbox's runtime state root."""
    return root / "state"


def _write_marker(root: Path, text: str) -> None:
    """Write the active-tier marker into a sandbox's own runtime state."""
    target = P.active_profile_path(_state(root))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


GOOD = ('[architecture]\nrole = "Lead"\nactive = "m:1"\n'
        'context_input = 100\nmax_output = 20\n')



def resolver_checks() -> None:
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
        ("malformed policy", sandbox(marker="64gb", profile_text="[architecture\n"),
         P.PROFILE_INVALID, "load"),
        ("non-integer context_input",
         sandbox(marker="64gb",
                 profile_text='[architecture]\nrole = "L"\nactive = "m"\ncontext_input = "x"\n'),
         P.ROLE_CONFIG_INVALID, "load"),
        ("role missing required field",
         sandbox(marker="64gb",
                 profile_text='[architecture]\nrole = "L"\ncontext_input = 10\n'),
         P.PROFILE_SCHEMA_INVALID, "load"),
    ]
    for label, root, expect, kind in cases:
        try:
            if kind == "tier":
                P.resolve_active_tier(root, _state(root))
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
        ('temprature = 0.1',       P.PROFILE_SCHEMA_INVALID),
        ('topk = 20',              P.PROFILE_SCHEMA_INVALID),
        ('keepalive = "6h"',       P.PROFILE_SCHEMA_INVALID),
        ('num_predict = 512',      P.PROFILE_SCHEMA_INVALID),   # retired
        ('context = 4096',         P.PROFILE_SCHEMA_INVALID),   # retired
        ('"repeat-penalty" = 1.0', P.PROFILE_SCHEMA_INVALID),
        ('preferred = [a, b',      P.PROFILE_INVALID),          # unclosed list
    ]
    for frag, expect in BAD:
        root = sandbox(marker="64gb", profile_text=GOOD + f"{frag}\n")
        try:
            P.load_profile("64gb", root)
            got = "ACCEPTED"
        except P.ProfileError as e:
            got = e.code
        check(got == expect, f"`{frag}` \u21d2 {expect} (got {got})")
        shutil.rmtree(root, ignore_errors=True)

    # Duplicates were last-wins under the old parser: a merge artefact could
    # change the deployed model with nothing to see in review.
    dup_key = GOOD + "context_input = 999\n"
    dup_sec = GOOD + '[architecture]\nrole = "X"\nactive = "other"\n'
    for label, text in (("duplicate key", dup_key), ("duplicate section", dup_sec)):
        root = sandbox(marker="64gb", profile_text=text)
        try:
            P.load_profile("64gb", root)
            got = "ACCEPTED"
        except P.ProfileError as e:
            got = e.code
        check(got == P.PROFILE_INVALID,
              f"{label} \u21d2 PROFILE_INVALID (got {got})")
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
                   profile_text=GOOD + 'secret_token = "abc123SECRET"\n')
    try:
        P.load_profile("64gb", root)
        msg = ""
    except P.ProfileError as e:
        msg = str(e)
    check("abc123SECRET" not in msg, "profile values never appear in an error string")
    shutil.rmtree(root, ignore_errors=True)

    print("\nNO SECOND PARSER, NO SILENT FALLBACK")
    sync = (REPO / "src" / "ailocal" / "generation.py").read_text()
    check('return "64gb"' not in sync, "sync-models no longer defaults to 64gb")

    # ADR 009. Config, data and state have separate homes, and policy.py owns
    # all three. A second implementation is how the state root acquired the
    # `|| echo ~/.local/state` shape that this module exists to prevent.
    check(all(hasattr(P, f) for f in ("config_root", "data_root", "state_root")),
          "policy exposes config_root, data_root and state_root")
    for env, fn in (("AILOCAL_CONFIG", P.config_root),
                    ("AILOCAL_DATA", P.data_root),
                    ("AILOCAL_STATE", P.state_root)):
        # Restore, never delete: a checkout run DECLARES these, so dropping one
        # silently repoints every later check at the XDG location instead.
        prior = os.environ.get(env)
        os.environ[env] = "/tmp/ailocal-root-probe"
        try:
            check(str(fn()) == "/tmp/ailocal-root-probe", f"{env} overrides its root")
        finally:
            if prior is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = prior
    check(P.state_root() != P.config_root(),
          "state root is never the config root (generated state stays out of the tree)")
    # A scheduled job outlives the shell that created it. If it embeds a path
    # inside the checkout, moving the checkout breaks it silently and weeks
    # later. Source inspection, because asserting this behaviourally would mean
    # installing a real LaunchAgent.
    src = (REPO / "src" / "ailocal" / "install.py").read_text()
    runner = src.split("def cmd_update_check")[1].split("\ndef ")[0]
    check("/lib" not in runner and "REPO" not in runner,
          "the generated update-check runner embeds no checkout path")
    check('shutil.which("ailocal")' in runner,
          "the update-check runner resolves the installed ailocal command")

    # Only policy.py may derive a root from XDG or ~/.local. Anything else is a
    # second owner that drifts the moment one of them moves.
    for path in sorted(REPO.glob("tests/benchmarks/*.py")):
        if path.name == "policy.py":
            continue
        body = path.read_text()
        check("XDG_STATE_HOME" not in body and "XDG_CONFIG_HOME" not in body
              and "XDG_DATA_HOME" not in body,
              f"{path.name} does not resolve an XDG root itself")
    bench = (REPO / "tests" / "benchmarks" / "suite.py").read_text()
    check("re.finditer" not in bench.split("def parse_profile")[1].split("def ")[0],
          "benchmark's parse_profile no longer parses YAML itself")

    print("\nBENCHMARK AND PRODUCTION AGREE ON EVERY ROLE")
    import suite as B
    for t in P.TIERS:
        a = B.parse_profile(t)
        b = {r: {"active": P.resolve_role(t, r)["active"],
                 "context": P.resolve_role(t, r)["context"],
                 "enabled": P.resolve_role(t, r)["enabled"]}
             for r in P.ROLES if r in P.load_profile(t)}
        check(a == b, f"{t}: benchmark and resolver return identical role values")

    print("\nCLI IS USABLE FROM A SHELL AND FAILS NON-ZERO")
    cli = [str(REPO / "ailocal"), "profile"]
    r = subprocess.run(cli + ["active-tier"], capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() in P.TIERS,
          f"active-tier prints a bare scalar ({r.stdout.strip()})")
    r = subprocess.run(cli + ["role", "architecture", "--field", "model"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and ":" in r.stdout,
          "role --field prints a bare scalar")
    r = subprocess.run(cli + ["role", "embeddings", "--field", "top_p"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == "",
          "an absent optional field prints empty, not the string 'None'")
    r = subprocess.run(cli + ["role", "nosuchrole"], capture_output=True, text=True)
    check(r.returncode != 0 and P.ROLE_MISSING in r.stderr,
          "an unknown role exits non-zero with a code on stderr")
    r = subprocess.run(cli + ["validate"], capture_output=True, text=True)
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
    claude = json.loads((P.state_root() / "clients/claude/settings.json").read_text())
    env = claude.get("env", {})
    check(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == str(comp["window"])
          and env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == str(comp["pct"]),
          "Claude compaction is generated from the profile block verbatim")

    codex = (P.state_root() / "clients/codex/config.toml").read_text()
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

    # Provider is a profile value, not a model-name sniff.
    check(P.effective_role("embeddings").get("provider") == "ollama",
          "embeddings declares provider: ollama in the profile")
    # BEHAVIOURAL: a role declaring provider: ollama must get the ollama route
    # even when the model is not called "nomic". A prose mention of the old
    # conditional is not the defect — this is the third test in this suite to
    # trip on its own explanatory comment.
    _sm4 = load_sync()
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
    cfg = (P.state_root() / "litellm" / "config.yaml").read_text()
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
    (box / "profiles").mkdir(parents=True)
    _write_marker(box, "64gb")
    (box / "profiles" / "64gb.toml").write_text(
        '[architecture]\nrole = "L"\nactive = "m"\ncontext = 4096\n')
    try:
        P.load_profile("64gb", box); got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.PROFILE_SCHEMA_INVALID, f"legacy `context` fails closed (got {got})")
    (box / "profiles" / "64gb.toml").write_text(
        '[architecture]\nrole = "L"\nactive = "m"\n'
        'context_input = 100\nmax_output = -1\n')
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
    sync_src = (REPO / "src" / "ailocal" / "generation.py").read_text()
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
    cfg3 = (P.state_root() / "litellm" / "config.yaml").read_text()
    for blk in cfg3.split("  - model_name: ")[1:]:
        name = blk.split("\n")[0].strip()
        gg = lambda k: (int(m.group(1)) if (m := _re3.search(rf"{k}:\s*(-?\d+)", blk)) else None)
        np_, mo = gg("num_predict"), gg("max_output_tokens")
        if np_ is None or mo is None:
            continue
        check(np_ == mo, f"{name}: advertised max_output_tokens == enforced num_predict")

    print("\nPRODUCTION ADMISSION RESPECTS PHYSICAL GEOMETRY")
    # Admission has ONE owner, policy.geometry(); generation emits what it
    # returns. Asserted on the deployed artifact rather than on a helper, so a
    # second derivation reappearing in generation would fail here.
    check(not hasattr(load_sync(), "admission_for"),
          "generation carries no second admission derivation")

    # Every generated finite-output alias must satisfy the invariant.
    import re as _re
    cfg = (P.state_root() / "litellm" / "config.yaml").read_text()
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

    import suite as _B
    for t in P.TIERS:
        a = _B.parse_profile(t)
        b = {r: {"active": c["model"], "context": c["context"], "enabled": c["enabled"]}
             for r, c in tiers[t]["roles"].items()}
        check(a == b, f"{t}: benchmark cross-tier == generated data")
    bsrc = (REPO / "tests" / "benchmarks" / "suite.py").read_text()
    check("effective_tiers" in bsrc and "re.finditer" not in
          bsrc.split("def parse_profile")[1].split("def ")[0],
          "benchmark parse_profile reads generated data, parses no YAML")

    print("\nSTALENESS AND CORRUPTION FAIL CLOSED")
    import shutil as _sh
    box = Path(tempfile.mkdtemp(prefix="eff-"))
    (box / "profiles").mkdir(parents=True)
    _eff_dst = P.effective_profile_path(_state(box))
    _eff_dst.parent.mkdir(parents=True, exist_ok=True)
    _sh.copy(P.effective_profile_path(), _eff_dst)
    _write_marker(box, P.active_profile_path().read_text())
    # ALL tiers: the artifact now normalizes every tier and validates every
    # source hash, so a fixture carrying only the active profile is incomplete.
    for _t in P.TIERS:
        _sh.copy(REPO / "profiles" / f"{_t}.toml", box / "profiles")
    check(P.load_effective(box, _state(box))["tier"] == eff["tier"], "a faithful copy validates")

    def expect(code, mutate, label):
        b = Path(tempfile.mkdtemp(prefix="eff-"))
        _sh.copytree(box / "profiles", b / "profiles")
        # Runtime state is external and sandbox-scoped: copy box's whole state
        # so b owns an independent, complete generation to mutate.
        _sh.copytree(_state(box), _state(b))
        mutate(b)
        try:
            P.load_effective(b, _state(b))
            got = "NO ERROR"
        except P.ProfileError as e:
            got = e.code
        check(got == code, f"{label} ⇒ {code} (got {got})")
        _sh.rmtree(b, ignore_errors=True)

    expect(P.EFFECTIVE_PROFILE_MISSING,
           lambda b: P.effective_profile_path(_state(b)).unlink(),
           "artifact deleted")
    expect(P.EFFECTIVE_PROFILE_STALE_TIER,
           lambda b: _write_marker(b, "32gb\n"),
           "active-profile changed")
    # Same tier NAME, different bytes. Only the marker hash can see this, and it
    # was unreachable while the generator hashed a path inside the checkout
    # where no marker exists: the hash was "", and the guard skips a falsy hash.
    expect(P.EFFECTIVE_PROFILE_STALE_TIER,
           lambda b: _write_marker(b, f"{eff['tier']}   \n\n"),
           "active-profile rewritten with the same tier")
    expect(P.EFFECTIVE_PROFILE_STALE_SOURCE,
           lambda b: (b / "profiles" / f"{eff['tier']}.toml")
                     .write_text("architecture:\n  role: x\n  active: y\n  context: 1\n"),
           "source profile edited")

    def corrupt(b):
        f = P.effective_profile_path(_state(b))
        d = json.loads(f.read_text())
        d["config_sha256"] = "0" * 64
        f.write_text(json.dumps(d))
    expect(P.EFFECTIVE_PROFILE_HASH_INVALID, corrupt, "config hash corrupted")

    def bad_schema(b):
        f = P.effective_profile_path(_state(b))
        d = json.loads(f.read_text())
        d["schema_version"] = 99
        f.write_text(json.dumps(d))
    expect(P.EFFECTIVE_PROFILE_SCHEMA_INVALID, bad_schema, "unsupported schema")
    _sh.rmtree(box, ignore_errors=True)

    print("\nNO SECOND PARSER IN THE GENERATOR")
    # Behavioural, not textual: typed values must survive generation without a
    # string round-trip, and the generator must own no scalar parser of its own.
    _sm = load_sync()
    _models = _sm.load_models_yaml(REPO / "profiles" /
                                   f"{P.active_tier()}.toml")
    _pref = _models["completion"]["preferred"]
    check(isinstance(_pref, list),
          f"lists stay typed through generation (preferred is {type(_pref).__name__})")
    check(not any(hasattr(_sm, n) for n in ("flow_dict", "truthy", "_int_or_none")),
          "the generator carries no second scalar parser")

    print("\nGENERATION IS ATOMIC AND INSTALL FAILS CLOSED")
    check("flush_stage" in sync and "os.replace" in sync,
          "outputs are staged and swapped atomically")
    # The generator's exit status must abort the install: everything after this
    # step (the plan, the image pull, the model pull) would otherwise run
    # against a half-generated configuration.
    check("raise SystemExit" in (REPO / "src/ailocal/install.py").read_text()
          .split("Generating configuration")[1].split("step(")[0],
          "install stops on a generation failure, before any model is pulled")

    print("\nGENERATED FILES ARE OUTPUTS, NOT SOURCES")
    # Generated artefacts live under the runtime root, never in the checkout.
    _root = P.state_root()
    for gen in ("litellm/capabilities.json", "integration-contract.json"):
        check((_root / gen).exists(), f"{gen} exists under the runtime root")
    check(not (REPO / "capabilities.generated.json").exists(),
          "no generated artefact remains in the checkout")
    caps = json.loads((_root / "litellm" / "capabilities.json").read_text())
    tier = P.resolve_active_tier()
    arch = P.resolve_role(tier, "architecture")
    blob = json.dumps(caps)
    check(arch["model"] in blob,
          "the generated capability file reflects the resolved profile model")

    print()


def hardware_checks() -> None:

    # ── schema ──────────────────────────────────────────────────────────────────
    print("SCHEMA")
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        check(set(CAPABILITIES) <= set(caps),
              f"{tier} exposes every capability",
              f"missing {sorted(set(CAPABILITIES) - set(caps))}")
        for cap in CAPABILITIES:
            if cap not in caps:
                continue
            active = caps[cap].get("active")
            check(bool(active), f"{tier}.{cap} names a backend", "active is empty")


    # ── the agreed tier design ──────────────────────────────────────────────────
    print("\nTIER DESIGN")
    SHARED = ("architecture", "implementation", "review", "fast")
    for tier, primary in (("16gb", "qwen3.5:4b"), ("32gb", "qwen3.5:9b")):
        caps, _ = PARSED[tier]
        check(all(caps[c].get("active") == primary for c in SHARED),
              f"{tier} shares {primary} across {', '.join(SHARED)}",
              {c: caps[c].get("active") for c in SHARED}.__repr__())
        # After the geometry migration these declare INPUT, and total_context
        # (= input + output) is what used to be called `context`.
        check(all(int(caps[c]["context_input"]) + int(caps[c].get("max_output") or 0) == 65536
                  for c in SHARED),
              f"{tier} primary total_context is 65536")
        # num_predict is now DERIVED from max_output; the profile declares the
        # intent, not the backend parameter name.
        check(caps["architecture"].get("max_output") == 8192,
              f"{tier} primary output ceiling is 8192")
        check(caps["completion"].get("active") == "qwen2.5-coder:1.5b",
              f"{tier} completion uses qwen2.5-coder:1.5b (native FIM)")

    # 64gb is the measured reference; 128gb must be its exact functional copy.
    c64, _ = PARSED["64gb"]
    c128, _ = PARSED["128gb"]
    for cap in CAPABILITIES:
        for field in ("active", "context_input", "max_output", "keep_alive", "temperature"):
            check(c64[cap].get(field) == c128[cap].get(field),
                  f"128gb.{cap}.{field} equals 64gb",
                  f"64={c64[cap].get(field)!r} 128={c128[cap].get(field)!r}")

    check(int(c64["architecture"]["context_input"]) + int(c64["architecture"]["max_output"])
          == 98304,
          "64gb architecture total_context is 98304, not the stale 64K")

    # Capability must never DECREASE as memory grows.
    for cap in CAPABILITIES:
        # embeddings has no max_output (embedding route, no generation).
        ctxs = [int(PARSED[t][0][cap]["context_input"])
                + int(PARSED[t][0][cap].get("max_output") or 0) for t in PROFILES]
        check(ctxs[3] >= ctxs[2],
              f"128gb.{cap} context is not below 64gb", f"{ctxs[2]} -> {ctxs[3]}")


    # ── embeddings ──────────────────────────────────────────────────────────────
    print("\nEMBEDDINGS")
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        ctx = int(caps["embeddings"]["context_input"])
        check(ctx <= 2048,
              f"{tier} embeddings context within nomic-embed-text's real 2048 limit",
              f"declares {ctx}")


    # ── deduplication ───────────────────────────────────────────────────────────
    print("\nMODEL SET")
    def unique_models(tier):
        caps, _ = PARSED[tier]
        out = {}
        for cap, f in caps.items():
            if cap not in CAPABILITIES:
                continue
            if (f.get("enabled") or "true").lower() == "false":
                continue
            if f.get("active"):
                out.setdefault(f["active"], []).append(cap)
        return out

    for tier in PROFILES:
        u = unique_models(tier)
        check(len(u) <= len(CAPABILITIES),
              f"{tier} unique models ({len(u)}) do not exceed capabilities ({len(CAPABILITIES)})")

    check(len(unique_models("16gb")) == 3, "16gb resolves to 3 unique models",
          str(sorted(unique_models("16gb"))))
    check(len(unique_models("32gb")) == 3, "32gb resolves to 3 unique models",
          str(sorted(unique_models("32gb"))))
    check(sorted(unique_models("64gb")) == sorted(unique_models("128gb")),
          "64gb and 128gb pull the identical model set, so their storage cost is equal")


    # ── tier selection ──────────────────────────────────────────────────────────
    print("\nTIER SELECTION")
    from ailocal import install as I
    for gb, expected in ((8, None), (16, "16gb"), (18, "16gb"), (24, "16gb"),
                         (32, "32gb"), (36, "32gb"), (48, "32gb"),
                         (64, "64gb"), (96, "64gb"), (128, "128gb"), (192, "128gb")):
        got = I.tier_for_memory(gb)
        check(got == expected, f"{gb} GB selects {expected or 'nothing (unsupported)'}",
              f"got {got}")
    src = (REPO / "src/ailocal/install.py").read_text()
    check("Refusing an unsafe override under --yes" in src,
          "an override above physical memory is refused unattended")

    # ── interactive compaction ──────────────────────────────────────────────────
    # Deterministic only: the arithmetic and the generated files. The LATENCY that
    # motivates these numbers costs 13 minutes of GPU time to reproduce, so it is
    # measured once and recorded in docs/troubleshooting.md, not re-run in the gate.
    print("\nCOMPACTION")
    DANGER = 55000   # measured: cold prefill passes ~5 min beyond roughly this point
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        c = caps.get("compaction", {})
        check(bool(c.get("window") and c.get("pct")),
              f"{tier} declares a compaction window and pct", repr(c))
        if not (c.get("window") and c.get("pct")):
            continue
        win, pct = int(c["window"]), int(c["pct"])
        trig = win * pct // 100
        arch = int(caps["architecture"]["context_input"]) + int(caps["architecture"].get("max_output") or 0)
        check(trig < arch,
              f"{tier} compaction trigger ({trig}) is below the architecture max ({arch})")
        check(trig < DANGER,
              f"{tier} compacts ({trig}) BEFORE the measured cold-prefill danger zone")
        # The point of compaction is to keep sessions out of that zone. A window set
        # ABOVE the model maximum would never fire before the model itself refused.
        check(win <= arch, f"{tier} compaction window ({win}) does not exceed the model max ({arch})")

        # THE ADMISSION INVARIANT. The checks above compare against
        # context_input + max_output, which is what let a real defect through:
        # codex's trigger was 18,432 on a role admitting 16,384, so a long session
        # took an HTTP 400 ContextWindowExceeded 2,048 tokens BEFORE it could
        # compact. Output space is not input space -- a compaction point above
        # context_input can never be reached.
        #
        # Mirrors the derivation in sync-models.py exactly; if that changes, this
        # must fail rather than drift.
        codex_role = "implementation"          # the capability codex-local defaults to
        if codex_role in caps:
            cx_in = int(caps[codex_role]["context_input"])
            codex_trig = min(win * pct // 100, int(cx_in * pct / 100))
            check(codex_trig <= cx_in,
                  f"{tier} codex compaction trigger ({codex_trig}) is within the "
                  f"admissible input for {codex_role} ({cx_in})",
                  f"trigger {codex_trig} EXCEEDS context_input {cx_in} -- the backend "
                  f"would 400 before compaction fires")

    # The generated client config must match the ACTIVE profile, or the client
    # compacts on a threshold this repository never chose.
    active = P.active_profile_path()
    tier = active.read_text().strip() if active.exists() else "64gb"
    cc = PARSED[tier][0].get("compaction", {})
    settings = P.state_root() / "clients/claude/settings.json"
    if settings.exists() and cc:
        import json
        env = json.loads(settings.read_text()).get("env", {})
        check(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == str(cc["window"]),
              f"claude settings.json window matches the active profile ({tier})",
              f"{env.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW')!r} != {cc['window']!r}")
        check(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == str(cc["pct"]),
              f"claude settings.json pct matches the active profile ({tier})",
              f"{env.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE')!r} != {cc['pct']!r}")

    # Codex's numbers must describe the model CODEX defaults to, not architecture.
    # Deriving them from architecture wrote a compaction limit of 49,152 against a
    # default model whose entire context is 24,576 -- unreachable, because the model
    # 400s on context length long before compaction could fire.
    codex = P.state_root() / "clients/codex/config.toml"
    cx_cap = P.load_client_policy().get("codex", {}).get("default", "implementation")
    if codex.exists() and cc and cx_cap in PARSED[tier][0]:
        txt = codex.read_text()
        _cx = PARSED[tier][0][cx_cap]
        cx_in = int(_cx["context_input"])
        cx_ctx = cx_in + int(_cx.get("max_output") or 0)
        # The advertised WINDOW is total context; the compaction TRIGGER is capped
        # by admissible INPUT. Deriving the trigger from cx_ctx put it 2,048 tokens
        # above what the backend admits -- see the admission invariant above.
        want = min(int(cc["window"]) * int(cc["pct"]) // 100, int(cx_in * int(cc["pct"]) / 100))
        check(f"model_context_window = {cx_ctx}" in txt,
              f"codex window is its OWN default capability '{cx_cap}' ({cx_ctx}), not architecture")
        check(f"model_auto_compact_token_limit = {want}" in txt,
              f"codex compaction limit is {want}")
        check(want <= cx_in,
              f"codex compaction limit ({want}) is within admissible input ({cx_in})")

    # ── README cannot drift from the profiles ───────────────────────────────────
    print("\nDOCUMENTATION")
    readme = (REPO / "README.md").read_text()

    # The capability table quotes the 64gb contexts. A hand-maintained table is
    # exactly how "64K" survived a profile that had moved to 98304.
    ctx_k = round((int(c64["architecture"]["context_input"])
                   + int(c64["architecture"].get("max_output") or 0)) / 1024)
    check(f"{ctx_k}K" in readme,
          f"README states the real 64gb architecture context ({ctx_k}K)",
          "README still quotes a stale figure")
    check("64K / resident" not in readme,
          "no stale 64K architecture claim remains")

    for tier, primary in (("16gb", "qwen3.5:4b"), ("32gb", "qwen3.5:9b")):
        check(primary in readme, f"README names {tier}'s primary model ({primary})")
    check("qwen2.5-coder:1.5b" in readme,
          "README names the small-tier completion model")
    check("16 GB unified memory minimum" in readme,
          "README states the real minimum, not a 64 GB requirement")
    check("85 GB" not in readme,
          "the unexplained 85 GB disk claim is gone")
    check("only the **64 gb** profile is active" not in readme.lower(),
          "README no longer claims a single active profile")


def policy_checks() -> None:
    """One owner for profile and client policy."""
    import subprocess

    _suite.section("CLIENT POLICY FAILS CLOSED")
    import tempfile, shutil
    cases = {
        "unknown client":    "bogus:\n  default: fast\n",
        "duplicate section": '[claude]\na = "fast"\n[claude]\nb = "fast"\n',
        "duplicate key":     '[claude]\ndefault = "fast"\ndefault = "review"\n',
        "unclosed list":     '[continue]\nchat = ["a", "b"\n',
        "unclosed mapping":  '[claude]\nslots = {opus = "fast"\n',
        "unknown client":    '[nosuchclient]\ndefault = "fast"\n',
        # An UNQUOTED dotted key nests instead of naming a model.
        "bare dotted key":   '[compat]\ngpt-5.5 = "implementation"\n',
    }
    for name, text in cases.items():
        d = Path(tempfile.mkdtemp()); (d / "profiles").mkdir()
        (d / "profiles" / "clients.toml").write_text(text)
        try:
            P.load_client_policy(repo_root=d)
            check(False, f"{name} is rejected", "ACCEPTED")
        except P.ProfileError as exc:
            check(exc.code == P.CLIENT_POLICY_INVALID, f"{name} is rejected",
                  f"got {exc.code}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    d = Path(tempfile.mkdtemp())
    try:
        P.load_client_policy(repo_root=d)
        check(False, "a missing clients.toml is rejected", "ACCEPTED")
    except P.ProfileError as exc:
        check(exc.code == P.CLIENT_POLICY_MISSING, "a missing clients.toml is rejected")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    _suite.section("ONE OWNER")
    policy_src = (REPO / "src" / "ailocal" / "policy.py").read_text()
    for fn in ("load_client_policy", "resolve_active_tier", "active_profile_path",
               "profile_path", "geometry", "required_models"):
        check(f"def {fn}(" in policy_src, f"policy.py owns {fn}()")

    # No production consumer may CONSTRUCT a policy path. Prose, prompts and
    # remediation text may name the file; only code that builds the path is a
    # second owner.
    prod = [q for q in (REPO / "src").rglob("*.py")
            if "/resources/" not in str(q) and q.name != "policy.py"]
    # Path CONSTRUCTION only: a quoted path that is the whole value, or a
    # pathlib join. Remediation sentences and prompts name the file in prose and
    # are not a second owner.
    build = re.compile(r'(=\s*["\']config/(active-profile|clients\.toml)["\']'
                       r'|/\s*["\']config["\']\s*/\s*["\'](active-profile|clients\.toml))')
    offenders = []
    for q in prod:
        for line in q.read_text().splitlines():
            t = line.strip()
            if t.startswith("#") or t.startswith('"""'):
                continue
            if build.search(line):
                offenders.append(q.name); break
    check(bool(prod), f"the ownership scan reaches production code ({len(prod)} files)")
    check(not offenders, "no production file constructs a policy path",
          ", ".join(sorted(set(offenders))))

    # The validator must not execute the generator to read policy.
    cfg = (REPO / "src" / "ailocal" / "checks" / "config.py").read_text()
    check("spec_from_file_location" not in cfg,
          "validation does not load the generator to read policy")

    # The command surface has ONE owner: cli.py's tables. Help used to be a
    # hand-maintained heredoc plus two copies in docs, and all three went stale
    # -- `test` and `install` were dispatched but undocumented. Rendering help
    # from the dispatch table makes that class of drift unrepresentable, and
    # this asserts the tables stay in agreement.
    sys.path.insert(0, str(REPO / "src"))
    from ailocal import cli  # noqa: E402

    listed = [n for _, names in cli.GROUPS for n in names]
    check(len(listed) == len(set(listed)), "no command is listed twice in help",
          ", ".join(sorted({n for n in listed if listed.count(n) > 1})))
    # `profile` is answered inside cli.main() rather than by a module.
    dispatchable = set(cli.COMMANDS) | {"profile"}
    check(set(listed) == dispatchable - cli.INTERNAL,
          "help lists every public command, and nothing it cannot dispatch",
          f"help-only={sorted(set(listed) - dispatchable)} "
          f"unlisted={sorted(dispatchable - cli.INTERNAL - set(listed))}")
    # An internal command must still dispatch: it is undiscoverable, not gone.
    check(cli.INTERNAL <= dispatchable, "every internal command still dispatches",
          ", ".join(sorted(cli.INTERNAL - dispatchable)))
    import importlib

    # A package command must import AND expose the main(argv) the CLI calls; a
    # benchmark target must exist as a file under the data root.
    missing = []
    for name, (module, _) in cli.COMMANDS.items():
        try:
            if not callable(getattr(importlib.import_module(module), "main", None)):
                missing.append(f"{name} -> {module} has no main(argv)")
        except ImportError as exc:
            missing.append(f"{name} -> {module}: {exc}")
    check(not missing, "every command resolves to a real implementation",
          "; ".join(missing))

    _suite.section("GENERATOR CONSUMES POLICY")
    gen = (REPO / "src" / "ailocal" / "generation.py").read_text()
    check("_pc.load_client_policy()" in gen,
          "sync-models reads client policy through the owner")
    check(gen.count("def load_clients_yaml") == 1
          and "for line in" not in gen.split("def load_clients_yaml")[1][:400],
          "sync-models no longer parses clients.toml itself")


SECTIONS = {"resolver": resolver_checks, "hardware": hardware_checks,
            "policy": policy_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
