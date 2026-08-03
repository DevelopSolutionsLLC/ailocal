#!/usr/bin/env python3
"""Hardware-profile invariants — schema, tier selection, and disk accounting.

These are the failures this suite exists for, all of them real:

  * 16gb and 32gb declared qwen3-coder:30b — an 18 GB model — for `architecture`.
    A 16 GB Mac cannot hold it. The small tiers were stale copies of an older
    64gb layout, so the profile named a model the hardware could not run.
  * 128gb declared a SMALLER architecture context (65536) than 64gb (98304), so
    the largest tier was the less capable one.
  * `fast` existed only in 64gb, so three profiles were missing a capability the
    router expects every profile to expose.
  * Tier selection rounded UP: 24 GB chose 32gb, 48 GB chose 64gb, 96 GB chose
    128gb. Every one of those gave a machine models sized for memory it lacked.
  * The model list was every `active:` line, so disabled capabilities were pulled
    and a shared backend was pulled once per capability that referenced it.

Nothing here loads a model or touches the network.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PROFILES = ("16gb", "32gb", "64gb", "128gb")
CAPABILITIES = ("architecture", "implementation", "review",
                "fast", "completion", "embeddings")

failures = []


def check(ok, label, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(f"{label}: {detail}")


def parse(tier):
    """Capability -> field map. A deliberately small reader: these files are ours."""
    text = (REPO / "config" / "profiles" / f"{tier}.yaml").read_text()
    out = {}
    for m in re.finditer(r'^([a-z_]+):\n((?:  .*\n)+)', text, re.M):
        cap, body = m.group(1), m.group(2)
        fields = {}
        for fm in re.finditer(r'^  ([a-z_]+): *(.+?)\s*(?:#.*)?$', body, re.M):
            fields[fm.group(1)] = fm.group(2).strip()
        out[cap] = fields
    return out, text


PARSED = {t: parse(t) for t in PROFILES}


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
        ctx = caps[cap].get("context_input")
        check(ctx is not None and ctx.isdigit() and int(ctx) > 0,
              f"{tier}.{cap} context_input is a positive integer", f"got {ctx!r}")


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
    check(caps["architecture"].get("max_output") == "8192",
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
install = (REPO / "scripts" / "install.sh").read_text()
for gb, expected in ((8, None), (16, "16gb"), (18, "16gb"), (24, "16gb"),
                     (32, "32gb"), (36, "32gb"), (48, "32gb"),
                     (64, "64gb"), (96, "64gb"), (128, "128gb"), (192, "128gb")):
    if gb >= 128:   got = "128gb"
    elif gb >= 64:  got = "64gb"
    elif gb >= 32:  got = "32gb"
    elif gb >= 16:  got = "16gb"
    else:           got = None
    check(got == expected, f"{gb} GB selects {expected or 'nothing (unsupported)'}",
          f"got {got}")

check('RAM_GB" -ge 128' in install and 'RAM_GB" -ge 64' in install,
      "install.sh selects tiers at their real thresholds, never rounding up")
check("requires at least 16 GB" in install,
      "install.sh refuses machines below 16 GB")
check("PROFILE_OVERRIDE" in install and "Refusing an unsafe override under --yes" in install,
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

# The generated client config must match the ACTIVE profile, or the client
# compacts on a threshold this repository never chose.
active = (REPO / "config" / "active-profile")
tier = active.read_text().strip() if active.exists() else "64gb"
cc = PARSED[tier][0].get("compaction", {})
settings = REPO / "config/clients/claude/settings.json"
if settings.exists() and cc:
    import json
    env = json.loads(settings.read_text()).get("env", {})
    check(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == cc["window"],
          f"claude settings.json window matches the active profile ({tier})",
          f"{env.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW')!r} != {cc['window']!r}")
    check(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") == cc["pct"],
          f"claude settings.json pct matches the active profile ({tier})",
          f"{env.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE')!r} != {cc['pct']!r}")

# Codex's numbers must describe the model CODEX defaults to, not architecture.
# Deriving them from architecture wrote a compaction limit of 49,152 against a
# default model whose entire context is 24,576 -- unreachable, because the model
# 400s on context length long before compaction could fire.
codex = REPO / "config/clients/codex/config.toml"
clients_yaml = (REPO / "config/clients.yaml").read_text()
m = re.search(r'(?m)^codex:\n(?:.*\n)*?\s*default:\s*(\w+)', clients_yaml)
cx_cap = m.group(1) if m else "implementation"
if codex.exists() and cc and cx_cap in PARSED[tier][0]:
    txt = codex.read_text()
    _cx = PARSED[tier][0][cx_cap]
    cx_ctx = int(_cx["context_input"]) + int(_cx.get("max_output") or 0)
    want = min(int(cc["window"]) * int(cc["pct"]) // 100, cx_ctx * int(cc["pct"]) // 100)
    check(f"model_context_window = {cx_ctx}" in txt,
          f"codex window is its OWN default capability '{cx_cap}' ({cx_ctx}), not architecture")
    check(f"model_auto_compact_token_limit = {want}" in txt,
          f"codex compaction limit is {want}")
    check(want < cx_ctx,
          f"codex compaction limit ({want}) is reachable within its model context ({cx_ctx})")

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

print()
if failures:
    print(f"PROFILES: {len(failures)} FAILED")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PROFILES: all checks passed")
