# Red-Green Mutation Test Status

**Last Updated:** 2026-10-16  
**Commit:** `1813aa5`  
**Status:** 96/112 passed, 16 failing

## Failing Mutations

| # | ID | Description | Status | Classification | Action Required |
|---|-----|-------------|--------|----------------|-----------------|
| 5 | - | Idempotent retry (no double-charge) | ⚠ INVALID | No file change | Code moved or deleted - needs retargeting |
| 11 | - | API rejects invalid/missing secrets | ⚠ INVALID | No file change | Code moved or deleted - needs retargeting |
| 23 | - | G4.09 picture lock blocks audio | ⚠ INVALID | No file change | Code moved or deleted - needs retargeting |
| 24 | - | GX.01 HOLD prevents auto-post | ⚠ INVALID | No file change | Code moved or deleted - needs retargeting |
| 66 | M-P3c | Lint is enforced in live mode | ✗ Non-AssertionError | Test failing incorrectly | Fix test or mutation pattern |
| 86 | M-R4r | Workflow passes missing refs to still | ✅ FIXED | Used stable anchor | Retargeted with comment anchor |
| 88 | M-R4t | Workflow uses the shot's model | ✅ FIXED | Used stable anchor | Retargeted with comment anchor |
| 89 | M-R4u | Workflow passes shot tags (wet) to clip | ✅ FIXED | Used stable anchor | Retargeted with line+context |
| 91 | M-R4w | Dry end-frame shot gets placeholder | ⚠ INVALID | No file change | Code moved - needs retargeting |
| 92 | M-R4x | End-image signal stores the URL | ✗ Mutation ineffective | Test not catching | Fix or restore test |
| 93 | M-R4y | Live end-frame shot waits for end image | ⚠ INVALID | No file change | Code moved - needs retargeting |
| 94 | M-R4z | Workflow passes end image to clip | ⚠ INVALID | No file change | Code moved - needs retargeting |
| 95 | M-R4A | G2.01 triggers when shot has missing refs | ✗ Mutation ineffective | Test not catching | Fix or restore test |
| 96 | M-R4B | Live G2.01 holds while refs missing | ⚠ INVALID | No file change | Code moved - needs retargeting |
| 97 | M-R4C | G2.01 recheck updates shot plans | ⚠ INVALID | No file change | Code moved - needs retargeting |
| 98 | M-R4D | Episode workflow loads per-shot data | ✗ Mutation ineffective | Test not catching | Fix or restore test |

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
