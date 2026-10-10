#!/usr/bin/env python3
"""Mutation script for idempotency key test"""
import sys

file_path = sys.argv[1]
with open(file_path) as f:
    content = f.read()

# Remove the idempotency key parameter passing - this will cause it to use random UUID fallback
content = content.replace(
    'idempotency_key=idempotency_key,',
    'idempotency_key=None,  # MUTATED'
)

with open(file_path, 'w') as f:
    f.write(content)

print("Mutated idempotency key")
