# Red-Green Mutation Testing Status

## Progress (commit 0a0df89)

### Fixed Mutations
- **#1, #2, #4**: Mock /estimate endpoint, assert provider NOT called and ledger unchanged
- **#26**: Updated to target actual SQL protection `AND reserved >= ?`
- **Test framework**: Replaced pytest.fail() with assertions for proper JUnit classification

### Remaining Issues - Task #1

#### High Priority (blocking red-green pass)
1. **Mutation #5 (idempotency)** - Baseline fails
   - Test: `test_idempotent_retry_no_double_charge`
   - Issue: Test itself may be broken
   - Fix: Debug why baseline test fails

2. **Mutation #7 (ledger math)** - Baseline fails
   - Test: `test_ledger_math_reserve_commit_release`
   - Issue: Test itself may be broken
   - Fix: Debug why baseline test fails

3. **Mutations #23, #24 (gates)** - Non-AssertionError
   - Tests: `test_episode_v2_picture_lock_blocks_audio`, `test_episode_v2_gx01_hold`
   - Issue: Same as old #1,#2,#4 - probably using pytest.fail() or wrong exception
   - Fix: Apply same pattern as mutations 1,2,4 (use call_raised_error flag + assert)

4. **Mutation #6 (auth)** - Mutation ineffective
   - Pattern `if not authorization:` may not exist or mutation not changing file
   - Fix: Check actual code and update mutation pattern

#### Missing Tests (baseline fails)
These mutations reference tests that don't exist or are completely broken:
- #11: test_api_auth_rejects_invalid_secret
- #14: test_canary_route_checks_g108
- #15: test_canary_route_checks_live_mode  
- #16: test_canary_route_starts_workflow
- #17: test_canary_reserves_l6_before_workflow
- #19: test_clip_idempotency_key_sent
- #20: test_still_approve_route_sends_signal
- #29: test_canary_l6_reconcile_on_failure

**Action**: Create these tests following the same pattern as working tests

### Task #2: Add New Mutations
Per verification report, add mutations for:
- Poll without cost commits estimate
- Estimate fails closed with no credits
- Kling estimate body complete
- Atomic release never negative
- L6 reconcile exactly once
- 409 on duplicate canary
- stop<=cap validation
- Gate routing to EpisodeWorkflowV2
- Prompt built from shot
- Revert fix #1 (escalated still → clip)

### Task #3: 20 Undetected Mutations from v4
See VERIFY-PR7-v4.md lines 75-92 for full list

### Task #4: Full-Episode Gate Test
Real Temporal env, 2-shot fixture, mock network only
Test gates: G1.08, G2.12, G4.09, GX.01
Assert: current_gate, not in passed_gates, RUNNING status
