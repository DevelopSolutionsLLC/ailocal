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
from harness import RESOURCES, REPO, Suite, load_module  # noqa: E402
from ailocal import policy as P

PROFILES = ("16gb", "32gb", "64gb", "128gb")
CAPABILITIES = ("architecture", "implementation", "review",
                "fast", "completion", "embeddings")

_suite = Suite()
check = _suite.check


def parse(tier):
    """Capability -> field map, straight from the policy owner's reader."""
    path = RESOURCES / "profiles" / f"{tier}.toml"
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
    # Four roots with four owners, and policy.py owns every resolution. A
    # second implementation is how the state root acquired the
    # `|| echo ~/.local/state` shape that this module exists to prevent.
    check(all(hasattr(P, f) for f in ("config_root", "data_root", "state_root",
                                      "deployed_client_root")),
          "policy exposes config, data, state and client roots")
    for env, fn in (("AILOCAL_CONFIG", P.config_root),
                    ("AILOCAL_DATA", P.data_root),
                    ("AILOCAL_STATE", P.state_root),
                    ("AILOCAL_CLIENTS", P.deployed_client_root)):
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

    # THE REGRESSION: config_root is AUTHORED POLICY, deployed_client_root is
    # GENERATED OUTPUT. They default to the same directory, but they are not the
    # same root. While one function answered both, the test harness pointing
    # AILOCAL_CONFIG at the checkout (to read the shipped profiles) redirected
    # every generated client file into the repository, where they were
    # committed. Overriding the policy root must move policy, and nothing else.
    _prior = os.environ.get("AILOCAL_CONFIG")
    os.environ["AILOCAL_CONFIG"] = "/tmp/ailocal-policy-probe"
    try:
        check(str(P.config_root()) == "/tmp/ailocal-policy-probe",
              "AILOCAL_CONFIG moves the policy root")
        check("/tmp/ailocal-policy-probe" not in str(P.deployed_client_root()),
              "AILOCAL_CONFIG does NOT move generated client output")
    finally:
        if _prior is None:
            os.environ.pop("AILOCAL_CONFIG", None)
        else:
            os.environ["AILOCAL_CONFIG"] = _prior
    # A scheduled job outlives the shell that created it. If it embeds a path
    # inside the checkout, moving the checkout breaks it silently and weeks
    # later. Source inspection, because asserting this behaviourally would mean
    # installing a real LaunchAgent.
    print("\nCLI IS USABLE FROM A SHELL AND FAILS NON-ZERO")
    cli = ["ailocal", "profile"]
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
    comp = P.effective_summary()["compaction"]
    check(comp.get("window") and comp.get("pct"),
          f"profile owns compaction ({comp.get('window')} x {comp.get('pct')}%)")
    claude = json.loads((P.deployed_client_root() / "claude/settings.json").read_text())
    env = claude.get("env", {})
    check(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == str(comp["window"])
          and env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == str(comp["pct"]),
          "Claude compaction is generated from the profile block verbatim")

    codex = (P.deployed_client_root() / "codex/config.toml").read_text()
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
    # BEHAVIOURAL, not textual: an earlier form matched one exact expression
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

    print("\nNO SECOND PARSER IN THE GENERATOR")
    # Behavioural, not textual: typed values must survive generation without a
    # string round-trip, and the generator must own no scalar parser of its own.
    _sm = load_sync()
    _models = _sm.load_models_yaml(RESOURCES / "profiles" /
                                   f"{P.active_tier()}.toml")
    _pref = _models["completion"]["preferred"]
    check(isinstance(_pref, list),
          f"lists stay typed through generation (preferred is {type(_pref).__name__})")
    check(not any(hasattr(_sm, n) for n in ("flow_dict", "truthy", "_int_or_none")),
          "the generator carries no second scalar parser")

    # Atomicity is proven behaviourally by tests/generation-rollback.py.
    print("\nGENERATED FILES ARE OUTPUTS, NOT SOURCES")
    # Generated artefacts live under the runtime root, never in the checkout.
    _root = P.state_root()
    check((_root / "litellm" / "capabilities.json").exists(),
          "capabilities.json exists under the runtime root")
    check((P.deployed_client_root() / "integration-contract.json").exists(),
          "integration-contract.json exists in the config root Cadence reads")
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
    for tier, primary, total in (("16gb", "qwen3.5:4b", 98304),
                                 ("32gb", "qwen3.5:9b", 131072)):
        caps, _ = PARSED[tier]
        check(all(caps[c].get("active") == primary for c in SHARED),
              f"{tier} shares {primary} across {', '.join(SHARED)}",
              {c: caps[c].get("active") for c in SHARED}.__repr__())
        # THE SHARED-RUNNER INVARIANT, and the reason it is an invariant rather
        # than a coincidence: qwen3.5 runs on llama_cpp, where num_ctx is a fixed
        # runner window chosen at FIRST LOAD and an over-long prompt is truncated
        # from the FRONT with HTTP 200 and no error (registry.yaml,
        # runtime_engines.llama_cpp). Four roles on one backend that declare
        # different totals do not get different windows -- they get whichever
        # role loaded the runner, and the others are silently clipped.
        #
        # So this asserts EQUALITY ACROSS ROLES first, and the tier's value
        # second. Raising one role's context_input on its own is the exact defect
        # this catches; context_input and max_output must move together.
        totals = {c: int(caps[c]["context_input"]) + int(caps[c].get("max_output") or 0)
                  for c in SHARED}
        check(len(set(totals.values())) == 1,
              f"{tier} roles sharing one llama_cpp runner declare ONE num_ctx",
              repr(totals))
        check(all(v == total for v in totals.values()),
              f"{tier} primary total_context is {total}", repr(totals))
        # num_predict is now DERIVED from max_output; the profile declares the
        # intent, not the backend parameter name.
        check(caps["architecture"].get("max_output") == 8192,
              f"{tier} primary output ceiling is 8192")
        check(caps["completion"].get("active") == "qwen2.5-coder:1.5b",
              f"{tier} completion uses qwen2.5-coder:1.5b (native FIM)")

    # 64gb is the measured reference. 128gb runs the SAME MODELS -- that is what
    # makes the 64 GB measurements transferable -- but is no longer a byte copy:
    # the per-token costs are identical, so the only thing more memory changes is
    # how many tokens fit. Model identity and behaviour stay locked together;
    # geometry is free to diverge upward.
    c64, _ = PARSED["64gb"]
    c128, _ = PARSED["128gb"]
    for cap in CAPABILITIES:
        for field in ("active", "keep_alive", "temperature"):
            check(c64[cap].get(field) == c128[cap].get(field),
                  f"128gb.{cap}.{field} equals 64gb",
                  f"64={c64[cap].get(field)!r} 128={c128[cap].get(field)!r}")

    check(int(c64["architecture"]["context_input"]) + int(c64["architecture"]["max_output"])
          == 147456,
          "64gb architecture total_context is 147456")

    # No role may declare more than its model can actually hold. This is the
    # ceiling that stops a future raise from inventing capacity: every chat model
    # in every tier reports 262144 native context (`ollama show`), and
    # nomic-embed-text reports 2048.
    NATIVE = {"gemma4:26b-mlx": 262144, "qwen3.5:9b": 262144, "qwen3.5:4b": 262144,
              "qwen3.5:2b": 262144, "qwen2.5-coder:3b-instruct-q4_K_M": 32768,
              "qwen2.5-coder:1.5b": 32768, "nomic-embed-text": 2048}
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        for cap in CAPABILITIES:
            model = caps[cap].get("active")
            limit = NATIVE.get(model)
            if not limit:
                continue
            tot = int(caps[cap]["context_input"]) + int(caps[cap].get("max_output") or 0)
            check(tot <= limit,
                  f"{tier}.{cap} num_ctx {tot} is within {model}'s native {limit}")

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
    # ── interactive compaction ──────────────────────────────────────────────────
    # Deterministic only: the arithmetic and the generated files. The LATENCY that
    # motivates these numbers costs 13 minutes of GPU time to reproduce, so it is
    # measured once and recorded in docs/troubleshooting.md, not re-run in the gate.
    print("\nCOMPACTION")
    # THE DANGER POINT IS A PROPERTY OF THE RUNNER, NOT A CONSTANT. A single
    # 55000 applied to all four tiers came from the llama_cpp/GGUF backend and
    # outlived it: the 64/128 GB tiers moved to the MLX runner, which prefills
    # gemma4 at 598 tok/s cold at 137,233 tokens -- 88K costs 147s there, not the
    # ~13 min the old figure assumed. Holding every tier to one number kept the
    # MLX tiers compacting at less than half the context they can comfortably
    # carry.
    #
    # Each budget below is the tokens reachable within roughly 200s of COLD
    # prefill on that tier's own primary, from measurements recorded in the
    # profile's [compaction] block. Cold, not warm: a warm runner reports its
    # prefix cache, and a resumed session is exactly the case that has none.
    DANGER = {
        "16gb":  55000,    # qwen3.5:4b   548 tok/s @ 70K, and the slowest GPU
        "32gb":  70000,    # qwen3.5:9b   421 tok/s @ 70K
        "64gb":  120000,   # gemma4-mlx   598 tok/s @ 137K
        "128gb": 145000,   # same model, same clock; more RAM buys no speed
    }
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
        check(trig < DANGER[tier],
              f"{tier} compacts ({trig}) BEFORE its runner's measured "
              f"cold-prefill danger zone ({DANGER[tier]})")
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
    settings = P.deployed_client_root() / "claude/settings.json"
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
    codex = P.deployed_client_root() / "codex/config.toml"
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
    # The inline-completion model is deliberately NOT in the README: it is a
    # detail of a capability the user never selects, and the README documents
    # what a user operates, not every model a profile names.
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
    for fn in ("load_client_policy", "resolve_active_tier", "active_profile_path",
               "profile_path", "geometry", "required_models"):
        check(callable(getattr(P, fn, None)), f"policy.py owns {fn}()")

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
    check(set(listed) == dispatchable,
          "help lists every command, and nothing it cannot dispatch",
          f"help-only={sorted(set(listed) - dispatchable)} "
          f"unlisted={sorted(dispatchable - set(listed))}")
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


SECTIONS = {"resolver": resolver_checks, "hardware": hardware_checks,
            "policy": policy_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
