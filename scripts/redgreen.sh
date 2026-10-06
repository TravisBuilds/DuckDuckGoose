#!/bin/bash
set -e

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
    if python3 -m pytest "$test_name" -v --tb=short 2>&1 | grep -q "FAILED\|ERROR"; then
        log_success "Test FAILED as expected (red confirmed)"
        return 0
    else
        log_error "Test PASSED when it should have FAILED"
        return 1
    fi
}

expect_green() {
    local test_name=$1
    log_green "Running test (expecting PASS)..."
    if python3 -m pytest "$test_name" -v --tb=short 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Test PASSED as expected (green confirmed)"
        return 0
    else
        log_error "Test FAILED when it should have PASSED"
        return 1
    fi
}

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

test_activity_enforcement() {
    log_test "4. Activity-Level Enforcement (Not Just API-Level)"
    
    local file="hfvg/workflows/shot.py"
    local test="tests/test_studio_safety.py::test_activity_level_enforcement"
    
    backup_file "$file"
    
    log_red "Mutation: Switching to unenforced activity..."
    sed -i 's/submit_still_job_enforced/submit_still_job/g' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

test_idempotency() {
    log_test "5. Idempotent Retry (No Double-Charge)"
    
    local file="hfvg/activities/studio_generation.py"
    local test="tests/test_studio_safety.py::test_idempotent_retry_no_double_charge"
    
    backup_file "$file"
    
    log_red "Mutation: Removing idempotency key..."
    sed -i 's/idempotency_key = generate_idempotency_key/idempotency_key = None  # generate_idempotency_key/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

test_auth() {
    log_test "6. Auth Fail-Closed (No Default Secret)"
    
    local file="api/main.py"
    local test="tests/test_studio_safety.py::test_auth_fail_closed"
    
    backup_file "$file"
    
    log_red "Mutation: Adding default admin secret..."
    sed -i 's/if not ADMIN_SECRET or len(ADMIN_SECRET) < 32:/if False and (not ADMIN_SECRET or len(ADMIN_SECRET) < 32):/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

test_ledger_math() {
    log_test "7. Ledger Math (Reserve, Commit, Release)"
    
    local file="hfvg/budget.py"
    local test="tests/test_studio_safety.py::test_ledger_math_reserve_commit_release"
    
    backup_file "$file"
    
    log_red "Mutation: Breaking commit to add twice..."
    sed -i 's/SET reserved = reserved - ?, spent = spent + ?/SET reserved = reserved - ?, spent = spent + ? + ?/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

test_hard_cap() {
    log_test "8. Hard Cap Enforcement"
    
    local file="hfvg/budget.py"
    local test="tests/test_budget.py::test_hard_cap_enforcement"
    
    backup_file "$file"
    
    log_red "Mutation: Removing hard cap check..."
    sed -i 's/if total > cap:/if False and total > cap:/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

test_canary_workflow() {
    log_test "9. Canary Starts ShotWorkflow"
    
    local file="hfvg/workflows/shot.py"
    local test="tests/test_studio_temporal_integration.py::test_canary_starts_shot_workflow"
    
    backup_file "$file"
    
    log_red "Mutation: Breaking workflow by removing signal handler..."
    sed -i 's/@workflow.signal/#@workflow.signal/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

test_approval_signal() {
    log_test "10. Approval Signal Reaches Workflow"
    
    local file="api/main.py"
    local test="tests/test_studio_temporal_integration.py::test_approval_signal_reaches_episode_workflow"
    
    backup_file "$file"
    
    log_red "Mutation: Breaking signal routing..."
    sed -i 's/await handle.signal("stills_approved")/await handle.signal("wrong_signal_name")/' "$file"
    expect_red "$test"
    
    restore_file "$file"
    
    expect_green "$test"
    
    echo ""
}

echo ""
echo "RED-GREEN SAFETY TEST PROOF"
echo "Testing all 10 critical safety mechanisms"
echo ""

test_live_mode
test_g108
test_budget_stop
test_activity_enforcement
test_idempotency
test_auth
test_ledger_math
test_hard_cap
test_canary_workflow
test_approval_signal

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Successful verifications: ${PASS_COUNT}"
echo "Failed verifications: ${FAIL_COUNT}"
echo ""

if [ $FAIL_COUNT -eq 0 ]; then
    echo "✓ ALL 10 RED-GREEN PROOFS PASSED"
    exit 0
else
    echo "✗ SOME RED-GREEN PROOFS FAILED"
    exit 1
fi
