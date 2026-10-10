#!/usr/bin/env python3
"""Mutate probe P1 to cause AttributeError by deleting method definition."""

import sys

if len(sys.argv) != 2:
    print("Usage: mutate_probe_p1.py <file>")
    sys.exit(1)

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    lines = f.readlines()

# Delete the working_method definition
new_lines = []
skip_next = False
for line in lines:
    if 'def working_method(self):' in line:
        # Skip this line and the next 2 lines (docstring and return)
        skip_next = 2
        continue
    if skip_next > 0:
        skip_next -= 1
        continue
    new_lines.append(line)

with open(filepath, 'w') as f:
    f.writelines(new_lines)

print("Mutated probe P1: deleted working_method")
