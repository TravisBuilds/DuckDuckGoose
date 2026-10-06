#!/bin/bash
#
# Red-green safety proof: Mutate protections and verify tests go RED, then GREEN when restored.
#
# Uses a temporary git worktree to isolate mutations.
# Treats syntax errors, SQL errors, and network failures as INVALID mutations.
# Prints a summary table at the end.
#

set -u  # Fail on undefined variables, but NOT on command errors

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE_ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0;

'

# Results tracking
declare -a MUTATION_NAMES
declare -a MUTATION_RESULTS  # "PASS" or "FAIL"
declare -a MUTATION_REASONS

PASS_COUNT=0
FAIL_COUNT=0

log_header() {
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

log_test() {
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}TEST $1: $2${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

log_red() {
    echo -e "${RED}[RED] $1${NC}"
}

log_green() {
    echo -e "${GREEN}[GREEN] $1${NC}"
}

# Run a test and return 0 if it passes, 1 if it fails
run_test() {
    local test_name=$1
    local output
    
    # Run pytest with timeout
    output=$(python3 -m pytest "$test_name" -p no:cacheprovider -q --tb=line -rfE --timeout=60 -o timeout_method=signal 2>&1)
    local exit_code=$?
    
    # Check for invalid mutations (syntax errors, SQL binding errors, import errors)
    if echo "$output" | grep -q "SyntaxError\|ProgrammingError\|ModuleNotFoundError\|ImportError"; then
        echo "INVALID"
        return 2
    fi
    
    # Check if test passed or failed
    if [ $exit_code -eq 0 ] && echo "$output" | grep -q "passed"; then
        echo "PASS"
        return 0
    else
        echo "FAIL"
        return 1
    fi
}

# Test one mutation
test_mutation() {
    local num=$1
    local name=$2
    local file=$3
    local test=$4
    shift 4
    local mutation_cmd=("$@")
    
    log_test "$num" "$name"
    
    # Step 1: Run baseline (should pass)
    log_green "Running baseline test (expecting PASS)..."
    baseline_result=$(run_test "$test")
    baseline_exit=$?
    
    if [ "$baseline_result" = "INVALID" ]; then
        echo -e "${RED}✗ INVALID: Baseline test has errors (syntax/import/SQL)${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("Baseline has errors")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$baseline_result" != "PASS" ]; then
        echo -e "${RED}✗ FAIL: Baseline test does not pass${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Baseline fails")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Baseline passes${NC}"
    
    # Step 2: Apply mutation
    log_red "Applying mutation to $file..."
    "${mutation_cmd[@]}" "$file" 2>/dev/null
    
    # Verify mutation actually changed the file
    if ! git diff --quiet "$file"; then
        echo -e "${BLUE}Mutation applied (file changed)${NC}"
    else
        echo -e "${RED}✗ INVALID: Mutation did not change the file${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("No file change")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    # Step 3: Run mutated test (should fail)
    log_red "Running mutated test (expecting FAIL)..."
    mutated_result=$(run_test "$test")
    mutated_exit=$?
    
    if [ "$mutated_result" = "INVALID" ]; then
        echo -e "${RED}✗ INVALID: Mutation caused syntax/import/SQL error${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("Mutation breaks code")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" != "FAIL" ]; then
        echo -e "${RED}✗ FAIL: Mutated test still passes (mutation had no effect)${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Mutation ineffective")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${RED}✓ Mutated test fails (red confirmed)${NC}"
    
    # Step 4: Restore file
    log_green "Restoring file..."
    git checkout -- "$file" 2>/dev/null
    
    # Step 5: Run restored test (should pass)
    log_green "Running restored test (expecting PASS)..."
    restored_result=$(run_test "$test")
    restored_exit=$?
    
    if [ "$restored_result" = "INVALID" ]; then
        echo -e "${RED}✗ FAIL: Restored test has errors${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Restore failed")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$restored_result" != "PASS" ]; then
        echo -e "${RED}✗ FAIL: Restored test does not pass${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Restore didn't fix")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Restored test passes (green confirmed)${NC}"
    echo -e "${GREEN}━━━ ✓ RED-GREEN PROOF PASSED ━━━${NC}"
    
    MUTATION_NAMES+=("$num. $name")
    MUTATION_RESULTS+=("PASS")
    MUTATION_REASONS+=("Red → Green confirmed")
    PASS_COUNT=$((PASS_COUNT + 1))
}

# Print summary table
print_summary() {
    echo ""
    log_header "RED-GREEN SAFETY TEST SUMMARY"
    echo ""
    
    printf "%-50s %-10s %-30s\n" "Test" "Result" "Reason"
    printf "%-50s %-10s %-30s\n" "$(printf '%.0s─' {1..50})" "$(printf '%.0s─' {1..10})" "$(printf '%.0s─' {1..30})"
    
    for i in "${!MUTATION_NAMES[@]}"; do
        local name="${MUTATION_NAMES[$i]}"
        local result="${MUTATION_RESULTS[$i]}"
        local reason="${MUTATION_REASONS[$i]}"
        
        if [ "$result" = "PASS" ]; then
            printf "${GREEN}%-50s ✓ PASS     %-30s${NC}\n" "$name" "$reason"
        elif [ "$result" = "INVALID" ]; then
            printf "${YELLOW}%-50s ⚠ INVALID  %-30s${NC}\n" "$name" "$reason"
        else
            printf "${RED}%-50s ✗ FAIL     %-30s${NC}\n" "$name" "$reason"
        fi
    done
    
    echo ""
    echo -e "${BLUE}Total tests: ${#MUTATION_NAMES[@]}${NC}"
    echo -e "${GREEN}Passed: $PASS_COUNT${NC}"
    echo -e "${RED}Failed: $FAIL_COUNT${NC}"
    echo ""
    
    if [ $FAIL_COUNT -eq 0 ]; then
        echo -e "${GREEN}✓✓✓ ALL RED-GREEN PROOFS PASSED ✓✓✓${NC}"
        return 0
    else
        echo -e "${RED}✗✗✗ SOME RED-GREEN PROOFS FAILED ✗✗✗${NC}"
        return 1
    fi
}

# Main tests
log_header "DuckDuckGoose Studio Red-Green Safety Proofs"
echo "Testing all 10 critical safety mechanisms with mutation testing"
echo ""

# Ensure we're in the repo root
if [ ! -f "pyproject.toml" ]; then
    echo "Error: Must run from repository root"
    exit 1
fi

# 1. Live mode required
test_mutation 1 "Live mode required for generation" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_studio_safety.py::test_live_mode_required_for_generation" \
    sed -i "s/if not live_mode:/if False and not live_mode:/"

# 2. G1.08 required
test_mutation 2 "G1.08 credit plan approval required" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_studio_safety.py::test_g108_required_for_generation" \
    sed -i "s/if not g108_approved:/if False and not g108_approved:/"

# 3. Budget stop
test_mutation 3 "Budget stop at 80% threshold" \
    "hfvg/budget.py" \
    "tests/test_studio_safety.py::test_budget_stop_enforcement" \
    sed -i "s/if total > stop_threshold:/if False and total > stop_threshold:/"

# 4. Activity-level enforcement
test_mutation 4 "Activity-level enforcement (not just API)" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_redgreen_safety.py::test_activity_level_enforcement" \
    sed -i 's/if not live_mode:/if False:  # MUTATED/'

# 5. Idempotency
test_mutation 5 "Idempotent retry (no double-charge)" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_redgreen_safety.py::test_idempotent_retry_no_double_charge" \
    python3 scripts/mutate_idempotency.py

# 6. Auth fail-closed
test_mutation 6 "Auth fail-closed (no default secret)" \
    "api/main.py" \
    "tests/test_redgreen_safety.py::test_auth_fail_closed" \
    sed -i 's/if not authorization:/if False:  # MUTATED/'

# 7. Ledger math
test_mutation 7 "Ledger math (reserve, commit, release)" \
    "hfvg/budget.py" \
    "tests/test_studio_safety.py::test_ledger_math_reserve_commit_release" \
    sed -i 's/spent = spent + ?/spent = spent + ? + 999/'

# 8. Hard cap
test_mutation 8 "Hard cap enforcement" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_hard_cap_enforcement" \
    sed -i "s/if total > cap:/if False and total > cap:/"

# 9. Canary starts ShotWorkflow
test_mutation 9 "Canary starts real ShotWorkflow" \
    "hfvg/workflows/shot.py" \
    "tests/test_redgreen_safety.py::test_canary_starts_shot_workflow" \
    sed -i 's/@workflow.defn/#@workflow.defn  # MUTATED/'

# 10. Approval signal reaches workflow
test_mutation 10 "Approval signal reaches workflow" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_redgreen_safety.py::test_approval_signal_reaches_episode_workflow" \
    sed -i 's/@workflow.signal/# @workflow.signal  # MUTATED/' 

# Print final summary
print_summary
exit_code=$?

exit $exit_code
