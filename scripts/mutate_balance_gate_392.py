#!/usr/bin/env python3
"""Mutate preflight balance check to always pass (mutation #392).

The check_preflight_balance gate should block when balance < estimate + reserve.
This mutation makes it always return (True, "OK"), bypassing the safety check.
"""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Find check_preflight_balance and make it always pass
    # Original:
    #     if balance.balance_usd < required:
    #         if auto_topup_enabled:
    #             return True, (...)
    #         return False, (...)
    # Mutated:
    #     if False:  # MUTATED: balance check bypassed
    #         ...
    
    lines = content.split('\n')
    new_lines = []
    i = 0
    in_check_method = False
    
    while i < len(lines):
        line = lines[i]
        
        # Find the check_preflight_balance method
        if 'async def check_preflight_balance' in line:
            in_check_method = True
        
        # Find the balance sufficiency check
        if in_check_method and 'if balance.balance_usd < required:' in line:
            # Mutate the condition to False
            indent = len(line) - len(line.lstrip())
            new_lines.append(' ' * indent + 'if False:  # MUTATED: balance check bypassed')
            in_check_method = False
            i += 1
            continue
        
        new_lines.append(line)
        i += 1
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
