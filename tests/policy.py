#!/usr/bin/env python3
"""Profile, client-policy and environment-ownership invariants.

Writes only temp roots. Generation keeps the real HOME; every hosted client root
is pointed at a temp dir that must stay empty."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, REPO, Suite  # noqa: E402
from ailocal import generation as G, policy as P  # noqa: E402

S = Suite()
check = S.check
TMP = Path(tempfile.mkdtemp(prefix="ailocal-policy-"))
GOOD = '[primary]\nrole = "Lead"\nactive = "m:1b"\ncontext_input = 100\nmax_output = 20\n'
MLX_EVIDENCE = "same-model GGUF comparison"   # docs/maintenance.md#model-format


@contextlib.contextmanager
def env_set(**values):
    """Set env vars and restore the prior values, never drop them: the gate
    wrapper declares the isolated roots and a pop would repoint later checks."""
    def apply(pairs):
        for k, v in pairs.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    prior = {k: os.environ.get(k) for k in values}
    apply(values)
    try:
        yield
    finally:
        apply(prior)


def box(marker=None, text=None, name="profiles/64gb.toml") -> Path:
    root = Path(tempfile.mkdtemp(dir=TMP))
    (root / "profiles").mkdir()
    if marker is not None:
        target = P.active_profile_path(root / "state")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(marker)
    if text is not None:
        (root / name).write_text(text)
    return root


def code_of(fn, *args, message=False):
    try:
        fn(*args)
        return "OK"
    except P.ProfileError as exc:
        return str(exc) if message else exc.code


def yaml_int(block, key):
    m = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(-?\d+)\s*$", block)
    return int(m.group(1)) if m else None


def resolver() -> None:
    S.section("RESOLVER FAILS CLOSED")
    for t in P.TIERS:
        check(code_of(P.load_profile, t) == "OK"
              and sum(r in P.load_profile(t) for r in P.ROLES) >= 3, f"{t} loads")
    tier_cases = [("missing marker", None, P.ACTIVE_PROFILE_MISSING),
                  ("empty marker", "   \n", P.ACTIVE_PROFILE_EMPTY),
                  ("unknown tier", "999gb", P.ACTIVE_PROFILE_INVALID)]
    for label, marker, want in tier_cases:
        root = box(marker)
        got = code_of(P.resolve_active_tier, root, root / "state")
        check(got == want, f"{label} => {want}", got)
    head = '[primary]\nrole = "L"\nactive = "m:1b"\n'
    load_cases = [
        ("missing profile file", None, P.PROFILE_FILE_MISSING),
        ("malformed policy", "[primary\n", P.PROFILE_INVALID),
        ("non-integer context_input", head + 'context_input = "x"\n', P.ROLE_CONFIG_INVALID),
        ("missing required field", '[primary]\nrole = "L"\ncontext_input = 10\n',
         P.PROFILE_SCHEMA_INVALID),
        ("legacy `context`", head + "context = 4096\n", P.PROFILE_SCHEMA_INVALID),
        ("max_output -1", head + "context_input = 100\nmax_output = -1\n", P.ROLE_CONFIG_INVALID),
        ("duplicate key", GOOD + "context_input = 999\n", P.PROFILE_INVALID),
        ("duplicate section", GOOD + '[primary]\nrole = "X"\nactive = "o:1b"\n', P.PROFILE_INVALID),
        ("retired role section", GOOD + '[review]\nrole = "R"\nactive = "m:1b"\ncontext_input = 100\nmax_output = 20\n', P.PROFILE_SCHEMA_INVALID),
        ("un-migrated 0.15 profile", GOOD.replace("[primary]", "[architecture]"), P.PROFILE_SCHEMA_INVALID),
    ]
    # Unknown keys were silently dropped before 2026-08-04, so a typo read as set and was not.
    src = 'source = { model="hf.co/a/b:t.gguf", sha256="' + "a" * 64 + '", renderer="r", parser="p", '
    for frag in ("temprature = 0.1", "topk = 20", '"repeat-penalty" = 1.0', "num_predict = 512",
                 "draft_num_predict = -1", "draft_num_predict = true", "draft_num_predict = 1.5",
                 src + 'draft_model="hf.co/a/b:h.gguf" }', src + 'draft_sha256="' + "b" * 64 + '" }',
                 src + 'projector_model="hf.co/a/b:p.gguf" }', src + 'projector_sha256="' + "c" * 64 + '" }'):
        load_cases.append((f"`{frag[:40]}`", GOOD + frag + "\n", P.PROFILE_SCHEMA_INVALID))
    load_cases.append(("unclosed list", GOOD + "preferred = [a, b\n", P.PROFILE_INVALID))
    for label, text, want in load_cases:
        got = code_of(P.load_profile, "64gb", box("64gb", text))
        check(got == want, f"{label} => {want}", got)
    root = box("64gb", GOOD)
    check(code_of(P.resolve_role, "64gb", "fast", root) == P.ROLE_MISSING, "absent role => ROLE_MISSING")
    check(P.resolve_role("64gb", "primary", root)["model"] == "m:1b", "a colon in a value survives")
    msg = code_of(P.load_profile, "64gb", box("64gb", GOOD.replace("[primary]", "[architecture]")), message=True)
    check("ailocal install" in msg, "a retired role section names the migration", msg)
    msg = code_of(P.load_profile, "64gb", box("64gb", GOOD + 'secret_token = "abc123SECRET"\n'), message=True)
    check(msg != "OK" and "abc123SECRET" not in msg, "errors carry a code, never profile values")

    S.section("ROOTS")
    for var, fn in (("AILOCAL_CONFIG", P.config_root), ("AILOCAL_DATA", P.data_root),
                    ("AILOCAL_STATE", P.state_root), ("AILOCAL_CLIENTS", P.deployed_client_root)):
        with env_set(**{var: "/tmp/ailocal-root-probe"}):
            check(str(fn()) == "/tmp/ailocal-root-probe", f"{var} overrides its root")
    # One resolver once answered both, so pointing policy at the checkout committed generated files.
    with env_set(AILOCAL_CONFIG="/tmp/ailocal-policy-probe", AILOCAL_CLIENTS=None):
        check("ailocal-policy-probe" not in str(P.deployed_client_root()),
              "AILOCAL_CONFIG does not move generated client output")
    check(P.state_root() != P.config_root(), "state root is never the config root")

    S.section("CLI SCALARS")
    # sys.executable, not PATH's `ailocal`: a stale pipx copy once answered and regenerated real config.
    cli = [sys.executable, "-m", "ailocal.cli", "profile"]
    run = lambda *a, **kw: subprocess.run(cli + list(a), capture_output=True, text=True, **kw)  # noqa: E731
    r, f = run("active-tier"), run("role", "primary", "--field", "model")
    check(r.returncode == f.returncode == 0 and r.stdout.strip() in P.TIERS and ":" in f.stdout,
          "active-tier and role --field print bare scalars", r.stdout + f.stdout)
    root = box("64gb", GOOD)
    shutil.copy(RESOURCES / "profiles/clients.toml", root / "profiles")
    r = run("role", "primary", "--field", "reasoning",
            env={**os.environ, "AILOCAL_CONFIG": str(root), "AILOCAL_STATE": str(root / "state")})
    check(r.returncode == 0 and r.stdout.strip() == "", "an absent field prints empty, not 'None'",
          (r.stdout + r.stderr).strip())
    r = run("role", "nosuchrole")
    check(r.returncode != 0 and P.ROLE_MISSING in r.stderr, "an unknown role exits non-zero with a code")


def geometry() -> None:
    S.section("DERIVED GEOMETRY")
    g = P.geometry(1000, 200)
    check((g["total_context"], g["num_ctx"], g["num_predict"], g["max_input_tokens"])
          == (1200, 1200, 200, 1000), "geometry() derives all four values")
    bad = []
    for t in P.TIERS:
        prof = P.load_profile(t)
        for r in (r for r in P.ROLES if r in prof):
            c = P.resolve_role(t, r)
            if not (c["num_ctx"] == c["total_context"] == c["context_input"] + (c["max_output"] or 0)
                    and c["max_input_tokens"] == c["context_input"]
                    and (not c["max_output"] or c["num_predict"] == c["max_output"])):
                bad.append(f"{t}.{r}")
            if f"num_predict: {c['max_output']}" not in G.gen_role_block(r, prof[r]) and c["max_output"]:
                bad.append(f"{t}.{r} emitted num_predict")
    check(not bad, "num_ctx = context_input + max_output, num_predict = max_output, "
          "admission = context_input, for every role", ", ".join(bad))
    check(not any(hasattr(G, n) for n in ("admission_for", "flow_dict", "truthy", "_int_or_none"))
          and isinstance(G.load_models_yaml(RESOURCES / "profiles/64gb.toml")["fast"]["preferred"], list),
          "the generator has no second admission or scalar parser; lists stay typed")
    blk = G.gen_role_block("fast", {"active": "nomic-shaped-name:1b", "context_input": 100,
                                    "max_output": 10, "role": "F"})
    check("model: ollama_chat/nomic-shaped-name:1b" in blk, "a nomic-shaped name keeps the chat route")
    for penalty in (0.0, 1.5):
        arch = P.load_profile("64gb", box("64gb", GOOD + f"min_p = 0.0\npresence_penalty = {penalty}\n"))
        blk = G.gen_role_block("primary", arch["primary"])
        check("      min_p: 0.0" in blk and f"      presence_penalty: {penalty}" in blk
              and 'allowed_openai_params: ["presence_penalty"]' in blk,
              f"native sampling survives parsing and generation ({penalty})")
    # Omitting it would fall back to Claude Code's 32000, the number the backend does not serve.
    models, clients = G.load_models_yaml(RESOURCES / "profiles/64gb.toml"), G.load_clients_yaml()
    launch = (clients.get("claude") or {}).get("launch_default", "primary")
    models[launch] = {k: v for k, v in models[launch].items() if k != "max_output"}
    try:
        G.regen_claude_settings(models, clients)
        check(False, "a launch_default without max_output is rejected", "returned")
    except SystemExit as exc:
        check("max_output" in str(exc), "a launch_default without max_output is rejected", str(exc))


def generated() -> None:
    S.section("GENERATION PER TIER (claude-local only, never hosted roots)")
    launch = P.load_client_policy(RESOURCES).get("claude", {}).get("launch_default", "primary")
    for tier in P.TIERS:
        b = Path(tempfile.mkdtemp(dir=TMP))
        shutil.copytree(RESOURCES / "profiles", b / "cfg/profiles")
        P.active_profile_path(b / "state").parent.mkdir(parents=True, exist_ok=True)
        P.active_profile_path(b / "state").write_text(tier)
        hosted = {k: b / "hosted" / k for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME",
                                                "AILOCAL_VSCODE_USER", "ZDOTDIR")}
        for d in hosted.values():
            d.mkdir(parents=True)
        env = {**os.environ, "AILOCAL_CONFIG": str(b / "cfg"), "AILOCAL_CLIENTS": str(b / "clients"),
               "AILOCAL_STATE": str(b / "state"), **{k: str(v) for k, v in hosted.items()}}
        r = subprocess.run([sys.executable, "-m", "ailocal.generation"], env=env,
                           capture_output=True, text=True)
        local = b / "clients/claude/settings.json"
        written = [str(p) for d in hosted.values() for p in d.rglob("*")]
        if not check(r.returncode == 0 and local.exists() and not written,
                     f"{tier}: generation writes claude-local and leaves every hosted root empty",
                     r.stderr.strip()[-300:] or f"wrote {written}"):
            continue
        settings = json.loads(local.read_text())
        cenv = settings.get("env", {})
        enabled = list(P.enabled_roles(tier, RESOURCES))
        names = ["ailocal-" + c for c in enabled]
        role = P.resolve_role(tier, launch, RESOURCES)
        # Claude Code 2.1.224 defaults to 32000 output; a mismatch looped on max_tokens for ~27 min.
        check(cenv.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(role["max_output"])
              and role["max_output"] <= role["num_predict"]
              and cenv.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == str(role["context_input"]),
              f"{tier}: Claude output and window are {launch}'s max_output and context_input", repr(cenv))
        # [REAL] Claude Code 2.1.310 compacts at window - min(max output, 20000) - 13000.
        trigger = role["context_input"] - min(role["max_output"], 20_000) - 13_000
        check(0 < trigger < role["max_input_tokens"]
              and not {"CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"} & set(cenv),
              f"{tier}: Claude compacts on its own below admission")
        # [REAL] 2026-10-26, 2.1.310: rows for unslotted roles only put built-ins atop /model, cyber twice.
        picker = settings.get("modelPicker", {})
        rows = [o["model"] for o in picker.get("options", [])]
        check(rows == names and picker.get("replaceBuiltInOptions") is True,
              f"{tier}: the picker lists every capability once, in ROLES order, and nothing else",
              f"rows={rows}")
        catalog = json.loads((b / "clients/codex/model_catalog.json").read_text())
        codex_text = (b / "clients/codex/config.toml").read_text()
        codex = tomllib.loads(codex_text)
        litellm = (b / "state/litellm/config.yaml").read_text()
        contract = json.loads((b / "clients/integration-contract.json").read_text())
        valid = next((l for l in codex_text.splitlines() if l.startswith("# Valid models:")), "")
        note = next((l for l in settings.get("//", []) if l.startswith("Models: ")), "")
        surfaces = {"codex catalog": [m["slug"] for m in catalog["models"]],
                    "proxy model_list": re.findall(r"^\s*- model_name: (ailocal-\S+)$", litellm, re.M),
                    "contract": contract["runtime"]["canonical_capabilities"]}
        check(all(v == names for v in surfaces.values()) and all(n in valid and n in note for n in names),
              f"{tier}: every capability is a peer on every generated surface", repr(surfaces))
        profile = P.load_profile(tier, RESOURCES)
        by_slug = {m["slug"]: m for m in catalog["models"]}
        by_row = {o["model"]: o for o in picker.get("options", [])}
        off = []
        for c in enabled:
            want, slug = P.capability_description(profile[c], P.guidance_for(profile, c)), "ailocal-" + c
            if not (by_row[slug]["label"] == by_slug[slug]["display_name"] == slug
                    and by_row[slug]["description"] == by_slug[slug]["description"] == want
                    and f"  # {slug} — {want} (" in litellm):
                off.append(f"{c}: {want!r}")
        check(not off, f"{tier}: one label and description on Claude, Codex and proxy",
              "; ".join(off))
        bad = []
        for m in catalog["models"]:
            g = P.resolve_role(tier, m["slug"].removeprefix("ailocal-"), RESOURCES)
            effort = "medium" if g.get("reasoning") else "none"
            if not (m["context_window"] == m["max_context_window"] == g["context_input"]
                    and not {"auto_compact_token_limit", "effective_context_window_percent"} & set(m)
                    and m.get("default_reasoning_level") == effort
                    and effort in {lv["effort"] for lv in m["supported_reasoning_levels"]}):
                bad.append(m["slug"])
        check(not bad and not {"model_context_window", "model_auto_compact_token_limit",
                               "model_reasoning_effort"} & set(codex)
              and not any((b / "clients/codex").glob("*.config.toml")),
              f"{tier}: Codex limits and reasoning come from the selected catalog entry only", repr(bad))
        bad, finite = [], 0
        for blk in litellm.split("  - model_name: ")[1:]:
            name = blk.split("\n")[0].strip()
            ctx, npr, mit, mot = (yaml_int(blk, k) for k in
                                  ("num_ctx", "num_predict", "max_input_tokens", "max_output_tokens"))
            if None not in (ctx, npr, mit) and npr > 0:
                finite += 1
                if mit > ctx - npr or (mot is not None and mot != npr):
                    bad.append(name)
        check(finite >= 3 and not bad, f"{tier}: proxy admits <= num_ctx - num_predict and advertises "
              f"what it enforces ({finite} aliases)", ", ".join(bad))
        check("ollama/nomic-embed-text" not in litellm
              and litellm.count("model: ollama_chat/") == len(enabled),
              f"{tier}: every capability routes through ollama_chat; no embedding route")
        # #89: the retired 0.15 names, and ailocal-fast where the tier omits [fast], are
        # deprecated aliases of the role they now mean, never model_list entries.
        aliases = dict(re.findall(r"^    (ailocal-\S+): (ailocal-\S+)$", litellm, re.M))
        want = {"ailocal-" + o: "ailocal-" + n for o, n in P.deprecated_aliases(enabled).items()}
        check(aliases == want and {"ailocal-architecture", "ailocal-implementation", "ailocal-review"} <= set(aliases)
              and ("ailocal-fast" in aliases) is ("fast" not in enabled)
              and "DEPRECATED since 0.16" in litellm and not set(aliases) & set(names),
              f"{tier}: retired role names are deprecated aliases of primary", repr(aliases))
        haiku = "ailocal-" + P.resolve_capability("fast", enabled)
        zsh = (b / "clients/configure.zsh").read_text()
        check(f'ANTHROPIC_DEFAULT_HAIKU_MODEL="{haiku}"' in zsh and zsh.count('="ailocal-primary"') == (3 if "fast" in enabled else 4)
              and settings["model"] == "ailocal-primary" and codex["model"] == "ailocal-primary"
              and re.search(r"claude-haiku-4-5: " + haiku, litellm) and not P.slot_problems(tier, RESOURCES),
              f"{tier}: haiku resolves to {haiku}, every other slot and both defaults to primary, no slot warning")
        check(P.resolve_role(tier, "primary", RESOURCES)["model"]
              in (b / "state/litellm/capabilities.json").read_text(),
              f"{tier}: capabilities.json reflects the resolved model")
        budgets = P.instruction_budgets(b / "cfg")
        check(contract.get("schema_version") == 3
              and contract.get("audit", {}).get("record", "").endswith("audit/local.json")
              and contract.get("audit", {}).get("command") == "ailocal audit"
              and contract.get("instructions") == {
                  "claude": {"max_bytes": budgets["claude"], "file": "CLAUDE.md"},
                  "codex": {"max_bytes": budgets["codex"], "file": "AGENTS.md"}},
              f"{tier}: contract is schema 3 with the audit record and instruction budgets")
    check("compaction" not in P.effective_summary(), "the profile summary publishes no compaction trigger")
    check("cyber" not in P.load_client_policy(RESOURCES).get("compat", {}), "cyber has no bare compat alias")


# Tier design (#89): one primary chat role per tier, `fast` only where the primary thinks
# (the same weights with thinking off), cyber on a separate model at the primary's total
# window, and FIM completion. 16/32 (#93): Qwen3.5 4B / 9B MTP, hash-pinned native imports
# under the structured names, thinking off and greedy (measured 2026-11-05), so no [fast];
# windows are memory bounds (96K / 160K), with the embedded MTP draft depth configured.
DESIGN = {   # primary model, primary thinks, cyber model, completion model, total window
    "16gb": ("qwen3.5:4b-mtp", False, "qwen3.5:4b-unrestricted-mtp", "qwen2.5-coder:1.5b-base", 98304),
    "32gb": ("qwen3.5:9b-calibrated-mtp", False, "qwen3.5:9b-unrestricted-mtp", "qwen2.5-coder:1.5b-base", 163840),
    "64gb": ("gemma4:26b-qat-mtp", True, "gemma4:26b-unrestricted-qat-mtp", "qwen2.5-coder:3b-base", 196608),
    "128gb": ("gemma4:26b-qat-mtp", True, "gemma4:26b-unrestricted-qat-mtp", "qwen2.5-coder:3b-base", 262144),
}
# Output ceilings per role; the key set is the tier's role set. Primary keeps the window the
# former architecture role carried; 16/32 pay KV for the ceiling up front, 64/128 take the
# largest step under the 262144 native window.
OUTPUT = {"16gb": {"primary": 16384, "cyber": 8192, "completion": 512},
          "32gb": {"primary": 16384, "cyber": 8192, "completion": 512},
          "64gb": {"primary": 32768, "fast": 8192, "cyber": 32768, "completion": 512},
          "128gb": {"primary": 32768, "fast": 8192, "cyber": 32768, "completion": 512}}
# Cyber's (context_input, max_output, reasoning): the primary's total with its own reserve.
CYBER = {"16gb": (90112, 8192, False), "32gb": (155648, 8192, False),
         "64gb": (163840, 32768, True), "128gb": (229376, 32768, True)}


def hardware() -> None:
    S.section("TIER DESIGN")
    parsed = {t: P.load_profile_file(RESOURCES / "profiles" / f"{t}.toml") for t in P.TIERS}
    for tier, (primary, thinks, cyber_model, completion, total) in DESIGN.items():
        caps = parsed[tier]
        rr = {r: P.resolve_role(tier, r, RESOURCES) for r in P.ROLES if r in caps}
        pr, cy = rr["primary"], rr["cyber"]
        check(pr["model"] == primary and bool(pr["reasoning"]) is thinks and pr["num_ctx"] == total,
              f"{tier}: primary is {primary} at {total} tokens, thinking {'on' if thinks else 'off'}")
        if thinks:
            check(rr["fast"]["model"] == primary and not rr["fast"]["reasoning"] and rr["fast"]["num_ctx"] == total
                  and rr["fast"]["keep_alive"] == pr["keep_alive"],
                  f"{tier}: fast is the primary with thinking off, same window and keep-alive")
        else:
            check("fast" not in caps and P.resolve_capability("fast", tuple(rr)) == "primary",
                  f"{tier}: a non-thinking primary declares no fast; fast resolves to primary")
        check(cy["num_ctx"] == total and cy["model"] == cyber_model
              and (cy["context_input"], cy["max_output"], bool(cy["reasoning"])) == CYBER[tier],
              f"{tier}: cyber is {cyber_model} at the primary's {total}-token total with its own reserve")
        # Every shipped chat backend runs on stock Ollama 0.40.14 (#80, #87, #93): no runtime
        # patch anywhere. Gemma-family cyber ends turns on its EOS with vendor top_k 64; Qwen
        # cyber carries the Qwen stop list and greedy sampling (temperature 0).
        qwen = cyber_model.startswith("qwen3.5:")
        check(not cy.get("runtime_patch")
              and (bool(cy.get("stop")) and cy["temperature"] == 0.0 if qwen else not cy.get("stop") and cy["top_k"] == 64),
              f"{tier}: cyber ends turns on {'the Qwen stop list, greedy' if qwen else 'its EOS'} and needs no runtime patch")
        check(rr["completion"]["enabled"] and rr["completion"]["model"] == completion
              and {completion, cyber_model} <= {c["active"] for c in rr.values() if c.get("enabled", True)},
              f"{tier}: installs {completion} and cyber")
        check(not P.required_runtime_patch(tier, RESOURCES)
              and not any(c.get("runtime_patch") for c in rr.values()),
              f"{tier}: stock Ollama installs it")
        check(all(re.fullmatch(r"[0-9a-f]{64}", c["source"]["sha256"]) for c in rr.values() if c.get("source")),
              f"{tier}: every native source is hash-pinned")
        ceilings = {r: c["max_output"] for r, c in rr.items()}
        check(ceilings == OUTPUT[tier], f"{tier}: roles and output ceilings are {OUTPUT[tier]}", repr(ceilings))
        over = [r for r, c in rr.items()
                if c["num_ctx"] > (32768 if c["model"].startswith("qwen2.5-coder:") else 262144)]
        check(not over, f"{tier}: every role fits its model's native window", ", ".join(over))
        check("compaction" not in caps, f"{tier} ships no [compaction] block")
        if thinks:   # disabling cyber must not change the runtime the other roles need
            text = (RESOURCES / "profiles" / f"{tier}.toml").read_text()
            root = box(text=text.replace("[cyber]\n", "[cyber]\nenabled = false\n", 1), name=f"profiles/{tier}.toml")
            check(not P.resolve_role(tier, "cyber", root)["enabled"]
                  and P.required_runtime_patch(tier, root) == P.required_runtime_patch(tier, RESOURCES),
                  f"{tier}: disabling cyber leaves the runtime requirement unchanged")
            for r in (r for r, c in rr.items() if c.get("runtime_patch")):
                text = text.replace(f"[{r}]\n", f"[{r}]\nenabled = false\n", 1)
            root = box(text=text, name=f"profiles/{tier}.toml")
            check(P.required_runtime_patch(tier, root) is None,
                  f"{tier}: disabled patch consumers do not require the runtime")
    c64, c128 = parsed["64gb"], parsed["128gb"]
    check(all(c64[c].get(f) == c128[c].get(f) for c in P.ROLES for f in ("active", "keep_alive", "temperature")),
          "128gb runs 64gb's models and sampling")
    check(all(P.resolve_role("128gb", c, RESOURCES)["num_ctx"] >= P.resolve_role("64gb", c, RESOURCES)["num_ctx"]
              for c in P.ROLES), "no 128gb context is below 64gb")

    S.section("CAPABILITY RESOLUTION (#89)")
    check(P.ROLES == ("primary", "fast", "cyber", "completion") and P.OPTIONAL_ROLES == {"fast": "primary"}
          and set(P.RETIRED_ROLES) == {"architecture", "implementation", "review"} == set(P.RETIRED_ROLE_SECTIONS),
          "the roles are primary, fast (optional), cyber and completion; the 0.15 names are retired")
    check(P.resolve_capability("fast", ("primary", "cyber")) == "primary"
          and P.resolve_capability("fast", ("primary", "fast")) == "fast"
          and P.resolve_capability("primary", ("primary",)) == "primary"
          and P.resolve_capability("nosuch", ("primary", "fast")) == "nosuch"
          and P.resolve_capability("fast", ("cyber",)) == "fast",
          "an absent optional role resolves to its stand-in; anything else is returned for the consumer to reject")
    check(P.deprecated_aliases(("primary", "cyber", "completion"))
          == {"architecture": "primary", "implementation": "primary", "review": "primary", "fast": "primary"}
          and P.deprecated_aliases(("primary", "fast", "cyber", "completion"))
          == {"architecture": "primary", "implementation": "primary", "review": "primary"}
          and P.deprecated_aliases(("cyber",)) == {},
          "deprecated aliases are the retired names plus an omitted optional role, each routed to a served role")
    check(P.enabled_roles("16gb", RESOURCES) == ("primary", "cyber", "completion")
          and P.enabled_roles("64gb", RESOURCES) == ("primary", "fast", "cyber", "completion"),
          "enabled_roles lists a tier's served roles in ROLES order")

    S.section("MODEL IDENTITY")
    accepted = ["qwen3.5:4b-mtp", "qwen3.5:9b-calibrated-mtp", "qwen3.5:4b-unrestricted-mtp", "gemma4:26b-qat-mtp",
                "gemma4:26b-unrestricted-qat-mtp", "qwen2.5-coder:1.5b-base", "nemotron-3.5-bolt:e4b-unrestricted",
                "gemma4:26b-it-qat", "nomic-embed-text:270m", "gemma4:26b"]
    rejected = ["qwen3.5:4b-mtp-q4_K_M", "gemma4:26b-unrestricted-qat-mtp-q4_K_M", "qwen3.5:4b-mtp-q8_0",
                "gemma4:26b-mtp-qat", "gemma4:26b-qat-qat", "gemma4:26", "x:latest", "hf.co/a/b:c",
                "Gemma4:26b-qat", "qwen3.5:4b-fp16", "m:1", "o", ""]
    check([t for t in accepted if P.selection_name_error(t)] == [] and all(P.selection_name_error(t) for t in rejected),
          "the naming structure accepts family:size with ordered variant tokens and rejects a quantization token, "
          "a repeated or misordered token, a size without its unit, :latest, a repository URL and uppercase (#93)",
          [t for t in accepted if P.selection_name_error(t)] + [t for t in rejected if not P.selection_name_error(t)])
    check(code_of(P.load_profile, "64gb", box("64gb", GOOD.replace("m:1b", "qwen3.5:4b-mtp-q4_K_M"))) == P.PROFILE_SCHEMA_INVALID
          and code_of(P.load_profile, "64gb", box("64gb", GOOD + 'preferred = ["m:1b", "m:1b-q4_K_M"]\n')) == P.PROFILE_SCHEMA_INVALID,
          "a profile with a quantization token in active or preferred fails closed (#93)")
    for tier, caps in parsed.items():
        tags = {r: [caps[r]["active"], *caps[r].get("preferred", [])] for r in P.ROLES if r in caps}
        flat = [t for ts in tags.values() for t in ts]
        # `ollama create` without a tag publishes `name:latest`, which names nothing.
        check(all(":" in t and not t.endswith(":latest") for t in flat),
              f"{tier}: every selection is a name:tag identity, never :latest", repr(flat))
        check(all(P.selection_name_error(t) is None for t in flat)
              and all(P.selection_name_error(k) is None for k in caps.get("model_guidance", {})),
              f"{tier}: every selection and guide key follows the one naming structure (#93)", repr(flat))
        check(all(t.startswith("qwen2.5-coder:") and t.endswith("-base") for t in tags["completion"]),
              f"{tier}: completion selects only official base tags")
        mlx = {t for r, ts in tags.items() if r != "completion" for t in ts
               if any(k in t for k in ("mlx", "nvfp4", "mxfp"))}
        unproven = [t for t in mlx if not ("[REAL]" in (ev := caps.get("model_guidance", {}).get(t, {})
                                                      .get("evidence", "")) and MLX_EVIDENCE in ev)]
        check(not unproven, f"{tier}: every MLX selection records [REAL] same-model GGUF evidence",
              ", ".join(unproven))
    unique = {t: {P.resolve_role(t, r, RESOURCES)["model"] for r in P.enabled_roles(t, RESOURCES)} for t in P.TIERS}
    check(unique["64gb"] == unique["128gb"], "64gb and 128gb pull the same model set")

    readme = (REPO / "README.md").read_text()
    ctx_k = round(P.resolve_role("64gb", "primary", RESOURCES)["num_ctx"] / 1024)
    check(f"{ctx_k}K" in readme, f"README quotes the 64gb primary context ({ctx_k}K)")
    for tier, caps in parsed.items():
        check(caps["primary"]["active"] in readme, f"README names {tier}'s primary model")


def client_policy() -> None:
    S.section("CLIENT POLICY FAILS CLOSED")
    cases = {"unknown client": '[nosuchclient]\ndefault = "fast"\n',
             "non-TOML": "bogus:\n  default: fast\n",
             "duplicate section": '[claude]\na = "fast"\n[claude]\nb = "fast"\n',
             "duplicate key": '[claude]\ndefault = "fast"\ndefault = "primary"\n',
             "unclosed list": '[continue]\nchat = ["a", "b"\n',
             "unclosed mapping": '[claude]\nslots = {opus = "fast"\n',
             "bare dotted key": '[compat]\ngpt-5.5 = "primary"\n'}   # nests instead of naming
    for name, text in cases.items():
        got = code_of(P.load_client_policy, box(text=text, name="profiles/clients.toml"))
        check(got == P.CLIENT_POLICY_INVALID, f"{name} is rejected", got)
    check(code_of(P.load_client_policy, box()) == P.CLIENT_POLICY_MISSING, "a missing clients.toml is rejected")
    build = re.compile(r'(=\s*["\']config/(active-profile|clients\.toml)["\']'
                       r'|/\s*["\']config["\']\s*/\s*["\'](active-profile|clients\.toml))')
    prod = [q for q in (REPO / "src").rglob("*.py") if "/resources/" not in str(q) and q.name != "policy.py"]
    offenders = sorted({q.name for q in prod for line in q.read_text().splitlines()
                        if not line.strip().startswith(("#", '"""')) and build.search(line)})
    check(prod and not offenders, "only policy.py constructs a policy path", ", ".join(offenders))

    S.section("CLI DISPATCH")
    from ailocal import cli
    listed = [n for _, names in cli.GROUPS for n in names]
    check(len(listed) == len(set(listed)) and set(listed) == set(cli.COMMANDS) | {"profile"},
          "help lists every dispatchable command exactly once")
    missing = [n for n, (mod, _) in cli.COMMANDS.items()
               if not callable(getattr(importlib.import_module(mod), "main", None))]
    check(not missing, "every command resolves to a main(argv)", "; ".join(missing))
    # `clients --help` once configured every client.
    check(set(cli.USAGE) == set(cli.COMMANDS) - {"install", "check"}, "every other command has a --help usage")
    dispatched, real = [], importlib.import_module
    importlib.import_module = lambda _n: type("M", (), {"main": staticmethod(lambda a: dispatched.append(a) or 0)})
    try:
        bad = []
        for name in cli.USAGE:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                if cli.main([name, "--help"]) != 0 or not out.getvalue().startswith(f"usage: ailocal {name}"):
                    bad.append(name)
    finally:
        importlib.import_module = real
    check(not bad and not dispatched, "`--help` prints usage and dispatches nothing", repr(bad or dispatched))
    from ailocal.checks import run as check_run
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = check_run.main(["check", "--help"])
    check(code == 0 and out.getvalue().startswith("usage: ailocal check"), "`check --help` runs no check")


OLD_KEY, OLD_SEARX = "sk-" + "a1" * 24, "b2" * 32   # shaped like real values, never live ones
LEGACY = (f"AILOCAL_ENV=local\nOLLAMA_URL=http://host.docker.internal:11434\nLITELLM_MASTER_KEY={OLD_KEY}\n"
          "POSTGRES_PASSWORD=obsolete-postgres-password\nJWT_SECRET=obsolete-jwt-secret\nANTHROPIC_API_KEY=\n"
          f"OPENAI_API_KEY=sk-user-openai-key\nSEARXNG_SECRET={OLD_SEARX}\nAILOCAL_TOOL_GATEWAY=filter\n"
          "AILOCAL_TOOL_GATEWAY_CAPTURE=\nBRAVE_API=user-brave-key\n")


@contextlib.contextmanager
def machine(legacy=LEGACY):
    base = Path(tempfile.mkdtemp(dir=TMP))
    cfg, state = base / "config", base / "state"
    cfg.mkdir(), state.mkdir()
    if legacy is not None:
        (cfg / ".env").write_text(legacy)
        (cfg / ".env").chmod(0o600)
    with env_set(AILOCAL_CONFIG=cfg, AILOCAL_STATE=state):
        yield cfg, state


def env_ownership() -> None:
    from ailocal import environment as e
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    S.section("ENVIRONMENT OWNERSHIP")
    with machine(None) as (cfg, state):
        e.ensure()
        gen, user = e.parse(state / "env"), e.parse(cfg / ".env.local")
        check(sorted(gen) == sorted(e.GENERATED) and all(gen[k] for k in e.GENERATED)
              and all(user.get(k) == ("false" if k == "ENABLE_CLOUD" else "") for k in e.USER_ALWAYS),
              "ensure() writes generated secrets under state and empty provider keys under config")
        check(mode(state / "env") == mode(cfg / ".env.local") == 0o600, "both owners are 0600")
        before = ((state / "env").read_text(), (cfg / ".env.local").read_text())
        e.ensure(), e.ensure()
        check(before == ((state / "env").read_text(), (cfg / ".env.local").read_text())
              and e.value("LITELLM_MASTER_KEY") == gen["LITELLM_MASTER_KEY"],
              "ensure() is idempotent and never rotates the key")
        check(state in e.generated_file().parents and cfg in e.user_file().parents,
              "generated secrets live under state, user secrets under config")
        check(not hasattr(e, "migrate") and not hasattr(e, "legacy_file"),
              "the v0.11 .env migration is gone (#96): there is nothing left to migrate from")
        check(e.bind() == e.BIND_LOCAL, "the proxy binds loopback unless told otherwise (#103)")
        e.set_bind(e.BIND_LAN)
        e.set_ollama_patch("")
        e.set_bind("10.0.0.5")
        user = e.parse(cfg / ".env.local")
        check(e.bind() == "10.0.0.5" and user["AILOCAL_BIND"] == "10.0.0.5" and user.get("AILOCAL_OLLAMA_PATCH") == ""
              and all(k in user for k in e.USER_ALWAYS) and mode(cfg / ".env.local") == 0o600,
              "set_bind replaces its one line and keeps every other override (#103)")

def timeouts() -> None:
    S.section("TIMEOUTS")
    # The client must outlast the proxy, or it abandons requests the backend is still serving.
    proxy = re.search(r"^ *timeout: *(\d+)", (RESOURCES / "deploy/litellm/config.template.yaml").read_text(), re.M)
    client = re.search(r"AILOCAL_API_TIMEOUT_MS:-(\d+)}", (RESOURCES / "clients/configure.template.zsh").read_text())
    check(proxy and client and int(client.group(1)) >= int(proxy.group(1)) * 1000,
          "client API_TIMEOUT_MS is not below the LiteLLM timeout",
          f"{client and client.group(1)} ms vs {proxy and proxy.group(1)} s")


def main() -> int:
    try:
        for section in (resolver, geometry, generated, hardware, client_policy, env_ownership, timeouts):
            try:
                section()
            except Exception as exc:  # noqa: BLE001
                S.error(section.__name__, exc)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
