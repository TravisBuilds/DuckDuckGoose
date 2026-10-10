"""
Parse CREDIT-PLAN.md at runtime for budget caps and stops.

Format:
| Line | Plan | Budget / 80 % stop | Note |
|---|---|---|---|
| L1 refs (2k high) | 91 | 120 / 96 | ... |
| L2 drafts (1k medium) | 87.5 | 100 / 80 | ... |
| L3 final stills (2k high) | 227.5 | 230 / 184 | ... |
| L4 video (Kling 3.0 pro) | 229.4 | 300 / 240 | ... |
| L5 finalize / upscale | 0 | 0 | – |
| L6 reserve | 250 | 250 / 200 | ... |
| **Total Higgsfield** | **first pass 635.4 · 885.4 with reserve** | target 1,000 / cap 1,250 | |
"""

import re
from pathlib import Path
from typing import Any


def parse_credit_plan(plan_path: str | Path) -> dict[str, Any]:
    """
    Parse CREDIT-PLAN.md into budget configuration.
    
    Returns:
        {
            "lines": {
                "L1_refs": {"cap": 120, "stop": 96, "plan": 91},
                "L2_drafts": {"cap": 100, "stop": 80, "plan": 87.5},
                ...
            },
            "higgsfield_cap": 1250,
            "higgsfield_target": 1000,
            "stop_fraction": 0.8,
        }
    """
    content = Path(plan_path).read_text()
    
    lines = {}
    higgsfield_cap = 950.0  # Default: 1,250 app credits * 0.76 = 950 API credits
    higgsfield_target = 1000
    
    # Parse line rows
    # Format: | L1 refs (2k high) | 91 | 120 / 96 | ... |
    pattern = r'\|\s*(L\d+)\s+([^|]+?)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*/\s*([\d.]+)'
    
    for match in re.finditer(pattern, content, re.MULTILINE):
        line_id = match.group(1).strip()  # L1, L2, etc.
        line_desc = match.group(2).strip()  # "refs (2k high)"
        plan_value = float(match.group(3))
        cap_value = float(match.group(4))
        stop_value = float(match.group(5))
        
        # Convert line ID to snake_case name
        # L1 refs -> L1_refs
        # L2 drafts -> L2_drafts
        # L3 final stills -> L3_final_stills
        # L4 video -> L4_video
        # L5 finalize / upscale -> L5_finalize
        # L6 reserve -> L6_reserve
        
        if "ref" in line_desc:
            line_name = f"{line_id}_refs"
        elif "draft" in line_desc:
            line_name = f"{line_id}_drafts"
        elif "final" in line_desc:
            line_name = f"{line_id}_final_stills"
        elif "video" in line_desc:
            line_name = f"{line_id}_video"
        elif "finalize" in line_desc or "upscale" in line_desc:
            line_name = f"{line_id}_finalize"
        elif "reserve" in line_desc:
            line_name = f"{line_id}_reserve"
        else:
            # Fallback
            line_name = f"{line_id}_{line_desc.split()[0].lower()}"
        
        lines[line_name] = {
            "cap": cap_value,
            "stop": stop_value,
            "plan": plan_value,
        }
    
    # Parse total line
    # | **Total Higgsfield** | ... | target 1,000 / cap 1,250 | |
    total_pattern = r'target\s+([\d,]+)\s*/\s*cap\s+([\d,]+)'
    total_match = re.search(total_pattern, content)
    
    if total_match:
        higgsfield_target = float(total_match.group(1).replace(',', ''))
        higgsfield_cap = float(total_match.group(2).replace(',', ''))
    
    return {
        "lines": lines,
        "higgsfield_cap": higgsfield_cap,
        "higgsfield_target": higgsfield_target,
        "stop_fraction": 0.8,
    }
