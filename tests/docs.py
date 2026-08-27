#!/usr/bin/env python3
"""Documentation must describe the system that is actually installed.

Deliberately NOT a prose snapshot: every check here compares documentation
against a source of truth in the repository, so it fails when the two drift
apart and stays quiet when someone rewrites a sentence. The failures it exists
to catch are the ones that already happened once -- a version number left
behind after an upgrade, a variable renamed out from under its own docs, and a
comment block still instructing maintainers to delete a module that is gone.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
# Documentation that SHIPS is documentation: the bundled component's own README
# reaches users inside the wheel, and carried standalone install instructions
# for exactly as long as nothing checked it.
DOCS = [REPO / "README.md", REPO / "AGENTS.md", REPO / "RELEASING.md",
        *sorted((REPO / "docs").rglob("*.md")),
        *sorted((REPO / "src/ailocal/resources/integrations").rglob("*.md"))]

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ── versions ────────────────────────────────────────────────────────────────
compose = _text(REPO / "src/ailocal/resources/deploy/litellm/compose.yaml")
env_default = re.search(r"AILOCAL_LITELLM_VERSION[=:]-?([0-9][^}\s]*)", compose)
configured = env_default.group(1) if env_default else ""
check("the LiteLLM version is declared in compose.yaml", bool(configured),
      "no AILOCAL_LITELLM_VERSION default found")

if configured:
    major_minor = ".".join(configured.split(".")[:2])
    stale = []
    for p in DOCS:
        for m in re.finditer(r"LiteLLM\s+(\d+\.\d+)", _text(p)):
            found = m.group(1)
            if found != major_minor:
                line = _text(p)[:m.start()].count("\n") + 1
                # A version named as history is fine; one asserted as current
                # is not. "1.93" beside a HISTORICAL/WITHDRAWN/used to marker
                # is provenance, which this suite exists to preserve.
                ctx = _text(p).splitlines()[line - 1]
                if not re.search(r"HISTORICAL|WITHDRAWN|SUPERSEDED|used to|"
                                 r"no longer|changed at|previous|>=|≥", ctx, re.I):
                    stale.append(f"{p.relative_to(REPO)}:{line} says {found}")
    check(f"docs name LiteLLM {major_minor}, or mark another version historical",
          not stale, "; ".join(stale[:4]))


# ── the deleted hook stays deleted ──────────────────────────────────────────
hook = REPO / "src/ailocal/resources/deploy/litellm/hooks/system_transport.py"
check("system_transport.py is gone", not hook.exists(), str(hook))
restore = []
for p in DOCS:
    for i, line in enumerate(_text(p).splitlines(), 1):
        if "system_transport" in line and not re.search(
                r"HISTORICAL|removed|deleted|do not restore", line, re.I):
            restore.append(f"{p.relative_to(REPO)}:{i}")
check("no doc presents system_transport as current or to-be-removed",
      not restore, "; ".join(restore[:4]))


# ── documented variables exist in the implementation ────────────────────────
# A variable may be read by the wrapper OR by the package; both are the
# implementation as far as a reader of the docs is concerned.
wrapper = _text(REPO / "src/ailocal/resources/clients/configure.template.zsh")
wrapper += "".join(_text(f) for f in sorted((REPO / "src/ailocal").rglob("*.py")))
documented = set()
for p in DOCS:
    documented |= set(re.findall(r"`(AILOCAL_[A-Z_]+)`", _text(p)))
# The role overrides are documented once with a <ROLE> placeholder.
documented.discard("AILOCAL_")
missing = sorted(v for v in documented
                 if v not in wrapper and "ALIAS_OVERRIDE" not in v)
check("every documented AILOCAL_* variable exists in the wrapper",
      not missing, ", ".join(missing))

for v in ("AILOCAL_NATIVE_WORKFLOWS", "AILOCAL_TOOL_SEARCH"):
    check(f"{v} is documented", any(f"`{v}`" in _text(p) for p in DOCS))

check("ENABLE_TOOL_SEARCH=force is not presented as supported",
      not any(re.search(r"ENABLE_TOOL_SEARCH=force(?!\*\* is \*\*not\*\*)", _text(p))
              and "not** a supported" not in _text(p) for p in DOCS))


# ── the bundled component is where the docs say it is ───────────────────────
bundle = REPO / "src/ailocal/resources/integrations/local-artifacts"
check("the bundled artifact component exists", (bundle / "server.py").is_file(),
      str(bundle))
clone = []
for p in DOCS:
    for i, line in enumerate(_text(p).splitlines(), 1):
        if re.search(r"(git clone|pipx install|pip install).*local-artifacts", line):
            clone.append(f"{p.relative_to(REPO)}:{i}")
check("no doc tells a user to install local-artifacts separately",
      not clone, "; ".join(clone[:4]))


# ── relative links resolve ──────────────────────────────────────────────────
broken = []
for p in DOCS:
    for m in re.finditer(r"\]\(([^)#:]+\.md)(?:#[^)]*)?\)", _text(p)):
        target = (p.parent / m.group(1)).resolve()
        if not target.is_file():
            line = _text(p)[:m.start()].count("\n") + 1
            broken.append(f"{p.relative_to(REPO)}:{line} -> {m.group(1)}")
check("every relative markdown link resolves", not broken, "; ".join(broken[:5]))


print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
