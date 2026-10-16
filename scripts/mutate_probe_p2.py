#!/usr/bin/env python3
"""Mutate probe P2 to cause respx unmocked request by changing mock URL."""

import sys

if len(sys.argv) != 2:
    print("Usage: mutate_probe_p2.py <file>")
    sys.exit(1)

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# Change only the mock registration, not the actual request
content = content.replace(
    'respx.get("http://example.com/api/data").mock(',
    'respx.get("http://example.com/api/WRONG").mock('
)

with open(filepath, 'w') as f:
    f.write(content)

print("Mutated probe P2: changed mock URL to cause unmocked request")
