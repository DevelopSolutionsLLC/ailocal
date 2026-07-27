#!/usr/bin/env python3
"""verify-session.py — compare what a session claimed to do against what changed.

Runs on the HOST, because the proxy container cannot see the repository. Reads a
ledger written by config/litellm/session_observer.py (what was asked, what tools
were called) and pairs it with facts only the host has: the git delta, untracked
files, and optionally a test command's outcome.

WHAT THIS IS FOR
----------------
The interesting local-model failure is not a wrong answer. It is a confident
report of work that did not happen: mutating tools called (or claimed), and
nothing on disk different afterwards. That is invisible from inside the
conversation and obvious from outside it.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not feed results back into the conversation, inject turns, or modify
client state. It prints a report. Closing that loop needs protocol ownership
that does not exist yet, and a verification layer that silently edited the
conversation would be a worse problem than the one it solves.

It also does not claim causation. A git delta proves the tree changed while the
session ran; it does not prove this session caused it, and it cannot distinguish
the model's edits from a human's in the same window. The report says so.

USAGE
    scripts/verify-session.py --repo . [--ledger <file>] [--test "pytest -q"]
    scripts/verify-session.py --repo . --list

Exit codes:  0 nothing suspicious   1 usage/IO error   2 suspicious pattern
"""

import argparse
import glob
import json
import os
import subprocess
import sys

DEFAULT_LEDGERS = "data/tool-captures/sessions"

# Tools whose whole purpose is to change the tree. If these ran and nothing
# changed, that is the pattern worth surfacing. Names span the dialects because
# Claude Code and Codex disagree on them.
MUTATING = {
    "Edit", "Write", "NotebookEdit", "MultiEdit",
    "apply_patch", "exec_command", "Bash", "write_stdin",
}
# Bash/exec_command are included above but are ambiguous: a session can
# legitimately run only read-only commands. Tracked separately so the report can
# say "possibly read-only" instead of asserting a fabrication.
AMBIGUOUS = {"Bash", "exec_command", "write_stdin"}


def run(cmd, cwd, shell=False):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=120, shell=shell)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def git_state(repo):
    """Working-tree delta. Returns None when the path is not a git repo, rather
    than an empty delta — 'not a repo' and 'no changes' must not look alike."""
    rc, _, _ = run(["git", "rev-parse", "--git-dir"], repo)
    if rc != 0:
        return None
    _, porcelain, _ = run(["git", "status", "--porcelain"], repo)
    _, stat, _ = run(["git", "diff", "--stat"], repo)
    _, staged, _ = run(["git", "diff", "--cached", "--stat"], repo)
    lines = [l for l in porcelain.splitlines() if l.strip()]
    return {
        "changed_paths": len(lines),
        # `git status --porcelain` is "XY<space>PATH", but the status field is
        # not always two characters wide in practice, and a fixed l[3:] slice
        # ate the first letter of the filename ("calc.py" printed as "alc.py").
        # Splitting on whitespace is what the format actually guarantees.
        "paths": [l.split(None, 1)[1] if len(l.split(None, 1)) > 1 else l
                  for l in lines[:40]],
        "untracked": sum(1 for l in lines if l.startswith("??")),
        "diff_stat": stat.splitlines()[-1] if stat else "",
        "staged_stat": staged.splitlines()[-1] if staged else "",
    }


def newest_ledger(ledger_dir):
    files = glob.glob(os.path.join(ledger_dir, "*.json"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--ledger", help="ledger JSON (default: newest)")
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGERS)
    ap.add_argument("--test", help="test command to run in --repo")
    ap.add_argument("--list", action="store_true", help="list ledgers and exit")
    args = ap.parse_args()

    if args.list:
        files = sorted(glob.glob(os.path.join(args.ledger_dir, "*.json")),
                       key=os.path.getmtime, reverse=True)
        if not files:
            print(f"No ledgers in {args.ledger_dir}. Set "
                  f"AILOCAL_SESSION_LEDGER=/app/captures/sessions on the proxy "
                  f"and run a session.")
            return 1
        for f in files:
            d = json.load(open(f))
            ask = " ".join((d.get("requested_change") or "").split())[:60]
            print(f"{os.path.basename(f):22} {d.get('tool_calls_total'):3} calls  {ask}")
        return 0

    path = args.ledger or newest_ledger(args.ledger_dir)
    if not path or not os.path.exists(path):
        print(f"No ledger found. Looked in {args.ledger_dir}. The observer is "
              f"off unless AILOCAL_SESSION_LEDGER is set — this is not "
              f"evidence that a session did nothing.")
        return 1

    led = json.load(open(path))
    git = git_state(args.repo)

    print("=" * 70)
    print(f"LEDGER  {os.path.basename(path)}")
    print(f"model   {led.get('model')}")
    ask = " ".join((led.get("requested_change") or "").split())
    print(f"asked   {ask[:200]}{'...' if len(ask) > 200 else ''}")
    print()
    print(f"EXECUTED  {led.get('tool_calls_total')} tool calls, "
          f"{led.get('tool_results_total')} results, "
          f"{led.get('tool_results_errored')} errored, "
          f"{led.get('tool_results_unknown_status')} unknown status")
    for name, n in sorted((led.get("tool_calls_by_name") or {}).items(),
                          key=lambda kv: -kv[1]):
        print(f"    {name:32} x{n}")

    print()
    if git is None:
        print(f"FILESYSTEM  {args.repo} is not a git repository — no delta "
              f"available. Cannot verify; not the same as verified clean.")
    else:
        print(f"FILESYSTEM  {git['changed_paths']} paths differ from HEAD "
              f"({git['untracked']} untracked)")
        if git["diff_stat"]:
            print(f"    unstaged: {git['diff_stat']}")
        if git["staged_stat"]:
            print(f"    staged:   {git['staged_stat']}")
        for p in git["paths"][:15]:
            print(f"    {p}")

    test_rc = None
    if args.test:
        print()
        print(f"TESTS  $ {args.test}")
        # shell=True because --test is an operator-supplied command line, and
        # naive .split() mangles anything quoted: `python3 -c "import calc; ..."`
        # arrived at the interpreter as three broken fragments and failed with a
        # SyntaxError that looked like a real test failure.
        test_rc, out, err = run(args.test, args.repo, shell=True)
        tail = (out or err).splitlines()[-8:]
        for line in tail:
            print(f"    {line}")
        print(f"    exit {test_rc}")

    # ── findings ────────────────────────────────────────────────────────────
    print()
    findings = []
    called = set(led.get("tool_calls_by_name") or {})
    mutators = called & MUTATING
    definite = mutators - AMBIGUOUS

    if git is not None and git["changed_paths"] == 0:
        if definite:
            findings.append(
                f"SUSPICIOUS: {sorted(definite)} ran, but nothing in the tree "
                f"differs from HEAD. Either the edits were reverted, they "
                f"targeted a path outside {args.repo}, or they did not happen.")
        elif mutators:
            findings.append(
                f"INCONCLUSIVE: {sorted(mutators)} ran and nothing changed. "
                f"These can be read-only (a Bash that only greps), so this is "
                f"not evidence of fabrication.")

    if led.get("tool_calls_total") == 0:
        findings.append(
            "No tool calls recorded. For a task that required tools, the model "
            "answered from its own assumptions.")

    if led.get("tool_results_errored"):
        findings.append(
            f"{led['tool_results_errored']} tool call(s) returned errors. Check "
            f"whether the model noticed, or narrated success over them.")

    if led.get("tool_results_unknown_status"):
        findings.append(
            f"{led['tool_results_unknown_status']} result(s) have no success/"
            f"failure signal on this route — absence of an error flag is not "
            f"success.")

    if test_rc not in (None, 0):
        findings.append(f"Test command exited {test_rc}.")

    if findings:
        print("FINDINGS")
        for f in findings:
            print(f"  - {f}")
    else:
        print("FINDINGS  none of the checked patterns present.")

    print()
    print("This compares a claim against a tree state. It does not establish "
          "that this session caused the delta, nor that the change was "
          "correct — only whether something plausibly happened at all.")

    suspicious = any(f.startswith(("SUSPICIOUS", "No tool calls")) for f in findings)
    return 2 if suspicious else 0


if __name__ == "__main__":
    sys.exit(main())
