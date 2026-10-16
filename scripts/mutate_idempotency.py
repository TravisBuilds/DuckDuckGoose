#!/usr/bin/env python3
"""Mutation script for idempotency key test"""
import sys

file_path = sys.argv[1]
with open(file_path) as f:
    content = f.read()

# Replace idempotency_key=job_id with None to break deterministic idempotency
content = content.replace(
    'idempotency_key=job_id,  # Use job_id as provider idempotency key',
    'idempotency_key=None,  # MUTATED'
)

with open(file_path, 'w') as f:
    f.write(content)

print("Mutated idempotency key")
