#!/usr/bin/env python3
"""Mutate reserve call to True (for mutation #18)."""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Find the still activity reserve and replace it with True
    # Original:
    #     reserved = await ledger.reserve(
    #         episode_id=episode_id,
    #         ...
    #     )
    # Mutated:
    #     reserved = True  # MUTATED: reserve skipped
    
    lines = content.split('\n')
    new_lines = []
    i = 0
    found_first = False
    while i < len(lines):
        line = lines[i]
        # Look for the first reserve call (still activity, around line 167)
        if 'reserved = await ledger.reserve(' in line and not found_first:
            found_first = True
            new_lines.append(line.split('reserved')[0] + 'reserved = True  # MUTATED: reserve skipped')
            # Skip until we find the closing parenthesis
            depth = line.count('(') - line.count(')')
            i += 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count('(') - lines[i].count(')')
                i += 1
            continue
        new_lines.append(line)
        i += 1
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
