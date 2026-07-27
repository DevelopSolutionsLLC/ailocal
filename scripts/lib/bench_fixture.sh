#!/usr/bin/env bash
# bench_fixture.sh — controlled repo fixtures for the model benchmark.
#
# Kept separate from the harness so the WORKLOAD and the MEASUREMENT can change
# independently: a fixture edit must never silently alter what is being measured
# about a model.
#
# The multi-file fixture is deliberately built so the bug CANNOT be fixed from
# one file. The wrong value originates in pricing.py, is passed through
# checkout.py, and only fails visibly in the test. A model that greps for the
# failing assertion and patches the nearest line produces a wrong fix that still
# looks plausible — which is exactly the behaviour worth measuring.
set -uo pipefail

fixture_multifile() { # $1 = target dir
  local d="$1"
  rm -rf "$d"; mkdir -p "$d/src" "$d/tests" "$d/docs"

  cat > "$d/src/pricing.py" <<'EOF'
"""Price calculation."""

TAX_RATE = 0.20


def line_total(unit_price: float, quantity: int) -> float:
    """Total for one order line, before tax."""
    return unit_price * quantity


def with_tax(amount: float) -> float:
    """Apply tax to an amount."""
    # BUG: returns only the tax, not the taxed amount.
    return amount * TAX_RATE
EOF

  cat > "$d/src/checkout.py" <<'EOF'
"""Checkout flow, built on pricing."""
from pricing import line_total, with_tax


def order_total(lines):
    """lines: [(unit_price, quantity)] -> total including tax."""
    subtotal = sum(line_total(p, q) for p, q in lines)
    return with_tax(subtotal)
EOF

  cat > "$d/tests/test_checkout.py" <<'EOF'
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
from checkout import order_total


class T(unittest.TestCase):
    def test_order_total(self):
        # 2 x 10.00 = 20.00 subtotal, +20% tax = 24.00
        self.assertAlmostEqual(order_total([(10.00, 2)]), 24.00)


if __name__ == "__main__":
    unittest.main()
EOF

  cat > "$d/docs/pricing.md" <<'EOF'
# Pricing

`line_total` computes a line before tax.
`with_tax` must return the amount INCLUDING tax, not the tax alone.
`order_total` sums the lines and applies tax once, at the end.
EOF

  cat > "$d/run_tests.sh" <<'EOF'
#!/bin/sh
cd "$(dirname "$0")" && python3 tests/test_checkout.py 2>&1
EOF
  chmod +x "$d/run_tests.sh"
  ( cd "$d" && git init -q && git add -A \
    && git -c user.email=b@b -c user.name=b commit -qm init )
}

# Assert the fixture fails for the RIGHT reason before any model touches it.
# A fixture that errors instead of failing an assertion would make every model
# look broken.
fixture_precondition() { # $1 = dir
  local out
  out="$(cd "$1" && ./run_tests.sh 2>&1)"
  printf '%s' "$out" | grep -q AssertionError && return 0
  echo "FIXTURE BROKEN (expected an AssertionError, got): $(printf '%s' "$out" | tr '\n' ' ' | tail -c 120)"
  return 1
}
