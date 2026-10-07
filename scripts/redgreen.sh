#!/bin/bash
#
# Red-green safety proof: Mutate protections and verify tests go RED, then GREEN when restored.
#
# Uses a temporary git worktree to isolate mutations - NEVER edits the main working tree.
# Treats syntax errors, SQL errors, and network failures as INVALID mutations.
# Prints a summary table at the end.
#

set -eu  # Fail on undefined variables AND command errors

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE_ROOT"

# Create isolated worktree for mutations
MUTATION_WORKTREE="/tmp/redgreen-worktree-$$"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
CURRENT_COMMIT="$(git rev-parse HEAD)"

cleanup_worktree() {
    if [ -d "$MUTATION_WORKTREE" ]; then
        echo "Cleaning up worktree at $MUTATION_WORKTREE"
        git worktree remove --force "$MUTATION_WORKTREE" 2>/dev/null || true
        rm -rf "$MUTATION_WORKTREE"
    fi
}

# Ensure cleanup on exit
trap cleanup_worktree EXIT

# Create worktree using commit SHA (avoids conflict when branch is already checked out)
echo "Creating isolated worktree at $MUTATION_WORKTREE"
git worktree add "$MUTATION_WORKTREE" "$CURRENT_COMMIT" || {
    echo "Failed to create worktree"
    exit 1
}

# All mutations will run in the worktree
cd "$MUTATION_WORKTREE"

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

# Run a test and classify result from JUnit XML
# Returns: PASS, FAIL_ASSERT, FAIL_OTHER, or INVALID
run_test() {
    local test_name=$1
    local junit_xml="/tmp/redgreen_junit_$$_$(date +%s).xml"
    local tmp_output="/tmp/redgreen_output_$$_$(date +%s).txt"
    
    # Run pytest with JUnit XML output
    python3 -m pytest "$test_name" -p no:cacheprovider -q --junitxml="$junit_xml" --timeout=60 -o timeout_method=signal -o addopts="" > "$tmp_output" 2>&1
    local exit_code=$?
    
    # Check for collection errors or syntax errors (INVALID mutation)
    if [ ! -f "$junit_xml" ] || grep -q "SyntaxError\|IndentationError\|ImportError.*SyntaxError" "$tmp_output"; then
        rm -f "$junit_xml" "$tmp_output"
        echo "INVALID"
        return 0
    fi
    
    # Parse JUnit XML to classify failure
    # A mutation is caught only if:
    # 1. At least one <failure> with type="AssertionError"
    # 2. No <error> outcomes (collection/import/runtime errors)
    
    # Check if all tests passed
    if grep -q 'failures="0"' "$junit_xml" && grep -q 'errors="0"' "$junit_xml"; then
        rm -f "$junit_xml" "$tmp_output"
        echo "PASS"
        return 0
    fi
    
    # Check for <error> nodes (collection errors, import errors, etc.)
    if grep -q '<error' "$junit_xml"; then
        rm -f "$junit_xml" "$tmp_output"
        echo "FAIL_OTHER"
        return 0
    fi
    
    # Check for <failure> nodes with AssertionError
    # In pytest's JUnit XML, the failure message contains the exception type:
    # - AssertionError: message="AssertionError: ..." or message="assert ..."
    # - Other exceptions: message="ValueError: ..." etc.
    if grep -q '<failure' "$junit_xml"; then
        # Check if it's an AssertionError (not other exception types)
        # Look for failures that contain AssertionError or bare assert statements
        # But exclude failures with other exception types like ValueError, AttributeError, etc.
        if grep -o '<failure message="[^"]*"' "$junit_xml" | grep -qE 'message="(AssertionError:|assert [^a-z])'; then
            rm -f "$junit_xml" "$tmp_output"
            echo "FAIL_ASSERT"
            return 0
        fi
        
        # Failed with non-AssertionError exception
        rm -f "$junit_xml" "$tmp_output"
        echo "FAIL_OTHER"
        return 0
    fi
    
    # Failed but no failure/error nodes (shouldn't happen)
    rm -f "$junit_xml" "$tmp_output"
    echo "FAIL_OTHER"
    return 0
}

# Test one probe (classifier verification)
test_probe() {
    local num=$1
    local name=$2
    local expected_class=$3  # "FAIL_ASSERT" or "FAIL_OTHER"
    local file=$4
    local test=$5
    shift 5
    local mutation_cmd=("$@")
    
    log_test "$num" "$name"
    
    # Step 1: Run baseline (should pass)
    log_green "Running baseline test (expecting PASS)..."
    baseline_result=$(run_test "$test")
    
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
    
    if ! git diff --quiet "$file"; then
        echo -e "${BLUE}Mutation applied (file changed)${NC}"
    else
        echo -e "${RED}✗ FAIL: Mutation did not change the file${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("No file change")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    # Step 3: Run mutated test and check classification
    log_red "Running mutated test (expecting $expected_class)..."
    mutated_result=$(run_test "$test")
    
    git checkout -- "$file" 2>/dev/null
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    
    if [ "$mutated_result" = "INVALID" ]; then
        echo -e "${RED}✗ FAIL: Mutation caused syntax error${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Syntax error")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" = "PASS" ]; then
        echo -e "${RED}✗ FAIL: Mutated test still passes${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Not detected")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    # Check if classification matches expected
    if [ "$mutated_result" != "$expected_class" ]; then
        echo -e "${RED}✗ FAIL: Expected $expected_class but got $mutated_result${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Wrong classification: $mutated_result")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Classified correctly as $expected_class${NC}"
    echo -e "${GREEN}━━━ ✓ PROBE VERIFICATION PASSED ━━━${NC}"
    
    MUTATION_NAMES+=("$num. $name")
    MUTATION_RESULTS+=("PASS")
    MUTATION_REASONS+=("Correct classification")
    PASS_COUNT=$((PASS_COUNT + 1))
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
    
    # Step 3: Run mutated test (should fail with AssertionError)
    log_red "Running mutated test (expecting FAIL with AssertionError)..."
    mutated_result=$(run_test "$test")
    mutated_exit=$?
    
    if [ "$mutated_result" = "INVALID" ]; then
        echo -e "${RED}✗ INVALID: Mutation caused syntax error${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("Syntax error")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" = "FAIL_OTHER" ]; then
        echo -e "${RED}✗ FAIL: Test failed with non-AssertionError (${mutated_result})${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Non-AssertionError")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" != "FAIL_ASSERT" ]; then
        echo -e "${RED}✗ FAIL: Mutated test still passes (mutation had no effect)${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Mutation ineffective")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${RED}✓ Mutated test fails with AssertionError (red confirmed)${NC}"
    
    # Step 4: Restore file
    log_green "Restoring file..."
    git checkout -- "$file" 2>/dev/null
    
    # Clear Python caches to ensure restored code is loaded
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    
    # Verify restoration: fail if mutation leaked into working tree
    if ! git diff --quiet "$file"; then
        echo -e "${RED}✗ FAIL: Mutation leaked - file not fully restored${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Restore incomplete")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
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
echo "Testing all critical safety mechanisms with mutation testing"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# PROBE MUTATIONS: Verify classifier detects wrong-reason failures
# ─────────────────────────────────────────────────────────────────────────────

log_header "PROBE TESTS: Verify JUnit XML classifier"
echo "These probes verify the classifier correctly rejects wrong-reason failures"
echo ""

# Positive control: real assertion should be caught (FAIL_ASSERT)
test_probe "PC" "Positive control: Real assertion caught" \
    "FAIL_ASSERT" \
    "tests/test_redgreen_probes.py" \
    "tests/test_redgreen_probes.py::test_probe_positive_control" \
    sed -i 's/assert value == 42/assert value == 999/'

# Probe P1: AttributeError (should FAIL_OTHER - wrong reason)
test_probe "P1" "Probe P1: AttributeError must report FAILED" \
    "FAIL_OTHER" \
    "tests/test_redgreen_probes.py" \
    "tests/test_redgreen_probes.py::test_probe_p1_attribute_error" \
    python3 scripts/mutate_probe_p1.py

# Probe P2: respx unmocked request (should FAIL_OTHER - wrong reason)
test_probe "P2" "Probe P2: respx unmocked must report FAILED" \
    "FAIL_OTHER" \
    "tests/test_redgreen_probes.py" \
    "tests/test_redgreen_probes.py::test_probe_p2_respx_unmocked" \
    python3 scripts/mutate_probe_p2.py

# Probe P3: RuntimeError during AssertionError handling (should FAIL_OTHER - wrong reason)
test_probe "P3" "Probe P3: RuntimeError in except must report FAILED" \
    "FAIL_OTHER" \
    "tests/test_redgreen_probes.py" \
    "tests/test_redgreen_probes.py::test_probe_p3_runtime_during_assert" \
    python3 scripts/mutate_probe_p3.py

echo ""
log_header "PRODUCTION SAFETY TESTS"
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

# 3. Budget stop (atomic SQL check)
test_mutation 3 "Budget stop at 80% threshold (atomic)" \
    "hfvg/budget.py" \
    "tests/test_studio_safety.py::test_concurrent_reserves_respect_stop_threshold" \
    sed -i "s/AND (spent + reserved + ?) <= stop_threshold/AND (spent + reserved + ?) <= budget_cap  -- MUTATED/"

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
    sed -i '0,/spent = spent + ?/s//spent = spent + ? + 999/'  # Only first occurrence

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

# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS (Task #2): Additional safety protections
# ─────────────────────────────────────────────────────────────────────────────

# 11. API auth rejects invalid secret
test_mutation 11 "API rejects invalid/missing secrets" \
    "api/main.py" \
    "tests/test_redgreen_safety.py::test_api_auth_rejects_invalid_secret" \
    sed -i 's/if token != ADMIN_SECRET:/if False and token != ADMIN_SECRET:/'

# 12. Episode 1,250 cap enforced
test_mutation 12 "Episode 1,250 credit cap" \
    "hfvg/budget.py" \
    "tests/test_redgreen_safety.py::test_episode_cap_enforced" \
    sed -i 's/if episode_total > EPISODE_CAP:/if False and episode_total > EPISODE_CAP:/'

# 13. DRY_RUN forces dry, never live
test_mutation 13 "DRY_RUN forces dry (never live)" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_redgreen_safety.py::test_dry_run_forces_dry_never_live" \
    sed -i 's/dry_run_env = os.getenv.*lower.*/dry_run_env = False  # MUTATED - force live/'

# 14. Canary route checks G1.08
test_mutation 14 "Canary route refuses without G1.08" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_route_checks_g108" \
    sed -i 's/if not g108_approved:/if False and not g108_approved:  # MUTATED/'

# 15. Canary route checks live mode in non-dry
test_mutation 15 "Canary route requires live_mode in non-dry" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_route_checks_live_mode" \
    sed -i 's/if not dry_run and not live_mode:/if False and not dry_run and not live_mode:  # MUTATED/'

# 16. Canary route actually starts workflow
test_mutation 16 "Canary route starts ShotWorkflow" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_route_starts_workflow" \
    python3 scripts/mutate_workflow_start.py

# 17. Canary reserves L6 before workflow
test_mutation 17 "Canary reserves L6_reserve before workflow" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_reserves_l6_before_workflow" \
    sed -i 's/reserved = await ledger.reserve/reserved = True  # MUTATED: skip; await ledger.reserve/'

# 18. Still activity reserves budget
test_mutation 18 "Still activity reserves before provider call" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_redgreen_safety.py::test_still_activity_reserves_budget" \
    python3 scripts/mutate_reserve.py

# 19. Clip idempotency key
test_mutation 19 "Clip sends Idempotency-Key header" \
    "hfvg/providers/kling_video.py" \
    "tests/test_redgreen_safety.py::test_clip_idempotency_key_sent" \
    sed -i 's/headers = {"Idempotency-Key": idempotency_key}/headers = {}  # MUTATED - no idempotency key/'

# 20. Still approve sends signal
test_mutation 20 "Still approve route sends signal" \
    "api/main.py" \
    "tests/test_api_routes.py::test_still_approve_route_sends_signal" \
    sed -i 's/await handle.signal("stills_approved")/# MUTATED: skip signal; await handle.signal("stills_approved")/'

# 21-22: Clip approve tests removed (route removed - workflow doesn't wait for signal)

# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS (Fixes #1-#8): PR #7 safety protections
# ─────────────────────────────────────────────────────────────────────────────

# 23. G4.09 picture lock (folded from redgreen_new.sh)
test_mutation 23 "G4.09 picture lock blocks audio" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_episode_v2.py::test_episode_v2_picture_lock_blocks_audio" \
    sed -i 's/await workflow.wait_condition(lambda: self.approved_g409)/# await workflow.wait_condition(lambda: self.approved_g409)  # MUTATED/'

# 24. GX.01 hold (folded from redgreen_new.sh)
test_mutation 24 "GX.01 HOLD prevents auto-post" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_episode_v2.py::test_episode_v2_gx01_hold" \
    sed -i 's/await workflow.wait_condition(lambda: self.approved_gx01)/# await workflow.wait_condition(lambda: self.approved_gx01)  # MUTATED/'

# 25. Stills approved wait (retargeted from human-approved check)
test_mutation 25 "Workflow waits for parent stills_approved signal" \
    "hfvg/workflows/shot.py" \
    "tests/test_shot_live.py::test_shot_workflow_human_approval_proceeds_to_clip" \
    sed -i 's/await workflow.wait_condition(lambda: self.stills_approved)/# await workflow.wait_condition(lambda: self.stills_approved)  # MUTATED/'

# 26. Idempotent release (clamp to current reserved)
test_mutation 26 "Idempotent release clamps to reserved" \
    "hfvg/budget.py" \
    "tests/test_exception_release.py::test_idempotent_release_safe" \
    python3 scripts/mutate_release_check.py

# 27. Overage commits actual cost
test_mutation 27 "Overage commits actual_cost not reserved" \
    "hfvg/budget.py" \
    "tests/test_exception_release.py::test_commit_handles_overage" \
    python3 scripts/mutate_overage.py

# 28. 5xx poll retry before release
test_mutation 28 "5xx poll retries before releasing budget" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_exception_release.py::test_poll_502_releases_reservation" \
    sed -i 's/max_retries_5xx = 3/max_retries_5xx = 0  # MUTATED - no retry/'

# 29. Canary L6 reconcile on failure
test_mutation 29 "Canary reconciles L6 on workflow failure" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_l6_reconcile_on_failure" \
    sed -i '/Workflow failed: release/,+1 s/await _reconcile_canary_l6/# await _reconcile_canary_l6  # MUTATED/'

# 30. QC fail-closed without episode_id
test_mutation 30 "QC fails closed when episode_id is missing" \
    "hfvg/activities/qc.py" \
    "tests/test_shot_live.py::test_qc_fail_closed_without_episode_id" \
    sed -i 's/if not episode_id:/if False and not episode_id:  # MUTATED - allow missing episode_id/'

# 31. QC escalates in live mode
test_mutation 31 "QC escalates to human review in live mode" \
    "hfvg/activities/qc.py" \
    "tests/test_shot_live.py::test_qc_escalates_in_live_mode" \
    sed -i 's/"escalate": True,/"escalate": False,  # MUTATED - auto-pass instead of escalate/'

# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS (Final Fix Round): R2, R3, R4, R5
# ─────────────────────────────────────────────────────────────────────────────

# 32. M-R2a: Random workflow-ID suffix for canary (409 can never fire)
test_mutation 32 "Canary uses stable workflow ID (409 detection)" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_concurrent_409" \
    sed -i 's/workflow_id = f"{episode_id}-canary-{first_shot_id}"/workflow_id = f"{episode_id}-canary-{first_shot_id}-{uuid.uuid4().hex[:8]}"  # MUTATED/'

# 33. M-R2b: Delete WorkflowAlreadyStartedError -> 409 handling
test_mutation 33 "Canary handles WorkflowAlreadyStartedError with 409" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_concurrent_409" \
    sed -i '/except WorkflowAlreadyStartedError:/,/^    except Exception/d'

# 34. M-R2d: Approve route lets exception propagate (500) on completed canary
test_mutation 34 "Approve route returns 409 on completed workflow" \
    "api/main.py" \
    "tests/test_api_routes.py::test_approve_completed_canary_409" \
    sed -i 's/if status != WorkflowExecutionStatus.RUNNING:/if False and status != WorkflowExecutionStatus.RUNNING:  # MUTATED/'

# 35. M-R2e: Live-mode estimate failure falls back to 10.0
test_mutation 35 "Live canary fails closed without estimate" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_live_estimate_fail_closed" \
    sed -i 's/raise HTTPException(/canary_cost = 10.0  # MUTATED fallback; raise HTTPException(/' | head -1

# 36. M-R3a: Drop character descriptions from composed prompt
test_mutation 36 "Prompt includes character descriptions" \
    "hfvg/continuity_parser.py" \
    "tests/test_continuity_parser.py::test_prompt_kit_resolves_characters" \
    sed -i '/char_descriptions.append(char_info\["description"\])/d'

# 37. M-R3b: Allow live still with unresolved character code
test_mutation 37 "Live mode refuses unresolved character codes" \
    "hfvg/continuity_parser.py" \
    "tests/test_continuity_parser.py::test_prompt_kit_live_refuses_unresolved" \
    sed -i 's/if prompt_kit and unresolved_codes:/if False and prompt_kit and unresolved_codes:  # MUTATED/'

# 38. M-R3d: Omit aspect_ratio from still request
test_mutation 38 "Still request includes aspect_ratio" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_provider_contracts.py::test_still_includes_aspect_ratio" \
    sed -i 's/aspect_ratio=aspect_ratio,/# aspect_ratio=aspect_ratio,  # MUTATED/'

# 39. M-R4a: Change conversion constant to 1.0
test_mutation 39 "App-to-API credit conversion constant is 0.76" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_conversion_constant_is_076" \
    sed -i 's/APP_TO_API_CREDIT_CONVERSION = 0.76/APP_TO_API_CREDIT_CONVERSION = 1.0  # MUTATED/'

# 40. M-R4b: Leave episode cap unconverted (1250)
test_mutation 40 "Episode cap converted to API credits (950)" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_episode_cap_converted" \
    sed -i 's/EPISODE_CAP = 950.0/EPISODE_CAP = 1250.0  # MUTATED - not converted/'

# 41. M-R4c: Change >= to > in stop check
test_mutation 41 "Reserve to exactly stop threshold triggers at_stop" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_80_percent_stop" \
    sed -i 's/total_committed >= stop_threshold/total_committed > stop_threshold  # MUTATED/'

# 42. M-R5a: review_clip returns passed=True in live mode
test_mutation 42 "review_clip escalates in live mode" \
    "hfvg/activities/qc.py" \
    "tests/test_shot_live.py::test_review_clip_escalates_in_live_mode" \
    sed -i 's/return {$/return {"passed": True, "issues": []}  # MUTATED; return {/'

# 43. M-R5b: DRY_RUN=false + DB live off returns passed=True
test_mutation 43 "DRY_RUN=false + DB live off escalates" \
    "hfvg/activities/qc.py" \
    "tests/test_shot_live.py::test_review_clip_dry_run_false_live_off_escalates" \
    sed -i 's/"escalate": True,$/"passed": True, "escalate": False,  # MUTATED/'

# Print final summary
print_summary
exit_code=$?

# Return to main workspace and verify it's clean
cd "$WORKSPACE_ROOT"

echo ""
log_header "VERIFYING MAIN WORKSPACE IS CLEAN"
if [ -n "$(git status --porcelain -- hfvg api)" ]; then
    echo -e "${RED}✗✗✗ CRITICAL: Main workspace has uncommitted changes in hfvg/ or api/${NC}"
    echo -e "${RED}Mutations leaked into the working tree!${NC}"
    git status --porcelain -- hfvg api
    exit 1
else
    echo -e "${GREEN}✓ Main workspace is clean (no mutations leaked)${NC}"
fi

exit $exit_code
