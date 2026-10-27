#!/usr/bin/env python3
"""Profile and configuration invariants.

Two sections, kept separately addressable so the gate reports them as distinct
behaviours:

  resolver   the single constrained parser — fail-closed loading, unknown and
             duplicate field rejection, active-tier selection, and the
             authoritative geometry contract (num_ctx, num_predict, admission).
  hardware   tier design — capability coverage, model sharing and disk cost,
             memory-to-tier selection, client windows, and the README
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
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, REPO, Suite, load_module  # noqa: E402
from ailocal import policy as P

PROFILES = ("16gb", "32gb", "64gb", "128gb")
CAPABILITIES = ("architecture", "implementation", "review",
                "fast", "completion")

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
    # THE INTERPRETER RUNNING THIS SUITE, not whatever `ailocal` PATH resolves to.
    # A bare "ailocal" found the pipx-installed copy instead of the working tree:
    # the suite then reported on a build nobody was editing, AND that older
    # package regenerated client config into the operator's real
    # ~/.config/ailocal from its own stale resources -- so `ailocal check`
    # alternated between OK and DRIFT depending on whether the gate had just run.
    # harness.py already declares that a suite runs against the WORKING TREE'S
    # CODE; this is that declaration applied to the subprocess boundary.
    cli = [sys.executable, "-m", "ailocal.cli", "profile"]
    r = subprocess.run(cli + ["active-tier"], capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() in P.TIERS,
          f"active-tier prints a bare scalar ({r.stdout.strip()})")
    r = subprocess.run(cli + ["role", "architecture", "--field", "model"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and ":" in r.stdout,
          "role --field prints a bare scalar")
    # In a sandbox whose profile really omits the field. Against the shipped
    # profiles this depended on the machine's active tier: every tier now
    # declares completion's `reasoning`, so no shipped role is a stable example.
    box = sandbox(marker="64gb", profile_text=GOOD)
    shutil.copy(RESOURCES / "profiles/clients.toml", box / "profiles")
    r = subprocess.run(cli + ["role", "architecture", "--field", "reasoning"],
                       capture_output=True, text=True,
                       env={**os.environ, "AILOCAL_CONFIG": str(box),
                            "AILOCAL_STATE": str(_state(box))})
    shutil.rmtree(box, ignore_errors=True)
    check(r.returncode == 0 and r.stdout.strip() == "",
          "an absent optional field prints empty, not the string 'None'",
          (r.stdout + r.stderr).strip())
    r = subprocess.run(cli + ["role", "nosuchrole"], capture_output=True, text=True)
    check(r.returncode != 0 and P.ROLE_MISSING in r.stderr,
          "an unknown role exits non-zero with a code on stderr")
    r = subprocess.run(cli + ["validate"], capture_output=True, text=True)
    check(r.returncode == 0, "validate passes for the current repository")





    print("\nCLIENT WINDOWS AND PROVIDER HAVE ONE OWNER")
    # Each client compacts on its own against the window ailocal declares to
    # it; ailocal configures no trigger. A profile-owned [compaction] block was
    # a second number that could disagree with the window it was derived from.
    check("compaction" not in P.effective_summary(),
          "the profile summary publishes no compaction trigger")
    claude = json.loads((P.deployed_client_root() / "claude/settings.json").read_text())
    env = claude.get("env", {})
    check(not {"CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"} & set(env),
          "claude-local settings carry no compaction override",
          f"env={env}")
    launch = P.load_client_policy().get("claude", {}).get("launch_default", "architecture")

    # The OUTPUT ceiling has one owner too. Claude Code's own default is 32000
    # [REAL, captured off the wire against 2.1.224]; the launch-default role
    # serves num_predict = max_output. When those disagreed, a long answer came
    # back stop_reason=max_tokens, the client re-asked instead of stopping, and
    # one response burned ~27 minutes before Claude Code's 32000 accumulator
    # aborted it. Both ends must be the same number, and that number must come
    # from the profile rather than being restated here.
    launch = P.load_client_policy().get("claude", {}).get("launch_default", "architecture")
    want_out = P.effective_role(launch)["max_output"]
    check(want_out, f"claude launch_default '{launch}' declares a max_output")
    check(env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(want_out),
          f"Claude output ceiling == {launch}.max_output ({want_out})",
          f"{env.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS')!r} != {want_out!r}")
    # The whole point is that the client never asks for more than the backend
    # will produce. Asserting the relationship, not just the value, is what
    # catches a future profile edit that raises num_predict on one side only.
    check(int(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]) <= P.effective_role(launch)["num_predict"],
          "Claude never requests more output than LiteLLM's num_predict serves")

    # The window Claude Code assumes is ADMISSION. Unset, it assumes 200000 for
    # an unrecognised gateway id. [REAL] Claude Code 2.1.310 (binary) compacts at
    # window - min(max output, 20000) - 13000, so declaring context_input keeps
    # its own trigger under what LiteLLM admits on every tier.
    _role = P.effective_role(launch)
    check(env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == str(_role["context_input"]),
          f"Claude assumed window == {launch}.context_input ({_role['context_input']})",
          f"{env.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS')!r}")
    _trigger = _role["context_input"] - min(_role["max_output"], 20_000) - 13_000
    check(0 < _trigger < _role["max_input_tokens"],
          f"Claude's own compaction point ({_trigger}) is below LiteLLM's admitted input")

    codex = (P.deployed_client_root() / "codex/config.toml").read_text()
    native = tomllib.loads(codex)
    check("model_context_window" not in native and
          "model_auto_compact_token_limit" not in native,
          "Codex has no global limits that override the selected model")
    catalog = json.loads((P.deployed_client_root() / "codex/model_catalog.json").read_text())
    for model in catalog["models"]:
        cap = model["slug"].removeprefix("ailocal-")
        role = P.effective_role(cap)
        # [REAL] Codex 0.163.0 source: unset auto_compact_token_limit resolves to
        # 90% of context_window, so the window alone keeps compaction under admission.
        check(model["context_window"] == model["max_context_window"] == role["context_input"]
              and not {"auto_compact_token_limit", "effective_context_window_percent"} & set(model),
              f"{cap}: catalog window is admission and Codex compacts on its own")
    # The regression this guards: the codex block read a key the geometry
    # migration deleted, so it silently never regenerated and kept stale values.

    # Provider is a profile value, not a model-name sniff. The embedding-route
    # half of this test went with the embeddings capability; what remains is the
    # regression that actually bit — a nomic-SHAPED model name must not force a
    # non-chat route.
    _sm4 = load_sync()
    blk = _sm4.gen_role_block("fast", {
        "active": "nomic-shaped-name:1b", "context_input": 100,
        "max_output": 10, "role": "F"})
    check("model: ollama_chat/nomic-shaped-name:1b" in blk,
          "a nomic-SHAPED name no longer forces a non-chat route")
    for penalty in (0.0, 1.5):
        root = sandbox(marker="64gb", profile_text=GOOD +
                       f"min_p = 0.0\npresence_penalty = {penalty}\n")
        try:
            tuned = P.load_profile("64gb", root)["architecture"]
            block = _sm4.gen_role_block("architecture", tuned)
            check("      min_p: 0.0" in block and
                  f"      presence_penalty: {penalty}" in block and
                  'allowed_openai_params: ["presence_penalty"]' in block,
                  f"native Qwen sampling survives parsing and generation ({penalty})")
        finally:
            shutil.rmtree(root, ignore_errors=True)
    cfg = (P.state_root() / "litellm" / "config.yaml").read_text()
    check("ollama/nomic-embed-text" not in cfg,
          "no embedding route is generated: ailocal no longer serves embeddings")
    role_count = sum(cfg["enabled"] for cfg in P.profile_summary(P.active_tier())["roles"].values())
    check(cfg.count("model: ollama_chat/") == role_count,
          "every capability uses ollama_chat")
    check("cyber" not in P.load_client_policy(RESOURCES).get("compat", {}),
          "cyber has one canonical model ID, without a bare compatibility alias")

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
    _pref = _models["fast"]["preferred"]
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
    _contract = json.loads((P.deployed_client_root() / "integration-contract.json").read_text())
    check(_contract.get("schema_version") == 2
          and _contract.get("audit", {}).get("record", "").endswith("audit/local.json")
          and _contract.get("audit", {}).get("command") == "ailocal audit",
          "the contract is schema 2 and publishes the audit record and its refresh command")
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
    # Totals follow docs/architecture.md#geometry: memory-bounded up to the
    # native window. 16 GB cannot hold the native 262144 (~8 GiB of KV on the
    # 4B); 32 GB can [calculated, UNVERIFIED on 32 GB hardware].
    for tier, primary, total in (("16gb", "qwen3.5:4b-mlx", 131072),
                                 ("32gb", "qwen3.5:9b-mlx", 262144)):
        caps, _ = PARSED[tier]
        check(all(caps[c].get("active") == primary for c in SHARED),
              f"{tier} shares {primary} across {', '.join(SHARED)}",
              {c: caps[c].get("active") for c in SHARED}.__repr__())
        # One resident backend, one window: the roles differ in output reserve only.
        totals = {c: int(caps[c]["context_input"]) + int(caps[c].get("max_output") or 0)
                  for c in SHARED}
        check(len(set(totals.values())) == 1,
              f"{tier} shared roles retain the agreed total context",
              repr(totals))
        check(all(v == total for v in totals.values()),
              f"{tier} primary total_context is {total}", repr(totals))
        # num_predict is now DERIVED from max_output; the profile declares the
        # intent, not the backend parameter name.
        check(caps["architecture"].get("max_output") == 16384,
              f"{tier} primary output ceiling is 16384")
        check(not P.resolve_role(tier, "completion", RESOURCES)["enabled"],
              f"{tier} unsupported Qwen2 MLX completion is disabled")

    # 64gb is the measured reference. 128gb runs the SAME MODELS -- that is what
    # makes the 64 GB measurements transferable -- but is no longer a byte copy:
    # the per-token costs are identical, so the only thing more memory changes is
    # how many tokens fit. Model identity and behaviour stay locked together;
    # geometry is free to diverge upward.
    c64, _ = PARSED["64gb"]
    c128, _ = PARSED["128gb"]
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        large = tier in ("64gb", "128gb")
        check("cyber" in caps, f"{tier}: cyber is available")
        if large:
            cyber = P.resolve_role(tier, "cyber", RESOURCES)
            arch = P.resolve_role(tier, "architecture", RESOURCES)
            check(cyber["model"] == "gemma4-26b-uncensored-mlx"
                  and (cyber["context_input"], cyber["max_output"], cyber["reasoning"])
                  == (arch["context_input"], arch["max_output"], arch["reasoning"]),
                  f"{tier}: cyber selects exact MLX model and architecture's geometry")
            check(cyber["source"]["revision"] == "46ab84dbe20198a2ee5e66c3e69c039b8c60ad59"
                  and cyber["top_k"] == 64
                  and P.required_runtime_patch(tier, RESOURCES) == "gemma4-switch-glu",
                  f"{tier}: pinned Gemma source and native runtime requirement reach installation")
            check(cyber["model"] in P.required_models(tier, RESOURCES),
                  f"{tier}: installer model set includes cyber")
            with tempfile.TemporaryDirectory() as box:
                root = Path(box)
                (root / "profiles").mkdir()
                text = (RESOURCES / "profiles" / f"{tier}.toml").read_text()
                (root / "profiles" / f"{tier}.toml").write_text(
                    text.replace("[cyber]\n", "[cyber]\nenabled = false\n", 1))
                check(not P.resolve_role(tier, "cyber", root)["enabled"]
                      and P.required_runtime_patch(tier, root) == "gemma4-switch-glu",
                      f"{tier}: disabling Cyber retains the fast model's MLX stop requirement")
                (root / "profiles" / f"{tier}.toml").write_text(
                    text.replace("[cyber]\n", "[cyber]\nenabled = false\n", 1)
                        .replace("[fast]\n", "[fast]\nenabled = false\n", 1))
                check(P.required_runtime_patch(tier, root) is None,
                      f"{tier}: disabled patch consumers do not require the runtime")
        else:
            cyber = P.resolve_role(tier, "cyber", RESOURCES)
            source = cyber["source"]
            from ailocal import generation
            check('stop: ["<|im_end|>", "<|im_start|>"]' in generation.gen_role_block("cyber", cyber),
                  f"{tier}: explicit chat boundaries reach the provider configuration")
            check(bool(source) and len(source["revision"]) == 40,
                  f"{tier}: small MLX source is commit-pinned")
            check(cyber["runtime_patch"] == P.ollama_patch_spec()["id"] and
                  P.required_runtime_patch(tier, RESOURCES) == cyber["runtime_patch"],
                  f"{tier}: MLX chat boundaries require the validated native runtime")
            impl = P.resolve_role(tier, "implementation", RESOURCES)
            check((cyber["context_input"], cyber["max_output"], cyber["reasoning"])
                  == (impl["context_input"], impl["max_output"], False),
                  f"{tier}: cyber has implementation's geometry and explicit thinking mode")
            check(cyber["model"] in P.required_models(tier, RESOURCES),
                  f"{tier}: installer includes the imported model")
    for cap in CAPABILITIES:
        for field in ("active", "keep_alive", "temperature"):
            check(c64[cap].get(field) == c128[cap].get(field),
                  f"128gb.{cap}.{field} equals 64gb",
                  f"64={c64[cap].get(field)!r} 128={c128[cap].get(field)!r}")

    check(int(c64["architecture"]["context_input"]) + int(c64["architecture"]["max_output"])
          == 262144,
          "64gb architecture total_context is the native 262144")

    # THE OUTPUT-CEILING POLICY, by ROLE and by ENGINE CLASS. It stopped being
    # one table on 2026-08-21, because the two runners do not charge for a
    # ceiling the same way and pretending otherwise forced the MLX tiers down to
    # what llama_cpp can afford.
    #
    # llama_cpp (16/32 GB): KV is allocated EAGERLY for the whole window, so a
    # larger ceiling is paid for out of that role's own context_input, up front,
    # whether or not the tokens are ever produced. The ceilings stay modest:
    #   architecture 16384  long design documents
    #   implementation 8192 large code generation without spending input budget
    #   review       16384  long review reports
    #
    # mlx (64/128 GB): KV is allocated LAZILY, per token in use. [REAL] num_ctx
    # 196608 loaded at 16.15 GiB resident -- identical to a small window -- so a
    # ceiling costs nothing until a response actually reaches it. The big roles
    # therefore take the largest step that still fits the model's 262144 native
    # window, which is 32768 on both 64gb and 128gb. This is why the tiers
    # legitimately differ.
    #
    # Every tier keeps fast 8192 and completion 512: both are role boundaries,
    # not capability limits. fast must answer in a word or two; completion is a
    # FIM suggestion, and a chat-sized ceiling there would make it one.
    NATIVE = 262144
    OUTPUT_POLICY = {
        "16gb":  {"architecture": 16384, "implementation": 8192, "review": 16384},
        "32gb":  {"architecture": 16384, "implementation": 8192, "review": 16384},
        "64gb":  {"architecture": 32768, "implementation": 32768, "review": 32768},
        # 128gb matches 64gb, 2026-08-21. The reason 64 GB uses 32768 is a
        # property of GEMMA, not of that machine: with reasoning on, the model
        # spends real output budget thinking before answering ([REAL] 5,142
        # eval tokens on a small coding task; a 2,048 cap truncated the answer
        # to 0/3 correct where 32,768 scored 3/3). Holding the larger machine
        # at half the ceiling made it strictly less capable than the smaller
        # one, which no evidence supported.
        "128gb": {"architecture": 32768, "implementation": 32768, "review": 32768},
    }
    UNIVERSAL = {"fast": 8192, "completion": 512}
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        for cap, want in {**OUTPUT_POLICY[tier], **UNIVERSAL}.items():
            check(int(caps[cap]["max_output"]) == want,
                  f"{tier}.{cap} output ceiling is {want}",
                  f"got {caps[cap].get('max_output')}")
        # The ceiling must FIT. This is the check that a future widening of
        # context_input cannot silently push a role past the model's own window:
        # geometry() would happily return a num_ctx the runner then truncates.
        for cap in OUTPUT_POLICY[tier]:
            total = int(caps[cap]["context_input"]) + int(caps[cap]["max_output"])
            check(total <= NATIVE,
                  f"{tier}.{cap} fits the 262144 native window ({total})")

    check("embeddings" not in P.ROLES,
          "embeddings is not a capability role: no consumer ever called it, and "
          "the only embedding client on the machine talks to Ollama directly")

    # Compaction is each client's own; a shipped profile declares no trigger.
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        check("compaction" not in caps, f"{tier} ships no [compaction] block")

    # No role may declare more than its model can actually hold. This is the
    # ceiling that stops a future raise from inventing capacity: every chat model
    # in every tier reports 262144 native context (`ollama show`), and
    # nomic-embed-text reports 2048.
    NATIVE = {"gemma4:26b-mlx": 262144, "qwen3.5:9b-mlx": 262144, "qwen3.5:4b-mlx": 262144,
              "qwen3.5:2b-mlx": 262144, "qwen2.5-coder:3b": 32768,
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
        ctxs = [int(PARSED[t][0][cap]["context_input"])
                + int(PARSED[t][0][cap].get("max_output") or 0) for t in PROFILES]
        check(ctxs[3] >= ctxs[2],
              f"128gb.{cap} context is not below 64gb", f"{ctxs[2]} -> {ctxs[3]}")


    # ── deduplication ───────────────────────────────────────────────────────────
    print("\nMODEL SET")
    for tier in PROFILES:
        caps, _ = PARSED[tier]
        for role in P.ROLES:
            cfg = caps[role]
            tags = [cfg["active"], *cfg.get("preferred", [])]
            check(all(tag.endswith("-mlx") or ":mlx-" in tag for tag in tags),
                  f"{tier}.{role}: every configured selection is MLX")
        check(not P.resolve_role(tier, "completion", RESOURCES)["enabled"],
              f"{tier}: unsupported autocomplete is not advertised")
    def unique_models(tier):
        caps, _ = PARSED[tier]
        out = {}
        for cap, f in caps.items():
            if cap not in P.ROLES:
                continue
            if not P.resolve_role(tier, cap, RESOURCES)["enabled"]:
                continue
            if f.get("active"):
                out.setdefault(f["active"], []).append(cap)
        return out

    for tier in PROFILES:
        u = unique_models(tier)
        check(len(u) <= len(P.ROLES),
              f"{tier} unique models ({len(u)}) do not exceed capabilities ({len(P.ROLES)})")

    check(len(unique_models("16gb")) == 2, "16gb resolves to 2 enabled MLX models including cyber",
          str(sorted(unique_models("16gb"))))
    check(len(unique_models("32gb")) == 2, "32gb resolves to 2 enabled MLX models including cyber",
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
    # The generated client config must match the ACTIVE profile.
    active = P.active_profile_path()
    tier = active.read_text().strip() if active.exists() else "64gb"
    settings = P.deployed_client_root() / "claude/settings.json"
    if settings.exists():
        env = json.loads(settings.read_text()).get("env", {})
        lp = P.load_client_policy().get("claude", {}).get("launch_default", "architecture")
        want = (PARSED[tier][0].get(lp) or {}).get("max_output")
        if want:
            check(env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(want),
                  f"claude settings.json output ceiling matches the active profile ({tier})",
                  f"{env.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS')!r} != {want!r}")

    # HOSTED CLAUDE IS NOT OURS. Every key above is projected into
    # CLAUDE_CONFIG_DIR only. ailocal writing an output ceiling or an assumed
    # window into ~/.claude would silently re-tune the user's hosted sessions
    # against a local model's geometry.
    #
    # Asserted on the GENERATOR, in a throwaway HOME, not on the real
    # ~/.claude: the same key names are legitimate hosted settings the user may
    # choose for themselves (CLAUDE_CODE_MAX_OUTPUT_TOKENS there is not
    # ailocal's), so their presence proves nothing about who wrote them.
    for each in P.TIERS:
        box = Path(tempfile.mkdtemp(prefix="hosted-"))
        (box / "home").mkdir(); (box / "state").mkdir()
        shutil.copytree(RESOURCES / "profiles", box / "cfg/profiles")
        (box / "state/active-profile").write_text(each)
        env = {**os.environ, "HOME": str(box / "home"), "AILOCAL_CONFIG": str(box / "cfg"),
               "AILOCAL_CLIENTS": str(box / "clients"), "AILOCAL_STATE": str(box / "state")}
        r = subprocess.run([sys.executable, "-m", "ailocal.generation"],
                           env=env, capture_output=True, text=True)
        local = box / "clients/claude/settings.json"
        projected = (r.returncode == 0 and local.exists()
                     and "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in json.loads(local.read_text()).get("env", {}))
        check(projected and not (box / "home/.claude").exists(),
              f"{each}: generation projects geometry into claude-local only, never ~/.claude",
              r.stderr.strip()[-300:] or f"wrote under {box / 'home'}")
        check(not any((box / "clients/codex").glob("*.config.toml")),
              f"{each}: selecting a model needs no generated launch presets")
        # Every enabled capability is a picker row under its canonical name, in
        # policy.ROLES order, and the picker replaces the built-in lineup.
        # [REAL] 2026-10-26, Claude Code 2.1.310: with discovery and rows only for
        # unslotted capabilities, the top of /model showed built-in names and
        # `ailocal-cyber` appeared twice around row 20.
        if local.exists():
            _settings = json.loads(local.read_text())
            _slots = P.load_client_policy(RESOURCES).get("claude", {}).get("slots", {})
            _enabled = [c for c in P.ROLES if P.resolve_role(each, c, RESOURCES)["enabled"]]
            _picker = _settings.get("modelPicker", {})
            _rows = [r["model"] for r in _picker.get("options", [])]
            check(_rows == ["ailocal-" + c for c in _enabled]
                  and _picker.get("replaceBuiltInOptions") is True,
                  f"{each}: the picker lists every capability once, in ROLES order, and nothing else",
                  f"rows={_rows} replace={_picker.get('replaceBuiltInOptions')!r}")
            check(not {"CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"}
                  & set(_settings.get("env", {})),
                  f"{each}: claude-local carries no compaction override")
            # Every enabled capability is a PEER on every generated surface:
            # claude-local (slots + picker rows, and the settings note), codex-local
            # (catalog and config), the proxy's model_list (what VS Code and the
            # local-agent MCP discover) and the integration contract (what Cadence
            # composes into both instruction files). A capability missing from any
            # one of these is reachable somewhere and unknown elsewhere.
            _all = ["ailocal-" + c for c in _enabled]
            _reach = sorted(set(_rows) | {"ailocal-" + c for c in _slots.values() if c in _enabled})
            check(_reach == sorted(_all),
                  f"{each}: claude-local reaches every capability by slot or picker row",
                  f"{_reach} != {sorted(_all)}")
            _note = next((l for l in _settings.get("//", []) if l.startswith("Models: ")), "")
            check(all(a in _note for a in _all),
                  f"{each}: claude-local settings note lists every capability", _note)
            _catalog = json.loads((box / "clients/codex/model_catalog.json").read_text())
            check([m["slug"] for m in _catalog["models"]] == _all,
                  f"{each}: codex-local catalog lists every capability",
                  str([m["slug"] for m in _catalog["models"]]))
            _codex_cfg = (box / "clients/codex/config.toml").read_text()
            _valid = next((l for l in _codex_cfg.splitlines() if l.startswith("# Valid models:")), "")
            check(all(a in _valid for a in _all),
                  f"{each}: codex-local config names every capability", _valid)
            _ml = (box / "state/litellm/config.yaml").read_text()
            _served = re.findall(r"^\s*- model_name: (ailocal-\S+)$", _ml, re.M)
            check(_served == _all,
                  f"{each}: proxy model_list serves every capability (VS Code, local-agent discover it)",
                  str(_served))
            # ONE DESCRIPTION CONVENTION. Codex's catalog, Claude Code's picker and
            # the proxy's model_list comment render every capability through
            # policy.capability_description, with the same label, so no surface
            # can show a capability differently from its peers.
            _profile = P.load_profile(each, RESOURCES)
            _by_slug = {m["slug"]: m for m in _catalog["models"]}
            _rows_by = {r["model"]: r for r in _picker.get("options", [])}
            for c in _enabled:
                _want = P.capability_description(_profile[c], P.guidance_for(_profile, c))
                _slug = "ailocal-" + c
                check(_rows_by[_slug]["label"] == _by_slug[_slug]["display_name"] == _slug
                      and _rows_by[_slug]["description"] == _by_slug[_slug]["description"] == _want
                      and f"  # {_slug} — {_want} (" in _ml,
                      f"{each}.{c}: one label and description on Claude, Codex and the proxy",
                      f"{_want!r} vs picker {_rows_by[_slug]['description']!r} / "
                      f"catalog {_by_slug[_slug]['description']!r}")
                check(re.fullmatch(r"[^—]+ — [^,:]+, \d+K ctx", _want) is not None
                      and "ailocal-" not in _want.split(" — ")[1],
                      f"{each}.{c}: description reads '<role> — <model name>, <N>K ctx'", _want)
            _contract = json.loads((box / "clients/integration-contract.json").read_text())
            check(_contract["runtime"]["canonical_capabilities"] == _all,
                  f"{each}: integration contract names every capability for Cadence's instructions",
                  str(_contract["runtime"]["canonical_capabilities"]))
        catalog = json.loads((box / "clients/codex/model_catalog.json").read_text())
        local_codex = tomllib.loads((box / "clients/codex/config.toml").read_text())
        check("model_reasoning_effort" not in local_codex,
              f"{each}: no global reasoning effort overrides the selected capability")
        for entry in catalog["models"]:
            capability = entry["slug"].removeprefix("ailocal-")
            geometry = P.resolve_role(each, capability, RESOURCES)
            check(entry["context_window"] == entry["max_context_window"] == geometry["context_input"]
                  and "auto_compact_token_limit" not in entry,
                  f"{each}.{capability}: catalog limits follow model selection")
            effort = "medium" if geometry.get("reasoning") else "none"
            check(entry.get("default_reasoning_level") == effort and
                  effort in {level["effort"] for level in entry["supported_reasoning_levels"]},
                  f"{each}.{capability}: selected capability owns its supported reasoning default")
        shutil.rmtree(box, ignore_errors=True)

    # A launch_default with no declared output ceiling must FAIL, not fall back
    # to Claude Code's 32000 — that default is exactly the number the backend
    # does not serve, so a silent omission restores the original defect.
    # regen_claude_settings raises before it stages anything, so this writes
    # nothing.
    _sm2 = load_sync()
    _m2 = _sm2.load_models_yaml(RESOURCES / "profiles" / f"{P.active_tier()}.toml")
    _c2 = _sm2.load_clients_yaml()
    _lp2 = (_c2.get("claude") or {}).get("launch_default", "architecture")
    _m2[_lp2] = {**_m2[_lp2]}
    _m2[_lp2].pop("max_output", None)
    try:
        _sm2.regen_claude_settings(_m2, _c2)
        check(False, "a launch_default without max_output is rejected",
              "regen_claude_settings returned instead of raising")
    except SystemExit as exc:
        check("max_output" in str(exc),
              "a launch_default without max_output is rejected", str(exc))

    # Every selected model owns its client limits. There are no global
    # default-role overrides to become stale after /model selection.
    check("model_context_window" not in tomllib.loads(
              (P.deployed_client_root() / "codex/config.toml").read_text()),
          "Codex model selection is governed by catalog metadata")

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

    for tier, primary in (("16gb", "qwen3.5:4b-mlx"), ("32gb", "qwen3.5:9b-mlx")):
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

    # A package command must import AND expose the main(argv) the CLI calls.
    # (There is no longer a second kind: file-target commands went with the
    # benchmark subsystem, and every entry is now a package command.)
    missing = []
    for name, (module, _) in cli.COMMANDS.items():
        try:
            if not callable(getattr(importlib.import_module(module), "main", None)):
                missing.append(f"{name} -> {module} has no main(argv)")
        except ImportError as exc:
            missing.append(f"{name} -> {module}: {exc}")
    check(not missing, "every command resolves to a real implementation",
          "; ".join(missing))

    # --help must never reach an implementation: `clients --help` once
    # configured every client. A stub main proves nothing was dispatched.
    own_help = {"install", "check"}
    check(set(cli.USAGE) == set(cli.COMMANDS) - own_help,
          "every command without its own help has a --help usage",
          f"missing={sorted(set(cli.COMMANDS) - own_help - set(cli.USAGE))}")
    import contextlib, io
    dispatched = []
    real_import = importlib.import_module
    importlib.import_module = lambda _name: type("M", (), {"main": staticmethod(
        lambda argv: dispatched.append(argv) or 0)})
    try:
        for name in cli.USAGE:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main([name, "--help"])
            check(code == 0 and out.getvalue().startswith(f"usage: ailocal {name}"),
                  f"`ailocal {name} --help` prints usage", out.getvalue()[:80])
    finally:
        importlib.import_module = real_import
    check(not dispatched, "--help dispatches no command", repr(dispatched))
    from ailocal.checks import run as check_run
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = check_run.main(["check", "--help"])
    check(code == 0 and out.getvalue().startswith("usage: ailocal check"),
          "`ailocal check --help` prints usage and runs no check", out.getvalue()[:80])

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
