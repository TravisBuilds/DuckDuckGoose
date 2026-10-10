#!/usr/bin/env python3
"""Mutate BudgetLedger.commit to spend the reserved amount instead of the actual cost.

Original:  spent_amount = actual
Mutated:   spent_amount = reserved_amount

Exits non-zero (file untouched) if the target line is not found, so a stale mutation
cannot pass silently. Used by redgreen mutation 27 and M-P1e.
"""
import sys

TARGET = "spent_amount = actual\n"
MUTATED = "spent_amount = reserved_amount  # MUTATED: overage not charged\n"


def mutate_file(filepath: str) -> int:
    with open(filepath) as f:
        content = f.read()
    if content.count(TARGET) != 1:
        print(f"expected exactly one {TARGET!r}, found {content.count(TARGET)}", file=sys.stderr)
        return 2
    with open(filepath, "w") as f:
        f.write(content.replace(TARGET, MUTATED))
    return 0


if __name__ == "__main__":
    sys.exit(mutate_file(sys.argv[1]))
