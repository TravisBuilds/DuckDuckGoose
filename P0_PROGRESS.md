# P0 Critical Fixes Progress

## Completed (Committed)

### P0.1a: Atomic Reserve + Episode Cap (commit e67b6aa)
- ✅ `budget.py reserve()`: BEGIN IMMEDIATE + conditional UPDATE (prevents concurrent double-reserve)
- ✅ 1,250 episode cap enforced (GC.04)  
- ✅ `commit()` signature changed to (reserved_amount, actual_cost)
- ✅ Overage handling: if actual > reserved, commits reserved and logs overage_warning
- ✅ Remainder handling: if actual < reserved, commits actual and releases remainder

**Evidence:**
```bash
# Atomic reserve prevents race
cd /workspace && grep -A 30 "async def reserve" hfvg/budget.py | grep -A 3 "BEGIN IMMEDIATE"
# Episode cap check
grep "EPISODE_CAP = 1250" hfvg/budget.py
```

### P0.1b: Remove Automatic Retry (commit 10e9fd2)
- ✅ `shot.py`: RetryPolicy maximum_attempts 3→1
- ✅ Removed still QC retry loop (line 170: `self.version += 1`)
- ✅ Removed clip QC retry loop (line 280)
- ✅ QC failure without escalate returns terminal failed status

**Evidence:**
```bash
grep "maximum_attempts=1" hfvg/workflows/shot.py
grep -A 5 "QC failed without escalation" hfvg/workflows/shot.py
```

## In Progress

### P0.1c: Release on Exception Paths
Need to wrap all paid calls in try/except and release reservations on:
- Submit exceptions (after reserve, before/during provider call)
- Poll exceptions (during await_job_enforced)
- Activity timeout/cancel (CancelledError handler)
- Workflow failure/termination

Files to update:
- `hfvg/activities/studio_generation.py`: submit_still_job_enforced, submit_clip_job_enforced
- `hfvg/activities/studio_generation.py`: await_job_enforced (update commit call signature)

### P0.2: Provider API Fixes
Current API is wrong:
- ❌ Using `Authorization: Bearer {key}` (should be `Key <id>:<secret>`)
- ❌ Endpoints: `/v1/generate/image`, `/v1/jobs/{id}` (should be `/{model_path}`, `/requests/{id}/status`)
- ❌ Response: `job_id` (should be `request_id`)
- ❌ Hard-coded cost estimates (should use real provider estimates)

Need to create new providers matching documented API or use `higgsfield-client`.

### P0.3: Canary Path Fixes
- ❌ L6 reserve happens AFTER workflow starts (api/main.py:955-969)
- ❌ L6 reservation never released
- ❌ No GET /api/canary/<id> status route
- ❌ still-approve targets wrong workflow ID (`ep04-shot-A01` vs `ep04-canary-A01-{ts}`)
- ❌ clip-approve doesn't send signal
- ❌ No POST /api/episodes/{id}/set-dry route
- ❌ Live still-QC always escalates, workflow terminates (can't reach clip)

## Next Steps

1. Fix exception handling in activities (P0.1c)
2. Update await_job to use new commit signature
3. Test atomic reserve with concurrent scenario
4. Move to P0.2 provider API fixes
5. Fix canary path issues (P0.3)

## Testing Strategy

All tests run with DRY_RUN=true and no real keys to maintain zero-spend.

Test atomic reserve:
```python
# Two concurrent reserves of 50 against stop 80 - both should not succeed
# One should get True, other should get False (conditional UPDATE)
```

Test episode cap:
```python
# Reserve 1,250 total across lines - should succeed
# Reserve 1 more - should raise ValueError with "1,250 credit cap"
```

Test overage handling:
```python
# Reserve 10, commit with actual_cost=12
# Check: reserved goes -10+10=0, spent goes +10, overage_warning logged
```
