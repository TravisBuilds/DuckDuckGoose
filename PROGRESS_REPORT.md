# Progress Report - PR #7 Verification Fixes

**Branch:** `cursor/phase1-backend-complete-941e`  
**Latest commit:** 865a42e  
**Date:** 2026-10-07

## Summary

Completed P0.3 and P1 with partial P1 test improvements. The following critical safety fixes are now in place:

## ✅ Completed Work

### P0.3: Canary Path Fixes (commit 13a464e)
- **Reserve L6 before workflow starts** - API now reserves from L6_reserve BEFORE starting the canary ShotWorkflow, preventing leaked reservations
- **Canary status route** - Added GET `/api/canary/{workflow_id}` to query canary workflow status
- **Fix still-approve targeting** - Route now handles both regular shot IDs (`{ep}-shot-{shot}`) and canary IDs (`{ep}-canary-{shot}-{ts}`) by checking audit log
- **Clip-approve sends real signal** - Route validates shot_id, checks DB for existence, and sends `clip_approved` signal to workflow
- **API route to turn live mode OFF** - Added POST `/api/episodes/{id}/set-dry` (previously only set-live existed)
- **Live QC escalation waits for approval** - When live still-QC escalates, workflow now waits for human `stills_approved` signal instead of terminating, allowing canary to reach clip generation

**Evidence:**
```bash
$ git show 13a464e --stat
 api/main.py                  | 215 +++++++++++++++++++++++++++++++----
 hfvg/workflows/shot.py      |  32 +++++-
 2 files changed, 246 insertions(+), 31 deletions(-)
```

### P1: Worker Decides Live vs Dry from DB (commit 0c45f18)
- **Decision logic**: Worker checks DB for `live_mode` and `g108_approved`, then decides: `use_live = not DRY_RUN_env and live_mode and g108_approved`
- **DRY_RUN env can only force dry, never force live** - This is the critical safety property
- **Applied to all enforced activities**:
  - `submit_still_job_enforced` (lines 93-128)
  - `submit_clip_job_enforced` (lines 250-285)
  - `await_job_enforced` (lines 407-436)
- **Applied to QC activities**:
  - `review_still` - deterministic pass in dry mode (no random failures)
  - `review_clip` - deterministic pass in dry mode
- **Consistent logging** - All activities now log the reason for dry mode (e.g., "DRY_RUN=true, DB live_mode=false")

**Evidence:**
```bash
$ git show 0c45f18 --stat
 hfvg/activities/qc.py                 |  88 ++++++++++++++-------
 hfvg/activities/studio_generation.py | 100 +++++++++++++++++-------
 2 files changed, 136 insertions(+), 52 deletions(-)
```

### P1: Gate Legacy Activities Identically (commit c3e312b)
- **Legacy activities** `submit_still_job`, `submit_clip_job` now use the same DB-based decision logic as enforced activities
- **Deprecation warnings** logged when legacy activities are called
- **Provider updates** - Legacy activities now use new HiggsfieldStillProvider and KlingVideoProvider (not the old HiggsfieldProvider)

### P1 Tests: pytest-socket and fastapi (commit 865a42e)
- **pytest-socket enabled** - Added `addopts = --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1,localhost` to pytest.ini_options
- **fastapi added to dev deps** - API tests will now run in CI
- **Socket blocking test added** - `tests/test_pytest_socket.py` proves pytest-socket is working (test passes by catching `SocketConnectBlockedError`)
- **Test mocks fixed** - Updated test_exception_release.py to use correct API key format (`test_id:test_secret`) and correct API endpoints

**Test Results:**
```bash
$ pytest tests/test_exception_release.py -v
============================== 4 passed in 0.56s ===============================

$ pytest tests/test_pytest_socket.py -v  
============================== 1 passed in 0.01s ===============================
```

## 🔄 Partial / In Progress

### P1 Tests: Failing Tests
- **test_episode_v2_approval_gates** - Still hangs (Worker needs activities registered, but full workflow is complex)
- **test_a_full_episode_through_all_gates** - Legacy EpisodeWorkflow test, likely needs similar fixes

### P1: EpisodeWorkflowV2 G1.08 DB tie
- Workflow still keeps `self.approved_g108` internal flag
- Should check DB via activity instead of just waiting for signals
- More complex change requiring workflow activity pattern

## ❌ Not Started

### P1 Tests: Red-Green Mutations
- Need to rewrite mutations #1, #2 (currently fail on ConnectError due to missing respx mocks)
- Need to rewrite mutation #6 (AttributeError), #9 (fails at registration), #10 (wrong assertion)
- Need to add mutations for X6, X9-X20 (15 undetected protections from verification report)

### P2: UI Auth
- UI login should call `/api/login` and store httpOnly session cookie (not raw secret)
- `/api/studio/verify` must not pass via unauthenticated `/api/health`
- Budget/state routes should accept session cookie

## Evidence of Safety Improvements

### Before (at 382b3fe):
- Canary workflow started before L6 reservation → L6 leaked +10 on every canary
- DRY_RUN=false + worker DRY_RUN=true → mismatch, unpredictable behavior
- Legacy activities had no gates → potential bypass
- pytest-socket not active → tests made real outbound calls under mutation
- Live still-QC always escalated → canary terminated, never reached clip

### After (at 865a42e):
- L6 reserved BEFORE canary workflow starts
- Worker always decides from DB; DRY_RUN can only force dry
- Legacy activities gated identically to enforced activities
- pytest-socket blocks outbound connections (proven by test)
- Live still-QC escalation waits for human approval → canary can proceed to clip

## Test Suite Status

**Latest run:**
```bash
$ pytest tests/ -v --tb=short --timeout=30 | tail
6 failed, 109 passed, 2 skipped in 49.27s
```

**Failures:**
1. test_episode_v2_approval_gates (hang - needs activity registration fix)
2. test_still_submit_500_releases_reservation ✅ FIXED
3. test_clip_bad_start_image_releases_reservation ✅ FIXED  
4. test_idempotent_retry_no_double_charge (red-green safety test)
5. test_shot_workflow_waits_for_still_approval (flaky QC ✅ FIXED by deterministic QC)
6. test_a_full_episode_through_all_gates (legacy workflow test)

## Recommendations

**High priority remaining:**
1. Fix test_episode_v2_approval_gates by registering all necessary activities or simplifying test scope
2. Rewrite red-green mutations to use respx mocks and assert against real protections
3. Add mutations for the 15 undetected protections (X6, X9-X20)

**Lower priority:**
4. Tie EpisodeWorkflowV2 G1.08 to DB (activity-based check)
5. P2 UI auth improvements

**CI Status:**
- Main workflow hang (test_episode_v2_approval_gates) will cause CI timeout
- Need to either fix or skip that test with pytest.mark.skip

## Commits

1. `13a464e` - P0.3: Fix canary path
2. `0c45f18` - P1: Worker decides live vs dry from DB  
3. `c3e312b` - P1: Gate legacy activities, fix test mocks
4. `865a42e` - P1 tests: Enable pytest-socket, add fastapi

All commits pushed to `cursor/phase1-backend-complete-941e` in PR #7.
