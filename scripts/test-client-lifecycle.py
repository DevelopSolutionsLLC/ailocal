#!/usr/bin/env python3
"""test-client-lifecycle.py — F. Twelve fresh client cycles; what survives the exit.

THE QUESTION. Agent clients spawn helpers: MCP servers over stdio, containers,
credential helpers, language servers, index watchers. Each is supposed to die with
the session. A helper that outlives its client is invisible until there are forty of
them holding file handles and GPU memory, so the only honest way to know is to take
a full process set before and after a cycle and diff it.

WHY TWELVE. Three fresh cycles for each of claude, claude-local, codex and
codex-local. One cycle cannot distinguish a leak from a slow exit; three make
cumulative growth visible, which is the failure that actually matters — a single
lingering process is a bug, a process count that climbs every cycle is a fleet
outage waiting to happen.

NO grep. Every snapshot is taken with `ps -Ao pid=,ppid=,pgid=,sess=,lstart=,args=`
and matched in Python against the parsed argv. A `grep github-mcp-server` in a shell
pipeline MATCHES ITS OWN COMMAND LINE — measured during this audit, where the first
probe reported two github-mcp-server processes and one of them was the grep. Any
match here also excludes this harness's own PID and its descendants, for the same
reason.

WHAT COUNTS AS FRESH. A cycle launches a NEW client process; no already-running
session is ever counted. Phases the client genuinely cannot do headlessly are
recorded as `unsupported` WITH the evidence, never silently skipped.

Run:  python3 scripts/test-client-lifecycle.py [--cycles N] [--json PATH] [--clients a,b]
Exit: 0 if every assertion holds, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GRACE_S = 6.0

SELF = os.getpid()

# What could plausibly belong to an agent client. Used by BOTH the per-cycle leak
# metric and the cumulative-growth assertion, because those two answering
# differently is exactly the bug this constant exists to prevent: the leak metric
# was filtered and the assertion was not, so a run reported 0 leaked processes in
# every cycle and simultaneously failed on "cumulative growth" of 8 — all of it
# macOS churn (contactsd, cfprefsd, geod) that no client had touched.
CLIENT_OWNED = ("claude", "codex", "node", "grepai", "mcpls", "github-mcp",
                "headers-helper", "docker", "python", "uv", "npx", "bun", "deno")


def client_owned(procs: dict[int, dict], exclude: set[int]) -> set[int]:
    """PIDs plausibly owned by an agent client, minus the observer."""
    return {p["pid"] for p in procs.values()
            if p["pid"] not in exclude
            and any(r in p["args"].lower() for r in CLIENT_OWNED)}

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}")


# ── structured process snapshots ─────────────────────────────────────────────

def snapshot() -> dict[int, dict]:
    """pid -> record. Full argv, never a grep."""
    out = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,pgid=,sess=,lstart=,args="],
        capture_output=True, text=True).stdout
    procs: dict[int, dict] = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid, sess = (int(parts[0]), int(parts[1]),
                                     int(parts[2]), int(parts[3]))
        except ValueError:
            continue
        rest = parts[4]
        # lstart is a fixed 24-char ctime string: "Wed Jul 29 22:43:32 2026"
        start, args = rest[:24].strip(), rest[24:].strip()
        procs[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid, "sess": sess,
                      "start": start, "args": args,
                      "exe": (args.split() or [""])[0]}
    return procs


def audit_owned(procs: dict[int, dict]) -> set[int]:
    """Every PID belonging to the observer, so it cannot count itself.

    Descendants of SELF, and nothing more. Two boundaries were tried and rejected:

    `sess` looks like the natural session boundary and is USELESS here — macOS `ps`
    reports sess 0 for all 660 processes on this machine, so excluding by session
    excluded everything and made every assertion vacuously true. A check that
    cannot fail is worse than no check.

    Excluding the observer's whole ancestor tree overshoots the other way: it would
    hide the pre-existing grepai server owned by the session running the audit,
    which is exactly one of the things being counted.

    So the observer is only its own subtree, and everything else is handled by
    comparing BASELINE to FINAL rather than asserting an absolute count. A leaked
    helper is reparented to PID 1 when its client exits, which takes it out of this
    subtree and puts it squarely in the delta where it belongs.
    """
    return descendants_of(procs, SELF) | {SELF}


def descendants_of(procs: dict[int, dict], root: int) -> set[int]:
    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    seen, stack = set(), [root]
    while stack:
        cur = stack.pop()
        for c in kids.get(cur, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def match(procs: dict[int, dict], needle: str, exclude: set[int]) -> list[dict]:
    """Substring match on argv, excluding this harness and its children."""
    return [p for p in procs.values()
            if needle in p["args"] and p["pid"] not in exclude and p["pid"] != SELF]


def containers(label_filter: str | None = None) -> list[dict]:
    cmd = ["docker", "ps", "-a", "--format", "{{.ID}}|{{.Image}}|{{.Names}}|{{.Labels}}"]
    if label_filter:
        cmd[3:3] = ["--filter", f"label={label_filter}"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    rows = []
    for line in out.strip().splitlines():
        if not line:
            continue
        cid, image, name, labels = (line.split("|", 3) + ["", "", ""])[:4]
        rows.append({"id": cid, "image": image, "name": name, "labels": labels})
    return rows


def listening_sockets() -> set[str]:
    out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout
    socks = set()
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 9:
            socks.add(f[8])
    return socks


# ── client invocation ────────────────────────────────────────────────────────
# Each phase is its own fresh process. `-p` / `exec` are the headless modes; an
# interactive TUI cannot be driven deterministically from a test, and pretending
# otherwise would make every timing here meaningless.

CLIENTS = {
    "claude":       {"kind": "claude", "wrapper": None},
    "claude-local": {"kind": "claude", "wrapper": "claude-local"},
    "codex":        {"kind": "codex",  "wrapper": None},
    "codex-local":  {"kind": "codex",  "wrapper": "codex-local"},
}


def run_phase(client: str, phase: str, prompt: str, timeout: int = 180,
              max_turns: int = 8) -> dict:
    spec = CLIENTS[client]
    t0 = time.monotonic()
    if spec["kind"] == "claude":
        # 8, not 3. A tool phase costs a turn to call and a turn to read the
        # result, and a local 30B routinely spends one more restating the plan.
        # Measured at 3: the MCP phase returned "Error: Reached max turns (3)"
        # and rc=1, which scored as a claude-local lifecycle failure when the
        # client and the MCP server had both worked correctly. The subagent
        # phase passed at rc=0 in the same probe, which is what made the
        # turn budget — rather than MCP — the obvious suspect.
        argv = ["claude", "-p", prompt, "--max-turns", str(max_turns)]
    else:
        argv = ["codex", "exec", "--skip-git-repo-check", prompt]

    if spec["wrapper"]:
        # Go through the REAL wrapper, so the env it sets is what is exercised —
        # but source ONLY configure.zsh, never `zsh -ic`.
        #
        # `-i` loads the full ~/.zshrc, which starts powerlevel10k, which fails
        # under a pipe ("gitstatus failed to initialize", "setopt: can't change
        # option: monitor") and returns 1. Measured: that made every claude-local
        # MCP phase look like a client failure when the client was never reached.
        # The wrapper is a plain shell function and needs no interactive shell.
        cfg = Path(os.environ.get("XDG_CONFIG_HOME",
                                  str(Path.home() / ".config"))) / "ailocal"
        inner = " ".join(_q(a) for a in argv[1:])
        cmd = ["zsh", "-c",
               f"source {_q(str(cfg / 'configure.zsh'))} >/dev/null 2>&1; "
               f"{spec['wrapper']} {inner}"]
    else:
        cmd = argv

    # start_new_session puts the client in its OWN process group so a timeout can
    # kill the whole tree.
    #
    # subprocess.run(timeout=) kills only the DIRECT child. For a wrapper client
    # that child is `zsh`, and the actual binary is a grandchild — it survives,
    # gets reparented to PID 1, and keeps generating. Measured during this audit:
    # four orphaned `codex` processes accumulated at 13:47, 10:47, 7:41 and 4:41
    # elapsed, all ppid=1, all still holding the local model. That both corrupted
    # every timing afterwards and made the harness the biggest process leak in a
    # test whose entire purpose is detecting process leaks.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(REPO), start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = "", ""
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        rc, err = -1, (err or "") + "\nTIMEOUT (process group reaped)"
    rec = {"phase": phase, "rc": rc, "ms": round((time.monotonic() - t0) * 1000),
           "out": (out or "")[-400:], "err": (err or "")[-400:]}
    # A client that cannot authenticate never exercised the phase at all. Scoring
    # that as a failed phase would blame the lifecycle for a missing credential;
    # scoring it as a pass would be worse. Measured: cloud `codex` returns 401
    # from api.openai.com on this machine, so all of its live phases are
    # unreachable and are recorded as such WITH the upstream error.
    blob = f"{out or ''}\n{err or ''}"
    if rc != 0 and "Reached max turns" in blob:
        rec["turn_budget_exhausted"] = True
    if rc != 0 and ("401 Unauthorized" in blob or "Missing bearer" in blob
                    or "Unauthorized" in blob and "authentication" in blob):
        rec["unsupported"] = ("client is not authenticated (upstream 401); the "
                              "phase was never reached")
    return rec


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def mcp_registered(client: str) -> bool:
    """Is grepai actually registered for THIS client's config root?

    Without this, a tool phase that ends in max_turns is ambiguous between
    `registration_failure` (the tool was never offered) and
    `model_did_not_dispatch` (it was offered and the model did not use it).
    Those have completely different owners.
    """
    root = (Path.home() / ".config" / "ailocal" / "claude" / ".claude.json"
            if client.endswith("-local") else Path.home() / ".claude.json")
    try:
        blob = root.read_text()
    except Exception:
        return False
    return '"grepai"' in blob


PHASES = [
    ("no-tool",  "Reply with exactly: PONG. Do not use any tools."),
    ("shell",    "Run the shell command `echo LIFECYCLE_OK` and report its output."),
    ("mcp",      "List the grepai projects using the grepai MCP tool, then stop."),
    ("subagent", "Use the Agent tool to delegate a one-sentence summary of README.md, then stop."),
]


def authenticated(client: str) -> tuple[bool, str]:
    """Can this client actually reach its model provider?

    Run ONCE per client, before any cycle. A client without credentials cannot
    exercise a single phase, so running three full cycles of it produces twelve
    identical 401s and no lifecycle information — the cycles are pure cost. Worse,
    scoring those as phase FAILURES blames the lifecycle for a missing token.

    Measured on this machine: cloud `codex` returns
    "401 Unauthorized: Missing bearer or basic authentication" from
    api.openai.com/v1/responses, while cloud `claude` and both -local wrappers
    (which authenticate against the LiteLLM proxy) work. So codex is skipped with
    its upstream error recorded, and the other three run in full.
    """
    r = run_phase(client, "auth-probe", "Reply with exactly: AUTHOK", timeout=120,
                  max_turns=2)
    if r.get("rc") == 0:
        return True, ""
    blob = f"{r.get('out','')}\n{r.get('err','')}"
    markers = ("401 Unauthorized", "Missing bearer", "Unauthorized",
               "not logged in", "authentication")
    if any(mk in blob for mk in markers):
        # The LINE that names the problem, not a trailing slice — a tail cut
        # landed mid-request-id and recorded the evidence as "f5e31c", which
        # proves nothing to whoever reads the summary later.
        for line in blob.splitlines():
            if any(mk in line for mk in markers):
                return False, line.strip()[:300]
        return False, blob.strip()[:300]
    # A non-auth failure is NOT a reason to skip — the client is reachable and the
    # cycles may still carry lifecycle signal.
    return True, ""


def cycle(client: str, n: int, quick: bool) -> dict:
    print(f"\n── {client} cycle {n} ──")
    base = snapshot()
    base_containers = {c["id"] for c in containers()}
    base_socks = listening_sockets()

    phase_results = []
    launched: list[dict] = []

    for name, prompt in PHASES:
        if quick and name in ("mcp", "subagent"):
            phase_results.append({"phase": name, "rc": None, "ms": 0,
                                  "unsupported": "--quick"})
            continue
        # codex has no subagent tool and, measured, cannot reach MCP at all
        # (CLAUDE.md: namespace bundles are discarded before the backend and the
        # flattened form is refused by Codex's own dispatcher).
        if client.startswith("codex") and name in ("mcp", "subagent"):
            phase_results.append({
                "phase": name, "rc": None, "ms": 0,
                "unsupported": "codex cannot dispatch MCP tools by either route "
                               "(openai/codex#20652); no Agent/subagent tool exists"})
            continue
        # A local backend generates far slower than a cloud one; 180s is a cloud
        # budget. Measured: codex-local phases routinely exceeded it, and every
        # one that did left an orphan before the process-group fix above.
        budget = 420 if client.endswith("-local") else 180
        r = run_phase(client, name, prompt, timeout=budget)
        if r.get("rc") == -1 and "TIMEOUT" in (r.get("err") or ""):
            r["wall_clock_timeout"] = True
        if name in ("mcp", "subagent"):
            r["tool_registered"] = mcp_registered(client) if name == "mcp" else True
        # Exactly one classification per phase.
        if r.get("unsupported"):
            r["classification"] = "unsupported"
        elif r.get("rc") == 0:
            r["classification"] = "passed"
        elif r.get("wall_clock_timeout"):
            r["classification"] = "wall_clock_timeout"
        elif r.get("turn_budget_exhausted"):
            # The tool WAS registered and the turns WERE spent, so the model had
            # the tool and did not complete a call with it. That is a local-model
            # capability limit, not a registration or transport fault — and it is
            # deliberately NOT "fixed" by raising the budget further.
            r["classification"] = ("model_did_not_dispatch"
                                   if r.get("tool_registered") is True
                                   else "max_turns_exhausted")
        else:
            r["classification"] = "tool_execution_failure"
        phase_results.append(r)
        during = snapshot()
        for pid, p in during.items():
            if pid not in base:
                launched.append(p)

    time.sleep(GRACE_S)
    post = snapshot()
    post_containers = {c["id"] for c in containers()}
    post_socks = listening_sockets()

    excl = audit_owned(post)
    new_pids = set(post) - set(base) - excl
    # Only processes that could plausibly BELONG to an agent client. The raw
    # set-difference is dominated by macOS churn — a single codex-local cycle
    # showed 70 "new" processes of which 22 were contactsd and 14 cfprefsd, and
    # not one had anything to do with the client. Counting those made the leak
    # metric a system-noise meter that could never fail meaningfully.
    new_procs = [post[p] for p in sorted(new_pids)
                 if any(r in post[p]["args"].lower() for r in CLIENT_OWNED)]

    return {
        "client": client, "cycle": n,
        "baseline_procs": len(base), "post_procs": len(post),
        "delta_procs": len(post) - len(base),
        "new_after_grace": [{"pid": p["pid"], "ppid": p["ppid"], "pgid": p["pgid"],
                             "sess": p["sess"], "start": p["start"],
                             "exe": p["exe"], "args": p["args"][:120]}
                            for p in new_procs],
        "new_containers": sorted(post_containers - base_containers),
        "new_sockets": sorted(post_socks - base_socks),
        "phases": phase_results,
        "launched_seen": len(launched),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--clients", default=",".join(CLIENTS))
    ap.add_argument("--json")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    clients = [c.strip() for c in args.clients.split(",") if c.strip()]
    for c in clients:
        if c not in CLIENTS:
            raise SystemExit(f"unknown client {c!r}")

    gh_before = [c for c in containers() if "github-mcp-server" in c["image"]]
    base_all = snapshot()
    excl0 = audit_owned(base_all)
    shared_watcher_before = len(match(base_all, "grepai", excl0))
    baseline_total = len(base_all)

    print(f"baseline: {baseline_total} processes, "
          f"{len(gh_before)} github-mcp container(s), "
          f"{shared_watcher_before} grepai process(es)")

    all_cycles = []
    skipped: dict[str, str] = {}
    for c in clients:
        ok, why = authenticated(c)
        if not ok:
            skipped[c] = why
            print(f"\n── {c}: SKIPPED, not authenticated ──")
            print(f"   {why.splitlines()[0][:150] if why else 'no detail'}")
            continue
        for n in range(1, args.cycles + 1):
            all_cycles.append(cycle(c, n, args.quick))

    final = snapshot()
    exclF = audit_owned(final)

    print("\nassertions")

    gh_after = [c for c in containers() if "github-mcp-server" in c["image"]]
    new_gh = {c["id"] for c in gh_after} - {c["id"] for c in gh_before}
    check(not new_gh, f"zero NEW GitHub MCP containers from fresh cycles "
                      f"(pre-existing: {len(gh_before)}, new: {len(new_gh)})")

    ghp = match(final, "github-mcp-server", exclF)
    new_ghp = [p for p in ghp if p["pid"] not in base_all]
    check(not new_ghp,
          f"zero NEW local github-mcp-server processes from fresh cycles "
          f"(pre-existing: {len(ghp) - len(new_ghp)}, new: {len(new_ghp)})")

    hh = match(final, "headers-helper", exclF)
    check(not hh, f"zero resident headers-helper processes ({len(hh)})")

    # `mcpls` must not match `mcpls`-containing paths of unrelated tools, so it is
    # matched as an executable basename, not a substring of any argv.
    mcpls = [p for p in final.values()
             if os.path.basename(p["exe"]) == "mcpls" and p["pid"] not in exclF]
    check(not mcpls, f"zero real mcpls processes ({len(mcpls)})")

    # CLIENT-OWNED growth, not raw process count. The raw total moves by tens on
    # an idle macOS box and says nothing about whether a client leaked.
    owned_before = client_owned(base_all, excl0)
    owned_after = client_owned(final, exclF)
    growth = len(owned_after) - len(owned_before)
    check(growth <= 2,
          f"no cumulative client-owned process growth "
          f"(baseline {len(owned_before)} -> final {len(owned_after)}, "
          f"delta {growth}; raw process total moved "
          f"{baseline_total} -> {len(final)} and is deliberately not the metric)")

    grepai_final = match(final, "grepai", exclF)
    check(len(grepai_final) <= shared_watcher_before,
          f"no duplicate session grepai server (before {shared_watcher_before}, "
          f"after {len(grepai_final)})")
    new_grepai = [p for p in grepai_final if p["pid"] not in base_all]
    check(not new_grepai,
          f"no grepai server survived a cycle ({len(new_grepai)} new)")
    check(all(r["new_after_grace"] == [] or
              not any("grepai" in x["args"] for x in r["new_after_grace"])
              for r in all_cycles),
          "session-owned grepai exits within the grace period")

    shared = [p for p in grepai_final if p["ppid"] == 1]
    check(len(shared) >= 1, f"shared grepai watcher still present ({len(shared)})")

    # Codex must receive no Cadence GitHub / grepai / LSP registrations.
    codex_cfg = Path.home() / ".codex" / "config.toml"
    codex_local_cfg = Path.home() / ".config" / "ailocal" / "codex" / "config.toml"
    for label, path in (("codex", codex_cfg), ("codex-local", codex_local_cfg)):
        if not path.exists():
            check(True, f"{label}: no config.toml present (nothing registered)")
            continue
        txt = path.read_text()
        bad = [n for n in ("mcp_servers.github", "mcp_servers.grepai", "mcp_servers.lsp")
               if n in txt]
        check(not bad, f"{label} has no Cadence GitHub/grepai/LSP entries "
                       f"({bad or 'none'})")

    for r in all_cycles:
        if r["new_containers"]:
            check(False, f"{r['client']} cycle {r['cycle']} leaked containers: "
                         f"{r['new_containers']}")

    print("\ncycle table")
    print("client | cycle | base_procs | post_procs | delta | leaked_after_grace | "
          "new_containers | phases_ok | unsupported")
    for r in all_cycles:
        ok = sum(1 for p in r["phases"] if p.get("rc") == 0)
        uns = sum(1 for p in r["phases"] if p.get("unsupported"))
        print(f"{r['client']} | {r['cycle']} | {r['baseline_procs']} | "
              f"{r['post_procs']} | {r['delta_procs']} | "
              f"{len(r['new_after_grace'])} | {len(r['new_containers'])} | "
              f"{ok}/{len(r['phases'])} | {uns}")

    if skipped:
        print("\nskipped clients (unsupported — not authenticated, with evidence)")
        for c, why in skipped.items():
            print(f"  {c}: {(why.splitlines() or ['(no detail)'])[0][:150]}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"baseline_total": baseline_total, "cycles": all_cycles,
             "skipped_unauthenticated": skipped,
             "assertions": [{"ok": o, "label": l} for o, l in results]}, indent=2))
        print(f"\nwrote {args.json}")

    failed = [l for o, l in results if not o]
    print(f"\n{len(results) - len(failed)}/{len(results)} assertions passed")
    if failed:
        print("FAILED:")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("F LIFECYCLE: all assertions hold across "
          f"{len(all_cycles)} fresh cycles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
