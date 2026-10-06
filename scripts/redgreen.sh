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
    if python3 -m pytest "$test_name" -v --tb=short --cache-clear 2>&1 | grep -q "FAILED\|ERROR"; then
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
    if python3 -m pytest "$test_name" -v --tb=short --cache-clear 2>&1 | grep -q "PASSED\|1 passed"; then
        log_success "Test PASSED as expected (green confirmed)"
        return 0
    else
        log_error "Test FAILED when it should have PASSED"
        return 1
    fi
}

test_live_mode() {
    log_test "1. Live Mode Required"
    backup_file "hfvg/activities/studio_generation.py"
    log_red "Mutation: Removing live mode check..."
    sed -i 's/if not live_mode:/if False and not live_mode:/' hfvg/activities/studio_generation.py
    expect_red "tests/test_studio_safety.py::test_live_mode_required_for_generation"
    restore_file "hfvg/activities/studio_generation.py"
    expect_green "tests/test_studio_safety.py::test_live_mode_required_for_generation"
    echo ""
}

test_g108() {
    log_test "2. G1.08 Required"
    backup_file "hfvg/activities/studio_generation.py"
    log_red "Mutation: Removing G1.08 check..."
    sed -i 's/if not g108_approved:/if False and not g108_approved:/' hfvg/activities/studio_generation.py
    expect_red "tests/test_studio_safety.py::test_g108_required_for_generation"
    restore_file "hfvg/activities/studio_generation.py"
    expect_green "tests/test_studio_safety.py::test_g108_required_for_generation"
    echo ""
}

test_budget_stop() {
    log_test "3. Budget Stop (80%)"
    backup_file "hfvg/budget.py"
    log_red "Mutation: Removing budget stop..."
    sed -i 's/if total > stop_threshold:/if False and total > stop_threshold:/' hfvg/budget.py
    expect_red "tests/test_studio_safety.py::test_budget_stop_enforcement"
    restore_file "hfvg/budget.py"
    expect_green "tests/test_studio_safety.py::test_budget_stop_enforcement"
    echo ""
}

test_idempotency() {
    log_test "4. Idempotency"
    log_green "Verifying..."
    if python3 -m pytest tests/test_studio_safety.py::test_idempotent_retry_no_double_charge -v --cache-clear 2>&1 | grep -q "PASSED"; then
        log_success "Idempotency confirmed"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Idempotency failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    echo ""
}

test_activity_enforcement() {
    log_test "5. Activity-Level Enforcement"
    backup_file "hfvg/activities/studio_generation.py"
    log_red "Mutation: Bypassing activity gates..."
    sed -i 's/await check_live_mode_and_g108/# await check_live_mode_and_g108/' hfvg/activities/studio_generation.py
    expect_red "tests/test_studio_safety.py::test_activity_level_enforcement"
    restore_file "hfvg/activities/studio_generation.py"
    expect_green "tests/test_studio_safety.py::test_activity_level_enforcement"
    echo ""
}

test_ledger_math() {
    log_test "6. Ledger Math"
    backup_file "hfvg/budget.py"
    log_red "Mutation: Breaking release..."
    sed -i 's/reserved -= amount/reserved -= 0  # /' hfvg/budget.py
    expect_red "tests/test_studio_safety.py::test_ledger_math_reserve_commit_release"
    restore_file "hfvg/budget.py"
    expect_green "tests/test_studio_safety.py::test_ledger_math_reserve_commit_release"
    echo ""
}

test_auth() {
    log_test "7. Auth Fail-Closed"
    log_green "Verifying..."
    if python3 -m pytest tests/test_studio_safety.py::test_auth_fail_closed -v --cache-clear 2>&1 | grep -q "PASSED"; then
        log_success "Auth fail-closed confirmed"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Auth test failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    echo ""
}

test_canary() {
    log_test "8. Canary Real Workflow"
    log_green "Verifying..."
    if python3 -m pytest tests/test_studio_temporal_integration.py::test_canary_starts_shot_workflow -v --cache-clear 2>&1 | grep -q "PASSED"; then
        log_success "Canary confirmed"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Canary failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    echo ""
}

test_signaling() {
    log_test "9. Approval Signaling"
    log_green "Verifying..."
    if python3 -m pytest tests/test_studio_temporal_integration.py::test_approval_signal_reaches_episode_workflow -v --cache-clear 2>&1 | grep -q "PASSED"; then
        log_success "Signaling confirmed"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        log_error "Signaling failed"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    echo ""
}

echo ""
echo "RED-GREEN SAFETY TEST PROOF"
echo ""

test_live_mode
test_g108
test_budget_stop
test_idempotency
test_activity_enforcement
test_ledger_math
test_auth
test_canary
test_signaling

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "SUMMARY: ${PASS_COUNT} successes, ${FAIL_COUNT} failures"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ $FAIL_COUNT -eq 0 ]; then
    echo "✓ ALL RED-GREEN PROOFS PASSED"
    exit 0
else
    echo "✗ SOME PROOFS FAILED"
    exit 1
fi
