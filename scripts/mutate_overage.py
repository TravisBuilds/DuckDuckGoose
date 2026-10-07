#!/usr/bin/env python3
"""Mutate overage commit to use reserved_amount (for mutation #27)."""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # In the overage path, replace actual_cost with reserved_amount in the commit
    # This is around line 299, in the overage branch (after line 283)
    lines = content.split('\n')
    new_lines = []
    in_overage = False
    
    for i, line in enumerate(lines):
        # Detect overage section
        if '# Overage: commit actual cost' in line or 'overage = actual_cost - reserved_amount' in line:
            in_overage = True
        
        # If we're in overage and find the commit VALUES line with actual_cost
        if in_overage and 'VALUES (?, ?, ?, \'commit\',' in line and i < 310:
            # This is line 299 - replace actual_cost with reserved_amount
            new_lines.append(line.replace('actual_cost', 'reserved_amount  # MUTATED'))
            in_overage = False  # Only mutate the first occurrence
        else:
            new_lines.append(line)
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
