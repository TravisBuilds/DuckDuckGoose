#!/usr/bin/env python3
"""Mutate 403 Insufficient Balance handling to be retryable (mutation #393).

ElevenLabsError with permanent=True should be non-retryable. This mutation makes
it retryable by removing the permanent check.
"""
import sys

def mutate_file(filepath: str):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Find the ElevenLabsError permanent check in audio.py
    # Original:
    #     if isinstance(e, ElevenLabsError) and e.permanent:
    #         raise ApplicationError(..., non_retryable=True) from None
    # Mutated:
    #     if isinstance(e, ElevenLabsError) and False:  # MUTATED: permanent errors now retryable
    #         ...
    
    lines = content.split('\n')
    new_lines = []
    
    for line in lines:
        if 'if isinstance(e, ElevenLabsError) and e.permanent:' in line:
            indent = len(line) - len(line.lstrip())
            new_lines.append(' ' * indent + 'if isinstance(e, ElevenLabsError) and False:  # MUTATED: permanent errors now retryable')
        else:
            new_lines.append(line)
    
    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        mutate_file(sys.argv[1])
