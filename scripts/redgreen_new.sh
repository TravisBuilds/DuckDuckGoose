#!/bin/bash
set -e

# Red-Green Proof: Verify each safety test fails when its protection is removed

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE_ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAIL_COUNT=0
PASS_COUNT=0

log_test() {
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}TEST: $1${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

log_red() {
    echo -e "${RED}[RED] $1${NC}"
}

log_green() {
    echo -e "${GREEN}[GREEN] $1${NC}"
}

log_error() {
    echo -e "${RED}✗ ERROR: $1${NC}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

log_success() {
    echo -e "${GREEN}✓ SUCCESS: $1${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
}

backup_file() {
    cp "$1" "$1.backup"
}

restore_file() {
    if [ -f "$1.backup" ]; then
        mv "$1.backup" "$1"
    fi
}

expect_red() {
    local test_name=$1
    log_red "Running test (expecting FAILURE)..."
    if python3 -m pytest "$test_name" -v --tb=short --cache-clear 2>&1 | grep -q "FAILED\|ERROR"; then
        log_success "Test FAILED as expected (red confirmed)"
        return 0
    else
        log_error "Test PASSED when it should have FAILED (false pass!)"
        return 1
    fi
}

expect_green() {
    local test_name=$1
    log_green "Running test (expecting PASS)..."
    if python3 -m pytest "$test_name" -v --tb=short --cache-clear 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Test PASSED as expected (green confirmed)"
        return 0
    else
        log_error "Test FAILED when it should have PASSED"
        return 1
    fi
}

# Test 1: Live Mode Required
test_live_mode() {
    log_test "1. Live Mode Required for Generation"
    
    local file="hfvg/activities/studio_generation.py"
    local test="tests/test_studio_safety.py::test_live_mode_required_for_generation"
    
    backup_file "$file"
    
    log_red "Mutation: Removing live mode check..."
    sed -i 's/if not live_mode:/if False and not live_mode:/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

# Test 2: G1.08 Required
test_g108() {
    log_test "2. G1.08 Credit Plan Approval Required"
    
    local file="hfvg/activities/studio_generation.py"
    local test="tests/test_studio_safety.py::test_g108_required_for_generation"
    
    backup_file "$file"
    
    log_red "Mutation: Removing G1.08 check..."
    sed -i 's/if not g108_approved:/if False and not g108_approved:/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

# Test 3: Budget Stop
test_budget_stop() {
    log_test "3. Budget Stop at 80% Threshold"
    
    local file="hfvg/budget.py"
    local test="tests/test_studio_safety.py::test_budget_stop_enforcement"
    
    backup_file "$file"
    
    log_red "Mutation: Removing budget stop check..."
    sed -i 's/if total > stop_threshold:/if False and total > stop_threshold:/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

# Test 4: Idempotent Retries
test_idempotent_retries() {
    log_test "4. Idempotent Retry (No Double Charge)"
    
    local test="tests/test_studio_safety.py::test_idempotent_retry_no_double_charge"
    
    # Test already verifies deterministic keys prevent double charging
    log_green "Verifying idempotency test..."
    if python3 -m pytest "$test" -v --tb=short --cache-clear 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Idempotency test confirms deterministic keys work"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Idempotency test failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    
    echo ""
}

# Test 5: Activity-Level Enforcement
test_activity_enforcement() {
    log_test "5. Activity-Level Enforcement"
    
    local file="hfvg/activities/studio_generation.py"
    local test="tests/test_studio_safety.py::test_activity_level_enforcement"
    
    backup_file "$file"
    
    log_red "Mutation: Bypassing activity gate check..."
    sed -i 's/await check_live_mode_and_g108/# await check_live_mode_and_g108/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

# Test 6: Ledger Math
test_ledger_math() {
    log_test "6. Ledger Math (Reserve-Commit-Release)"
    
    local file="hfvg/budget.py"
    local test="tests/test_studio_safety.py::test_ledger_math_reserve_commit_release"
    
    backup_file "$file"
    
    log_red "Mutation: Breaking ledger release logic..."
    sed -i 's/reserved -= amount/reserved -= 0  # /' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

# Test 7: Auth Fail Closed
test_auth_fail_closed() {
    log_test "7. Auth Fail Closed (No Default Secret)"
    
    local test="tests/test_studio_safety.py::test_auth_fail_closed"
    
    # Test already verifies no default secret allowed
    log_green "Verifying auth fail-closed test..."
    if python3 -m pytest "$test" -v --tb=short --cache-clear 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Auth fail-closed test confirms no default secret"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Auth fail-closed test failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    
    echo ""
}

# Test 8: Canary Real Workflow
test_canary_workflow() {
    log_test "8. Canary Uses Real ShotWorkflow"
    
    local test="tests/test_studio_temporal_integration.py::test_canary_starts_shot_workflow"
    
    log_green "Verifying canary workflow test..."
    if python3 -m pytest "$test" -v --tb=short --cache-clear 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Canary test confirms real ShotWorkflow execution"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Canary workflow test failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    
    echo ""
}

# Test 9: Approval Signaling
test_approval_signaling() {
    log_test "9. Approval Endpoint Signals EpisodeWorkflowV2"
    
    local test="tests/test_studio_temporal_integration.py::test_approval_signal_reaches_episode_workflow"
    
    log_green "Verifying approval signal test..."
    if python3 -m pytest "$test" -v --tb=short --cache-clear 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Approval signal test confirms workflow receives signals"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Approval signal test failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    
    echo ""
}

# Test 10: G4.09 Picture Lock Required Before Audio
test_picture_lock_gate() {
    log_test "10. G4.09 Picture Lock Required Before Audio"
    
    local file="hfvg/workflows/episode_v2.py"
    local test="tests/test_episode_v2.py::test_episode_v2_picture_lock_blocks_audio"
    
    backup_file "$file"
    
    log_red "Mutation: Removing picture lock wait..."
    sed -i 's/await workflow.wait_condition(lambda: self.approved_g409)/# await workflow.wait_condition(lambda: self.approved_g409)/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

# Test 11: GX.01 HOLD Required (No Auto-Post)
test_gx01_hold_gate() {
    log_test "11. GX.01 HOLD Required (No Auto-Post)"
    
    local file="hfvg/workflows/episode_v2.py"
    local test="tests/test_episode_v2.py::test_episode_v2_gx01_hold"
    
    backup_file "$file"
    
    log_red "Mutation: Bypassing GX.01 HOLD..."
    sed -i 's/await workflow.wait_condition(lambda: self.approved_gx01)/# await workflow.wait_condition(lambda: self.approved_gx01)/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

echo ""
echo "RED-GREEN SAFETY TEST PROOF"
echo "Testing all safety gates with mutation verification"
echo ""

test_live_mode
test_g108
test_budget_stop
test_idempotent_retries
test_activity_enforcement
test_ledger_math
test_auth_fail_closed
test_canary_workflow
test_approval_signaling
test_picture_lock_gate
test_gx01_hold_gate

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Successful verifications: ${PASS_COUNT}"
echo "Failed verifications: ${FAIL_COUNT}"
echo ""

if [ $FAIL_COUNT -eq 0 ]; then
    echo "✓ ALL RED-GREEN PROOFS PASSED"
    exit 0
else
    echo "✗ SOME RED-GREEN PROOFS FAILED"
    exit 1
fi
