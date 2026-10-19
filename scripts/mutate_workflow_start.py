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
    
    # Find the pattern in the canary route (around line 1141, in run_canary function)
    lines = content.split('\n')
    new_lines = []
    i = 0
    in_canary_route = False
    while i < len(lines):
        line = lines[i]
        # Track when we're in the canary route
        if 'async def run_canary(' in line:
            in_canary_route = True
        elif in_canary_route and line.strip().startswith('async def '):
            # We've entered a new function, no longer in canary
            in_canary_route = False
        
        # Look for the workflow start in canary route
        if in_canary_route and 'handle = await temporal_client.start_workflow(' in line:
            # This is the canary route workflow start
            # Skip this line and subsequent lines until we find the closing )
            indent = len(line) - len(line.lstrip())
            new_lines.append(' ' * indent + 'handle = None  # MUTATED: workflow start skipped')
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
