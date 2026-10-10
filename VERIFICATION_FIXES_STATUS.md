# Verification Report Fixes - Status

**Branch:** cursor/phase1-backend-complete-941e  
**HEAD:** 10e9fd2  
**Completed:** 2/39 fixes (5%)

## ✅ Completed

### P0.1a: Atomic Reserve + Episode Cap (e67b6aa)
- BEGIN IMMEDIATE + conditional UPDATE (prevents race)
- 1,250 episode cap enforced (GC.04)
- commit(reserved_amount, actual_cost) handles overage

### P0.1b: Remove Retry (10e9fd2)
- RetryPolicy maximum_attempts 3→1
- Removed QC retry loops (shot.py:170, 280)

## ❌ Critical Remaining (Blocks Paid Canary)

### P0.1c: Release on Exceptions
Need try/except around submit/poll to release reservations on errors.

### P0.2: Provider API Rewrite
Current uses wrong API. Need:
- Auth: `Key <id>:<secret>` (not Bearer)
- Endpoints: `/{model_path}`, `/requests/{id}/status`
- Response: `request_id` (not `job_id`)

### P0.3: Canary Path (8 issues)
- L6 reserve leaks (never released)
- still-approve targets wrong workflow ID
- clip-approve doesn't send signal
- No live mode OFF route
- Live QC terminates (can't reach clip)

## Estimate
15-20 hours remaining for complete verification report fixes.

See PHASE1_FIXES.md and P0_PROGRESS.md for details.
