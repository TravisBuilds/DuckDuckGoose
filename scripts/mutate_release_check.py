#!/usr/bin/env python3
"""Mutate release check to remove reserved >= amount condition."""

import sys
import re

if len(sys.argv) != 2:
    print("Usage: mutate_release_check.py <file>")
    sys.exit(1)

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# Find the release function and mutate it to remove the reserved check
# The SQL query uses 3 parameters: (amount, line_id, amount)
# We need to change it to use 2 parameters and remove the check

# Original:
#     cursor = await db.execute("""
#         UPDATE budget_lines
#         SET reserved = reserved - ?
#         WHERE line_id = ? AND reserved >= ?
#     """, (amount, line_id, amount))
#
# Mutated:
#     cursor = await db.execute("""
#         UPDATE budget_lines
#         SET reserved = reserved - ?
#         WHERE line_id = ?
#     """, (amount, line_id))

pattern = r'(cursor = await db\.execute\("""[\s\S]*?UPDATE budget_lines[\s\S]*?SET reserved = reserved - \?[\s\S]*?)WHERE line_id = \? AND reserved >= \?[\s\S]*?""", \(amount, line_id, amount\)'
replacement = r'\1WHERE line_id = ?  -- MUTATED: removed reserved check\n                """, (amount, line_id)'

content = re.sub(pattern, replacement, content)

with open(filepath, 'w') as f:
    f.write(content)

print("Mutated release: removed reserved >= amount check")
