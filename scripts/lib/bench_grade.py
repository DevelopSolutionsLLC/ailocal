#!/usr/bin/env python3
"""Grade a model's code answer by APPLYING it and running the tests.

WHY NOT PATTERN MATCHING
The first version of this grader used a regex for the expected shape and scored
qwen3-coder as FAILING the multi-file task. The model had actually answered
`return amount * 1.2` — numerically correct (1 + TAX_RATE) — and the regex was
looking for a literal TAX_RATE reference. A capable model would have been
recorded as incapable.

So grading is behavioural: splice the returned function into the fixture and run
the real test suite. That is the same standard the rest of this project uses —
the filesystem and the test runner decide, not a string match.

It also distinguishes outcomes that a regex cannot:
  correct           the suite passes
  wrong             the suite fails on an assertion (a real, wrong answer)
  unusable          the answer would not even parse or apply (a formatting
                    failure, which is a DIFFERENT defect from a wrong answer)

Usage: bench_grade.py <fixture_dir> <target_file> <func_name>   (answer on stdin)
"""
import ast
import json
import re
import subprocess
import sys


def extract_code(text):
    """Pull code out of an answer that may or may not be fenced. Models are
    asked for a bare body and frequently fence it anyway; treating that as a
    failure would measure instruction-following, not coding."""
    fence = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    body = fence.group(1) if fence else text
    return body.strip()


def main():
    fixture, target, func = sys.argv[1], sys.argv[2], sys.argv[3]
    answer = sys.stdin.read()
    code = extract_code(answer)

    if not code:
        print(json.dumps({"grade": "unusable", "why": "empty answer"}))
        return

    path = f"{fixture}/{target}"
    original = open(path, encoding="utf-8").read()

    # If the answer is a full function def, replace the existing one. If it is
    # only a body, graft it onto the existing signature.
    if re.match(r"\s*def\s+" + re.escape(func), code):
        new_src = re.sub(r"def\s+" + re.escape(func) + r"\s*\([^)]*\)[^:]*:(?:\n(?:[ \t]+.*|\s*)*)",
                         code.rstrip() + "\n", original, count=1)
    else:
        indented = "\n".join("    " + l if l.strip() else l
                             for l in code.splitlines())
        new_src = re.sub(r"(def\s+" + re.escape(func) + r"\s*\([^)]*\)[^:]*:\n)(?:[ \t]+.*\n|\s*\n)*",
                         r"\1" + indented + "\n", original, count=1)

    try:
        ast.parse(new_src)
    except SyntaxError as exc:
        print(json.dumps({"grade": "unusable",
                          "why": f"patched file does not parse: {exc}"}))
        return

    open(path, "w", encoding="utf-8").write(new_src)
    try:
        p = subprocess.run(["./run_tests.sh"], cwd=fixture, capture_output=True,
                           text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0:
            grade, why = "correct", "test suite passes after applying the answer"
        elif "AssertionError" in out:
            grade, why = "wrong", "suite runs and fails on an assertion"
        else:
            # NOT graded as a wrong answer: the suite did not actually run.
            grade, why = "unusable", "suite did not run: " + out.strip()[-160:]
    except Exception as exc:
        grade, why = "unusable", f"{type(exc).__name__}: {exc}"
    finally:
        open(path, "w", encoding="utf-8").write(original)

    print(json.dumps({"grade": grade, "why": why, "applied": code[:200]}))


if __name__ == "__main__":
    main()
