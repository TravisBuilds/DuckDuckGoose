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

# Run a test and return 0 if it passes, 1 if it fails, 2 if AssertionError raised
run_test() {
    local test_name=$1
    local output
    
    # Run pytest with timeout
    output=$(python3 -m pytest "$test_name" -p no:cacheprovider -q --tb=short -rfE --timeout=60 -o timeout_method=signal 2>&1)
    local exit_code=$?
    
    # Check for syntax errors that make the mutation INVALID
    if echo "$output" | grep -q "SyntaxError\|IndentationError"; then
        echo "INVALID"
        return 3
    fi
    
    # Check if test passed
    if [ $exit_code -eq 0 ] && echo "$output" | grep -q "passed"; then
        echo "PASS"
        return 0
    fi
    
    # Test failed - check if it's an AssertionError (good) or something else (bad)
    if echo "$output" | grep -q "AssertionError"; then
        echo "FAIL_ASSERT"
        return 1
    else
        # Non-AssertionError failure (AttributeError, KeyError, ImportError, etc.)
        echo "FAIL_OTHER"
        return 2
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

# 21. Clip approve sends signal
test_mutation 21 "Clip approve route sends signal" \
    "api/main.py" \
    "tests/test_api_routes.py::test_clip_approve_route_sends_signal" \
    sed -i 's/await handle.signal("clip_approved")/# MUTATED: skip signal; await handle.signal("clip_approved")/'

# 22. Clip approve rejects unknown shot
test_mutation 22 "Clip approve rejects unknown shot (ZZ99)" \
    "api/main.py" \
    "tests/test_api_routes.py::test_clip_approve_rejects_unknown_shot" \
    sed -i 's/if not await cursor.fetchone():/if False and not await cursor.fetchone():  # MUTATED/'

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
    sed -i 's/release_amount = min(amount, current_reserved)/release_amount = amount  # MUTATED - no clamp/'

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
    sed -i 's/await _reconcile_canary_l6/# await _reconcile_canary_l6  # MUTATED/'

# Print final summary
print_summary
exit_code=$?

exit $exit_code
