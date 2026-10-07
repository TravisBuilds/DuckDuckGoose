#!/usr/bin/env python3
"""Mutate workflow start to None (for mutation #16)."""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Find the canary workflow start and replace it with None
    # Original:
    #     handle = await temporal_client.start_workflow(
    #         ShotWorkflow.run,
    #         ...
    #     )
    # Mutated:
    #     handle = None  # MUTATED
    
    # Find the pattern in the canary route (around line 445)
    lines = content.split('\n')
    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for the workflow start in canary route
        if 'handle = await temporal_client.start_workflow(' in line and i > 400 and i < 500:
            # This is the canary route (first occurrence around line 445)
            # Skip this line and subsequent lines until we find the closing )
            new_lines.append('    handle = None  # MUTATED: workflow start skipped')
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
