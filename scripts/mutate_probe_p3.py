#!/usr/bin/env python3
"""Mutate probe P3 to raise RuntimeError chained from AssertionError."""

import sys

if len(sys.argv) != 2:
    print("Usage: mutate_probe_p3.py <file>")
    sys.exit(1)

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# Replace the assertion with a chained exception
old = '''def test_probe_runtime_chained_from_assert():
    """Should pass normally, fail with RuntimeError when mutated (wrong reason)."""
    assert True, "assertion passes"'''

new = '''def test_probe_runtime_chained_from_assert():
    """Should pass normally, fail with RuntimeError when mutated (wrong reason)."""
    try:
        assert False, "assertion failed"
    except AssertionError:
        raise RuntimeError("Error during handling") from None'''

content = content.replace(old, new)

with open(filepath, 'w') as f:
    f.write(content)

print("Mutated probe P3")
