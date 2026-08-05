#!/usr/bin/env python3
"""verify-session.py — compare what a session claimed to do against what changed.

Runs on the HOST, because the proxy container cannot see the repository. Reads a
ledger written by deploy/litellm/hooks/session_observer.py (what was asked, what tools
were called) and pairs it with facts only the host has: the git delta, untracked
files, and optionally a test command's outcome.

The failure it targets is not a wrong answer but a confident report of work that
did not happen: mutating tools called or claimed, and nothing on disk different
afterwards. That is invisible from inside the conversation and obvious from
outside it.

SCOPE
-----
It prints a report and nothing else — no feedback into the conversation, no
injected turns, no client-state changes. Closing that loop needs protocol
ownership that does not exist yet.

It does not claim causation. A git delta proves the tree changed while the
session ran, not that the session caused it, and it cannot separate the model's
edits from a human's in the same window. The report says so.

USAGE
    scripts/diagnostics/verify-session.py --repo . [--ledger <file>] [--test "pytest -q"]
    scripts/diagnostics/verify-session.py --repo . --list

Exit codes:  0 VERIFIED or PARTIALLY_VERIFIED
             1 usage/IO error
             2 SUSPICIOUS
             3 UNVERIFIED  (deliberately non-zero: "could not check" must not
                            be scriptable as success)
"""

import argparse
import glob
import json
import os
import subprocess
import sys

DEFAULT_LEDGERS = "data/tool-captures/sessions"

# Which tools mutate the tree is a registry fact (registry.yaml:mutating_tools),
# so the verification pipeline and the negotiator cannot disagree about it. This
# script runs on the HOST, where PyYAML is absent, so the registry may be
# unreadable even when correct. Fall back to these constants and always report
# which source was used, so a drift between the two is visible rather than
# silently assumed away.
_FALLBACK_DEFINITE = {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"}
_FALLBACK_AMBIGUOUS = {"Bash", "exec_command", "write_stdin"}

REGISTRY_YAML = os.environ.get(
    "AILOCAL_REGISTRY_HOST",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "deploy/litellm/registry.yaml"))


def mutating_sets():
    """(definite, ambiguous, source). `source` is reported, never hidden."""
    try:
        import yaml
        with open(REGISTRY_YAML, encoding="utf-8") as f:
            spec = (yaml.safe_load(f) or {}).get("mutating_tools") or {}
        definite = set(spec.get("definite") or [])
        ambiguous = set(spec.get("ambiguous") or [])
        if definite:
            return definite, ambiguous, "registry.yaml"
        return (_FALLBACK_DEFINITE, _FALLBACK_AMBIGUOUS,
                "builtin fallback (registry had no mutating_tools)")
    except ImportError:
        return (_FALLBACK_DEFINITE, _FALLBACK_AMBIGUOUS,
                "builtin fallback (PyYAML absent on host)")
    except Exception as exc:
        return (_FALLBACK_DEFINITE, _FALLBACK_AMBIGUOUS,
                f"builtin fallback (registry unreadable: {type(exc).__name__})")


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
        # not reliably two characters wide, so a fixed l[3:] slice eats the first
        # character of the path. Whitespace splitting is what the format guarantees.
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
        # naive .split() mangles quoting: `python3 -c "import calc; ..."` would
        # reach the interpreter as broken fragments and fail as a bogus test error.
        test_rc, out, err = run(args.test, args.repo, shell=True)
        tail = (out or err).splitlines()[-8:]
        for line in tail:
            print(f"    {line}")
        print(f"    exit {test_rc}")

    # ── classification ──────────────────────────────────────────────────────
    #   VERIFIED            the work happened AND holds
    #   PARTIALLY_VERIFIED  it happened, but something is unresolved
    #   UNVERIFIED          no evidence either way — the checks could not run
    #   SUSPICIOUS          a claim with positive evidence of no substance
    #
    # UNVERIFIED exists so "could not check" never renders as "clean".
    print()
    signals = []          # (name, outcome) for the evidence table

    called = set(led.get("tool_calls_by_name") or {})
    definite_set, ambiguous_set, mut_source = mutating_sets()
    mutators = called & (definite_set | ambiguous_set)
    definite = mutators & definite_set
    ambiguous_only = mutators - definite_set

    errored = led.get("tool_results_errored") or 0
    unknown = led.get("tool_results_unknown_status") or 0
    total_calls = led.get("tool_calls_total") or 0

    # ── signal: did anything change on disk ─────────────────────────────────
    if git is None:
        signals.append(("filesystem", "unavailable"))
    elif git["changed_paths"] > 0:
        signals.append(("filesystem", "changed"))
    else:
        signals.append(("filesystem", "unchanged"))

    # ── signal: were mutating tools used ────────────────────────────────────
    if definite:
        signals.append(("mutating tools", "definite: " + ", ".join(sorted(definite))))
    elif ambiguous_only:
        signals.append(("mutating tools", "ambiguous only: "
                        + ", ".join(sorted(ambiguous_only))))
    elif total_calls:
        signals.append(("mutating tools", "none (read-only session)"))
    else:
        signals.append(("mutating tools", "no tool calls at all"))

    # ── signal: tool failures ───────────────────────────────────────────────
    if errored:
        signals.append(("tool errors", f"{errored} errored"))
    elif unknown:
        signals.append(("tool errors", f"unknown for {unknown} result(s) — this "
                        f"route carries no error flag"))
    else:
        signals.append(("tool errors", "none reported"))

    # ── signal: tests ───────────────────────────────────────────────────────
    if args.test is None:
        signals.append(("tests", "not run (no --test given)"))
    elif test_rc == 0:
        signals.append(("tests", "passed"))
    else:
        signals.append(("tests", f"FAILED (exit {test_rc})"))

    print("EVIDENCE")
    for name, outcome in signals:
        print(f"    {name:16} {outcome}")
    print(f"    {'mutating source':16} {mut_source}")

    # ── the classification ──────────────────────────────────────────────────
    # Ordered most-severe first; the first matching rule decides.
    changed = bool(git and git["changed_paths"] > 0)
    cannot_see_tree = git is None

    if definite and not changed and not cannot_see_tree:
        verdict = "SUSPICIOUS"
        why = (f"{sorted(definite)} ran but the tree is identical to HEAD. The "
               f"edits were reverted, targeted a path outside {args.repo}, were "
               f"blocked by a permission or sandbox layer, or did not happen. "
               f"Note the last two are indistinguishable from here — this is not "
               f"proof the model fabricated anything.")
    elif total_calls == 0:
        verdict = "UNVERIFIED"
        why = ("No tool calls were recorded, so there is nothing to verify. For "
               "a task that needed tools, the model answered from assumption.")
    elif cannot_see_tree:
        verdict = "UNVERIFIED"
        why = (f"{args.repo} is not a git repository, so no delta is available. "
               f"Cannot verify is NOT the same as verified clean.")
    elif test_rc not in (None, 0):
        verdict = "PARTIALLY_VERIFIED"
        why = (f"Work reached the filesystem, but the test command exited "
               f"{test_rc}. Something happened; it does not yet hold.")
    elif errored:
        verdict = "PARTIALLY_VERIFIED"
        why = (f"{errored} tool call(s) errored. Check whether the model noticed "
               f"or narrated success over them.")
    elif changed and test_rc == 0:
        verdict = "VERIFIED"
        why = ("Mutating tools ran, the tree changed, and the supplied test "
               "passed. This is the only combination that earns VERIFIED.")
    elif changed:
        verdict = "PARTIALLY_VERIFIED"
        why = ("The tree changed and no tool errored, but no test was run, so "
               "correctness is unestablished. Pass --test to reach VERIFIED.")
    elif unknown:
        verdict = "UNVERIFIED"
        why = (f"{unknown} result(s) have no success/failure signal on this "
               f"route, and the tree did not change. Absence of an error flag "
               f"is not success.")
    else:
        verdict = "UNVERIFIED"
        why = ("A read-only session with no tree change. Nothing was claimed to "
               "change, so there is nothing to confirm.")

    print()
    print(f"CLASSIFICATION  {verdict}")
    for line in _wrap(why):
        print(f"    {line}")

    if ambiguous_only and not changed:
        print()
        print("    Not counted as suspicious: the only mutating tools used "
              "were ambiguous ones")
        print("    (a shell command can legitimately be read-only).")

    print()
    print("This compares a claim against a tree state. It does not establish "
          "that this")
    print("session caused the delta, nor that the change was correct — only "
          "whether")
    print("something plausibly happened at all.")

    # Stable for scripting; UNVERIFIED is deliberately non-zero (see docstring).
    return {"VERIFIED": 0, "PARTIALLY_VERIFIED": 0,
            "SUSPICIOUS": 2, "UNVERIFIED": 3}[verdict]


def _wrap(text, width=68):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line); line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())
