#!/usr/bin/env python3
"""Mutate alert deduplication to always send (mutation #395).

Alert deduplication should suppress duplicate alerts within the window. This
mutation bypasses deduplication, allowing alert spam.
"""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Find the deduplication check in _send_alert
    # Original:
    #     if await self._is_recent_duplicate(dedupe_key):
    #         return
    # Mutated:
    #     if False:  # MUTATED: alert deduplication bypassed
    #         return
    
    lines = content.split('\n')
    new_lines = []
    
    for line in lines:
        if 'if await self._is_recent_duplicate(dedupe_key):' in line:
            indent = len(line) - len(line.lstrip())
            new_lines.append(' ' * indent + 'if False:  # MUTATED: alert deduplication bypassed')
        else:
            new_lines.append(line)
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
