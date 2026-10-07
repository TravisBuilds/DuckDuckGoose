#!/usr/bin/env python3
"""Mutate overage commit to use reserved_amount (for mutation #27)."""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # In the overage path, replace actual_cost with reserved_amount in the commit
    # This is around line 298, in the overage branch (after "Overage: commit actual cost")
    lines = content.split('\n')
    new_lines = []
    in_overage = False
    
    for i, line in enumerate(lines):
        # Detect overage section by looking for the comment
        if 'Overage: commit actual cost' in line:
            in_overage = True
        
        # Check if this is the parameter line with actual_cost after we found the overage section
        if in_overage and i > 290 and i < 310 and 'actual_cost' in line and 'overage = actual_cost' not in line and 'spent + ?' not in line:
            # Replace actual_cost with reserved_amount in the parameter tuple
            # Add comment at the end of the line instead of inline
            new_lines.append(line.replace('actual_cost', 'reserved_amount') + '  # MUTATED: should use actual_cost')
            in_overage = False
        else:
            new_lines.append(line)
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
