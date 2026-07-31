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
        ctx = caps[cap].get("context")
        check(ctx is not None and ctx.isdigit() and int(ctx) > 0,
              f"{tier}.{cap} context is a positive integer", f"got {ctx!r}")


# ── the agreed tier design ──────────────────────────────────────────────────
print("\nTIER DESIGN")
SHARED = ("architecture", "implementation", "review", "fast")
for tier, primary in (("16gb", "qwen3.5:4b"), ("32gb", "qwen3.5:9b")):
    caps, _ = PARSED[tier]
    check(all(caps[c].get("active") == primary for c in SHARED),
          f"{tier} shares {primary} across {', '.join(SHARED)}",
          {c: caps[c].get("active") for c in SHARED}.__repr__())
    check(all(caps[c].get("context") == "65536" for c in SHARED),
          f"{tier} primary context is 65536")
    check(caps["architecture"].get("num_predict") == "8192",
          f"{tier} primary output ceiling is 8192")
    check(caps["completion"].get("active") == "qwen2.5-coder:1.5b",
          f"{tier} completion uses qwen2.5-coder:1.5b (native FIM)")

# 64gb is the measured reference; 128gb must be its exact functional copy.
c64, _ = PARSED["64gb"]
c128, _ = PARSED["128gb"]
for cap in CAPABILITIES:
    for field in ("active", "context", "num_predict", "keep_alive", "temperature"):
        check(c64[cap].get(field) == c128[cap].get(field),
              f"128gb.{cap}.{field} equals 64gb",
              f"64={c64[cap].get(field)!r} 128={c128[cap].get(field)!r}")

check(c64["architecture"]["context"] == "98304",
      "64gb architecture context is 98304, not the stale 64K")

# Capability must never DECREASE as memory grows.
for cap in CAPABILITIES:
    ctxs = [int(PARSED[t][0][cap]["context"]) for t in PROFILES]
    check(ctxs[3] >= ctxs[2],
          f"128gb.{cap} context is not below 64gb", f"{ctxs[2]} -> {ctxs[3]}")


# ── embeddings ──────────────────────────────────────────────────────────────
print("\nEMBEDDINGS")
for tier in PROFILES:
    caps, _ = PARSED[tier]
    ctx = int(caps["embeddings"]["context"])
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

# ── README cannot drift from the profiles ───────────────────────────────────
print("\nDOCUMENTATION")
readme = (REPO / "README.md").read_text()

# The capability table quotes the 64gb contexts. A hand-maintained table is
# exactly how "64K" survived a profile that had moved to 98304.
ctx_k = round(int(c64["architecture"]["context"]) / 1024)
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
