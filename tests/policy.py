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
GOOD = '[architecture]\nrole = "Lead"\nactive = "m:1"\ncontext_input = 100\nmax_output = 20\n'
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
              and sum(r in P.load_profile(t) for r in P.ROLES) >= 4, f"{t} loads")
    tier_cases = [("missing marker", None, P.ACTIVE_PROFILE_MISSING),
                  ("empty marker", "   \n", P.ACTIVE_PROFILE_EMPTY),
                  ("unknown tier", "999gb", P.ACTIVE_PROFILE_INVALID)]
    for label, marker, want in tier_cases:
        root = box(marker)
        got = code_of(P.resolve_active_tier, root, root / "state")
        check(got == want, f"{label} => {want}", got)
    head = '[architecture]\nrole = "L"\nactive = "m"\n'
    load_cases = [
        ("missing profile file", None, P.PROFILE_FILE_MISSING),
        ("malformed policy", "[architecture\n", P.PROFILE_INVALID),
        ("non-integer context_input", head + 'context_input = "x"\n', P.ROLE_CONFIG_INVALID),
        ("missing required field", '[architecture]\nrole = "L"\ncontext_input = 10\n',
         P.PROFILE_SCHEMA_INVALID),
        ("legacy `context`", head + "context = 4096\n", P.PROFILE_SCHEMA_INVALID),
        ("max_output -1", head + "context_input = 100\nmax_output = -1\n", P.ROLE_CONFIG_INVALID),
        ("duplicate key", GOOD + "context_input = 999\n", P.PROFILE_INVALID),
        ("duplicate section", GOOD + '[architecture]\nrole = "X"\nactive = "o"\n', P.PROFILE_INVALID),
    ]
    # Unknown keys were silently dropped before 2026-08-04, so a typo read as set and was not.
    src = 'source = { model="hf.co/a/b:t.gguf", sha256="' + "a" * 64 + '", renderer="r", parser="p", '
    for frag in ("temprature = 0.1", "topk = 20", '"repeat-penalty" = 1.0', "num_predict = 512",
                 "draft_num_predict = -1", "draft_num_predict = true", "draft_num_predict = 1.5",
                 src + 'draft_model="hf.co/a/b:h.gguf" }', src + 'draft_sha256="' + "b" * 64 + '" }'):
        load_cases.append((f"`{frag[:40]}`", GOOD + frag + "\n", P.PROFILE_SCHEMA_INVALID))
    load_cases.append(("unclosed list", GOOD + "preferred = [a, b\n", P.PROFILE_INVALID))
    for label, text, want in load_cases:
        got = code_of(P.load_profile, "64gb", box("64gb", text))
        check(got == want, f"{label} => {want}", got)
    root = box("64gb", GOOD)
    check(code_of(P.resolve_role, "64gb", "review", root) == P.ROLE_MISSING, "absent role => ROLE_MISSING")
    check(P.resolve_role("64gb", "architecture", root)["model"] == "m:1", "a colon in a value survives")
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
    r, f = run("active-tier"), run("role", "architecture", "--field", "model")
    check(r.returncode == f.returncode == 0 and r.stdout.strip() in P.TIERS and ":" in f.stdout,
          "active-tier and role --field print bare scalars", r.stdout + f.stdout)
    root = box("64gb", GOOD)
    shutil.copy(RESOURCES / "profiles/clients.toml", root / "profiles")
    r = run("role", "architecture", "--field", "reasoning",
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
        blk = G.gen_role_block("architecture", arch["architecture"])
        check("      min_p: 0.0" in blk and f"      presence_penalty: {penalty}" in blk
              and 'allowed_openai_params: ["presence_penalty"]' in blk,
              f"native sampling survives parsing and generation ({penalty})")
    # Omitting it would fall back to Claude Code's 32000, the number the backend does not serve.
    models, clients = G.load_models_yaml(RESOURCES / "profiles/64gb.toml"), G.load_clients_yaml()
    launch = (clients.get("claude") or {}).get("launch_default", "architecture")
    models[launch] = {k: v for k, v in models[launch].items() if k != "max_output"}
    try:
        G.regen_claude_settings(models, clients)
        check(False, "a launch_default without max_output is rejected", "returned")
    except SystemExit as exc:
        check("max_output" in str(exc), "a launch_default without max_output is rejected", str(exc))


def generated() -> None:
    S.section("GENERATION PER TIER (claude-local only, never hosted roots)")
    launch = P.load_client_policy(RESOURCES).get("claude", {}).get("launch_default", "architecture")
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
        enabled = [c for c in P.ROLES if P.resolve_role(tier, c, RESOURCES)["enabled"]]
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
        check(P.resolve_role(tier, "architecture", RESOURCES)["model"]
              in (b / "state/litellm/capabilities.json").read_text(),
              f"{tier}: capabilities.json reflects the resolved model")
        budgets = P.instruction_budgets(b / "cfg")
        # #50: Claude's published budget leaves room for the guide hook it adds to the same context.
        hook = int(subprocess.run([sys.executable, "-c", "from ailocal.guidance import launch_default_hook_bytes as h; "
                                   f"print(h({tier!r})[0])"], env=env, capture_output=True, text=True).stdout)
        check(contract.get("schema_version") == 3
              and contract.get("audit", {}).get("record", "").endswith("audit/local.json")
              and contract.get("audit", {}).get("command") == "ailocal audit"
              and 0 < hook < budgets["claude"]
              and contract.get("instructions") == {
                  "claude": {"max_bytes": budgets["claude"] - hook, "file": "CLAUDE.md"},
                  "codex": {"max_bytes": budgets["codex"], "file": "AGENTS.md"}},
              f"{tier}: contract is schema 3 with the audit record and instruction budgets")
    check("compaction" not in P.effective_summary(), "the profile summary publishes no compaction trigger")
    check("cyber" not in P.load_client_policy(RESOURCES).get("compat", {}), "cyber has no bare compat alias")


# Tier design: primary (shared by the four chat roles), cyber, completion, total window, draft depth.
DESIGN = {
    "16gb": ("qwen3.5:4b-mtp-q4_K_M", "qwen3.5:4b-unrestricted-mtp-q4_K_M", "qwen2.5-coder:1.5b-base", 106496, 1),
    "32gb": ("qwen3.5:9b-calibrated-mtp-q4_K_M", "qwen3.5:9b-unrestricted-mtp-q4_K_M",
             "qwen2.5-coder:1.5b-base", 139264, 3),
    "64gb": (None, "gemma4:26b-unrestricted-qat-mtp-q4_K_M", "qwen2.5-coder:3b-base", 196608, None),
    "128gb": (None, "gemma4:26b-unrestricted-qat-mtp-q4_K_M", "qwen2.5-coder:3b-base", 262144, None),
}
# 16/32 pay KV for the ceiling up front; 64/128 take the largest step under the 262144 native window.
OUTPUT = {"16gb": (16384, 8192, 16384), "32gb": (16384, 8192, 16384),
          "64gb": (32768, 32768, 32768), "128gb": (32768, 32768, 32768)}
SHARED = ("architecture", "implementation", "review", "fast")


def hardware() -> None:
    S.section("TIER DESIGN")
    parsed = {t: P.load_profile_file(RESOURCES / "profiles" / f"{t}.toml") for t in P.TIERS}
    patch_id = P.ollama_patch_spec()["id"]
    for tier, (primary, cyber_model, completion, total, draft) in DESIGN.items():
        caps = parsed[tier]
        rr = {r: P.resolve_role(tier, r, RESOURCES) for r in P.ROLES if r in caps}
        if primary:
            check(all(caps[c]["active"] == primary for c in SHARED)
                  and {rr[c]["num_ctx"] for c in SHARED} == {total},
                  f"{tier}: {primary} serves {', '.join(SHARED)} at {total} tokens")
        cy, arch, impl = rr["cyber"], rr["architecture"], rr["implementation"]
        check(arch["num_ctx"] == cy["num_ctx"] == total and cy["model"] == cyber_model,
              f"{tier}: cyber is {cyber_model} at architecture's {total}-token total")
        mirror = (impl["context_input"], impl["max_output"], False) if primary else \
                 (arch["context_input"], arch["max_output"], arch["reasoning"])
        check((cy["context_input"], cy["max_output"], cy["reasoning"]) == mirror,
              f"{tier}: cyber mirrors {'implementation' if primary else 'architecture'}'s geometry")
        if primary:
            check('stop: ["<|im_end|>", "<|im_start|>"]' in G.gen_role_block("cyber", cy),
                  f"{tier}: cyber's chat boundaries reach the provider")
        else:
            check(not cy.get("runtime_patch") and cy["top_k"] == 64,
                  f"{tier}: QAT/MTP cyber needs no runtime patch")
        check(rr["completion"]["enabled"] and rr["completion"]["model"] == completion
              and {completion, cyber_model} <= set(P.required_models(tier, RESOURCES)), f"{tier}: installs {completion} and cyber")
        qwen = [c for r, c in rr.items() if str(caps[r]["active"]).startswith("qwen3.5:")]
        check(qwen and all(c.get("runtime_patch") == patch_id for c in qwen)
              and P.required_runtime_patch(tier, RESOURCES) == patch_id,
              f"{tier}: every Qwen3.5 route requires the thinking-channel runtime")
        if draft:
            check(all(rr[r].get("draft_num_predict") == draft
                      and f"draft_num_predict: {draft}" in G.gen_role_block(r, rr[r])
                      for r in (*SHARED, "cyber")), f"{tier}: draft depth {draft} reaches the provider")
        check(all(re.fullmatch(r"[0-9a-f]{64}", c["source"]["sha256"]) for c in rr.values() if c.get("source")),
              f"{tier}: every native source is hash-pinned")
        ceilings = tuple(rr[c]["max_output"] for c in ("architecture", "implementation", "review", "fast", "completion"))
        check(ceilings == (*OUTPUT[tier], 8192, 512), f"{tier}: output ceilings are {(*OUTPUT[tier], 8192, 512)}",
              repr(ceilings))
        over = [r for r, c in rr.items()
                if c["num_ctx"] > (32768 if c["model"].startswith("qwen2.5-coder:") else 262144)]
        check(not over, f"{tier}: every role fits its model's native window", ", ".join(over))
        check("compaction" not in caps, f"{tier} ships no [compaction] block")
        if not primary:   # disabling cyber must not change the runtime the other roles need
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

    S.section("MODEL IDENTITY")
    for tier, caps in parsed.items():
        tags = {r: [caps[r]["active"], *caps[r].get("preferred", [])] for r in P.ROLES}
        flat = [t for ts in tags.values() for t in ts]
        # `ollama create` without a tag publishes `name:latest`, which names nothing.
        check(all(":" in t and not t.endswith(":latest") for t in flat),
              f"{tier}: every selection is a name:tag identity, never :latest", repr(flat))
        check(all(t.startswith("qwen2.5-coder:") and t.endswith("-base") for t in tags["completion"]),
              f"{tier}: completion selects only official base tags")
        mlx = {t for r, ts in tags.items() if r != "completion" for t in ts
               if any(k in t for k in ("mlx", "nvfp4", "mxfp"))}
        unproven = [t for t in mlx if not ("[REAL]" in (ev := caps.get("model_guidance", {}).get(t, {})
                                                      .get("evidence", "")) and MLX_EVIDENCE in ev)]
        check(not unproven, f"{tier}: every MLX selection records [REAL] same-model GGUF evidence",
              ", ".join(unproven))
    unique = {t: {P.resolve_role(t, r, RESOURCES)["model"] for r in P.ROLES
                  if P.resolve_role(t, r, RESOURCES)["enabled"]} for t in P.TIERS}
    check(unique["64gb"] == unique["128gb"], "64gb and 128gb pull the same model set")

    readme = (REPO / "README.md").read_text()
    ctx_k = round(P.resolve_role("64gb", "architecture", RESOURCES)["num_ctx"] / 1024)
    check(f"{ctx_k}K" in readme, f"README quotes the 64gb architecture context ({ctx_k}K)")
    for tier, caps in parsed.items():
        check(caps["architecture"]["active"] in readme, f"README names {tier}'s primary model")


def client_policy() -> None:
    S.section("CLIENT POLICY FAILS CLOSED")
    cases = {"unknown client": '[nosuchclient]\ndefault = "fast"\n',
             "non-TOML": "bogus:\n  default: fast\n",
             "duplicate section": '[claude]\na = "fast"\n[claude]\nb = "fast"\n',
             "duplicate key": '[claude]\ndefault = "fast"\ndefault = "review"\n',
             "unclosed list": '[continue]\nchat = ["a", "b"\n',
             "unclosed mapping": '[claude]\nslots = {opus = "fast"\n',
             "bare dotted key": '[compat]\ngpt-5.5 = "implementation"\n'}   # nests instead of naming
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
    with machine() as (cfg, state):
        (cfg / "env").write_text(f"AILOCAL_API_KEY={OLD_KEY}\n")   # a retired second copy of the key
        e.migrate()
        gen, user = e.parse(state / "env"), e.parse(cfg / ".env.local")
        check(gen.get("LITELLM_MASTER_KEY") == OLD_KEY and gen.get("SEARXNG_SECRET") == OLD_SEARX
              and sorted(gen) == sorted(e.GENERATED), "migration keeps generated secrets byte-for-byte, alone")
        check(user.get("OPENAI_API_KEY") == "sk-user-openai-key" and user.get("ANTHROPIC_API_KEY") == ""
              and user.get("AILOCAL_TOOL_GATEWAY") == "filter"
              and not {"AILOCAL_TOOL_GATEWAY_CAPTURE", "OLLAMA_URL"} & set(user),
              "user keys and non-default flags move to .env.local; defaults are not persisted")
        live = (state / "env").read_text() + (cfg / ".env.local").read_text()
        check(not (set(gen) | set(user)) & {*e.DEAD, "POSTGRES_PASSWORD", "JWT_SECRET"}
              and "obsolete-postgres-password" not in live, "dead variables and obsolete credentials are dropped")
        backups = list((state / "backups").glob("env-migration-*/.env"))
        check(len(backups) == 1 and "obsolete-postgres-password" in backups[0].read_text()
              and mode(backups[0]) == 0o600, "obsolete credentials survive only in one 0600 backup")
        check(not (cfg / ".env").exists() and not (cfg / "env").exists() and mode(state / "env") == mode(cfg / ".env.local") == 0o600,
              "the legacy file and key projection are retired; both owners are 0600")
        before = ((state / "env").read_text(), (cfg / ".env.local").read_text())
        e.migrate(), e.ensure(), e.ensure()
        check(not e.needs_migration() and before == ((state / "env").read_text(), (cfg / ".env.local").read_text())
              and e.value("LITELLM_MASTER_KEY") == OLD_KEY, "migration is idempotent and never rotates the key")
        check(state in e.generated_file().parents and cfg in e.user_file().parents,
              "generated secrets live under state, user secrets under config")
    with machine(f"LITELLM_MASTER_KEY={OLD_KEY}\nSEARXNG_SECRET={OLD_SEARX}\n") as (cfg, _):
        e.migrate()
        user = e.parse(cfg / ".env.local")
        check(all(user.get(k) == "" for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API"))
              and all(k in user for k in e.USER_ALWAYS), "absent provider keys become present and empty")
    with machine() as (cfg, state):
        real = e._write
        e._write = lambda p, h, v: real(p, h, {k: x for k, x in v.items()
                                               if not (p == e.user_file() and k == "OPENAI_API_KEY")})
        try:
            e.migrate()
            check(False, "a lossy migration fails closed", "reported success")
        except SystemExit as exc:
            check("OPENAI_API_KEY" in str(exc), "a lossy migration fails closed and names the loss")
        finally:
            e._write = real
        check(e.parse(cfg / ".env").get("OPENAI_API_KEY") == "sk-user-openai-key"
              and list((state / "backups").glob("env-migration-*/.env")),
              "a failed migration leaves its source intact and its backup taken")
    with machine(None) as (cfg, state):
        e.ensure()
        key = e.value("LITELLM_MASTER_KEY")
        e.ensure()
        check(key.startswith("sk-") and len(key) > 20 and e.value("SEARXNG_SECRET")
              and e.value("LITELLM_MASTER_KEY") == key, "a fresh install generates secrets once")
        (state / "env").write_text(f"LITELLM_MASTER_KEY={OLD_KEY}\nSEARXNG_SECRET={OLD_SEARX}\nOLLAMA_URL=gen\n")
        (cfg / ".env.local").write_text((cfg / ".env.local").read_text() + "OLLAMA_URL=http://o:11434\n")
        check(e.value("OLLAMA_URL") == "http://o:11434" and [p.name for p in e.env_files()] == ["env", ".env.local"],
              "the user file wins over generated state, in compose order too")
        original = (cfg / ".env.local").read_text() + "# operator comment\nCUSTOM_SETTING=keep-me\n"
        e.user_file().write_text(original)
        secrets = e.generated_file().read_bytes()
        e.set_ollama_patch("gemma4-switch-glu"), e.set_ollama_patch("gemma4-switch-glu")
        text = e.user_file().read_text()
        check(text.startswith(original) and text.count("AILOCAL_OLLAMA_PATCH=") == 1
              and e.value("AILOCAL_OLLAMA_PATCH") == "gemma4-switch-glu" and mode(e.user_file()) == 0o600,
              "runtime selection is idempotent and preserves authored settings")
        e.set_ollama_patch("")
        check(e.value("AILOCAL_OLLAMA_PATCH") == "" and e.generated_file().read_bytes() == secrets,
              "returning to the stock runtime preserves generated secrets")


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
