#!/usr/bin/env bash
# test-idempotent-install.sh — running an installer twice must change nothing the
# second time.
#
# The failure this catches is duplication: a second run appending another MCP
# stanza, another provider group, another settings block. That is how an
# installation rots into a state nobody can reason about, and it is invisible
# until something picks the wrong duplicate.
#
# METHOD
# Fingerprint the observable state, run the installer, fingerprint again, run it
# a SECOND time, fingerprint again. The first run may legitimately change things
# (it is installing). The second must not.
#
#   baseline --[install #1]--> state A --[install #2]--> state B
#   assert A == B
#
# WHAT IS FINGERPRINTED, AND WHY NOT JUST FILE HASHES
# Some generated files embed a timestamp, so a byte hash would report a false
# difference every run. Those are compared STRUCTURALLY (parsed, timestamp keys
# dropped). Where a file is compared by hash, it is because it should be
# byte-stable. Each entry says which it is.
#
# Usage: ./tests/idempotent-install.sh [--include-vscode]
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
ROOT="$ROOT_DIR"
cd "$ROOT"

INCLUDE_VSCODE=""
[ "${1:-}" = "--include-vscode" ] && INCLUDE_VSCODE=1

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/output.sh"

fingerprint() { # $1=label -> writes /tmp/ailocal-fp-$1.txt
  local out="/tmp/ailocal-fp-$1.txt"
  : > "$out"
  python3 - "$out" <<'PY'
import hashlib, json, os, re, sys

out = open(sys.argv[1], "w", encoding="utf-8")

def emit(key, value):
    out.write(f"{key}\t{value}\n")

def sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    except Exception:
        return "absent"

# ── byte-stable files: a hash is the right comparison ──────────────────────
for p in (os.path.expanduser("~/.local/state/ailocal/litellm/config.yaml"),
          os.path.expanduser("~/.local/state/ailocal/clients/model_catalog.json"),
          os.path.expanduser("~/.local/state/ailocal/clients/claude/settings.json"),
          os.path.expanduser("~/.local/state/ailocal/clients/codex/config.toml")):
    emit("hash:" + p, sha(p))

# ── capabilities.generated.json carries generated_at, so compare STRUCTURE ──
# A byte hash here would fail every run for a reason that is not duplication.
try:
    doc = json.load(open("config/capabilities.generated.json"))
    caps = doc.get("capabilities") or []
    shape = [(c.get("name"), c.get("backend"), c.get("context"),
              c.get("keep_alive")) for c in caps]
    emit("struct:capabilities", hashlib.sha256(
        json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16])
    emit("count:capabilities", len(caps))
except Exception as exc:
    emit("struct:capabilities", f"unreadable:{type(exc).__name__}")

# ── the duplication-sensitive counts ───────────────────────────────────────
# These are the numbers that GROW when an installer is not idempotent.
try:
    txt = open(os.path.expanduser(
        "~/.config/ailocal/codex/config.toml"), encoding="utf-8").read()
    emit("count:codex_mcp_stanzas", len(re.findall(r"^\[mcp_servers\.", txt, re.M)))
    emit("count:codex_total_lines", len(txt.splitlines()))
except Exception:
    emit("count:codex_mcp_stanzas", "absent")

try:
    doc = json.load(open(os.path.expanduser(
        "~/.config/ailocal/claude/.claude.json")))
    servers = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "mcpServers" and isinstance(v, dict):
                    servers.update(v)
                walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)
    walk(doc)
    emit("set:claude_mcp_servers", ",".join(sorted(servers)))
except Exception:
    emit("set:claude_mcp_servers", "absent")

for base in ("~/Library/Application Support/Code/User",
             "~/.config/Code/User"):
    p = os.path.expanduser(base + "/chatLanguageModels.json")
    if os.path.exists(p):
        try:
            entries = json.load(open(p))
            emit("count:vscode_provider_groups", len(entries))
            emit("set:vscode_groups", ",".join(
                sorted(f"{e.get('vendor')}@{e.get('baseUrl')}"
                       for e in entries if isinstance(e, dict))))
        except Exception as exc:
            emit("count:vscode_provider_groups", f"unreadable:{type(exc).__name__}")
        break

# ── shell wiring: a repeated source block is a classic non-idempotency ─────
zshrc = os.path.expanduser("~/.zshrc")
try:
    txt = open(zshrc, encoding="utf-8").read()
    emit("count:zshrc_ailocal_markers", txt.count("ailocal"))
except Exception:
    emit("count:zshrc_ailocal_markers", "absent")

# ── docker: containers must not multiply ───────────────────────────────────
import subprocess
try:
    names = subprocess.run(["docker", "ps", "-a", "--filter", "name=ailocal-",
                            "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=30).stdout.split()
    emit("set:docker_containers", ",".join(sorted(names)))
except Exception:
    emit("set:docker_containers", "unknown")

out.close()
PY
  echo "$out"
}

compare() { # $1=a $2=b $3=label
  if diff -u "/tmp/ailocal-fp-$1.txt" "/tmp/ailocal-fp-$2.txt" > /tmp/ailocal-fp-diff.txt; then
    ok "$3 — state identical"
  else
    bad "$3 — state CHANGED on a repeat run:"
    sed 's/^/        /' /tmp/ailocal-fp-diff.txt | grep -E '^\s+[+-]' | head -12
  fi
}

echo "══════════════════════════════════════════════════════════════════════"
echo " INSTALLER IDEMPOTENCY"
echo "══════════════════════════════════════════════════════════════════════"

banner "fingerprint: baseline"
fingerprint baseline >/dev/null

# ── ailocal sync ───────────────────────────────────────────────────────────
banner "ailocal sync x2"
./ailocal sync >/dev/null 2>&1 || bad "ailocal sync #1 failed"
fingerprint sync1 >/dev/null
./ailocal sync >/dev/null 2>&1 || bad "ailocal sync #2 failed"
fingerprint sync2 >/dev/null
compare sync1 sync2 "ailocal sync"

# ── ailocal clients ────────────────────────────────────────────────────────
# claude + codex only by default: the vscode target touches the user's editor
# config, which is opt-in here.
banner "ailocal clients claude codex x2"
if ./ailocal clients claude codex >/dev/null 2>&1; then
  fingerprint clients1 >/dev/null
  ./ailocal clients claude codex >/dev/null 2>&1
  fingerprint clients2 >/dev/null
  compare clients1 clients2 "ailocal clients claude codex"
else
  bad "ailocal clients failed on the first run — idempotency untested"
fi

# ── ailocal vscode (opt-in) ────────────────────────────────────────────────
if [ -n "$INCLUDE_VSCODE" ]; then
  banner "ailocal vscode x2"
  if ./ailocal vscode >/dev/null 2>&1; then
    fingerprint vsc1 >/dev/null
    ./ailocal vscode >/dev/null 2>&1
    fingerprint vsc2 >/dev/null
    compare vsc1 vsc2 "ailocal vscode"
  else
    bad "ailocal vscode failed on the first run"
  fi
else
  printf '  \033[2m—\033[0m ailocal vscode skipped (--include-vscode to test it)\n'
fi

# ── the specific counts that must not grow ─────────────────────────────────
banner "duplication-sensitive counts"
python3 - <<'PY'
import sys
def load(p):
    d = {}
    for line in open(p, encoding="utf-8"):
        k, _, v = line.rstrip("\n").partition("\t")
        d[k] = v
    return d
base = load("/tmp/ailocal-fp-baseline.txt")
last = load(sorted(__import__("glob").glob("/tmp/ailocal-fp-*.txt"))[-1])
bad = 0
for key in ("count:codex_mcp_stanzas", "count:vscode_provider_groups",
            "count:zshrc_ailocal_markers", "count:codex_total_lines"):
    b, l = base.get(key), last.get(key)
    if b is None or l is None:
        continue
    try:
        grew = int(l) > int(b)
    except ValueError:
        grew = False
    status = "\033[31mGREW\033[0m" if grew else "\033[32mstable\033[0m"
    print(f"    {key:34} {b} -> {l}   {status}")
    if grew:
        bad = 1
sys.exit(bad)
PY
[ $? -eq 0 ] && ok "no duplication-sensitive count grew" \
             || bad "a count grew across installer runs — that is duplication"

echo
echo "══════════════════════════════════════════════════════════════════════"
report " IDEMPOTENCY" || exit 1
echo " Installers can be re-run safely; a second run changes nothing."
