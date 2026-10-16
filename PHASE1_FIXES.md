# Phase 1 Backend Fixes - Work Plan

Based on independent verification report at 382b3fe.

## P0 Critical Fixes (blocks paid canary)

### P0.1 Ledger Fixes
- [ ] Make `reserve()` atomic (BEGIN IMMEDIATE + conditional UPDATE)
- [ ] Add 1,250 episode cap enforcement in `reserve()`
- [ ] Fix `commit()` to accept (reserved_amount, actual_cost) and handle overage
- [ ] Add `try/except` around submit calls to release on failure
- [ ] Remove `RetryPolicy(maximum_attempts=3)` from paid activities  
- [ ] Remove QC retry loop in shot.py (lines 166-170, 278-280)
- [ ] Add `try/except` around poll to release on failure
- [ ] Handle CancelledError to release reservations

### P0.2 Provider API Fixes
- [ ] Update Authorization header: `Key <id>:<secret>` (not Bearer)
- [ ] Fix endpoints: `POST /{model_path}`, `GET /requests/{id}/status`
- [ ] Fix response field: `request_id` (not `job_id`)
- [ ] Implement real cost estimates (not hard-coded 4.0)
- [ ] Make base URL configurable
- [ ] Update idempotency key to cover all params (refs, quality, duration)

### P0.3 Canary Path Fixes
- [ ] Reserve L6 BEFORE starting workflow
- [ ] Pass reservation to workflow for commit/release
- [ ] Add GET /api/canary/<id> status route
- [ ] Fix still-approve to target `{ep}-canary-{shot}-{ts}` workflow ID
- [ ] Make clip-approve send real signal (not just audit)
- [ ] Add POST /api/episodes/{id}/set-dry route (turn live mode OFF)
- [ ] Fix live still-QC to allow human approval signal (not terminate)
- [ ] Validate shot_id in approve routes

## P1 High Priority

### P1.1 Single Source of Truth
- [ ] Worker reads live mode from DB (DRY_RUN only forces dry, never live)
- [ ] Tie EpisodeWorkflowV2 G1.08 to DB
- [ ] API message reflects actual worker behavior

### P1.2 Remove Legacy
- [ ] Delete or gate activities/generation.py (ungated submit_still_job, etc.)
- [ ] Delete HiggsfieldProvider (wrong API)
- [ ] Delete ElevenLabs provider or gate it
- [ ] Remove unused deps: higgsfield-client, elevenlabs

### P1.3 Tests & CI
- [ ] Enable pytest-socket: `--disable-socket --allow-hosts=127.0.0.1,localhost`
- [ ] Add fastapi to `[dev]` dependencies
- [ ] Fix test_episode_v2_approval_gates (register activities)
- [ ] Fix test_a_full_episode_through_all_gates
- [ ] Make QC deterministic (no random.random in tests)
- [ ] Add timeout or remove duplicate "Test report" step
- [ ] Fix red-green mutations 1,2,6,9,10 (wrong reason/fake)
- [ ] Add mutations for X6, X9-X20 (15 undetected protections)

## P2 Lower Priority

### P2.1 UI Auth
- [ ] UI login calls /api/login (not client-side cookie)
- [ ] Remove raw secret from cookie (web/app/login/page.tsx:34)
- [ ] Budget/state routes accept session cookie
- [ ] Fix /api/studio/verify (not via /api/health)

## Proof Requirements

Each fix must include:
1. Code change with file:line reference
2. Test command showing it works
3. Output proving the fix

Zero spend constraint maintained throughout.
