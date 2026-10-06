# Studio Console Ep04 Implementation TODO

## 1. Dependencies & Imports ✅
- [ ] Keep httpx (already in deps, not aiohttp)
- [ ] Fix hfvg/providers/__init__.py exports
- [ ] Verify all imports work

## 2. Budget System
- [ ] Update BudgetLedger to use studio DB (./data/studio.db)
- [ ] Parse CREDIT-PLAN at runtime for caps/stops
- [ ] Fix spent/reserved calculations
- [ ] Add idempotent reserve/commit/release

## 3. Provider Enforcement
- [ ] Create higgsfield_still.py provider using httpx
- [ ] Create kling_video.py provider for Kling 3.0 Pro
- [ ] Add enforcement: live mode, G1.08, budget reserve
- [ ] Add Idempotency-Key to all calls
- [ ] Cost estimation before submit

## 4. Activities
- [ ] Create record_shot_result as @activity.defn
- [ ] Register it in worker.py
- [ ] Fix ShotWorkflow.run arg mismatch (2 args not 3)
- [ ] Use real providers in activities

## 5. API Endpoints
- [ ] POST /api/episodes/{id}/shots/{shot_id}/approve (still)
- [ ] POST /api/episodes/{id}/shots/{shot_id}/reject (still)
- [ ] POST /api/episodes/{id}/clips/{clip_id}/approve
- [ ] POST /api/episodes/{id}/clips/{clip_id}/reject
- [ ] GET /api/episodes/{id}/gates
- [ ] All endpoints send signals to workflow

## 6. Auth & Live Mode
- [ ] Login route POST /api/login sets httpOnly cookie
- [ ] POST /api/episodes/{id}/set-live requires confirmation
- [ ] Audit trail for live mode changes
- [ ] Fail closed (no default ADMIN_SECRET)
- [ ] .env.example committed

## 7. Beatmap Parser
- [ ] Upload endpoint parses BEATMAP into shots table
- [ ] Verify 31 shots for Ep04

## 8. Database Schema
- [ ] shots table
- [ ] episodes table with live_mode flag
- [ ] audit_log table

## 9. Tests (No stubs)
- [ ] test_budget_enforcement
- [ ] test_live_mode_required
- [ ] test_g108_required
- [ ] test_stop_threshold
- [ ] test_idempotent_retry
- [ ] test_activity_enforcement
- [ ] test_auth_fail_closed
- [ ] test_approval_signal
- [ ] test_canary_workflow
- [ ] test_ledger_math

## 10. Web Build & ESLint
- [ ] Fix ESLint config
- [ ] npm run build passes
- [ ] ESLint runs in CI

## 11. End-to-End Test
- [ ] Start Temporal dev server
- [ ] Start worker
- [ ] Start API
- [ ] Start web
- [ ] Upload Ep04 beatmap (31 shots)
- [ ] Run canary (dry run)
- [ ] Screenshots: home, episode overview, still strip, clip review, approvals, budget, audit, canary

## 12. PR & Documentation
- [ ] Commit everything
- [ ] Push to branch
- [ ] Create draft PR
- [ ] Add screenshots to PR
- [ ] Document what's done/not done
