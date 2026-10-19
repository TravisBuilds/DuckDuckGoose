#!/usr/bin/env python3
"""Mutate BudgetLedger.release: drop the clamp to the amount actually reserved.

Original:  release_amount = min(amount, row[0])
Mutated:   release_amount = amount   (reserved can go negative)

Exits non-zero (file untouched) if the target line is not found, so a stale mutation
cannot pass silently. Used by redgreen mutation 26 and M-P1c.
"""
import sys

TARGET = "release_amount = min(amount, row[0])"
MUTATED = "release_amount = amount  # MUTATED: no clamp to the hold"

if len(sys.argv) != 2:
    print("Usage: mutate_release_check.py <file>", file=sys.stderr)
    sys.exit(1)

path = sys.argv[1]
content = open(path).read()
if content.count(TARGET) != 1:
    print(f"expected exactly one {TARGET!r}, found {content.count(TARGET)}", file=sys.stderr)
    sys.exit(2)
open(path, "w").write(content.replace(TARGET, MUTATED))
print("Mutated release: clamp to reserved removed")
