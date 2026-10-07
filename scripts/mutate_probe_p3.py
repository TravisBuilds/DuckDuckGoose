#!/usr/bin/env python3
"""Mutate probe P3 to raise RuntimeError during AssertionError handling."""

import sys

if len(sys.argv) != 2:
    print("Usage: mutate_probe_p3.py <file>")
    sys.exit(1)

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# Replace the simple assertion with try/except raising RuntimeError
old = '''    value = 100
    # This will be mutated to raise RuntimeError during except
    assert value == 100, "Value should be 100"'''

new = '''    value = 100
    # MUTATED: raise RuntimeError during except
    try:
        assert value != 100, "Force assertion to fail"
    except AssertionError:
        raise RuntimeError("Error during handling of AssertionError")'''

content = content.replace(old, new)

with open(filepath, 'w') as f:
    f.write(content)

print("Mutated probe P3")
