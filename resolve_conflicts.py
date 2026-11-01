#!/usr/bin/env python3
"""
Resolve git 3-way merge conflicts by intelligently merging both sides.
"""
import re
import sys

def resolve_file_conflicts(filepath):
    """Read file, resolve conflicts, return resolved content."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Pattern to match conflict blocks
    conflict_pattern = re.compile(
        r'<<<<<<< ours\n(.*?)\n=======\n(.*?)\n>>>>>>> theirs',
        re.DOTALL
    )
    
    def resolve_conflict(match):
        ours = match.group(1)
        theirs = match.group(2)
        
        # If one side is empty or whitespace-only, keep the other
        if not ours.strip():
            return theirs
        if not theirs.strip():
            return ours
        
        # If both sides are identical, keep one
        if ours == theirs:
            return ours
        
        # For imports, merge unique imports
        if 'import ' in ours or 'import ' in theirs:
            ours_lines = set(l.strip() for l in ours.split('\n') if l.strip())
            theirs_lines = set(l.strip() for l in theirs.split('\n') if l.strip())
            all_imports = sorted(ours_lines | theirs_lines)
            return '\n'.join(all_imports)
        
        # Default: keep both, with ours first, separated by a newline if needed
        # This preserves both behaviors as instructed
        if ours.strip() and theirs.strip():
            # If they look like separate route definitions or functions, keep both
            if ('@app.' in ours and '@app.' in theirs) or ('def ' in ours and 'def ' in theirs):
                return f"{ours}\n\n{theirs}"
            # If they're both code blocks, keep both
            return f"{ours}\n{theirs}"
        return ours + theirs
    
    resolved = conflict_pattern.sub(resolve_conflict, content)
    
    # Check if any conflicts remain
    if '<<<<<<< ours' in resolved:
        return None  # Couldn't fully resolve
    
    return resolved

if __name__ == '__main__':
    filepath = sys.argv[1]
    resolved = resolve_file_conflicts(filepath)
    if resolved is None:
        print(f"Could not fully resolve {filepath}", file=sys.stderr)
        sys.exit(1)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(resolved)
    print(f"Resolved {filepath}")
