# Round 9 Progress Checklist

## Defects
- [ ] 1. GAP-30, GAP-31, delivery v3 (IN PROGRESS - codec updates done, need: duck_mode in sfx, test files, bed_dips, subtitle_dur)
- [x] 2. Mutation 333 fix (preserve suffix, remove try/except)
- [ ] 3. POST /api/episodes/{id}/assembly/rebuild route
- [ ] 4. clip_estimate resolution for Seedance
- [ ] 12. 403 handling resumable (body match, workflow pause)
- [ ] 11. Balance gate scoping (provider-specific, DRY_RUN guard, remove default key, stale=warn)
- [ ] 8. config.get_higgsfield fixes (use real env vars, log redacted exceptions)
- [ ] 10. budget.py Higgsfield-only gating
- [ ] 13. Notifier fixes (dedupe key, record after send, wire/remove unused, schedule/redaction)
- [ ] 14. `hfvg balance` fresh DB and DRY_RUN tests
- [ ] 6. shot.py clip_path for non-escalated QC-fail
- [ ] 5. Edit recipe partial (fps, recipe, tail_hold_s, dissolves, dry-run)
- [ ] 7. Duplicates removal (episode_v2.py and audio_mix.py)
- [ ] 9. test_ep05full_edit_plan.py fixture

## GAPs
- [ ] GAP-32: Recompose meta/headcount endpoint
- [ ] GAP-33: Edit fps conform
- [ ] GAP-34: Bed dips
- [ ] GAP-35: Subtitle duration
- [ ] GAP-36: Kling reserve floor
- [ ] GAP-37: Unique recompose job ids
- [ ] GAP-38: Codec-guard heartbeat
- [ ] GAP-39: Recompose grant

## Final
- [ ] 15. Update PR body (accurate counts, CI truth, missing items)
- [ ] Full test suite run
- [ ] All 4 redgreen shards
