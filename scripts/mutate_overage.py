#!/usr/bin/env python3
"""Mutate overage commit to spend reserved_amount instead of actual_cost (mutation #27).

Anchors on the 'Overage: commit actual cost' comment (no line-number windows) and rewrites
the first UPDATE parameter tuple after it. Exits non-zero (and leaves the file untouched)
if the anchor or the target tuple is not found, so a stale mutation cannot pass silently.
"""
import sys


def mutate_file(filepath: str) -> int:
    with open(filepath) as f:
        content = f.read()
    anchor = "Overage: commit actual cost"
    target = "(reserved_amount, actual_cost, line_id)"
    idx = content.find(anchor)
    if idx < 0:
        print("anchor not found", file=sys.stderr)
        return 2
    tidx = content.find(target, idx)
    if tidx < 0:
        print("target tuple not found", file=sys.stderr)
        return 2
    # MUTATED: spend reserved_amount where actual_cost belongs (no trailing comment: the
    # tuple is followed by the closing paren of execute() on the same line)
    mutated = "(reserved_amount, reserved_amount, line_id)"
    content = content[:tidx] + mutated + content[tidx + len(target):]
    with open(filepath, "w") as f:
        f.write(content)
    return 0


if __name__ == "__main__":
    sys.exit(mutate_file(sys.argv[1]))
