#!/usr/bin/env python3
"""Mutate stale balance check to always pass (mutation #394).

The stale balance check should block when age > max_age_hours. This mutation
bypasses the check, making stale balances appear fresh.
"""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Find the stale balance check in check_preflight_balance
    # Original:
    #     if age_hours > HIGGSFIELD_BALANCE_MAX_AGE_HOURS:
    #         if not auto_topup_enabled:
    #             return False, (...)
    # Mutated:
    #     if False:  # MUTATED: stale balance check bypassed
    #         ...
    
    lines = content.split('\n')
    new_lines = []
    
    for line in lines:
        if 'if age_hours > HIGGSFIELD_BALANCE_MAX_AGE_HOURS:' in line:
            indent = len(line) - len(line.lstrip())
            new_lines.append(' ' * indent + 'if False:  # MUTATED: stale balance check bypassed')
        else:
            new_lines.append(line)
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
