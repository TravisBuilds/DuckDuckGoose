# Status: PR #7 Verification Fixes - Phase 1 Complete

## Completed (Commits: 0c16c21, d5288e4, 6b11e38, de16b28)

### Issue 1: Worker Registration ✅
- Registered `poll_job_status`, `commit_job_budget`, `release_job_budget`, `mark_job_pending_reconcile` in `PRODUCTION_ACTIVITIES`
- Added `test_worker_registration.py` that imports from worker.py and asserts all workflow activities are registered

### Issue 2 (R1): Runtime Tests ✅
- Fixed workflow `except Exception` block to not swallow `ContentBlockError`/`RuntimeError` from confirmed failures
- Added `test_poll_exhaustion_runtime_keeps_reservation`: asserts reserved > 0, pending_reconcile, one submit, zero releases
- Added `test_confirmed_failure_releases_once`: asserts exactly one release

### Issue 3 (R2): Partial ✅
- Fixed 409 handling: removed swallowing `except Exception: pass`, let HTTPExceptions propagate
- Fixed approve route to work with episode shot workflows using ID pattern `{episode_id}-shot-{shot_id}`

### Issue 4 (R3): Mostly Complete ✅
- Removed duplicated aspect ratio text from prompt builder
- Fixed episode_parser regex to `[A-Z]+\d*` to match multi-letter codes (AG, B, W, M1, M2)
- Added `test_multi_letter_character_codes` test
- Fixed `test_prompt_kit_live_refuses_unresolved` to assert on exception message
- Added `test_canary_refuses_local_refs_in_live` test
- Added POST `/api/episodes/{episode_id}/upload-prompt-kit` route

### Issue 5 (R4): Complete ✅
- Updated web UI `EPISODE_CAP_CREDITS` to 950.0
- Changed all budget labels to "API credits"
- Get canary estimate from API instead of hard-coded values
- Fixed `credit_plan_parser` default to 950.0
- Tightened `test_episode_cap_converted` to explicitly check EPISODE_CAP constant and error messages
- Replaced bare `except:` with `except OSError:` in test cleanups

### Issue 6 (R5): Complete ✅
- Fixed `commit_job_budget` to skip ledger commit in dry mode (no reservation was made)
- Added `test_dry_mode_no_negative_reservations` test

## Remaining Work

### R2 - Still Needed:
- Test for deterministic workflow ID (M-R2a)
- Additional tests for M-R2b, M-R2c mutations

### R3 - Still Needed:
- GET shots API to return composed_prompt, aspect_ratio, refs BEFORE spend
- Store these fields in shots table from canary route before workflow starts

### R6 - Redgreen Mutations:
Fix broken mutations in `scripts/redgreen.sh`:
- Mutation 20 (invalid): Fix sed command syntax
- Mutation 32 (ineffective): Add uuid import
- Mutation 33 (ineffective): Fix sed deletion range
- Mutation 34 (ineffective): Check target line exists
- Mutation 36 (invalid): Fix deletion target
- Mutation 37 (non-AssertionError): Already fixed in test
- Mutation 38 (ineffective): Check target line
- Mutation 40 (ineffective): Target EPISODE_CAP constant exists
- Mutation 41 (invalid): Check SQL target
- Mutation 42 (invalid): Fix return statement mutation
- Mutation 45 (ineffective): Simplify sed command

## Test Results Expected:
- All existing tests should pass
- `scripts/redgreen.sh` should report fewer failures after fixes
- CI should be green on test-python, test-web, redgreen jobs

## Current Branch State:
Branch: cursor/phase1-backend-complete-941e
Head: de16b28
All changes pushed to remote
