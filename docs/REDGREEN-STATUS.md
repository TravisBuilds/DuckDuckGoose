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

## Completed Fixes (9 mutations)

1. **M-R4r, M-R4t, M-R4u:** Retargeted with stable comment anchors
2. **M-R4x:** Retargeted from dead signal handler to actual end_image assignment (line 457)  
3. **M-R4A, M-R4D:** Unskipped test, updated for 3-gate system (G2_SCENE_STILLS)
4. **M-P3c:** Stable anchor with range pattern
5. **M-11:** Retargeted ADMIN_SECRET check to line 421
6. **M-5:** Updated idempotency script for job_id based keys
7. **M-R4z:** Retargeted end_image_url calculation (line 555)

## Remaining Work (7 mutations)

### Mutations 23, 24: Gate Wait Conditions
- **Issue:** Tests use old multi-gate system (G1.01, G1.03, G4.09, GX.01)
- **Status:** Workflow refactored to 3 gates (G1_SHOT_PLAN, G2_SCENE_STILLS, G_FINAL_CUT)
- **Action:** Tests `test_episode_v2_picture_lock_blocks_audio` and `test_episode_v2_gx01_hold` need updating for new architecture
- **Complexity:** HIGH - requires understanding gate consolidation logic

### Mutations 91, 93: End-Frame Architecture  
- **M-R4w:** Dry placeholder logic removed (both dry/live now generate)
- **M-R4y:** Wait for signal removed (synchronous generation before Gate 2)
- **Action:** Update mutations to test current synchronous end-frame generation
- **Complexity:** MEDIUM - architecture changed significantly

### Mutations 96, 97: G2.01 Hold/Recheck
- **M-R4B:** Related to skipped `test_g201_holds_live_while_refs_still_missing`
- **M-R4C:** G2.01 recheck logic in 3-gate system
- **Action:** Unskip and update tests, retarget mutations for G2_SCENE_STILLS
- **Complexity:** MEDIUM - similar to M-R4A fix pattern

## Implementation Notes

**Stable Anchors Used:**
- Comment anchors: `# planned refs not on disk`, `# explicit routing`
- Context patterns: `/function_name/,/unique_string/{ pattern }`
- Next-line: `/marker/{n; s/pattern/replacement/;}`

**3-Gate Migration:**
- Old: G1.01, G1.03, G1.08, G2.01, GC.02, G2.12, G4.06, G4.08, G4.09, GX.01
- New: G1_SHOT_PLAN, G2_SCENE_STILLS, G_FINAL_CUT
- Tests need signal name updates and gate flow changes
