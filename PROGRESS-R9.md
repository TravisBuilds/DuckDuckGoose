# Round 9 Progress Checklist

## Defects
- [x] 1. GAP-30, GAP-31, delivery v3 (DELIVERY_VERSION=4, codec updates, duck_mode, 3 test files added)
- [x] 2. Mutation 333 fix (preserve suffix, remove try/except)
- [x] 3. POST /api/episodes/{id}/assembly/rebuild route
- [x] 4. clip_estimate resolution for Seedance
- [x] 12. 403 handling resumable (body match, workflow pause)
- [x] 11. Balance gate scoping (provider-specific, DRY_RUN guard, remove default key, stale=warn)
- [x] 8. config.get_higgsfield fixes (use real env vars, log redacted exceptions)
- [x] 10. budget.py Higgsfield-only gating
- [x] 13. Notifier fixes (dedupe key, record after send, wire/remove unused, schedule/redaction)
- [x] 14. `hfvg balance` fresh DB and DRY_RUN tests
- [x] 6. shot.py clip_path for non-escalated QC-fail
- [ ] 5. Edit recipe partial (fps, recipe, tail_hold_s, dissolves, dry-run)
- [ ] 7. Duplicates removal (episode_v2.py and audio_mix.py)
- [ ] 9. test path fixture (appears already fixed or different issue)

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
