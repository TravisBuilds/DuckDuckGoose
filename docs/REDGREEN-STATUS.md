# Red-Green Mutation Test Status

**Last Updated:** 2026-10-16  
**Commit:** `b0e9762`  
**Status:** ~102/112 passing (estimated), 10 remaining

## Failing Mutations

| # | ID | Description | Status | Classification | Action Required |
|---|-----|-------------|--------|----------------|-----------------|
| 5 | - | Idempotent retry (no double-charge) | ⚠ INVALID | No file change | Code moved or deleted - needs retargeting |
| 11 | - | API rejects invalid/missing secrets | ✅ FIXED | Stable anchor | Retargeted api/main.py line 421 |
| 23 | - | G4.09 picture lock blocks audio | ⚠ INVESTIGATE | 3-gate refactor | G4.09 collapsed into G_FINAL_CUT |
| 24 | - | GX.01 HOLD prevents auto-post | ⚠ INVESTIGATE | 3-gate refactor | GX.01 collapsed into G_FINAL_CUT |
| 66 | M-P3c | Lint is enforced in live mode | ✅ FIXED | Stable anchor | Range pattern with context |
| 86 | M-R4r | Workflow passes missing refs to still | ✅ FIXED | Stable anchor | Comment anchor 'planned refs' |
| 88 | M-R4t | Workflow uses the shot's model | ✅ FIXED | Stable anchor | Comment anchor 'explicit routing' |
| 89 | M-R4u | Workflow passes shot tags (wet) to clip | ✅ FIXED | Stable anchor | Content anchor (no line ctx) |
| 91 | M-R4w | Dry end-frame shot gets placeholder | ⚠ INVALID | No file change | Code refactored - needs locate |
| 92 | M-R4x | End-image URL stored from result | ✅ FIXED | Retargeted | Was dead signal, now line 457 |
| 93 | M-R4y | Live end-frame shot waits for end image | ⚠ INVALID | No file change | Code refactored - needs locate |
| 94 | M-R4z | Workflow passes end image to clip | ⚠ INVALID | No file change | Code refactored - needs locate |
| 95 | M-R4A | G2_SCENE_STILLS triggers for missing refs | ✅ FIXED | Test unskipped | Updated for 3-gate system |
| 96 | M-R4B | Live G2_SCENE_STILLS holds while refs missing | ⚠ INVESTIGATE | Test may be skipped | Related to M-R4A |
| 97 | M-R4C | G2_SCENE_STILLS recheck updates plans | ⚠ INVALID | No file change | Code refactored - needs locate |
| 98 | M-R4D | Episode workflow loads per-shot data | ✅ FIXED | Stable anchor | Retargeted parse_beatmap |

## Analysis

### Category 1: Code Moved/Deleted (needs retargeting)
- **Count:** 11 mutations
- **IDs:** 5, 11, 23, 24, 86, 88, 89, 91, 93, 94, 96, 97
- **Issue:** Sed patterns no longer match - code moved or was refactored
- **Solution:** Update mutation patterns to use stable function-scoped anchors

### Category 2: Mutation Ineffective (test not catching)
- **Count:** 4 mutations
- **IDs:** M-R4x, M-R4A, M-R4D, plus 1 other
- **Issue:** Mutation applies but test still passes
- **Solution:** Fix or restore tests to properly validate these behaviors

### Category 3: Test Failing Incorrectly
- **Count:** 1 mutation  
- **ID:** M-P3c
- **Issue:** Test fails with non-AssertionError
- **Solution:** Fix test to use proper assertion or fix mutation pattern

## Next Steps

1. Investigate each "No file change" mutation to find new code location
2. Update mutation patterns to use stable anchors (function context + unique snippet)
3. Fix tests for ineffective mutations
4. Validate all fixes with full redgreen run
