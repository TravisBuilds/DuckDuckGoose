#!/bin/bash
#
# Red-green safety proof: Mutate protections and verify tests go RED, then GREEN when restored.
#
# Uses a temporary git worktree to isolate mutations - NEVER edits the main working tree.
# Treats syntax errors, SQL errors, and network failures as INVALID mutations.
# Prints a summary table at the end.
#
# Sharding (CI runs 4 shards in parallel, see .github/workflows/test.yml):
#   REDGREEN_SHARD=N REDGREEN_TOTAL_SHARDS=M bash scripts/redgreen.sh
#   Every declared probe/mutation gets a 0-based ordinal in declaration order; shard N runs the
#   ones with (ordinal % M) == N - 1 (round-robin, so shards stay balanced as mutations are added).
#   Each run writes one "<id>\t<status>\t<reason>" line per probe/mutation it ran to
#   REDGREEN_RESULTS_FILE (default /tmp/redgreen-results-shard-N-of-M.tsv). The CI summary job
#   feeds all shard files to scripts/redgreen_coverage.py, which fails unless the union covers
#   every declared id exactly once with PASS (or a SKIP listed in scripts/redgreen_skip.conf).
#

set -eu  # Fail on undefined variables AND command errors

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE_ROOT"

SHARD_NUM="${REDGREEN_SHARD:-1}"
TOTAL_SHARDS="${REDGREEN_TOTAL_SHARDS:-1}"
if ! [[ "$SHARD_NUM" =~ ^[0-9]+$ && "$TOTAL_SHARDS" =~ ^[0-9]+$ ]] \
   || [ "$TOTAL_SHARDS" -lt 1 ] || [ "$SHARD_NUM" -lt 1 ] || [ "$SHARD_NUM" -gt "$TOTAL_SHARDS" ]; then
    echo "Invalid REDGREEN_SHARD=$SHARD_NUM / REDGREEN_TOTAL_SHARDS=$TOTAL_SHARDS (need 1 <= N <= M)" >&2
    exit 2
fi
echo "Running shard $SHARD_NUM of $TOTAL_SHARDS"
RESULTS_FILE="${REDGREEN_RESULTS_FILE:-/tmp/redgreen-results-shard-${SHARD_NUM}-of-${TOTAL_SHARDS}.tsv}"
case "$RESULTS_FILE" in /*) ;; *) RESULTS_FILE="$WORKSPACE_ROOT/$RESULTS_FILE" ;; esac
DECL_ORDINAL=-1

# True when the next declared probe/mutation belongs to this shard (advances the ordinal).
next_in_shard() {
    DECL_ORDINAL=$((DECL_ORDINAL + 1))
    [ $((DECL_ORDINAL % TOTAL_SHARDS)) -eq $((SHARD_NUM - 1)) ]
}

# Load skip configuration
SKIP_CONFIG="$WORKSPACE_ROOT/scripts/redgreen_skip.conf"
declare -A SKIP_REASONS
if [ -f "$SKIP_CONFIG" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        
        # Parse "M-<num> reason" format
        if [[ "$line" =~ ^M-([0-9]+)[[:space:]]+(.+)$ ]]; then
            num="${BASH_REMATCH[1]}"
            reason="${BASH_REMATCH[2]}"
            SKIP_REASONS[$num]="$reason"
        fi
    done < "$SKIP_CONFIG"
fi

# Check if a mutation should be skipped
should_skip() {
    local num=$1
    [ -n "${SKIP_REASONS[$num]:-}" ]
}

get_skip_reason() {
    local num=$1
    echo "${SKIP_REASONS[$num]:-}"
}

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
NC='\033[0m'

# Results tracking
declare -a MUTATION_NAMES
declare -a MUTATION_IDS
declare -a MUTATION_RESULTS  # "PASS" or "FAIL"
declare -a MUTATION_REASONS

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
INVALID_COUNT=0

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
    python3 -m pytest "$test_name" -p no:cacheprovider -q --junitxml="$junit_xml" --timeout=60 -o timeout_method=signal -o addopts="--disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1,localhost -p no:warnings" > "$tmp_output" 2>&1
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
    
    next_in_shard || return 0
    log_test "$num" "$name"
    
    # Step 1: Run baseline (should pass)
    log_green "Running baseline test (expecting PASS)..."
    baseline_result=$(run_test "$test")
    
    if [ "$baseline_result" != "PASS" ]; then
        echo -e "${RED}✗ FAIL: Baseline test does not pass${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Baseline fails")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Baseline passes${NC}"
    
    # Step 2: Apply mutation
    log_red "Applying mutation to $file..."
    "${mutation_cmd[@]}" "$file" 2>/dev/null || true  # a failing mutator is caught as "No file change"
    
    if ! git diff --quiet "$file"; then
        echo -e "${BLUE}Mutation applied (file changed)${NC}"
    else
        echo -e "${RED}✗ FAIL: Mutation did not change the file${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
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
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Syntax error")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" = "PASS" ]; then
        echo -e "${RED}✗ FAIL: Mutated test still passes${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Not detected")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    # Check if classification matches expected
    if [ "$mutated_result" != "$expected_class" ]; then
        echo -e "${RED}✗ FAIL: Expected $expected_class but got $mutated_result${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Wrong classification: $mutated_result")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Classified correctly as $expected_class${NC}"
    echo -e "${GREEN}━━━ ✓ PROBE VERIFICATION PASSED ━━━${NC}"
    
    MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
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
    
    next_in_shard || return 0
    log_test "$num" "$name"
    
    # Check if this mutation should be skipped
    if should_skip "$num"; then
        local skip_reason=$(get_skip_reason "$num")
        echo -e "${YELLOW}⊘ SKIP: $skip_reason${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("SKIP")
        MUTATION_REASONS+=("$skip_reason")
        SKIP_COUNT=$((SKIP_COUNT + 1))
        return
    fi
    
    # Step 1: Run baseline (should pass)
    log_green "Running baseline test (expecting PASS)..."
    baseline_result=$(run_test "$test")
    baseline_exit=$?
    
    if [ "$baseline_result" = "INVALID" ]; then
        echo -e "${RED}✗ INVALID: Baseline test has errors (syntax/import/SQL)${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("Baseline has errors")
        INVALID_COUNT=$((INVALID_COUNT + 1))
        return
    fi
    
    if [ "$baseline_result" != "PASS" ]; then
        echo -e "${RED}✗ FAIL: Baseline test does not pass${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Baseline fails")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Baseline passes${NC}"
    
    # Step 2: Apply mutation
    log_red "Applying mutation to $file..."
    "${mutation_cmd[@]}" "$file" 2>/dev/null || true  # a failing mutator is caught as "No file change"
    
    # Verify mutation actually changed the file
    if ! git diff --quiet "$file"; then
        echo -e "${BLUE}Mutation applied (file changed)${NC}"
    else
        echo -e "${RED}✗ INVALID: Mutation did not change the file${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("No file change")
        INVALID_COUNT=$((INVALID_COUNT + 1))
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
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("INVALID")
        MUTATION_REASONS+=("Syntax error")
        INVALID_COUNT=$((INVALID_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" = "FAIL_OTHER" ]; then
        echo -e "${RED}✗ FAIL: Test failed with non-AssertionError (${mutated_result})${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Non-AssertionError")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$mutated_result" != "FAIL_ASSERT" ]; then
        echo -e "${RED}✗ FAIL: Mutated test still passes (mutation had no effect)${NC}"
        git checkout -- "$file" 2>/dev/null
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
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
        MUTATION_IDS+=("$num")
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
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Restore failed")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    if [ "$restored_result" != "PASS" ]; then
        echo -e "${RED}✗ FAIL: Restored test does not pass${NC}"
        MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
        MUTATION_RESULTS+=("FAIL")
        MUTATION_REASONS+=("Restore didn't fix")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    
    echo -e "${GREEN}✓ Restored test passes (green confirmed)${NC}"
    echo -e "${GREEN}━━━ ✓ RED-GREEN PROOF PASSED ━━━${NC}"
    
    MUTATION_NAMES+=("$num. $name")
        MUTATION_IDS+=("$num")
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
        elif [ "$result" = "SKIP" ]; then
            printf "${YELLOW}%-50s ⊘ SKIP     %-30s${NC}\n" "$name" "$reason"
        elif [ "$result" = "INVALID" ]; then
            printf "${YELLOW}%-50s ⚠ INVALID  %-30s${NC}\n" "$name" "$reason"
        else
            printf "${RED}%-50s ✗ FAIL     %-30s${NC}\n" "$name" "$reason"
        fi
    done
    
    echo ""
    echo -e "${BLUE}Total tests: ${#MUTATION_NAMES[@]}${NC}"
    echo -e "${GREEN}Passed: $PASS_COUNT${NC}"
    echo -e "${YELLOW}Skipped: $SKIP_COUNT${NC}"
    echo -e "${YELLOW}Invalid: $INVALID_COUNT${NC}"
    echo -e "${RED}Failed: $FAIL_COUNT${NC}"
    echo ""
    
    # An INVALID id (baseline/mutant could not even run: missing deps, syntax/import error) proved
    # nothing, so it fails the shard too - a broken shard must not report green on its own.
    if [ $FAIL_COUNT -eq 0 ] && [ $INVALID_COUNT -eq 0 ]; then
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
    sed -i "s/AND (spent_usd_micros + reserved_usd_micros + ?) <= stop_usd_micros/AND (spent_usd_micros + reserved_usd_micros + ?) <= cap_usd_micros  -- MUTATED/"

# 4. Activity-level enforcement
test_mutation 4 "Activity-level enforcement (not just API)" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_redgreen_safety.py::test_activity_level_enforcement" \
    sed -i 's/if not live_mode:/if False:  # MUTATED/'

# 5. Idempotency
test_mutation 5 "Idempotent retry (no double-charge)" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_redgreen_safety.py::test_idempotent_retry_no_double_charge" \
    sed -i 's/key_input = f"{model_path}|{prompt}|{resolution}|{quality}|{aspect_ratio or \x27\x27}|{refs_str}|{anchor_str}|attempt:{attempt}"/key_input = str(uuid.uuid4())  # MUTATED - random idempotency key/'

# 6. Auth fail-closed
test_mutation 6 "Auth fail-closed (no default secret)" \
    "api/main.py" \
    "tests/test_redgreen_safety.py::test_auth_fail_closed" \
    sed -i -e 's/if not authorization:/if False:  # MUTATED/' -e 's/^    if (method, path) in PUBLIC_ROUTES:$/    if True:  # MUTATED: bypass the app-wide guard so the handler check is what is tested/'

# 7. Ledger math
test_mutation 7 "Ledger math (reserve, commit, release)" \
    "hfvg/budget.py" \
    "tests/test_studio_safety.py::test_ledger_math_reserve_commit_release" \
    sed -i '0,/spent_usd_micros = spent_usd_micros + ?/s//spent_usd_micros = spent_usd_micros + ? + 999/'  # Only first occurrence

# 8. Hard cap
test_mutation 8 "Hard cap enforcement" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_hard_cap_enforcement" \
    sed -i "s/if spent + reserved + amount > cap:/if False and spent + reserved + amount > cap:/"

# 9. Canary starts ShotWorkflow
test_mutation 9 "Canary starts real ShotWorkflow" \
    "hfvg/workflows/shot.py" \
    "tests/test_redgreen_safety.py::test_canary_starts_shot_workflow" \
    sed -i 's/@workflow.defn/#@workflow.defn  # MUTATED/'

# 10. Approval update reaches workflow (retargeted to @workflow.update)
test_mutation 10 "Approval update reaches workflow" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_redgreen_safety.py::test_approval_signal_reaches_episode_workflow" \
    sed -i -e '/@workflow.update/{N;/async def approve_g1_shot_plan/s/@workflow.update/# @workflow.update  # MUTATED/}' \
           -e 's/^\(\s*\)@approve_g1_shot_plan\.validator/\1# @approve_g1_shot_plan.validator  # MUTATED (validator needs the update; else import-time AttributeError)/' 

# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS (Task #2): Additional safety protections
# ─────────────────────────────────────────────────────────────────────────────

# 11. API auth rejects invalid secret
test_mutation 11 "API rejects invalid/missing secrets" \
    "api/main.py" \
    "tests/test_redgreen_safety.py::test_api_auth_rejects_invalid_secret" \
    sed -i 's/^    if not request.admin_secret or not secrets.compare_digest($/    if False and secrets.compare_digest(  # MUTATED/'

# 12. Episode 1,250 cap enforced
test_mutation 12 "Episode 60.00 USD cap" \
    "hfvg/budget.py" \
    "tests/test_redgreen_safety.py::test_episode_cap_enforced" \
    sed -i 's/if episode_total + amount > settings.episode_cap_usd_micros:/if False and episode_total + amount > settings.episode_cap_usd_micros:/'

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
    sed -i '/reserved = await ledger.reserve(/,/^        )$/c\        reserved = True  # MUTATED: skip the L6 reserve'

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

# 23. Audio stages (Round 4, G-AUD-1b): audio/mix/delivery runs only once the picture is
# assembled, current and not blocked by assembly QC (the old picture-lock gate G4.09 was removed in the
# 3-gate system; its test stays skipped and untouched). Intent: picture lock required before audio.
test_mutation 23 "M-23: Audio waits for a locked (non-blocking) picture" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_audio_delivery_workflow.py::TestDeliveryInEpisode::test_audio_waits_for_picture_lock" \
    sed -i 's/^        if self.assembly_sig is None or self._assembly_stale() or self._assembly_blocking():$/        if self.assembly_sig is None or self._assembly_stale():  # MUTATED/'

# 24. Delivery (Round 4, G-AUD-1b): the delivered master never auto-proceeds; G_FINAL_CUT still needs
# a human approval after a passing delivery QC (GX.01 was removed in the 3-gate system; its test stays
# skipped and untouched). Intent: delivery hold prevents auto-posting without approval.
test_mutation 24 "M-24: Delivery holds at G_FINAL_CUT until a human approves" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_audio_delivery_workflow.py::TestDeliveryInEpisode::test_delivery_pass_holds_for_final_cut_approval" \
    sed -i 's/^                        self.approved_g_final_cut and not self._assembly_blocking()$/                        not self._assembly_blocking()  # MUTATED/'

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
    sed -i 's/await _reconcile_canary_l6/pass  # MUTATED; await _reconcile_canary_l6/'

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
    sed -i '/^    except WorkflowAlreadyStartedError:/,/^    except Exception as e:/{/^    except Exception as e:/!d}'

# 34. M-R2d: Approve route lets exception propagate (500) on completed canary
test_mutation 34 "Approve route returns 409 on completed workflow" \
    "api/main.py" \
    "tests/test_api_routes.py::test_approve_completed_canary_409" \
    sed -i 's/if status != WorkflowExecutionStatus.RUNNING:/if False and status != WorkflowExecutionStatus.RUNNING:  # MUTATED/'

# 35. M-R2e: Live-mode estimate failure falls back to a default amount
test_mutation 35 "Live canary fails closed without estimate" \
    "api/main.py" \
    "tests/test_api_routes.py::test_canary_live_estimate_fail_closed" \
    sed -i -e '/still_estimate = await still_provider.estimate_usd_micros(/,/await still_provider.close()/{s/raise HTTPException(/still_estimate = 100_000; _mutated = dict(  # MUTATED fallback estimate/}' -e '/clip_estimate = await clip_provider.estimate_usd_micros(/,/await clip_provider.close()/{s/raise HTTPException(/clip_estimate = 100_000; _mutated = dict(  # MUTATED fallback estimate/}'

# 36. M-R3a: Drop character descriptions from composed prompt
test_mutation 36 "Prompt includes character descriptions" \
    "hfvg/continuity_parser.py" \
    "tests/test_continuity_parser.py::test_prompt_kit_resolves_characters" \
    sed -i 's/char_descriptions.append(char_info\["description"\])/pass  # MUTATED - description dropped/'

# 37. M-R3b: Allow live still with unresolved character code
test_mutation 37 "Live mode refuses unresolved character codes" \
    "hfvg/continuity_parser.py" \
    "tests/test_continuity_parser.py::test_prompt_kit_live_refuses_unresolved" \
    sed -i 's/if prompt_kit and unresolved_codes:/if False and prompt_kit and unresolved_codes:  # MUTATED/'

# 38. M-R3d: Omit aspect_ratio from still request
test_mutation 38 "Still request includes aspect_ratio" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_provider_contracts.py::test_still_activity_passes_aspect_ratio" \
    sed -i 's/aspect_ratio=aspect_ratio,/# aspect_ratio=aspect_ratio,  # MUTATED/'

# 39. Legacy plan import conversion (app credits -> usd_micros) constant
test_mutation 39 "Legacy plan import converts at 47_500 usd_micros per app credit" \
    "hfvg/budget.py" \
    "tests/test_ledger_races.py::test_stop_threshold_validation" \
    sed -i 's/^LEGACY_APP_CREDIT_USD_MICROS = 47_500/LEGACY_APP_CREDIT_USD_MICROS = 75_000  # MUTATED/'

# 40. M-R4b: Episode cap default no longer 60.00 USD
test_mutation 40 "Episode cap default is 60.00 USD" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_default_settings_are_literal_dollars" \
    sed -i 's/^DEFAULT_EPISODE_CAP_USD = "60.00"/DEFAULT_EPISODE_CAP_USD = "1250.00"  # MUTATED/'

# 41. M-R4c: Change >= to > in stop check
test_mutation 41 "Reserve to exactly stop threshold triggers at_stop" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_80_percent_stop" \
    sed -i 's/at_stop = total_committed >= stop$/at_stop = total_committed > stop  # MUTATED/'

# 42. M-R5a: review_clip returns passed=True in live mode
test_mutation 42 "review_clip escalates in live mode" \
    "hfvg/activities/qc.py" \
    "tests/test_shot_live.py::test_review_clip_escalates_in_live_mode" \
    sed -i '/async def review_clip/,${/Live mode confirmed/,${s/"passed": False,/"passed": True,  # MUTATED/}}'

# 43. M-R5b: DRY_RUN=false + DB live off returns passed=True
test_mutation 43 "DRY_RUN=false + DB live off escalates" \
    "hfvg/activities/qc.py" \
    "tests/test_shot_live.py::test_review_clip_dry_run_false_live_off_escalates" \
    sed -i 's/"escalate": True,$/"passed": True, "escalate": False,  # MUTATED/'

# ─────────────────────────────────────────────────────────────────────────────
# R1 MUTATIONS: Poll timeout behavior (keeps reservation, marks pending_reconcile)
# ─────────────────────────────────────────────────────────────────────────────

# M-R1a: in the timeout/cancel/exhaustion path, call the ledger release (restore old behaviour)
test_mutation 44 "Poll exhaustion keeps reservation (no release)" \
    "hfvg/workflows/shot.py" \
    "tests/test_poll_timeout.py::test_poll_timeout_keeps_reservation" \
    sed -i '/Exhausted polling window/,/raise RuntimeError/{ /await workflow.execute_activity/,/retry_policy=retry_policy,/{ s/mark_job_pending_reconcile/release_job_budget/; s/args=\[self.episode_id, self.shot_id, job_id, job_type\]/args=[self.episode_id, self.shot_id, job_id, job_type, line_name, reserved_amount, "Poll exhausted"]/; } }'

# M-R1b: make the generic exception handler release the reservation
test_mutation 45 "Poll exception does not release reservation" \
    "hfvg/workflows/shot.py" \
    "tests/test_poll_timeout.py::test_poll_activity_errors_do_not_release_runtime" \
    sed -i '/async def _poll_job_to_completion/,/@workflow.run/{/Poll activity error on attempt/a\                await workflow.execute_activity(release_job_budget, args=[self.episode_id, self.shot_id, job_id, job_type, line_name, reserved_amount, "Exception"], start_to_close_timeout=timedelta(seconds=30), retry_policy=retry_policy)  # MUTATED - release on exception
}'

# M-R1c: on poll exhaustion, resubmit the job once
test_mutation 46 "Poll exhaustion never resubmits" \
    "hfvg/workflows/shot.py" \
    "tests/test_poll_timeout.py::test_poll_single_provider_submit" \
    sed -i '/Exhausted polling window/i\        # MUTATED: resubmit once\n        await workflow.execute_activity(submit_still_job_enforced, args=[self.episode_id, self.shot_id, "retry", 1, [], "1k", "medium", "9:16"], start_to_close_timeout=timedelta(minutes=2), retry_policy=retry_policy)\n'

# ─────────────────────────────────────────────────────────────────────────────
# P1 MUTATIONS: dollar ledger (integer usd_micros)
# ─────────────────────────────────────────────────────────────────────────────

# 47. M-P1a: `>=` to `>` in the episode stop check (summary flag at exactly 48.00 USD)
test_mutation 47 "M-P1a Episode at_stop uses >= (exactly 48.00 USD is at stop)" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_episode_cap_default_60_usd_and_stop_48" \
    sed -i 's/"episode_at_stop": episode_total >= settings.episode_stop_usd_micros,/"episode_at_stop": episode_total > settings.episode_stop_usd_micros,  # MUTATED/'

# 48. M-P1b: stop uses the cap instead of 80%
test_mutation 48 "M-P1b Stop threshold is 80% of cap, not the cap" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_init_episode_budget" \
    sed -i 's|^    return cap_usd_micros \* stop_percent // 100$|    return cap_usd_micros  # MUTATED: stop == cap|'

# 49. M-P1c: release can go negative
test_mutation 49 "M-P1c Release is clamped to the hold (never negative)" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_release_over_hold_is_clamped_never_negative" \
    python3 scripts/mutate_release_check.py

# 50. M-P1d: GC.01 headroom check removed
test_mutation 50 "M-P1d GC.01 headroom refuses low balance" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_gc01_headroom_refuses_when_balance_too_low" \
    sed -i 's/if balance_after_hold < settings.gc01_headroom_usd_micros:/if False and balance_after_hold < settings.gc01_headroom_usd_micros:  # MUTATED/'

# 51. M-P1e: commit uses reserved instead of actual on overage
test_mutation 51 "M-P1e Commit spends actual cost on overage" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_commit_overage_spends_actual_not_reserved" \
    python3 scripts/mutate_overage.py

# 52. M-P1f: float arithmetic reintroduced (cap * 0.8)
test_mutation 52 "M-P1f Stop uses integer arithmetic (no float drift)" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_stop_is_integer_80_percent_no_float_drift" \
    sed -i 's|^    return cap_usd_micros \* stop_percent // 100$|    return cap_usd_micros * 0.8  # MUTATED: float|'

# 53. M-P1g: revision reserve usable without the revision tag
test_mutation 53 "M-P1g Revision reserve requires the revision tag" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_revision_reserve_requires_revision_tag" \
    sed -i 's/if line_name == REVISION_LINE and not revision:/if False and line_name == REVISION_LINE and not revision:  # MUTATED/'

# 54. M-P1h: micro-dollar conversion off by 10x
test_mutation 54 "M-P1h USD to usd_micros conversion is 1_000_000" \
    "hfvg/pricing.py" \
    "tests/test_budget.py::test_usd_micro_conversion_literals" \
    sed -i 's/^MICROS_PER_USD = 1_000_000/MICROS_PER_USD = 10_000_000  # MUTATED/'

# ─────────────────────────────────────────────────────────────────────────────
# P2/P3 MUTATIONS: Seedance, routing, declined_preset_id, prompt lint
# ─────────────────────────────────────────────────────────────────────────────

# 55. M-P2a Seedance generate_audio is always false
test_mutation 55 "M-P2a Seedance generate_audio is always false" \
    "hfvg/providers/seedance_video.py" \
    "tests/test_seedance_routing.py::test_seedance_request_body_exact_and_audio_false" \
    sed -i 's/"generate_audio": False,  # always false/"generate_audio": True,  # MUTATED/'

# 56. M-P2b Seedance refuses duration 3 (min 4 s)
test_mutation 56 "M-P2b Seedance refuses duration 3 (min 4 s)" \
    "hfvg/providers/seedance_video.py" \
    "tests/test_seedance_routing.py::test_seedance_refuses_short_and_long_durations_before_any_request" \
    sed -i 's/if duration < MIN_DURATION_S:/if duration < 3:  # MUTATED/'

# 57. M-P2c Planner never routes wet shots to Seedance
test_mutation 57 "M-P2c Planner never routes wet shots to Seedance" \
    "hfvg/routing.py" \
    "tests/test_seedance_routing.py::test_planner_never_routes_wet_shot_to_seedance" \
    sed -i 's/^        if blocked:/        if blocked and False:  # MUTATED/'

# 58. M-P2d declined_preset_id is sent on Kling requests
test_mutation 58 "M-P2d declined_preset_id is sent on Kling requests" \
    "hfvg/providers/kling_video.py" \
    "tests/test_seedance_routing.py::test_declined_preset_sent_on_kling_request" \
    sed -i 's/^        apply_declined_preset(payload)$/        pass  # MUTATED/'

# 59. M-P2e declined_preset_id is sent on still requests
test_mutation 59 "M-P2e declined_preset_id is sent on still requests" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_seedance_routing.py::test_declined_preset_sent_on_still_request" \
    sed -i 's/^        apply_declined_preset(payload)$/        pass  # MUTATED/'

# 60. M-P2f Seedance end_image_url is sent
test_mutation 60 "M-P2f Seedance end_image_url is sent" \
    "hfvg/providers/seedance_video.py" \
    "tests/test_seedance_routing.py::test_seedance_end_image_url_is_sent" \
    sed -i 's/^            payload\["end_image_url"\] = end_image_url$/            pass  # MUTATED/'

# 61. M-P2g Seedance model path is bytedance/seedance-2.5/image-to-video
test_mutation 61 "M-P2g Seedance model path is bytedance/seedance-2.5/image-to-video" \
    "hfvg/providers/seedance_video.py" \
    "tests/test_seedance_routing.py::test_seedance_model_path_is_documented_literal" \
    sed -i 's|^SEEDANCE_MODEL_PATH = .*|SEEDANCE_MODEL_PATH = "bytedance/seedance-2.0/image-to-video"  # MUTATED|'

# 62. M-P2h Seedance resolution is forwarded (not fixed 480p)
test_mutation 62 "M-P2h Seedance resolution is forwarded (not fixed 480p)" \
    "hfvg/providers/seedance_video.py" \
    "tests/test_seedance_routing.py::test_seedance_resolution_is_forwarded" \
    sed -i 's/^            "resolution": resolution,$/            "resolution": "480p",  # MUTATED/'

# 63. M-P2i Seedance 1080p estimate uses the 1080p rate
test_mutation 63 "M-P2i Seedance 1080p estimate uses the 1080p rate" \
    "hfvg/providers/seedance_video.py" \
    "tests/test_seedance_routing.py::test_seedance_estimates_use_per_resolution_rates" \
    sed -i 's/rate_table_estimate(SEEDANCE_RATE_KEY, resolution, duration)/rate_table_estimate(SEEDANCE_RATE_KEY, "480p", duration)  # MUTATED/'

# 64. M-P3a Lint checks the count clause
test_mutation 64 "M-P3a Lint checks the count clause" \
    "hfvg/prompt_lint.py" \
    "tests/test_prompt_lint.py::test_missing_count_clause_refused" \
    sed -i -e 's/^        if not found:/        if False:  # MUTATED/' -e 's/^        elif any(n != count for n in found):/        elif False:  # MUTATED/'

# 65. M-P3b Lint forbids 'nothing else moves'
test_mutation 65 "M-P3b Lint forbids 'nothing else moves'" \
    "hfvg/prompt_lint.py" \
    "tests/test_prompt_lint.py::test_nothing_else_moves_is_forbidden" \
    sed -i 's/^DEFAULT_FORBIDDEN_PHRASES = ("nothing else moves", "realistic wild")/DEFAULT_FORBIDDEN_PHRASES = ("realistic wild",)  # MUTATED/'

# 66. M-P3c Lint is enforced in live mode  
test_mutation 66 "M-P3c Lint is enforced in live mode" \
    "hfvg/prompt_lint.py" \
    "tests/test_prompt_lint.py::test_live_still_lint_failure_refuses_before_estimate_reserve_and_submit" \
    sed -i '/^    if problems and live:$/,/^    for problem in problems:$/{ /^    if problems and live:$/s/.*/    if problems and False:  # MUTATED/; }'

# 67. M-P3d Lint applies the banned-decor list
test_mutation 67 "M-P3d Lint applies the banned-decor list" \
    "hfvg/prompt_lint.py" \
    "tests/test_prompt_lint.py::test_banned_decor_words_forbidden" \
    sed -i 's/^    for word in kit.banned_decor:/    for word in ():  # MUTATED/'

# 68. M-P3e Lint requires the antler clause when an antler character is present
test_mutation 68 "M-P3e Lint requires the antler clause when an antler character is present" \
    "hfvg/prompt_lint.py" \
    "tests/test_prompt_lint.py::test_antler_clause_required_when_antler_character_present" \
    sed -i 's/^    if present \& {c.lower() for c in kit.antler_characters}:/    if False:  # MUTATED/'

# 69. M-R4a shot_meta.count comes from shots.json
test_mutation 69 "M-R4a shot_meta.count comes from shots.json" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_shot_meta_filled_from_shots_json" \
    sed -i 's/^                "count": count,$/                "count": count + 1,  # MUTATED/'

# 70. M-R4b prompt from shots.json is used as-is
test_mutation 70 "M-R4b prompt from shots.json is used as-is" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_prompt_is_used_as_is" \
    sed -i 's/^            "prompt": prompt,  # AS-IS: never recomposed$/            "prompt": prompt.strip(),  # MUTATED/'

# 71. M-R4c SHOTS_JSON_PATH env override is honoured
test_mutation 71 "M-R4c SHOTS_JSON_PATH env override is honoured" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_shots_json_path_env_override" \
    sed -i '/^def shots_json_path/,/^def refs_json_path/ s/return Path(override)/return Path("\/nonexistent-MUTATED")/'

# 72. M-R4d Live refuses when shots.json is missing
test_mutation 72 "M-R4d Live refuses when shots.json is missing" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_live_refuses_without_shots_json" \
    sed -i 's/^        if require:$/        if False:  # MUTATED/'

# 73. M-R4e Live refuses beatmap/shots.json id mismatch
test_mutation 73 "M-R4e Live refuses beatmap/shots.json id mismatch" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_merge_requires_matching_shot_sets_in_live" \
    sed -i 's/^    if require and beat_ids != set(plans):$/    if False:  # MUTATED/'

# 74. M-R4f DRY_RUN=false forces per-shot data requirement
test_mutation 74 "M-R4f DRY_RUN=false forces per-shot data requirement" \
    "hfvg/activities/workflow_support.py" \
    "tests/test_round4_shot_data.py::test_parse_beatmap_activity_merges_and_live_requires" \
    sed -i 's/^    return bool(require) or os.getenv("DRY_RUN", "true").lower() != "true"$/    return bool(require)  # MUTATED/'

# 75. M-R4g per-shot duration from shots.json (E05 12 s, 3 s stays 3 s)
test_mutation 75 "M-R4g per-shot duration from shots.json (E05 12 s, 3 s stays 3 s)" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_params_carry_model_duration_and_end_frame" \
    sed -i 's/^                "duration": duration,$/                "duration": 5.0,  # MUTATED/'

# 76. M-R4h per-shot model from shots.json
test_mutation 76 "M-R4h per-shot model from shots.json" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_params_carry_model_duration_and_end_frame" \
    sed -i 's/^                "model": model,$/                "model": "kling",  # MUTATED/'

# 77. M-R4i end_frame flag from shots.json
test_mutation 77 "M-R4i end_frame flag from shots.json" \
    "hfvg/shot_data.py" \
    "tests/test_round4_shot_data.py::test_params_carry_model_duration_and_end_frame" \
    sed -i 's/"end_frame": end_frame,/"end_frame": False,  # MUTATED/'

# 78. M-R4j dry clip reservation uses real model and duration
test_mutation 78 "M-R4j dry clip reservation uses real model and duration" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_round4_shot_data.py::test_dry_clip_reservation_uses_model_and_duration" \
    sed -i 's/clip_estimate_usd_micros(route.model, tier, duration)/clip_estimate_usd_micros("kling", tier, 5)  # MUTATED/'

# 79. M-R4k Seedance draft estimate uses the 480p rate
test_mutation 79 "M-R4k Seedance draft estimate uses the 480p rate" \
    "hfvg/pricing.py" \
    "tests/test_round4_shot_data.py::test_dry_clip_estimates_use_real_model_and_duration" \
    sed -i 's/^        resolution = "480p" if tier == "draft" else "1080p"$/        resolution = "1080p"  # MUTATED/'

# 80. M-R4l Live start+end shot without end image is refused
test_mutation 80 "M-R4l Live start+end shot without end image is refused" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_round4_shot_data.py::test_live_start_end_shot_without_end_image_is_refused_before_spend" \
    sed -i 's/^        if (shot_meta or {}).get("end_frame") and not end_image_url:$/        if False:  # MUTATED/'

# 81. M-R4m Live still held when planned refs are missing
test_mutation 81 "M-R4m Live still held when planned refs are missing" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_round4_shot_data.py::test_live_still_with_missing_refs_is_held_before_spend" \
    sed -i '/# Refs gate: a shot never runs/,+1 s/if missing_refs:/if False:  # MUTATED/'

# 82. M-R4n Live clip held when planned refs are missing
test_mutation 82 "M-R4n Live clip held when planned refs are missing" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_round4_shot_data.py::test_live_clip_with_missing_refs_is_held_before_spend" \
    sed -i '/# A shot never runs without its planned refs/,+1 s/if missing_refs:/if False:  # MUTATED/'

# 83. M-R4o Still activity refuses refs over the limit
test_mutation 83 "M-R4o Still activity refuses refs over the limit" \
    "hfvg/preflight.py" \
    "tests/test_round4_shot_data.py::test_live_still_over_ref_limit_is_refused_not_truncated" \
    sed -i 's/^    if len(assembled) > cap:$/    if False:  # MUTATED/'

# 84. M-R4p Still provider refuses instead of truncating refs
test_mutation 84 "M-R4p Still provider refuses instead of truncating refs" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_round4_shot_data.py::test_still_provider_refuses_instead_of_truncating_refs" \
    sed -i 's/^            if len(image_urls) > max_refs:$/            if False:  # MUTATED/'

# 85. M-R4q Workflow passes the shot's refs to the still activity
test_mutation 85 "M-R4q Workflow passes the shot's refs to the still activity" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_refs_and_missing_refs_to_still_activity" \
    sed -i '/submit_still_job_enforced/,/shot_plan\.get("missing_refs"/{ /^[[:space:]]*refs,$/s/.*/                                [],  # MUTATED - no refs/; }'

# 86. M-R4r Workflow passes missing refs to the still activity
test_mutation 86 "M-R4r Workflow passes missing refs to the still activity" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_refs_and_missing_refs_to_still_activity" \
    sed -i '/self\.shot_id,$/,/shot_plan\.get("missing_refs"/{ s/shot_plan\.get("missing_refs", \[\]),  # planned refs not on disk: live refuses$/[],  # MUTATED - no missing_refs/; }'

# 87. M-R4s Workflow uses the shot's real clip duration
test_mutation 87 "M-R4s Workflow uses the shot's real clip duration" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_real_model_duration_tags_to_clip" \
    sed -i 's/^                duration = params.get("duration", 5.0)$/                duration = 5.0  # MUTATED/'

# 88. M-R4t Workflow uses the shot's model
test_mutation 88 "M-R4t Workflow uses the shot's model" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_real_model_duration_tags_to_clip" \
    sed -i '/# explicit routing/s/params\.get("model", "kling"),/"kling",  # MUTATED/'

# 89. M-R4u Workflow passes shot tags (wet) to the clip activity
test_mutation 89 "M-R4u Workflow passes shot tags (wet) to the clip activity" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_real_model_duration_tags_to_clip" \
    sed -i '/# explicit routing/{n; s/shot_plan\.get("tags", \[\]),/[],  # MUTATED/; }'

# 90. M-R4v Workflow passes missing refs to the clip activity
test_mutation 90 "M-R4v Workflow passes missing refs to the clip activity" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_real_model_duration_tags_to_clip" \
    sed -i '/submit_clip_job_enforced/,/start_to_close_timeout=timedelta/{ /shot_plan.get("missing_refs", \[\]),/s/.*/                                [],  # MUTATED - no missing_refs/; }'

# 91. M-R4w End-frame generation succeeds in both dry and live modes
test_mutation 91 "M-R4w End-frame generation succeeds in both dry and live modes" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_end_frame_dry_gets_placeholder_and_live_waits_for_signal" \
    sed -i '/Generate end-frame still if required/,/end_image_url = end_still_result\["url"\]/{ /if params\.get("end_frame"):/s/.*/                    if False:  # MUTATED/; }'

# 92. M-R4x End-image URL is stored from the end-frame still result
test_mutation 92 "M-R4x End-image URL is stored from the end-frame still result" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_end_frame_dry_gets_placeholder_and_live_waits_for_signal" \
    sed -i '/self\.end_still_asset = end_still_result/{ n; s/self\.end_image_url = end_still_result\["url"\]/self.end_image_url = "MUTATED-url"/; }'

# 93. EXPECTED SKIP: Signal pattern obsolete (architecture changed)
# shot.py lines 83-117: end-frame generation happens synchronously in run() before stills_approved wait.
# Signal handler set_end_image (lines 132-135) exists but is NEVER called - no signal sender remains.
# Old async pattern (signal → set → proceed) replaced by synchronous (generate → wait → proceed).
# Proof: shot.py line 83 'if shot_plan.get("end_frame")' runs before line 123 'await workflow.wait_condition'.
# Test validates synchronous path works; signal path cannot be tested as it doesn't execute.
test_mutation 93 "M-R4y SKIP: Synchronous generation replaced async signal" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_end_frame_dry_gets_placeholder_and_live_waits_for_signal" \
    true  # Expected skip - no mutation needed

# 94. M-R4z Workflow passes the end image to the clip activity
test_mutation 94 "M-R4z Workflow passes the end image to the clip activity" \
    "hfvg/workflows/shot.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_end_frame_dry_gets_placeholder_and_live_waits_for_signal" \
    sed -i '/Use the end_image_url that was generated before Gate 2/{ n; s/end_image_url = self\.end_image_url if params\.get("end_frame") else None/end_image_url = None  # MUTATED/; }'

# 95. M-R4A G2_SCENE_STILLS gate triggers when a shot has missing refs
test_mutation 95 "M-R4A G2_SCENE_STILLS gate triggers when a shot has missing refs" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_round4_shot_data.py::test_g2_detects_missing_refs_and_recheck_merges_data" \
    sed -i '/def _needs_new_refs/,/^    def/{ /return bool(self\._collect_missing_refs(shots))/s/.*/        return False  # MUTATED/; }'

# 96. M-R4B Live shot activity refuses when refs are missing  
test_mutation 96 "M-R4B Live shot activity refuses when refs are missing" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_round4_shot_data.py::test_shot_workflow_passes_refs_to_still_provider_activity" \
    sed -i '/# Refs gate.*shot never runs without its planned refs/{n; s/if missing_refs:/if False:  # MUTATED/; }'

# 97. M-R4C Recheck updates shot plans with new ref data
test_mutation 97 "M-R4C Recheck updates shot plans with new ref data" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_round4_shot_data.py::test_g2_detects_missing_refs_and_recheck_merges_data" \
    sed -i '/rechecked = await workflow\.execute_activity/,/self\.missing_refs = self\._collect_missing_refs/{ /{\*\*s, \*\*rechecked\[s\["shot_id"\]\]}/s/.*/                    s  # MUTATED/; }'

# 98. M-R4D Episode workflow loads per-shot data for its episode
test_mutation 98 "M-R4D Episode workflow loads per-shot data for its episode" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_round4_shot_data.py::test_g201_triggers_for_new_refs_and_plans_carry_data" \
    sed -i '/parse_beatmap_activity/,/start_to_close_timeout/{ /args=\[beatmap_path, episode_id, not dry_run\]/s/episode_id/None/; }'

# 99. M-R4E GET episode looks up the id the workflow was started with
test_mutation 99 "M-R4E GET episode looks up the id the workflow was started with" \
    "api/main.py" \
    "tests/test_round4_shot_data.py::test_get_episode_looks_up_the_started_workflow_id" \
    sed -i 's/^    workflow_id = episode_workflow_id(episode_id)$/    workflow_id = f"{episode_id}-"  # MUTATED/'

# 100. M-R4F GET episode accepts the ep04-episode id style
test_mutation 100 "M-R4F GET episode accepts the ep04-episode id style" \
    "api/main.py" \
    "tests/test_round4_shot_data.py::test_get_episode_accepts_episode_suffix_style_id" \
    sed -i 's/^    return episode_id\[: -len("-episode")\] if episode_id.endswith("-episode") else episode_id$/    return episode_id  # MUTATED/'

# 101. M-R4G GET episode unknown workflow is 404
test_mutation 101 "M-R4G GET episode unknown workflow is 404" \
    "api/main.py" \
    "tests/test_round4_shot_data.py::test_get_episode_unknown_workflow_is_404_not_500" \
    sed -i 's/^        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:$/        if False:  # MUTATED/'

# 102. M-R4H upload-shots validates shots.json
test_mutation 102 "M-R4H upload-shots validates shots.json" \
    "api/main.py" \
    "tests/test_round4_shot_data.py::test_upload_shots_route_validates_and_stores" \
    sed -i 's/^        plans = parse_shots(json.loads(shots_bytes))$/        plans = {"x": 1}  # MUTATED/'

# 103. M-R4I Lint kit loads the lint section of a full prompt_kit.json
test_mutation 103 "M-R4I Lint kit loads the lint section of a full prompt_kit.json" \
    "hfvg/prompt_lint.py" \
    "tests/test_round4_shot_data.py::test_lint_kit_loads_lint_section_of_full_prompt_kit" \
    sed -i 's/^    section = data.get("lint") if (not override or isinstance(data.get("lint"), dict)) else data$/    section = data  # MUTATED/'

# 104. M-R4J Lint requires antler/duck clauses on shot_meta flags
test_mutation 104 "M-R4J Lint requires antler/duck clauses on shot_meta flags" \
    "hfvg/prompt_lint.py" \
    "tests/test_round4_shot_data.py::test_lint_flags_require_antler_and_duck_clauses_without_character_codes" \
    sed -i 's/^        if meta.get(flag) and not (present \& {c.lower() for c in codes}):$/        if False:  # MUTATED/'

# 105. M-R5a Still provider cap follows its model (not a global 3)
test_mutation 105 "M-R5a Still provider cap follows its model" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_model_ref_caps.py::test_qwen_still_provider_refuses_four_refs_but_grok_accepts_them" \
    sed -i 's/^            max_refs = max_refs_for(self.model_path)$/            max_refs = 3  # MUTATED/'

# 106. M-R5b Still activity cap follows MODEL_PATH_STILL
test_mutation 106 "M-R5b Still activity cap follows the still model" \
    "hfvg/preflight.py" \
    "tests/test_model_ref_caps.py::test_live_still_activity_cap_follows_the_still_model" \
    sed -i 's/^    cap = max_refs_for(model)$/    cap = 3  # MUTATED/'

# 107. M-R5c Per-model table lookup is used
test_mutation 107 "M-R5c Per-model table lookup is used" \
    "hfvg/config.py" \
    "tests/test_model_ref_caps.py::test_default_caps_match_documented_limits" \
    sed -i 's/^    caps = registry.resolve(model_path)  # raises UnknownModelError$/    caps = registry.resolve(still_model_path())  # MUTATED/'

# 108. M-R5d MODEL_MAX_REFS_JSON override is honoured
test_mutation 108 "M-R5d MODEL_MAX_REFS_JSON override is honoured" \
    "hfvg/config.py" \
    "tests/test_model_ref_caps.py::test_env_json_overrides_one_model_only" \
    sed -i 's/^    override_json = os.getenv("MODEL_MAX_REFS_JSON")$/    override_json = None  # MUTATED/'

# 109. M-R5e Negative per-model cap is rejected
test_mutation 109 "M-R5e Negative per-model cap is rejected" \
    "hfvg/config.py" \
    "tests/test_model_ref_caps.py::test_env_json_negative_cap_is_rejected" \
    sed -i 's/^            if override_value < 0:$/            if False:  # MUTATED/'

# 110. M-R5f Unknown models are refused (no fallback cap)
test_mutation 110 "M-R5f Unknown models are refused (no fallback cap)" \
    "hfvg/config.py" \
    "tests/test_model_ref_caps.py::test_unknown_model_raises_with_no_fallback" \
    sed -i 's/^    caps = registry.resolve(model_path)  # raises UnknownModelError$/    caps = registry.models.get(model_path) or registry.resolve(still_model_path())  # MUTATED/'

# ─────────────────────────────────────────────────────────────────────────────
# BUG FIX MUTATIONS: Harness canary bugs (PR #10)
# ─────────────────────────────────────────────────────────────────────────────

# 111. BUG 2: poll_job_status resolves harness→provider ID
test_mutation 111 "BUG 2: poll uses provider ID not harness ID" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_harness_canary_bugs.py::test_workflow_polls_with_provider_id_not_harness_id" \
    sed -i 's/job_status = await provider.get_job_status(provider_job_id)/job_status = await provider.get_job_status(job_id)  # MUTATED - use harness ID/'

# ─────────────────────────────────────────────────────────────────────────────
# UPLOAD CACHE MUTATIONS: PR #10 upload caching
# ─────────────────────────────────────────────────────────────────────────────

# 112. Upload cache: drop content_type from upload POST
test_mutation 112 "Upload POST includes content_type" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_upload_cache.py::test_upload_cache_reuses_url_across_submits" \
    sed -i 's/json={"content_type": content_type}/json={}  # MUTATED - no content_type/'

# 113. Upload cache: drop upload_headers from PUT
test_mutation 113 "Upload PUT includes upload_headers" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_upload_cache.py::test_upload_cache_reuses_url_across_submits" \
    sed -i 's/headers=upload_headers$/headers={}  # MUTATED - no upload_headers/'

# 114. Clip submit persists provider_job mapping
test_mutation 114 "Clip submit persists provider_job mapping" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_harness_canary_bugs.py::test_clip_submit_persists_provider_job_mapping" \
    sed -i '/await credit_ledger.persist_provider_job(job_id, provider_job_id, provider.model_path)/s/.*/            pass  # MUTATED - skip persist_provider_job/'

# 115. Upload cache: cache is consulted before upload
test_mutation 115 "Upload checks cache before uploading" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_upload_cache.py::test_upload_cache_reuses_url_across_submits" \
    sed -i 's/cached_url = await cache.get_cached_url(str(upload_path), version)/cached_url = None  # MUTATED - bypass cache/'

# 116. Smoke test gate: paid stages refuse without passing smoke test
test_mutation 116 "Smoke test gate enforced on paid stages" \
    "hfvg/studio_db.py" \
    "tests/test_smoke_gate.py::test_smoke_gate_refuses_without_passing_test" \
    sed -i '/if not passed:/,/^[[:space:]]*$/ { /if not passed:/s/.*/    pass  # MUTATED - disable gate/; t; d }'

# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS: Single-shot retry and G1 plan (Feature set)
# ─────────────────────────────────────────────────────────────────────────────

# 117. M-retry1: Retry limit enforced
test_mutation 117 "Retry respects max_retries limit" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_retry_shot.py::test_retry_shot_respects_max_retries" \
    sed -i 's/if attempt_count > max_retries:/if False:  # MUTATED - allow unlimited retries/'

# 118. M-retry2: Different idempotency key per attempt
test_mutation 118 "Retry uses different workflow ID (includes attempt)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_retry_shot.py::test_retry_shot_uses_different_idempotency_key" \
    sed -i 's/retry_num = self.state.shot_attempts\[shot_id\] - 1/retry_num = 0  # MUTATED - always retry0/'

# 121. M-retry-version: Retry increments version for unique job_id
test_mutation 121 "Retry increments version for unique job_id" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_retry_shot.py::test_retry_shot_increments_version_for_unique_job_id" \
    sed -i '/# Pass attempt number and version into shot_plan/,/shot_plan\["version"\] = actual_attempts/{s/shot_plan\["version"\] = actual_attempts/shot_plan["version"] = 1  # MUTATED - always v1/}'

# 119. M-anchor: Anchor still is injected as ref
test_mutation 119 "Anchor still is injected into refs" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_anchor_still.py::test_anchor_still_injected_into_refs" \
    sed -i 's/^        anchor=anchor_path if anchor else None,$/        anchor=None,  # MUTATED/'

# 120. M-anchor-cap: Anchor + refs enforces ref cap
test_mutation 120 "Anchor + refs respects model ref cap" \
    "hfvg/preflight.py" \
    "tests/test_anchor_still.py::test_anchor_still_enforces_ref_cap" \
    sed -i 's/assembled, _ = assemble_still_refs(lead=lead, anchor=route.get("anchor_ref"),/assembled, _ = assemble_still_refs(lead=lead, anchor=None,  # MUTATED/'

# 122: End-frame anchor: strip '_end' suffix to find anchor
test_mutation 122 "End-frame anchor: strip _end suffix" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_harness_pr10_mutations.py::test_end_frame_anchor_strips_suffix" \
    sed -i 's/base_shot_id = shot_id.replace("_end", "") if shot_id.endswith("_end") else shot_id/base_shot_id = shot_id  # MUTATED - do not strip _end suffix/'

# 123: Ref cap on end frames includes the anchor
test_mutation 123 "Ref cap on end frames includes anchor" \
    "hfvg/still_refs.py" \
    "tests/test_harness_pr10_mutations.py::test_end_frame_anchor_enforces_ref_cap" \
    sed -i 's/^    refs = (\[lead\] if lead else \[\]) + (\[anchor\] if anchor else \[\]) + planned$/    refs = ([lead] if lead else []) + planned  # MUTATED/'

# 124: Retry refused after G2 is approved
test_mutation 124 "Retry refused after G2 is approved" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_retry_refused_after_g2_approved" \
    sed -i 's/if "G2_SCENE_STILLS" in self.passed_gates:/if False:  # MUTATED - allow retry after G2/'

# 125: Retry refused while one is already pending
test_mutation 125 "Retry refused while one is already pending" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_retry_refused_while_pending" \
    sed -i 's/if shot_id in self.state.pending_retries:/if False:  # MUTATED - allow duplicate retry/'

# 126: G1 approval refused in live mode when estimates unavailable
test_mutation 126 "G1 approval refused without estimates" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_g1_approval_refused_without_estimates" \
    sed -i 's/if not dry_run and not estimates_available and not override_unavailable_estimates:/if False:  # MUTATED - allow G1 without estimates/'

# 127: Clip loop uses shot_results (latest handle), not stale original
test_mutation 127 "Clip loop uses shot_results (latest handle)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_clip_loop_uses_latest_retry_handle" \
    sed -i '/# Use shot_results to get latest handle/,/handle = self\.shot_results\.get/{s/handle = self\.shot_results\.get(shot\["shot_id"\])/handle = self.shot_workflow_handles.get(shot["shot_id"])  # MUTATED - use stale handle dict/}'

# 128: shot_plan version = shot_attempts (already incremented)
test_mutation 128 "shot_plan version = shot_attempts" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_attempt_override_gives_retry_suffix" \
    sed -i '/# Total attempt number for job_id\/version/,/shot_plan\["version"\] = total_attempt/{s/shot_plan\["version"\] = total_attempt/shot_plan["version"] = 1  # MUTATED/}'

# 129: 3f - .jpeg glob when resolving G2-approved anchor still  
test_mutation 129 "3f: .jpeg glob when resolving G2-approved anchor still" \
    "api/main.py" \
    "tests/test_harness_pr10_items_remaining.py::test_g2_approved_jpeg_anchor_glob" \
    sed -i 's/glob("still_v\*\.jpeg")/glob("still_v*.XXX-MUTATED-not-jpeg")/'

# 130: 3g - verify_episode_files_activity honours EPISODES_DIR
test_mutation 130 "3g: verify_episode_files_activity honours EPISODES_DIR" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_harness_pr10_items_remaining.py::test_verify_episode_files_honors_episodes_dir" \
    sed -i 's|episodes_dir = Path(os\.getenv("EPISODES_DIR", "\./data/episodes"))|episodes_dir = Path(".")  # MUTATED|'

# 131: 3h - CLI retry-shot uses execute_update
test_mutation 131 "3h: CLI retry-shot uses execute_update" \
    "hfvg/cli.py" \
    "tests/test_harness_pr10_items_remaining.py::test_cli_retry_shot_execute_update_and_exit_code" \
    sed -i 's/result = await handle\.execute_update("retry_shot", shot_id)/result = await handle.signal("retry_shot", shot_id)  # MUTATED/'

# ─────────────────────────────────────────────────────────────────────────────
# Bug Fixes: Episode workflow robustness
# ─────────────────────────────────────────────────────────────────────────────

# 132: Bug Fix 1 - G2 approval fails when signaling finished child
test_mutation 132 "G2 approval handles finished child gracefully" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_g2_approval_with_finished_child_succeeds" \
    sed -i 's/if e\.type != "ExternalWorkflowExecutionNotFound":/if True:  # MUTATED - no NotFound tolerance, re-raise everything/'

# 134: Bug Fix 1 addendum - Other errors must not be swallowed
test_mutation 134 "G2 signal error checking is specific (NOT_FOUND only)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_g2_approval_does_not_swallow_other_errors" \
    sed -i 's/if e\.type != "ExternalWorkflowExecutionNotFound":/if False:  # MUTATED - catch all, swallows other errors/'

# 133: Bug Fix 2 - Pending retry causes 30s timeout before clips
test_mutation 133 "Pending retry signaled before wait (no 30s timeout)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_pending_retry_does_not_block_clips" \
    sed -i '/# Signal all shot workflows/,+12{/await handle\.signal("stills_approved")/s/await handle\.signal("stills_approved")/pass  # MUTATED - skip signal/}'

# ─────────────────────────────────────────────────────────────────────────────
# Safety Design: G2 approval must refuse while retry is pending
# ─────────────────────────────────────────────────────────────────────────────

# 135: Safety - G2 refused while retry pending
test_mutation 135 "G2 approval refused while retry is pending" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_g2_refused_while_retry_pending" \
    sed -i 's/^\( *\)@approve_g2_scene_stills\.validator/\1# @approve_g2_scene_stills.validator  # MUTATED - no validator/'

# 136: Safety - G2 accepted after retry completes
test_mutation 136 "G2 approval accepted after retry completes" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_g2_accepted_after_retry_completes" \
    sed -i 's/self\.state\.pending_retries\.pop(shot_id, None)/pass  # MUTATED - pending_retries never cleared/'

# 137: Safety - No 30s stall after G2 (validator ensures pending_retries empty)
# 137: SKIP - No longer relevant with new design (pending_retries cleared by retry_still_ready)
# The 30s wait was removed, but adding it back has no effect because pending_retries is
# always empty at this point (cleared when retry still lands, not when shot finishes).
# test_mutation 137 "No 30s pending-retry wait after G2" \
#     "hfvg/workflows/episode_v2.py" \
#     "tests/test_harness_pr10_mutations.py::test_pending_retry_does_not_block_clips" \
#     sed -i '/comment-does-not-exist/'

# 138: G2 API route must use execute_update (a signal to an update handler is dropped)
test_mutation 138 "API G2 approval uses execute_update, not signal" \
    "api/main.py" \
    "tests/test_g2_api_route_update.py::test_api_g2_accepted_reaches_workflow_when_no_retry_pending" \
    sed -i 's/await handle\.execute_update(signal_name)$/await handle.signal(signal_name)  # MUTATED - signal dropped by update handler/'

# 139: API approve route must not turn its 409 into a 500
test_mutation 139 "API G2 refusal returns 409 (HTTPException re-raised)" \
    "api/main.py" \
    "tests/test_g2_api_route_update.py::test_api_g2_refused_with_409_while_retry_pending" \
    sed -i 's/raise  # keep 404\/409 - do not turn them into 500/raise HTTPException(status_code=500, detail="MUTATED")  # keep 404\/409/'

# 140: retry_still_ready signal must clear pending_retries
test_mutation 140 "retry_still_ready signal clears pending_retries" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_harness_pr10_mutations.py::test_pending_retry_does_not_block_clips" \
    sed -i '/clearing pending_retries.*$/{ n; s/^/# MUTATED - disabled: /; }'

# 141: REAL ShotWorkflow must send retry_still_ready before waiting for stills_approved (else G2 deadlock)
test_mutation 141 "Real ShotWorkflow sends retry_still_ready after retry stills land" \
    "hfvg/workflows/shot.py" \
    "tests/test_real_shot_retry_still_ready.py::test_real_shot_workflow_retry_unblocks_g2_after_both_stills_land" \
    sed -i 's/^\( *\)await self\._notify_parent_retry_still_ready(shot_plan)$/\1pass  # MUTATED - retry_still_ready never sent/'

# 142: G1 clip estimate must send valid string image_url (not None)
test_mutation 142 "G1 estimate sends string image_url to clip provider" \
    "hfvg/activities/g1_plan.py" \
    "tests/test_g1_estimate_image_url.py::test_g1_estimate_sends_string_image_url_for_kling" \
    sed -i 's/placeholder_url = cfg\.get_placeholder_image_url()/placeholder_url = None  # MUTATED/'

# 143: G1 estimate must pass last_image_url for end-frame shots
test_mutation 143 "G1 estimate sends last_image_url for end-frame shots" \
    "hfvg/activities/g1_plan.py" \
    "tests/test_g1_estimate_image_url.py::test_g1_estimate_sends_last_image_url_for_end_frame_shots" \
    sed -i 's/last_image_url = placeholder_url if end_frame else None/last_image_url = None  # MUTATED - never send last_image_url/'

# 144: G1 estimate must send duration as int
test_mutation 144 "G1 estimate sends duration as int not float" \
    "hfvg/activities/g1_plan.py" \
    "tests/test_g1_estimate_image_url.py::test_g1_estimate_sends_duration_as_int_not_float" \
    sed -i 's/duration=int(duration)/duration=duration/'

# 145: Reference images must be normalized before upload
test_mutation 145 "Reference images are normalized before upload" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_pr10_216_fixes.py::test_still_upload_reference_puts_normalized_jpeg_not_original" \
    sed -i 's/normalized_path, original_sha = normalize_reference_image(Path(image_path))/normalized_path, original_sha = Path(image_path), ""/'

# 146: End prompt must be used for _end stills when provided
test_mutation 146 "End still uses end_prompt when provided" \
    "hfvg/workflows/shot.py" \
    "tests/test_pr10_216_fixes.py::test_real_shot_end_still_uses_end_prompt" \
    sed -i 's/end_prompt = self.end_prompt_override or shot_plan.get("end_prompt") or shot_plan\["prompt"\]/end_prompt = self.end_prompt_override or shot_plan["prompt"]  # MUTATED/'

# 147: Normalized images must be cached by original SHA256 + size limits
test_mutation 147 "Normalized images cached by original SHA" \
    "hfvg/image_normalize.py" \
    "tests/test_image_normalize.py::test_normalize_cache_by_sha" \
    sed -i 's/cache_key = f"{source_sha}_{REF_MAX_LONG_SIDE}_{REF_MAX_BYTES}"/cache_key = f"{__import__('\''os'\'').urandom(8).hex()}"  # MUTATED/'

# 148: Provider job ID must be included in failure error messages
test_mutation 148 "Job failures include provider job ID" \
    "hfvg/workflows/shot.py" \
    "tests/test_pr10_216_fixes.py::test_failed_job_reason_has_job_id_and_raw_error" \
    sed -i 's/raise RuntimeError(f"Job {job_id} {status}: {error_details}")/raise RuntimeError(f"Job {status}: {error_details}")/'

# 149: Reference normalization must not be skippable via env (SKIP_IMAGE_NORMALIZE must not exist in product)
test_mutation 149 "Normalization cannot be skipped by SKIP_IMAGE_NORMALIZE" \
    "hfvg/image_normalize.py" \
    "tests/test_pr10_216_fixes.py::test_normalization_cannot_be_skipped_by_env" \
    sed -i 's/^    source_sha = compute_file_sha256(source_path)$/&\n    if os.getenv("SKIP_IMAGE_NORMALIZE", "").lower() == "true":  # MUTATED\n        return (source_path, source_sha)/'

# 150: Live (escalated) review must generate the _end still BEFORE waiting for stills_approved/G2
test_mutation 150 "Escalated review generates _end still before the G2 wait" \
    "hfvg/workflows/shot.py" \
    "tests/test_pr10_216_fixes.py::test_escalated_review_generates_end_still_before_waiting" \
    sed -i 's/if params.get("end_frame") and workflow.patched("end-still-before-human-review"):/if False:  # MUTATED/'

# 151: _end still must lead with the shot's start still
test_mutation 151 "_end still uses the start still as first ref" \
    "hfvg/workflows/shot.py" \
    "tests/test_pr10_216_fixes.py::test_escalated_review_generates_end_still_before_waiting" \
    sed -i 's/self.still_url,  # lead_ref: the approved\/landed start still goes first/None,  # MUTATED lead_ref dropped/'

# 152: generate_end_still backfill for runs parked at review without an _end still
test_mutation 152 "generate_end_still backfills a parked run" \
    "hfvg/workflows/shot.py" \
    "tests/test_pr10_216_fixes.py::test_backfill_signal_generates_end_still_for_parked_legacy_run" \
    sed -i 's/while not self.stills_approved and self._end_still_backfill_due(params):/while False:  # MUTATED/'

# 153: escalated retry must not signal retry_still_ready before its _end still lands
test_mutation 153 "Escalated retry signals ready only after _end lands" \
    "hfvg/workflows/shot.py" \
    "tests/test_pr10_216_fixes.py::test_escalated_retry_signals_ready_only_after_end_still_lands" \
    sed -i 's/^\( *\)# Both review paths produce the _end still BEFORE the human-review\/G2 wait (and$/\1await self._notify_parent_retry_still_ready(shot_plan)  # MUTATED - early\n&/'

# 154: _end lead ref (start still) is downloaded + normalized + re-uploaded, never the provider output link
test_mutation 154 "_end lead ref re-uploaded, not provider link" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_pr10_216_fixes.py::test_end_still_lead_ref_is_downloaded_normalized_and_reuploaded" \
    sed -i 's/lead_local, lead_sha = await _localize_lead_ref(lead_ref, episode_id, shot_id) if lead_ref else (None, None)/lead_local, lead_sha = (lead_ref, "0" * 64) if lead_ref else (None, None)  # MUTATED (provider link passed through)/'

# 155: a failed/non-image start-still download refuses before any estimate/reserve/submit
test_mutation 155 "Lead ref download failure refuses before spend" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_pr10_216_fixes.py::test_end_still_lead_ref_download_failure_refuses_before_spend" \
    sed -i 's/if response.status_code != 200 or not content_type.startswith("image\/") or not data:/if False:  # MUTATED/'

# 156: concurrent normalization of one ref must not share a temp file (unique temp per call)
test_mutation 156 "Concurrent normalization of a shared ref does not collide" \
    "hfvg/image_normalize.py" \
    "tests/test_image_normalize_enhancements.py::test_concurrent_normalize_same_source_does_not_collide" \
    sed -i 's/temp_path = normalized_path.with_name(f"{cache_key}.{os.getpid()}-{uuid.uuid4().hex}.tmp")/temp_path = normalized_path.with_suffix(".tmp")  # MUTATED/'

# 157: EXIF orientation must be applied before normalizing
test_mutation 157 "EXIF orientation applied during normalization" \
    "hfvg/image_normalize.py" \
    "tests/test_image_normalize_enhancements.py::test_exif_orientation_applied" \
    sed -i 's/^        img = ImageOps.exif_transpose(img)$/        pass  # MUTATED/'

# 158: high-detail images fall back to an unoptimized save instead of crashing (Pillow optimize buffer)
test_mutation 158 "High-detail image saves without crashing" \
    "hfvg/image_normalize.py" \
    "tests/test_pr10_216_fixes.py::test_normalize_high_detail_image_does_not_crash" \
    sed -i 's/^                img.save(temp_path, "JPEG", quality=quality, optimize=False, subsampling=0)$/                raise  # MUTATED/'

# 159: normalized cache key includes the size limits (changing limits invalidates the cache)
test_mutation 159 "Normalize cache key includes size limits" \
    "hfvg/image_normalize.py" \
    "tests/test_image_normalize_enhancements.py::test_cache_key_includes_size_limits" \
    sed -i 's/cache_key = f"{source_sha}_{REF_MAX_LONG_SIDE}_{REF_MAX_BYTES}"/cache_key = f"{source_sha}"  # MUTATED/'

# 160: an embedded ICC profile is converted to sRGB pixel values (not silently skipped)
test_mutation 160 "ICC profile converted to sRGB" \
    "hfvg/image_normalize.py" \
    "tests/test_image_normalize_enhancements.py::test_icc_profile_converted_to_srgb" \
    sed -i 's/input_profile = ImageCms.ImageCmsProfile(_io.BytesIO(icc_bytes))/input_profile = ImageCms.ImageCmsProfile(icc_bytes)  # MUTATED (raw bytes: TypeError, swallowed)/'

# 161: RGBA refs (Studio PNGs) get their ICC profile converted too
test_mutation 161 "ICC conversion covers RGBA refs" \
    "hfvg/image_normalize.py" \
    "tests/test_image_normalize_enhancements.py::test_icc_profile_converted_for_rgba_png" \
    sed -i 's/img.mode in ("RGB", "RGBA", "CMYK"):/img.mode in ("RGB", "CMYK"):  # MUTATED/'

test_mutation 162 "Grok Image 2.0 effective ref cap is 5 (6 refused)" \
    "hfvg/data/model_caps.json" \
    "tests/test_round4_shot_data.py::test_still_provider_refuses_instead_of_truncating_refs" \
    sed -i '0,/"value": 5,/s//"value": 8,/'

test_mutation 163 "lead over cap drops the anchor first" \
    "hfvg/still_refs.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_drops_anchor_first_when_lead_pushes_over_cap" \
    sed -i 's/^    if lead and anchor and len(refs) > cap:$/    if False and lead and anchor and len(refs) > cap:  # MUTATED/'

test_mutation 164 "anchor drop removes the anchor, not the lead" \
    "hfvg/still_refs.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_drops_anchor_first_when_lead_pushes_over_cap" \
    sed -i 's/^        refs = \[lead\] + planned$/        refs = [anchor] + planned  # MUTATED/'

test_mutation 165 "anchor drop never drops a planned ref" \
    "hfvg/still_refs.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_drops_anchor_first_when_lead_pushes_over_cap" \
    sed -i 's/^        refs = \[lead\] + planned$/        refs = [lead, anchor] + planned[:-1]  # MUTATED/'

test_mutation 166 "anchor never dropped without a lead" \
    "hfvg/still_refs.py" \
    "tests/test_ref_cap_lead_anchor.py::test_start_still_never_drops_anchor_without_a_lead" \
    sed -i -e 's/^    if lead and anchor and len(refs) > cap:$/    if anchor and len(refs) > cap:  # MUTATED/' -e 's/^        refs = \[lead\] + planned$/        refs = ([lead] if lead else []) + planned  # MUTATED/'

test_mutation 167 "still over cap after anchor drop is refused" \
    "hfvg/preflight.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_still_over_cap_after_anchor_drop_is_refused_before_spend" \
    sed -i 's/^    if len(assembled) > cap:$/    if len(assembled) > cap + 1:  # MUTATED/'

test_mutation 168 "_end job id carries -lead<sha8>" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_without_anchor_keeps_lead_first" \
    sed -i 's/lead_suffix = f"-lead{lead_sha8}" if lead_sha8 else ""/lead_suffix = ""  # MUTATED/'

test_mutation 169 "dropped anchor removes the -anchor suffix" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_drops_anchor_first_when_lead_pushes_over_cap" \
    sed -i '/anchor_suffix = /s/ if anchor_used else ""/ if anchor else ""  # MUTATED/'

test_mutation 170 "lead within cap keeps the anchor second" \
    "hfvg/still_refs.py" \
    "tests/test_ref_cap_lead_anchor.py::test_end_still_keeps_anchor_after_lead_when_within_cap" \
    sed -i 's/^    if lead and anchor and len(refs) > cap:$/    if lead and anchor:  # MUTATED/'

test_mutation 171 "per-model cap override via MODEL_MAX_REFS_JSON" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_ref_cap_lead_anchor.py::test_cap_is_configurable_per_model_and_keeps_anchor_when_raised" \
    sed -i 's/    still_cap = max_refs_for(still_model_path())/    still_cap = 5  # MUTATED/'

test_mutation 172 "end backfill reuses the landed start (no start resubmit)" \
    "hfvg/workflows/shot.py" \
    "tests/test_end_still_backfill.py::test_retry_end_still_submits_only_the_end_still_with_the_landed_start_as_lead" \
    sed -i 's/                reuse = shot_plan.get("reuse_start_still")/                reuse = None  # MUTATED/'

test_mutation 173 "end backfill keeps the parked attempt (r3 ids)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_end_still_backfill.py::test_retry_end_still_submits_only_the_end_still_with_the_landed_start_as_lead" \
    sed -i 's/        shot_plan\["version"\] = attempt$/        shot_plan["version"] = attempt + 1  # MUTATED/'

test_mutation 174 "end backfill leads with the landed start still" \
    "hfvg/workflows/shot.py" \
    "tests/test_end_still_backfill.py::test_retry_end_still_submits_only_the_end_still_with_the_landed_start_as_lead" \
    sed -i 's/poll_result = {"url": reuse\["url"\], "cost": 0}/poll_result = {"url": "fake:\/\/wrong-start", "cost": 0}  # MUTATED/'

test_mutation 175 "end backfill applies the supplied end_prompt" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_end_still_backfill.py::test_retry_end_still_submits_only_the_end_still_with_the_landed_start_as_lead" \
    sed -i 's/            shot_plan\["end_prompt"\] = end_prompt/            pass  # MUTATED/'

test_mutation 176 "end backfill refused when the start failed" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_end_still_backfill.py::test_retry_end_still_refused_when_start_failed" \
    sed -i 's/        if not str(failed.get("reason", "")).startswith("End still failed"):/        if False:  # MUTATED/'

test_mutation 177 "end backfill clears pending on resolve refusal" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_end_still_backfill.py::test_retry_end_still_resolve_refusal_spends_nothing_and_keeps_shot_failed" \
    sed -i '/start = await workflow.execute_activity(/,/raise ApplicationError(f"Cannot backfill/s/self.state.pending_retries.pop(shot_id, None)/pass  # MUTATED/'

test_mutation 178 "resolve refuses after an _end with lead was submitted" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_end_still_backfill.py::test_resolve_refuses_when_an_end_with_lead_was_already_submitted" \
    sed -i 's/    if prior_end:/    if False and prior_end:  # MUTATED/'

test_mutation 179 "resolve picks exactly this attempt's start" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_end_still_backfill.py::test_resolve_landed_start_still_finds_exactly_this_attempts_start" \
    sed -i 's/    suffix = f"-r{attempt - 1}" if attempt > 1 else f"-v{version}"/    suffix = f"-r{attempt - 2}" if attempt > 1 else f"-v{version}"  # MUTATED/'

test_mutation 180 "resolve requires a committed start" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_end_still_backfill.py::test_resolve_refuses_when_start_not_committed_or_not_completed" \
    sed -i 's/    committed = \[r\[0\] for r in rows if r\[1\] == "committed"\]/    committed = [r[0] for r in rows]  # MUTATED/'

test_mutation 181 "worker registers resolve_landed_start_still" \
    "hfvg/worker.py" \
    "tests/test_end_still_backfill.py::test_worker_registers_resolve_landed_start_still" \
    sed -i 's/^    activities.resolve_landed_start_still,/    # MUTATED activities.resolve_landed_start_still,/'

# RAW PROVIDER EVIDENCE MUTATIONS: full failure body + headers (X-Correlation-ID)

test_mutation 182 "terminal failure logs raw body+headers" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_provider_raw_failure_logging.py::test_failed_status_keeps_raw_body_and_headers" \
    sed -i 's/^        if status_str in ("failed", "nsfw", "blocked", "canceled"):/        if False:  # MUTATED/'

test_mutation 183 "ProviderJob keeps raw status body" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_provider_raw_failure_logging.py::test_failed_status_keeps_raw_body_and_headers" \
    sed -i 's/^            raw=data,/            raw=None,  # MUTATED/'

test_mutation 184 "cookies stripped from kept headers" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_provider_raw_failure_logging.py::test_failed_status_keeps_raw_body_and_headers" \
    sed -i 's/ if k.lower() not in _HIDDEN_HEADERS}/}  # MUTATED/'

test_mutation 185 "submit summary counts image_urls" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_provider_raw_failure_logging.py::test_submit_rejection_carries_body_headers_and_request_summary" \
    sed -i 's/"n_image_urls": len(payload.get("image_urls") or \[\]),/"n_image_urls": 0,  # MUTATED/'

test_mutation 186 "submit success logs correlation id" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_provider_raw_failure_logging.py::test_submit_success_logs_correlation_id_and_summary" \
    sed -i 's/data.get("request_id"), hdrs.get("x-correlation-id"), json.dumps(summary),/data.get("request_id"), None, json.dumps(summary),  # MUTATED/'

test_mutation 187 "status HTTP error keeps body" \
    "hfvg/providers/higgsfield_still.py" \
    "tests/test_provider_raw_failure_logging.py::test_status_http_error_carries_body_and_correlation_id" \
    sed -i '/Higgsfield status {request_id} -> HTTP {response.status_code}/{n;s/: {body}",/: <dropped>",  # MUTATED/}'

test_mutation 188 "poll failure error carries correlation id" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_provider_raw_failure_logging.py::test_poll_activity_failure_returns_and_logs_raw_evidence" \
    sed -i 's/^                error = f"{error} (x-correlation-id {cid})"/                pass  # MUTATED/'

test_mutation 189 "poll failure returns raw body" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_provider_raw_failure_logging.py::test_poll_activity_failure_returns_and_logs_raw_evidence" \
    sed -i 's/^                "provider_raw": raw,/                "provider_raw": None,  # MUTATED/'

test_mutation 190 "poll failure logs raw body" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_provider_raw_failure_logging.py::test_poll_activity_failure_returns_and_logs_raw_evidence" \
    sed -i 's/^                f"| raw status body: {json.dumps(raw)} | response headers: {json.dumps(hdrs)}"/                f""  # MUTATED/'

# MODEL CAPABILITIES REGISTRY MUTATIONS

test_mutation 191 "resolve() falls back to the first model instead of raising" \
    "hfvg/model_caps.py" \
    "tests/test_model_caps_registry.py::test_unknown_model_refused" \
    sed -i 's/^        raise UnknownModelError($/        return list(self.models.values())[0]  # MUTATED\n&/'

test_mutation 192 "max_refs_for returns the Higgsfield value instead of the stricter value" \
    "hfvg/config.py" \
    "tests/test_model_ref_caps.py::test_default_caps_match_documented_limits" \
    sed -i 's/registry_value = limit.value/registry_value = limit.higgsfield or limit.value  # MUTATED/'

test_mutation 193 "override <= -> >= (raise allowed, lower refused)" \
    "hfvg/config.py" \
    "tests/test_model_caps_registry.py::test_override_can_lower_not_raise" \
    sed -i 's/if override_value <= registry_value:/if override_value >= registry_value:  # MUTATED/'

test_mutation 194 "still ref count uses the planned refs instead of assemble_still_refs output" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight.py::test_end_still_counts_after_anchor_drop" \
    sed -i 's/    assembled, _ = assemble_still_refs(lead=lead, anchor=route.get("anchor_ref"),/    assembled, _ = (list(shot.get("ref_keys") or []), None) if True else assemble_still_refs(lead=lead, anchor=route.get("anchor_ref"),  # MUTATED/'

test_mutation 195 "> -> >= in the ref-cap comparison" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight.py::test_still_over_cap_refused_with_creator_message" \
    sed -i 's/    if len(assembled) > cap:/    if len(assembled) >= cap:  # MUTATED/'

test_mutation 196 "unknown prompt limit treated as 0 (refuse)" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight.py::test_prompt_limit_kling_refused_unknown_grok_allowed" \
    sed -i 's/^    limit = caps.limit("prompt_max_chars")$/&\n    limit = limit.model_copy(update={"value": 0, "basis": "higgsfield"}) if limit is not None and limit.unknown else limit  # MUTATED/'

test_mutation 197 "whole-seconds duration check removed" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight.py::test_duration_rules" \
    sed -i 's/fractional = dlim.value.get("integer") and float(duration) != int(duration)/fractional = False  # MUTATED/'

test_mutation 198 "Kling prompt limit read from upstream (3072) instead of Higgsfield (2500)" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight.py::test_prompt_limit_kling_refused_unknown_grok_allowed" \
    sed -i 's/^    limit = caps.limit("prompt_max_chars")$/&\n    limit = limit.model_copy(update={"value": limit.upstream}) if limit is not None and limit.upstream else limit  # MUTATED/'

test_mutation 199 "aspect check skipped" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight_extended.py::test_aspect_resolution_quality" \
    sed -i 's/"aspect_ratio", "aspect_ratios",/"aspect_ratio", "aspect_ratios_MUTATED",/'

test_mutation 200 "frame format check skipped" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight_extended.py::test_frames_rules" \
    sed -i 's/if flim and not flim.unknown and isinstance(flim.value, list) and fmt not in flim.value:/if False and flim:  # MUTATED/'

test_mutation 201 "mcp_only_not_on_api status allowed" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight_extended.py::test_model_status_rules" \
    sed -i 's/    if caps.status in REFUSE_STATUSES:/    if False and caps.status in REFUSE_STATUSES:  # MUTATED/'

test_mutation 202 "SFX min 0.5 -> 0.1" \
    "hfvg/data/model_caps.json" \
    "tests/test_caps_preflight_extended.py::test_audio_limits" \
    sed -i 's/"min": 0.5,/"min": 0.1,/'

test_mutation 203 "submit preflight ignores the actual anchor" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_anchor_still.py::test_anchor_still_enforces_ref_cap" \
    sed -i 's/{"still_model": still_model_path(), "anchor_ref": anchor_path if anchor else None,/{"still_model": still_model_path(), "anchor_ref": None,  # MUTATED/'

test_mutation 204 "PreflightError made retryable" \
    "hfvg/preflight.py" \
    "tests/test_caps_enforcement.py::test_still_preflight_runs_before_estimate_and_reserve" \
    sed -i 's/super().__init__(message, type="PreflightError", non_retryable=True)/super().__init__(message, type="PreflightError", non_retryable=False)  # MUTATED/'

test_mutation 205 "clip preflight removed from submit" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_caps_enforcement.py::test_clip_preflight_runs_before_estimate_and_reserve" \
    sed -i '/# Preflight: validate the clip against/,/activity.logger.warning(f"Preflight warning: {preflight.format_violation(w)}")/d'

test_mutation 206 "G1 approval ignores preflight errors" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_caps_enforcement.py::test_g1_refused_while_preflight_errors" \
    sed -i 's/^        if preflight_errors:$/        if False and preflight_errors:  # MUTATED/'

test_mutation 207 "format_violation drops the source/checked suffix" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight.py::test_still_over_cap_refused_with_creator_message" \
    sed -i 's/        msg += f" Limit source:/        pass  # MUTATED; msg += f" Limit source:/'

test_mutation 208 "freshness check compares with > 3650 days" \
    "hfvg/model_caps.py" \
    "tests/test_model_caps_registry.py::test_caps_freshness_warns" \
    sed -i 's/return age_days > self.recheck_interval_days/return age_days > 3650  # MUTATED/'

test_mutation 209 "drift check ignores a maxItems change" \
    "scripts/recheck_model_caps.py" \
    "tests/test_recheck_model_caps.py::test_recheck_detects_schema_drift" \
    sed -i 's/if registry_value != max_items:/if False:  # MUTATED/'

test_mutation 210 "coverage collector skips AUDIO_COSTS keys" \
    "tests/test_model_caps_registry.py" \
    "tests/test_model_caps_registry.py::test_every_configured_model_has_caps" \
    sed -i 's/ids |= set(AUDIO_COSTS) | /ids |= /'

test_mutation 211 "still preflight removed from submit" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_caps_enforcement.py::test_still_preflight_runs_before_estimate_and_reserve" \
    sed -i '/# Preflight: validate the still against/,/activity.logger.warning(f"Preflight warning: {preflight.format_violation(w)}")/d'

test_mutation 212 "still preflight drops the actual lead (end stills under-counted at submit)" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_caps_enforcement.py::test_end_still_over_cap_with_lead_refused_before_fetching_the_lead" \
    sed -i 's/         "lead_ref": lead_ref or None},/         "lead_ref": None},  # MUTATED/'

test_mutation 213 "G1 plan skips preflight" \
    "hfvg/activities/g1_plan.py" \
    "tests/test_caps_enforcement.py::test_g1_refused_while_preflight_errors" \
    sed -i 's/    report = preflight_plans(shots, anchors=await _anchors(episode_id, shots))/    report = {"shots": {}, "errors": [], "violations": []}  # MUTATED/'

test_mutation 214 "end_frame False counted as an end frame" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight_extended.py::test_plan_normalizer_reads_plans_and_shots_json" \
    sed -i 's/    return value not in (None, False, "", 0)/    return value is not None  # MUTATED/'

test_mutation 215 "quality checked as asked instead of as sent (high refused)" \
    "hfvg/preflight.py" \
    "tests/test_caps_preflight_extended.py::test_aspect_resolution_quality" \
    sed -i 's/    sent = provider_quality(asked)/    sent = asked  # MUTATED/'

test_mutation 216 "preflight endpoint ignores the uploaded shots.json" \
    "api/main.py" \
    "tests/test_caps_enforcement.py::test_preflight_endpoint_reports_per_shot_pass_fail" \
    sed -i 's/        shots = raw\["shots"\]/        shots = []  # MUTATED/'

test_mutation 217 "registry cache ignores a changed MODEL_CAPS_PATH" \
    "hfvg/model_caps.py" \
    "tests/test_model_caps_registry.py::test_caps_freshness_warns" \
    sed -i 's/    if _registry is None or _registry_path != _default_path():/    if _registry is None:  # MUTATED/'

test_mutation 218 "raising an override without the flag is silent" \
    "hfvg/config.py" \
    "tests/test_model_caps_registry.py::test_override_can_lower_not_raise" \
    sed -i 's/            warnings.warn(msg, UserWarning, stacklevel=2)/            pass  # MUTATED/'


# ─────────────────────────────────────────────────────────────────────────────
# ROUND 1 MUTATIONS: Gate correctness and spend safety (G-GATE-1, G-QC-1,
# G-MODEL-1, G-RETRY-1, G-BUD-1). These must run BEFORE the summary.
# ─────────────────────────────────────────────────────────────────────────────

# 219: G-GATE-1a: a rejected shot is sent still_rejected, never stills_approved
test_mutation 219 "Rejected shots are never released to clip generation" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_gate_correctness_round1.py::TestPerShotG2Decisions::test_rejected_shot_never_reaches_clip_generation" \
    sed -i 's/                    if self\.state\.shot_decisions\.get(shot\["shot_id"\]) != "rejected":/                    if True:  # MUTATED/'

# 220: G-GATE-1b: G2 approval in the studio DB skips rejected/failed/blocked shots
test_mutation 220 "G2 only approves shots that are still awaiting review" \
    "hfvg/studio_db.py" \
    "tests/test_gate_correctness_round1.py::TestPerShotG2Decisions::test_studio_db_only_approves_pending_shots" \
    sed -i "/AND status NOT IN ('rejected', 'failed', 'blocked', 'approved')/d"

# 221: G-QC-1a: expected clip dims come from the shot params, not a 16:9 default
test_mutation 221 "Clip QC derives expected dims from params (preflight refuses bad spec)" \
    "hfvg/workflows/shot.py" \
    "tests/test_gate_correctness_round1.py::TestClipQCDefaults::test_clip_qc_fails_loudly_without_resolution" \
    sed -i 's/            expected_dims = expected_clip_dims(clip_params)/            expected_dims = {"resolution": "1024x576", "aspect_ratio": "16:9"}  # MUTATED/'

# 222: G-MODEL-1a: gpt_image_2 raises a plain ValueError (dry-run included)
test_mutation 222 "Higgsfield refuses gpt_image_2 instead of fallback" \
    "hfvg/providers/higgsfield.py" \
    "tests/test_gate_correctness_round1.py::TestModelDefaults::test_higgsfield_provider_refuses_gpt_image_2" \
    sed -i '/if model == "gpt_image_2":/,/)/{ s/raise ValueError(/_mutated = str(  # MUTATED/; }'

# 223: G-GATE-1c: reject_shot validator refuses unknown shots
test_mutation 223 "reject_shot refuses shots not launched in the episode" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_gate_correctness_round1.py::TestPerShotG2Decisions::test_reject_refused_outside_g2_and_for_unknown_shot" \
    sed -i 's/            return "unknown shot (not launched in this episode)"/            pass  # MUTATED/'

# 224: G-QC-1b: live clip QC never falls back to 1024x576 / 16:9
test_mutation 224 "Live clip QC escalates when the rubric has no expected dims" \
    "hfvg/activities/qc.py" \
    "tests/test_gate_correctness_round1.py::TestClipQCDefaults::test_live_review_clip_without_dims_escalates" \
    sed -i -e 's/expected_resolution = rubric.get("resolution")$/expected_resolution = rubric.get("resolution", "1024x576")/' -e 's/expected_aspect = rubric.get("aspect_ratio")$/expected_aspect = rubric.get("aspect_ratio", "16:9")/'

# 225: G-BUD-1a: per-round 80% stop is enforced on the USD ledger
test_mutation 225 "Round cap refuses a paid retry past the round stop" \
    "hfvg/budget.py" \
    "tests/test_gate_correctness_round1.py::TestBudgetUnification::test_round_cap_refuses_before_submit" \
    sed -i 's/                if round_total + prior + delta > settings\.round_stop_usd_micros:/                if False:  # MUTATED/'

# 226: G-BUD-1b: max paid retries per shot per round
test_mutation 226 "At most N (default 1) paid retries per shot per round" \
    "hfvg/budget.py" \
    "tests/test_gate_correctness_round1.py::TestBudgetUnification::test_max_one_paid_retry_per_shot_per_round" \
    sed -i 's/                    if other_retries >= limit:/                    if False:  # MUTATED/'

# 227: G-BUD-1c: live clip retry is authorized (with its retry key) before the provider call
test_mutation 227 "Live clip retry hold carries its retry key before submit" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_gate_correctness_round1.py::TestBudgetUnification::test_live_clip_retry_refused_before_provider_call" \
    sed -i 's/            retry_key=retry_key, reason=f"paid retry/            retry_key=None, reason=f"paid retry/'

# 228: G-RETRY-1a: retry_clip is bounded per shot per round
test_mutation 228 "retry_clip refuses beyond the per-shot per-round limit" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_retry_clip_reuses_approved_still_and_is_bounded" \
    sed -i 's/        if self\.clip_retries\.get(shot_id, 0) >= limit:/        if False:  # MUTATED/'

# 229: G-RETRY-1b: retry_clip never animates a still rejected at G2
test_mutation 229 "retry_clip refuses shots rejected at G2" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_retry_clip_refused_for_rejected_shot" \
    sed -i 's/            return "still was rejected at G2; a rejected still is never animated"/            pass  # MUTATED/'

# 230: G-RETRY-1c: retry_clip budget pre-check prices the actual clip
test_mutation 230 "retry_clip pre-check refuses when the round cap is exhausted" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_retry_clip_refused_when_round_cap_exhausted" \
    sed -i 's/^    amount = clip_estimate_usd_micros(model, None, duration)/    amount = 1  # MUTATED/'

# 231: G-RETRY-1d: final cut approval waits for pending clip retries
test_mutation 231 "Final cut approval is held while a clip retry is pending" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_final_cut_waits_for_pending_clip_retry_and_report_has_real_caps" \
    sed -i 's/^        if self\.pending_clip_retries:$/        if False:  # MUTATED/'

# 232: G-BUD-1d: run report caps come from the ledger, not a placeholder
test_mutation 232 "Run report uses real ledger caps (no placeholder)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_final_cut_waits_for_pending_clip_retry_and_report_has_real_caps" \
    sed -i 's/                    None,  # credits_cap: read from the credit ledger.*/                    total_credits * 2,  # MUTATED placeholder/'

# 233: G-GATE-1d / G-RETRY-1e: API maps refused workflow updates to 409 (no DB write)
test_mutation 233 "API returns 409 when the workflow refuses a reject/retry" \
    "api/main.py" \
    "tests/test_gate_correctness_round1.py::TestPerShotG2Decisions::test_reject_still_persists_state_in_db" \
    sed -i 's/^        raise HTTPException(status_code=409, detail=str(e))$/        return {"refused": str(e)}  # MUTATED/'

# 234: G-RETRY-1f: a clip retry reuses the approved still (no new still submit)
test_mutation 234 "Clip retry child reuses the approved still" \
    "hfvg/workflows/shot.py" \
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_retry_clip_reuses_approved_still_and_is_bounded" \
    sed -i 's/^        clip_only = shot_plan\.get("clip_only")/        clip_only = None  # MUTATED/'

# Round 2 mutations: moderation recovery ladder (G-REC-1), reconciler (G-REC-2), stall watchdog (G-REC-3)

# 235: G-REC-1a: Neutral reword rewrites only listed trigger words
test_mutation 235 "Neutral reword rewrites only listed trigger words" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_first_rung_is_neutral_reword_and_audited" \
    sed -i 's/^        if key not in NEUTRALIZE:$/        if True:  # MUTATED/'

# 236: G-REC-1b: Hard-stop terms are never rewritten (straight to review)
test_mutation 236 "Hard-stop terms are never rewritten (straight to review)" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_hard_stop_terms_escalate_without_rewrite" \
    sed -i 's/^    if terms:$/    if False:  # MUTATED/'

# 237: G-REC-1c: Per-shot-per-round paid retry cap counts ladder steps
test_mutation 237 "Per-shot-per-round paid retry cap counts ladder steps" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_fallback_after_reword_when_budget_allows" \
    sed -i 's/^    if used >= settings\.max_paid_retries_per_shot_per_round:$/    if False:  # MUTATED/'

# 238: G-REC-1d: Ladder budget pre-check uses the per-round retry key
test_mutation 238 "Ladder budget pre-check uses the per-round retry key" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_round_ledger_refuses_when_shot_already_used_its_paid_retry" \
    sed -i 's/retry_key=f"m{n}", reason="moderation retry pre-check"/retry_key=None, reason="moderation retry pre-check"/'

# 239: G-REC-1e: Fallback re-route validated against routing
test_mutation 239 "Fallback re-route validated against routing" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_fallback_refused_by_routing" \
    sed -i 's/^            errors = validate_fallback(request, fallback)$/            errors = []  # MUTATED/'

# 240: G-REC-1f: Fallback re-route runs model_caps preflight
test_mutation 240 "Fallback re-route runs model_caps preflight" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_fallback_refused_by_model_caps_preflight" \
    sed -i 's/^    errors\.extend(preflight\.format_violation(v) for v in violations if v\.severity == "error")$/    errors.extend([])  # MUTATED/'

# 241: G-REC-1g: Provider cancel is classified distinctly from a content block
test_mutation 241 "Provider cancel is classified distinctly from a content block" \
    "hfvg/providers/kling_video.py" \
    "tests/test_moderation_recovery.py::TestClassification::test_kling_status_mapping" \
    sed -i 's/^            status = ProviderJobStatus\.CANCELED$/            status = ProviderJobStatus.BLOCKED  # MUTATED/'

# 242: G-REC-1h: Only content blocks enter the ladder (cancel does not)
test_mutation 242 "Only content blocks enter the ladder (cancel does not)" \
    "hfvg/workflows/shot.py" \
    "tests/test_moderation_recovery.py::TestLadderInWorkflow::test_canceled_clip_never_enters_the_ladder" \
    sed -i 's/if status == "blocked" or status_result/if status in ("blocked", "canceled") or status_result/'

# 243: G-REC-1i: Moderation resubmit gets its own job id
test_mutation 243 "Moderation resubmit gets its own job id" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_moderation_recovery.py::TestLiveModerationSubmit::test_moderation_resubmit_gets_own_job_id_and_round_hold" \
    sed -i 's/^        job_id += f"-m{int(moderation_retry\[.n.\])}"$/        pass  # MUTATED/'

# 244: G-REC-1j: Moderation resubmit takes a per-round paid retry hold
test_mutation 244 "Moderation resubmit takes a per-round paid retry hold" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_moderation_recovery.py::TestLiveModerationSubmit::test_moderation_resubmit_gets_own_job_id_and_round_hold" \
    sed -i 's/paid_retry_key = f"m{int(moderation_retry\[.n.\])}"/paid_retry_key = None  # MUTATED/'

# 245: G-REC-1k: Workflow resubmits the reworded prompt
test_mutation 245 "Workflow resubmits the reworded prompt" \
    "hfvg/workflows/shot.py" \
    "tests/test_moderation_recovery.py::TestLadderInWorkflow::test_blocked_clip_reworded_and_resubmitted_once" \
    sed -i 's/^        return self\.moderation\["clip"\]\["prompt"\] or self\._submitted_base_prompt("clip", shot_plan)$/        return self._submitted_base_prompt("clip", shot_plan)  # MUTATED/'

# 246: G-REC-1l: Workflow counts ladder steps already paid this round
test_mutation 246 "Workflow counts ladder steps already paid this round" \
    "hfvg/workflows/shot.py" \
    "tests/test_moderation_recovery.py::TestLadderInWorkflow::test_repeated_block_escalates_with_exact_provider_message" \
    sed -i 's/"paid_retries_used": len(mod\["steps"\]),/"paid_retries_used": 0,  # MUTATED/'

# 247: G-REC-1m: No attempts left escalates instead of retrying
test_mutation 247 "No attempts left escalates instead of retrying" \
    "hfvg/activities/moderation_recovery.py" \
    "tests/test_moderation_recovery.py::TestLadderDecisions::test_no_attempts_left_escalates" \
    sed -i 's/^    if not request\.get("attempts_left", 1):$/    if False:  # MUTATED/'

# 248: G-REC-1n: Escalated (blocked) shot reported as needs_review by the episode
test_mutation 248 "Escalated (blocked) shot reported as needs_review by the episode" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_moderation_recovery.py::TestLadderInWorkflow::test_repeated_block_escalates_with_exact_provider_message" \
    sed -i 's/result\.get("status") in ("failed", "blocked")/result.get("status") in ("failed",)/g'

# 249: G-REC-2a: Reconciler commits late completions
test_mutation 249 "Reconciler commits late completions" \
    "hfvg/workflows/reconcile.py" \
    "tests/test_reconciler.py::TestReconcilerWorkflow::test_reconcile_commits_releases_lands_and_never_double_charges" \
    sed -i 's/^                if state == "completed":$/                if state == "never":  # MUTATED/'

# 250: G-REC-2b: Reconciler never re-polls committed jobs (no double charge)
test_mutation 250 "Reconciler never re-polls committed jobs (no double charge)" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestReconcilerWorkflow::test_reconcile_commits_releases_lands_and_never_double_charges" \
    sed -i "s/FROM job_ledger WHERE status = 'pending_reconcile' ORDER BY/FROM job_ledger WHERE status IN ('pending_reconcile', 'committed') ORDER BY/"

# 251: G-REC-2c: Late content block / cancel releases the hold
test_mutation 251 "Late content block / cancel releases the hold" \
    "hfvg/workflows/reconcile.py" \
    "tests/test_reconciler.py::TestReconcilerWorkflow::test_content_block_and_cancel_on_reconcile_release" \
    sed -i 's/^_TERMINAL_FAILURES = ("failed", "blocked", "canceled")$/_TERMINAL_FAILURES = ("failed",)  # MUTATED/'

# 252: G-REC-2d: Late asset never overwrites an existing asset
test_mutation 252 "Late asset never overwrites an existing asset" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestLandLateAsset::test_never_overwrites_existing_asset" \
    sed -i 's/^    attach = not is_end and (row is None or not row\[0\])$/    attach = True  # MUTATED/'

# 253: G-REC-2e: Failed landings are retried by later passes
test_mutation 253 "Failed landings are retried by later passes" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestReconcilerWorkflow::test_failed_landing_is_retried_without_recommit" \
    sed -i 's/la\.attempts < ? AND/la.attempts < 0 + 0 * ? AND/'

# 254: G-REC-2f: Late stills are normalized to PNG
test_mutation 254 "Late stills are normalized to PNG" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestReconcilerWorkflow::test_failed_landing_is_retried_without_recommit" \
    sed -i 's/^        return out\.getvalue(), "\.png"$/        return data, ext  # MUTATED/'

# 255: G-REC-2g: Worker creates the reconcile schedule
test_mutation 255 "Worker creates the reconcile schedule" \
    "hfvg/worker.py" \
    "tests/test_reconciler.py::TestScheduling::test_worker_creates_the_schedule_idempotently" \
    sed -i 's/^    await ensure_reconcile_schedule(client, task_queue)$/    pass  # MUTATED/'

# 256: G-REC-3a: Stall watchdog honours the threshold
test_mutation 256 "Stall watchdog honours the threshold" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestStallWatchdog::test_threshold_alert_and_routing" \
    sed -i 's/^        if minutes >= threshold_minutes:$/        if minutes >= 0:  # MUTATED/'

# 257: G-REC-3b: Stall alert routes the job to the reconciler
test_mutation 257 "Stall alert routes the job to the reconciler" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestStallWatchdog::test_threshold_alert_and_routing" \
    sed -i 's/^            await ledger\.mark_job_status(job\["job_id"\], "pending_reconcile")$/            pass  # MUTATED/'

# 258: G-REC-3c: Stall alert status filter in the API
test_mutation 258 "Stall alert status filter in the API" \
    "hfvg/activities/reconcile.py" \
    "tests/test_reconciler.py::TestStallWatchdog::test_alerts_visible_in_api" \
    sed -i 's/^        clauses\.append("status = ?")$/        clauses.append("? IS NOT NULL")  # MUTATED/'
# Round 3 mutations: picture assembly, motion QC, clip prompts (G-AUD-1a, G-QC-2, G-PROMPT-1)

# 259: G-AUD-1a-1: Assembly path is saved to workflow state
test_mutation 259 "Mute edit path is saved to workflow state (and exposed)" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyInEpisode::test_no_checker_escalates_and_holds_final_cut" \
    sed -i 's/^                self\.state\.assembly_path = result\["assembly_path"\]$/                pass  # MUTATED/'

# 260: G-AUD-1a-2: A completed shot without a local clip escalates (never edited around)
test_mutation 260 "Completed shot without a local clip -> NEEDS_REVIEW" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyInEpisode::test_completed_shot_without_local_clip_escalates" \
    sed -i 's/^        if missing:$/        if False:  # MUTATED/'

# 261: G-AUD-1a-3: Assembly is idempotent (up-to-date edit reused)
test_mutation 261 "Up-to-date mute edit is reused (idempotency)" \
    "hfvg/activities/media.py" \
    "tests/test_picture_assembly_round3.py::TestAssembleMuteEdit::test_assemble_mute_edit_idempotent" \
    sed -i 's/^        if old\.get("input_key") == input_key and old\.get("size_bytes") == assembly_path\.stat()\.st_size:$/        if False:  # MUTATED/'

# 262: G-QC-2a: Motion check runs on the assembly
test_mutation 262 "Assembly QC runs the stillgate on the mute edit" \
    "hfvg/activities/qc.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyQC::test_still_only_shot_fails" \
    sed -i 's/^        rows = await asyncio\.to_thread(check_motion, assembly_path, manifest)$/        rows = []  # MUTATED/'

# 263: G-QC-2b: Stillgate decodes at its analysis fps (30 fps edit windows)
test_mutation 263 "Stillgate resamples to its analysis fps before windowing" \
    "hfvg/qc/motion.py" \
    "tests/test_picture_assembly_round3.py::TestStillgate::test_stillgate_windows_use_analysis_fps_on_30fps_edit" \
    sed -i 's/^        "-vf", f"fps={fps},scale={W}:{H}",$/        "-vf", f"scale={W}:{H}",  # MUTATED/'

# 264: G-PROMPT-1a: Clip prompt is parsed from shots.json
test_mutation 264 "shots.json clip_prompt field is parsed" \
    "hfvg/shot_data.py" \
    "tests/test_picture_assembly_round3.py::TestShotData::test_shot_data_clip_prompt_parsing" \
    sed -i 's/^            "clip_prompt": clip_prompt,  # None.*$/            "clip_prompt": None,  # MUTATED/'

# 265: G-PROMPT-1b: Clip prompt REPLACES the still prompt for the clip submit
test_mutation 265 "Clip submit uses clip_prompt alone (not still prompt)" \
    "hfvg/workflows/shot.py" \
    "tests/test_picture_assembly_round3.py::TestClipPromptInWorkflow::test_clip_prompt_replaces_still_prompt" \
    sed -i 's/^        if kind == "clip" and shot_plan\.get("clip_prompt"):$/        if False:  # MUTATED/'

# 266: G-PROMPT-1c: Preflight runs on the FINAL submitted clip prompt
test_mutation 266 "Live clip submit preflights the final clip prompt" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_picture_assembly_round3.py::TestClipPromptLimits::test_submit_clip_preflights_final_prompt" \
    sed -i 's/^        clip_violations = preflight\.preflight_shot($/        clip_violations = [] and preflight.preflight_shot(  # MUTATED/'

# 267: G-AUD-1a-4: Edit input key covers clip content (a changed clip rebuilds)
test_mutation 267 "Mute edit key includes clip content hash" \
    "hfvg/activities/media.py" \
    "tests/test_picture_assembly_round3.py::TestAssembleMuteEdit::test_assemble_mute_edit_idempotent" \
    sed -i 's/^        key_src\.append(\[e\["shot_id"\], e\["sha256"\], e\["trim_in"\], e\["trim_out"\]\])$/        key_src.append([e["shot_id"], e["trim_in"], e["trim_out"]])  # MUTATED/'

# 268: G-PROMPT-1d: merge_into_shots carries clip_prompt
test_mutation 268 "merge_into_shots carries clip_prompt to the plan" \
    "hfvg/shot_data.py" \
    "tests/test_picture_assembly_round3.py::TestShotData::test_merge_carries_clip_prompt_and_trim" \
    sed -i 's/^        new\["clip_prompt"\] = plan\.get("clip_prompt").*$/        pass  # MUTATED/'

# 269: G-QC-2c: NEEDS_REVIEW (no checker) blocks G_FINAL_CUT
test_mutation 269 "Assembly NEEDS_REVIEW verdict is blocking" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyInEpisode::test_no_checker_escalates_and_holds_final_cut" \
    sed -i 's/^        qc\["blocking"\] = qc\["verdict"\] in ("FAIL", "NEEDS_REVIEW")$/        qc["blocking"] = qc["verdict"] in ("FAIL",)  # MUTATED/'

# 270: G-QC-2d: Final cut approval refused while assembly QC is blocking
test_mutation 270 "approve_g_final_cut refuses a blocking assembly verdict" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyInEpisode::test_no_checker_escalates_and_holds_final_cut" \
    sed -i 's/^        if self\._assembly_blocking():$/        if False:  # MUTATED/'

# 271: G-QC-2e: An early approval is withdrawn by a blocking verdict
test_mutation 271 "Blocking assembly verdict withdraws an earlier approval" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyInEpisode::test_still_only_fail_blocks_until_clip_retry_rebuilds" \
    sed -i 's/^            if self\.approved_g_final_cut:  # an approval sent before this verdict does not count$/            if False:  # MUTATED/'

# 272: G-QC-2f: Per-clip stillgate in review_clip fails a still-only clip
test_mutation 272 "review_clip fails a still-only clip (per-clip stillgate)" \
    "hfvg/activities/qc.py" \
    "tests/test_picture_assembly_round3.py::TestStillgate::test_live_review_clip_runs_stillgate_per_clip" \
    sed -i 's/^            if motion\["verdict"\] == "FAIL":$/            if False:  # MUTATED/'

# 273: G-QC-2g: No animal checker escalates (never a silent pass)
test_mutation 273 "No animal checker -> NEEDS_REVIEW" \
    "hfvg/qc/animal_count.py" \
    "tests/test_picture_assembly_round3.py::TestAnimalChecks::test_no_checker_escalates_never_passes" \
    sed -i 's/^        "status": NEEDS_REVIEW,$/        "status": PASS,  # MUTATED/'

# 274: G-AUD-1a-5: API exposes the mute edit path
test_mutation 274 "Episode state API exposes assembly_path" \
    "api/main.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyAPI::test_state_exposes_assembly_and_accept_sends_signal" \
    sed -i 's/^        assembly_path=(state\.get("assembly") or {})\.get("path"),$/        assembly_path=None,  # MUTATED/'

# 275: G-PROMPT-1e: Clip submit lints with the clip (motion) rules
test_mutation 275 "Clip lint uses the motion rules for kind=clip" \
    "hfvg/prompt_lint.py" \
    "tests/test_picture_assembly_round3.py::TestClipPromptLimits::test_submit_clip_lints_final_prompt_with_clip_rules" \
    sed -i 's/^        check = clip_lint_violations if kind == "clip" else lint_violations$/        check = lint_violations  # MUTATED/'

# 276: G-AUD-1a-6: Worker registers the assembly activities
test_mutation 276 "Production worker registers assemble_mute_edit" \
    "hfvg/worker.py" \
    "tests/test_picture_assembly_round3.py::test_worker_registers_assembly_activities" \
    sed -i '/^    activities\.assemble_mute_edit,$/d'

# 277: G-PROMPT-1f: Moderation rewords the clip prompt actually submitted
test_mutation 277 "Clip moderation reword starts from the submitted clip prompt" \
    "hfvg/workflows/shot.py" \
    "tests/test_picture_assembly_round3.py::TestClipPromptInWorkflow::test_moderation_rewords_the_submitted_clip_prompt" \
    sed -i 's/^            "prompt": mod\["prompt"\] or self\._submitted_base_prompt(kind, shot_plan),$/            "prompt": mod["prompt"] or shot_plan["prompt"],  # MUTATED/'

# 278: G-AUD-1a-7: Per-shot trims come from the shot plan
test_mutation 278 "Assembly plan uses the shot's trim_in" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_picture_assembly_round3.py::TestAssemblyInEpisode::test_no_checker_escalates_and_holds_final_cut" \
    sed -i 's/^                "trim_in": float(trim\.get("in", 0\.0)),$/                "trim_in": 0.0,  # MUTATED/'

# 279: G-CAPS-1-1: Preflight refuses over-limit requests before spend
test_mutation 279 "Preflight enforces model_caps limits" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_preflight_refuses_over_limit_before_any_spend" \
    sed -i 's/^        if value < lo or value > hi:$/        if False:  # MUTATED/'

# 280: G-CAPS-1-2: eleven_v3 char limit is the documented 5,000
test_mutation 280 "eleven_v3 caps limit is 5000" \
    "hfvg/data/model_caps.json" \
    "tests/test_audio_round4_activities.py::test_caps_rows_match_official_numbers_and_costs" \
    sed -i 's/^     "value": 5000,$/     "value": 10000,/'

# 281: G-CAPS-1-3: Deprecated models are refused before spend
test_mutation 281 "Preflight refuses deprecated models" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_preflight_refuses_over_limit_before_any_spend" \
    sed -i 's/^    if caps.status in ("deprecated_upstream",):$/    if False:  # MUTATED/'

# 282: G-AUD-1b-1: Generated audio is reused by content hash (never paid twice)
test_mutation 282 "Generated VO reused by content hash" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_live_generation_reserves_commits_and_reuses" \
    sed -i 's/^    if out.is_file():  # content-hash reuse: already paid for (needs no key, spends nothing)$/    if False:  # MUTATED/'

# 283: G-CAPS-1-4: A failed generation releases its ledger reservation
test_mutation 283 "Failed audio generation releases the reservation" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_live_generation_failure_releases_hold" \
    sed -i 's/^        await ledger.release(episode_id, line, usd_micros, f"{kind} {asset_id} failed", job_id=job_id)$/        pass  # MUTATED/'

# 284: G-CAPS-1-5: Audio spend is authorized against the AUDIO round cap
test_mutation 284 "Audio spend authorized against the AUDIO round cap" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_round_cap_refuses_audio_spend" \
    sed -i 's/^        await ledger.authorize_round_spend(episode_id, AUDIO_ROUND, /        0 and await ledger.authorize_round_spend(episode_id, AUDIO_ROUND, /'

# 285: G-CAPS-1-6: Provider cost is character-based, never 0.0 (no-header path: the API estimate)
test_mutation 285 "TTS job cost is the per-character price" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_round4_activities.py::test_provider_tts_writes_durable_file_with_real_cost" \
    sed -i 's/^            cost = estimate_usd(job_type, params)$/            cost = 0.0  # MUTATED/'

# 286: G-CAPS-1-7: Music uses the real compose endpoint body
test_mutation 286 "Music request sends music_length_ms" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_round4_activities.py::test_provider_music_uses_compose_endpoint" \
    sed -i 's/"music_length_ms": int(round(duration \* 1000)),/"duration_seconds": duration,/'

# 287: G-CAPS-1-8: Provider returns a durable file URI
test_mutation 287 "Job status returns the written file URI" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_round4_activities.py::test_provider_tts_writes_durable_file_with_real_cost" \
    sed -i 's#output_url=Path(job\["path"\]).as_uri(), cost=job\["cost"\]#output_url=f"/tmp/{job_id}.mp3", cost=job["cost"]#'

# 288: G-AUD-1b-2: Missing audio inputs are non-retryable live
test_mutation 288 "Missing audio input errors are non-retryable" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_missing_inputs_are_non_retryable_live" \
    sed -i 's/^    return ApplicationError(msg, type=kind, non_retryable=True)$/    return ApplicationError(msg, type=kind, non_retryable=False)/'

# 289: G-AUD-1b-3: A live episode without an audio plan fails loudly
test_mutation 289 "Missing audio plan raises live" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_missing_inputs_are_non_retryable_live" \
    sed -i 's/^        raise _missing(f"No audio plan for {episode_id}: {p}", "MissingAudioPlan")$/        return {"plan_found": False}  # MUTATED/'

# 290: G-AUD-1b-4: VO placement enforces the minimum gap
test_mutation 290 "VO placement rejects gaps under the minimum" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_mix_audio_vo_gap_validation" \
    sed -i 's/^        if gap < min_gap_s - 1e-6:$/        if False:  # MUTATED/'

# 291: G-AUD-1b-5: The VO minimum gap setting is validated (0.3-0.4 s)
test_mutation 291 "Min VO gap validated to 0.3-0.4 s" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_vo_gap_constant" \
    sed -i 's/^    return _num(value, "min_vo_gap_s", lo, hi)$/    return float(value)  # MUTATED/'

# 292: G-AUD-1b-6: Music is ducked under VO
test_mutation 292 "Music bed gets the duck envelope" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_music_is_ducked_under_vo" \
    sed -i 's/^                music\[off:\] = bed \* env\[off:, None\]$/                music[off:] = bed  # MUTATED/'

# 293: G-AUD-1b-7: Pickup lines are RMS-matched
test_mutation 293 "VO pickup RMS match applied" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_mix_audio_final_basic" \
    sed -i 's/^        auto = pickup_gains(levels) if levels else {}$/        auto = {}  # MUTATED/'

# 294: G-AUD-1b-8: loudnorm JSON values must be finite
test_mutation 294 "loudnorm parse rejects non-finite values" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_loudnorm_json_parse_is_real_and_strict" \
    sed -i 's/^        if not math.isfinite(out\[k\]):$/        if False:  # MUTATED/'

# 295: G-AUD-1b-9: Delivery QC checks the true-peak ceiling
test_mutation 295 "Delivery QC true-peak ceiling" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_delivery_qc_fails_on_measured_misses" \
    sed -i 's/m\["true_peak_dbtp"\] <= loud\["tp_ceiling_dbtp"\], m\["true_peak_dbtp"\],/True, m["true_peak_dbtp"],/'

# 296: G-AUD-1b-10: Delivery QC checks loudness tolerance
test_mutation 296 "Delivery QC loudness tolerance" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_delivery_qc_fails_on_measured_misses" \
    sed -i 's/abs(m\["integrated_lufs"\] - loud\["target_lufs"\]) <= loud\["tolerance_lu"\],/True,/'

# 297: G-AUD-1b-11: Delivery QC checks duration vs picture
test_mutation 297 "Delivery QC duration vs picture" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_delivery_qc_fails_on_measured_misses" \
    sed -i 's/abs(dur - pic) <= DURATION_TOLERANCE_S,/True,/'

# 298: G-AUD-1b-12: Delivery QC requires an AAC audio stream
test_mutation 298 "Delivery QC audio stream" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_delivery_qc_requires_audio_stream" \
    sed -i 's/bool(a) and a\[0\].get("codec_name") == "aac",/True,/'

# 299: G-AUD-1b-13: Delivery QC checks written subtitle timing
test_mutation 299 "Delivery QC subtitle timing" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_delivery_qc_fails_on_measured_misses" \
    sed -i 's/bool(found) and dev <= CUE_TOLERANCE_S,/True,/'

# 300: G-AUD-1b-14: Over-size phone encode gets one re-encode
test_mutation 300 "Phone encode re-encodes once when over size" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_phone_over_size_gets_one_reencode" \
    sed -i 's/^        if size > max_bytes:$/        if False:  # MUTATED/'

# 301: G-AUD-1b-15: Delivery reuse key hashes every input file
test_mutation 301 "Delivery input key includes file hashes" \
    "hfvg/audio_mix.py" \
    "tests/test_audio_delivery.py::test_mix_audio_idempotent_reuse" \
    sed -i 's/^    hashes = {p: sha256_file(p) for p in inputs}$/    hashes = {p: "same" for p in inputs}  # MUTATED/'

# 302: G-AUD-1b-16: Final cut approval refused while delivery QC blocks
test_mutation 302 "Final cut refused on blocking delivery QC" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_audio_delivery_workflow.py::TestDeliveryInEpisode::test_delivery_failure_blocks_final_cut_until_accepted" \
    sed -i 's/^        if self._delivery_blocking():$/        if False:  # MUTATED/'

# 303: G-AUD-1b-17: Delivery FAIL / NEEDS_REVIEW is blocking
test_mutation 303 "Delivery QC verdict sets blocking" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_audio_delivery_workflow.py::TestDeliveryInEpisode::test_delivery_failure_blocks_final_cut_until_accepted" \
    sed -i '/self.delivery_phase = "done"/{n;s/.*/        qc["blocking"] = False  # MUTATED/}'

# 304: G-AUD-1b-18: Delivery QC acceptance needs a reason
test_mutation 304 "Delivery acceptance requires a reason" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_audio_delivery_workflow.py::TestDeliveryInEpisode::test_delivery_failure_blocks_final_cut_until_accepted" \
    sed -i '/def accept_delivery_qc/,/a reason is required/s/if not (reason or "").strip():/if False:  # MUTATED/'

# 305: G-AUD-1b-19: API delivery accept sends the delivery signal
test_mutation 305 "API delivery accept signal" \
    "api/main.py" \
    "tests/test_audio_delivery_workflow.py::test_state_exposes_delivery_and_accept_sends_signal" \
    sed -i 's/await handle.signal("accept_delivery_qc", request.reason.strip())/await handle.signal("accept_assembly_qc", request.reason.strip())/'

# 306: G-AUD-1b-20: Production worker registers mix_and_deliver
test_mutation 306 "Worker registers mix_and_deliver" \
    "hfvg/worker.py" \
    "tests/test_audio_delivery_workflow.py::test_worker_registers_audio_delivery_activities" \
    sed -i '/^    activities.mix_and_deliver,$/d'

# 307: G-CAPS-1-9: TTS cost estimate needs the text
test_mutation 307 "TTS estimate refuses missing text" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_round4_activities.py::test_char_based_cost_never_zero" \
    sed -i 's/^        if chars == 0:$/        if False:  # MUTATED/'

# 308: G-AUD-1b-21: Offline mix CLI never generates
test_mutation 308 "hfvg mix refuses generation offline" \
    "hfvg/cli.py" \
    "tests/test_audio_delivery_workflow.py::test_cli_mix_on_episode_dir" \
    sed -i 's/^            if not dry_run:$/            if False:  # MUTATED/'

# 309: G-AUD-1b-22: CLI stand-in delivery is DRY_RUN, not PASS
test_mutation 309 "hfvg mix --dry-run verdict is DRY_RUN" \
    "hfvg/cli.py" \
    "tests/test_audio_delivery_workflow.py::test_cli_mix_on_episode_dir" \
    sed -i '/if dry_inputs and qc.get("verdict") == "PASS":/s/if dry_inputs and/if False and/'

# 310: G-CAPS-1-10: Live ElevenLabs provider requires an API key
test_mutation 310 "Live provider requires an API key" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_round4_activities.py::test_live_provider_requires_api_key" \
    sed -i 's/^        if not self.dry_run and not self.api_key:$/        if False:  # MUTATED/'

# 311: G-AUD-1b-23: Stand-in audio never reports PASS
test_mutation 311 "Stand-in inputs give DRY_RUN not PASS" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_delivery.py::test_stand_in_inputs_are_never_a_pass" \
    sed -i 's/"verdict": "DRY_RUN" if qc\["verdict"\] == "PASS" else qc\["verdict"\],/"verdict": qc["verdict"],/'

# 312: G-CAPS-1-11: TTS preflight runs before the spend
test_mutation 312 "VO generation preflights text length" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_round4_activities.py::test_preflight_refuses_over_limit_before_any_spend" \
    sed -i 's/^    _preflight(model, text=script)$/    pass  # MUTATED/'

# 313: G-AUTH-1: POST /api/lint requires authentication
test_mutation 313 "lint route requires auth" \
    "api/main.py" \
    "tests/test_api_routes.py::test_lint_route_requires_auth" \
    sed -i -e 's/^    ("POST", "\/api\/login"),$/&\n    ("POST", "\/api\/lint"),/' -e '/^async def lint_prompt_endpoint/,/^    return {"problems"/s/^    await verify_auth_unified(authorization, studio_admin_token)$/    pass  # MUTATED/'

# 314: G-UI-1: review file route path containment (resolved is_relative_to)
test_mutation 314 "file route path containment" \
    "api/main.py" \
    "tests/test_api_routes.py::test_file_download_path_containment" \
    sed -i 's/^    if not resolved.is_relative_to(root_resolved):$/    if False:  # MUTATED/'

# 315: G-AUTH-1: app-wide auth guard (only the PUBLIC_ROUTES allowlist skips auth)
test_mutation 315 "global auth guard" \
    "api/main.py" \
    "tests/test_api_routes.py::test_all_mutating_routes_require_auth" \
    sed -i 's/^    if (method, path) in PUBLIC_ROUTES:$/    if method != "GET" or path.startswith("\/api\/episodes"):  # MUTATED/'

# 316: G-AUTH-1: approve workflow lookup query parenthesised
test_mutation 316 "approve query parentheses" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_approve_lookup_query_is_parenthesised" \
    sed -i 's/(WorkflowId = "{episode_id}" OR WorkflowId STARTS_WITH "{episode_id}-") AND WorkflowType/WorkflowId = "{episode_id}" OR WorkflowId STARTS_WITH "{episode_id}-" AND WorkflowType/'

# 317: G-AUTH-1: web middleware verifier checks the HMAC
test_mutation 317 "middleware HMAC check" \
    "web/lib/session.mjs" \
    "tests/test_round5_guards.py::test_web_middleware_session_verifier_unit_tests" \
    sed -i "s/^  if (!timingSafeEqual(expected, token)) return { ok: false, reason: 'bad-signature' };$/  \/\/ MUTATED: signature not checked/"

# 318: G-AUTH-1: web middleware verifier checks expiry
test_mutation 318 "middleware expiry check" \
    "web/lib/session.mjs" \
    "tests/test_round5_guards.py::test_web_middleware_session_verifier_unit_tests" \
    sed -i "s/^  if (!(Number(parts\[1\]) > nowSeconds)) return { ok: false, reason: 'expired' };$/  \/\/ MUTATED: expiry not checked/"

# 319: G-AUTH-1: web middleware compare is constant-time over both lengths
test_mutation 319 "middleware compare folds length" \
    "web/lib/session.mjs" \
    "tests/test_round5_guards.py::test_web_middleware_session_verifier_unit_tests" \
    sed -i 's/^  let diff = x.length ^ y.length;$/  let diff = 0;  \/\/ MUTATED/; s/^  const n = Math.max(x.length, y.length);$/  const n = Math.min(x.length, y.length);/'

# 320: G-AUTH-1: API session signature compare
test_mutation 320 "API token signature check" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_session_signature_check_rejects_forgery_expiry_and_malformed" \
    sed -i 's/^    if not secrets.compare_digest(expected.encode(), token.encode()):$/    if False:  # MUTATED/'

# 321: G-AUTH-1: API session token expiry from the signed payload
test_mutation 321 "API token expiry" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_api_refuses_forged_expired_and_revoked_sessions" \
    sed -i 's/^    return int(parts\[1\]) > (time.time() if now is None else now)$/    return True  # MUTATED/'

# 322: G-AUTH-1: logout revokes the session row
test_mutation 322 "logout revokes session" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_api_refuses_forged_expired_and_revoked_sessions" \
    sed -i 's/^        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))$/        pass  # MUTATED/'

# 323: G-AUTH-1: API verifies the signature before trusting the DB row
test_mutation 323 "signature checked before DB" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_api_refuses_forged_expired_and_revoked_sessions" \
    sed -i '/^async def verify_session_token/,/^async def revoke_session/s/^    if not check_session_signature(token):$/    if False:  # MUTATED/'

# 324: G-AUTH-1: web middleware verifies the token (not cookie presence)
test_mutation 324 "middleware uses verifier" \
    "web/middleware.ts" \
    "tests/test_round5_guards.py::test_web_middleware_uses_the_verifier" \
    sed -i 's/verifySessionToken(token, key)/({ ok: Boolean(token) })/'

# 325: G-UI-1: review file route whitelist
test_mutation 325 "file route whitelist" \
    "api/main.py" \
    "tests/test_api_routes.py::test_file_route_whitelist" \
    sed -i 's/^    "mute_edit": ("edit", "mute_assembly.mp4"),$/&\n    "shots.json": ("edit", "..\/shots.json"),/'

# 326: G-UI-1: start refuses an invalid uploaded shots.json before the workflow starts
test_mutation 326 "start refuses bad shots" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_start_refuses_invalid_uploaded_shots_before_starting" \
    sed -i 's/^    if shots_problems:$/    if False:  # MUTATED/'

# 327: G-UI-1: approve G_FINAL_CUT refused (409) while assembly QC blocks
test_mutation 327 "final approve refused on assembly QC" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_final_cut_approve_refused_while_blocked" \
    sed -i '/^def final_cut_blockers/,/^    return problems/s/^    if assembly.get("blocking"):$/    if False:  # MUTATED/'

# 328: G-UI-1: approve G_FINAL_CUT refused (409) while delivery QC blocks
test_mutation 328 "final approve refused on delivery QC" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_final_cut_approve_refused_while_blocked" \
    sed -i '/^def final_cut_blockers/,/^    return problems/s/^    if delivery.get("blocking"):$/    if False:  # MUTATED/'

# 329: G-UI-1: approve G_FINAL_CUT route enforces the blockers
test_mutation 329 "final approve enforces blockers" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_final_cut_approve_refused_while_blocked" \
    sed -i 's/^                problems = final_cut_blockers(state)$/                problems = []  # MUTATED/'

# 330: G-UI-1: final-cut reject refused off-gate
test_mutation 330 "final reject off-gate 409" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_final_cut_reject_route" \
    sed -i '/^async def reject_final_cut/,/^class RaiseCapRequest/s/^    if state.get("current_gate") != "G_FINAL_CUT":$/    if False:  # MUTATED/'

# 331: G-UI-1: raise-cap only upward
test_mutation 331 "raise cap only upward" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_raise_cap_route_only_upward" \
    sed -i 's/^    if current is not None and request.new_cap <= current:$/    if False:  # MUTATED/'

# 332: G-UI-1: episode id fullmatch (no trailing newline / longer ids)
test_mutation 332 "episode id fullmatch" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_episode_id_format_is_exact" \
    sed -i 's/re.fullmatch(EPISODE_ID_PATTERN, episode_id)/re.match(EPISODE_ID_PATTERN, episode_id)/'

# 333: G-UI-1: episode id ASCII digits only
test_mutation 333 "episode id ascii digits" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_episode_id_format_is_exact" \
    sed -i 's/^EPISODE_ID_PATTERN = r"ep\[0-9\]{2}"/EPISODE_ID_PATTERN = r"ep\\d{2}"/'

# 334: G-UI-1: cap raise is applied while parked at G_FINAL_CUT
test_mutation 334 "cap raise at final gate" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_round5_guards.py::test_dry_run_final_gate_actions_real_workflow" \
    sed -i 's/^                lambda: self.state.new_credit_cap > 0 or (not self.pending_clip_retries and ($/                lambda: (not self.pending_clip_retries and (/'

# 335: G-UI-1: per-shot G2 approve records the decision
test_mutation 335 "per-shot approve recorded" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_round5_guards.py::test_dry_run_final_gate_actions_real_workflow" \
    sed -i 's/^        self.state.shot_decisions\[shot_id\] = "approved"$/        pass  # MUTATED/'

# 336: G-UI-1: final-cut reject recorded and approval withdrawn
test_mutation 336 "final reject recorded" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_round5_guards.py::test_dry_run_final_gate_actions_real_workflow" \
    sed -i '/^    def reject_g_final_cut/,/^    def accept_assembly_qc/s/^        self.final_cut_rejections.append({$/        [].append({/'

# 337: G-AUTH-1: login compares the admin secret in constant time
test_mutation 337 "login constant-time compare" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_login_compares_secret_in_constant_time" \
    sed -i 's/^    if not request.admin_secret or not secrets.compare_digest($/    if not request.admin_secret or request.admin_secret != admin_secret or not (  # MUTATED/'

# 338: G-UI-1: preflight reports the offline cost estimate
test_mutation 338 "preflight cost estimate" \
    "api/main.py" \
    "tests/test_round5_guards.py::test_preflight_reports_offline_cost_estimate" \
    sed -i 's/"cost_estimate": /"cost_estimate_unused": /'

# 339: G-BRAND-1: the policy project comes from HFVG_PROJECT (default demo), not a hardcoded name
test_mutation 339 "HFVG_PROJECT read from env" \
    "hfvg/gates.py" \
    "tests/test_no_brand_content.py::test_hfvg_project_defaults_to_demo_and_is_read_from_env" \
    sed -i 's/^        project = os.getenv("HFVG_PROJECT", "demo")$/        project = "demo"  # MUTATED: env ignored/'

# 340: G-BRAND-1: the denylist matcher is case-insensitive
test_mutation 340 "brand matcher case-insensitive" \
    "tests/test_no_brand_content.py" \
    "tests/test_no_brand_content.py::test_matcher_flags_content_and_file_names_case_insensitively" \
    sed -i 's/^        lowered = data.lower()$/        lowered = data  # MUTATED: case-sensitive/'

# 341: G-CI-2: the 5xx back-off sleeps the module poll interval (patchable), not a literal
test_mutation 341 "poll interval from module constant" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_exception_release.py::test_poll_502_releases_reservation" \
    sed -i 's/^        active_poll_interval = ACTIVE_POLL_INTERVAL_S$/        active_poll_interval = 5.0  # MUTATED: hardcoded/'

# 342: G-AUD-KEY: TTS checks the ElevenLabs key before preflight/override log/hold/reservation
test_mutation 342 "elevenlabs tts key check" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_credentials.py::test_missing_elevenlabs_key_refused_before_any_ledger_row[tts]" \
    sed -i 's/^    _check_elevenlabs_credentials()  # G-AUD-KEY tts:.*$/    pass  # MUTATED/'

# 343: G-AUD-KEY: SFX checks the ElevenLabs key first
test_mutation 343 "elevenlabs sfx key check" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_credentials.py::test_missing_elevenlabs_key_refused_before_any_ledger_row[sfx]" \
    sed -i 's/^    _check_elevenlabs_credentials()  # G-AUD-KEY sfx:.*$/    pass  # MUTATED/'

# 344: G-AUD-KEY: music checks the ElevenLabs key first
test_mutation 344 "elevenlabs music key check" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_credentials.py::test_missing_elevenlabs_key_refused_before_any_ledger_row[music]" \
    sed -i 's/^    _check_elevenlabs_credentials()  # G-AUD-KEY music:.*$/    pass  # MUTATED/'

# 345: G-AUD-KEY: a live still checks the Higgsfield key before the smoke gate and any ledger row
test_mutation 345 "higgsfield still key check" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_higgsfield_credentials.py::test_live_submit_without_usable_key_refused_before_any_ledger_row[still-malformed-no-colon-InvalidCredentials]" \
    sed -i 's/^        _check_higgsfield_credentials()  # G-AUD-KEY still$/        pass  # MUTATED/'

# 346: G-AUD-KEY: a live clip checks the Higgsfield key first
test_mutation 346 "higgsfield clip key check" \
    "hfvg/activities/studio_generation.py" \
    "tests/test_higgsfield_credentials.py::test_live_submit_without_usable_key_refused_before_any_ledger_row[clip-malformed-no-colon-InvalidCredentials]" \
    sed -i 's/^        _check_higgsfield_credentials()  # G-AUD-KEY clip$/        pass  # MUTATED/'

# 347: G-AUD-COST: the character-cost header is read
test_mutation 347 "character-cost header read" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_cost.py::test_tts_commits_header_credits_and_releases_the_rest" \
    sed -i 's/^        if str(k).lower() == CHARACTER_COST_HEADER:$/        if False:  # MUTATED/'

# 348: G-AUD-COST: commit the header credits x rate, not the reservation
test_mutation 348 "commit header credits" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_cost.py::test_tts_commits_header_credits_and_releases_the_rest" \
    sed -i 's/^        actual = credits_to_usd_micros(float(credits))$/        actual = usd_micros  # MUTATED: commit the reservation/'

# 349: G-AUD-COST: the migration adds every usage column to job_ledger
test_mutation 349 "job_ledger usage migration" \
    "hfvg/budget.py" \
    "tests/test_audio_cost.py::test_job_ledger_migration_adds_usage_columns_and_keeps_rows" \
    sed -i 's/for col, decl in (("characters", "INTEGER"), ("credits", "REAL"), ("cost_source", "TEXT")):/for col, decl in (("characters", "INTEGER"), ("credits", "REAL")):  # MUTATED/'

# 350: G-AUD-COST: commit stores the credits on the job row
test_mutation 350 "commit stores credits" \
    "hfvg/budget.py" \
    "tests/test_audio_cost.py::test_tts_commits_header_credits_and_releases_the_rest" \
    sed -i 's/credits = COALESCE(?, credits),$/credits = COALESCE(NULL * ?, credits),  -- MUTATED/'

# 351: G-AUD-COST: without the header the estimate is committed and flagged cost_source=estimate
test_mutation 351 "missing header flagged" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_cost.py::test_missing_header_commits_the_estimate_flagged" \
    sed -i 's/^        cost_source = "estimate"$/        cost_source = COST_SOURCE_HEADER  # MUTATED: unflagged/'

# 352: G-BRAND-1: the denylist keeps every required term
test_mutation 352 "brand denylist complete" \
    "tests/test_no_brand_content.py" \
    "tests/test_no_brand_content.py::test_denylist_covers_the_required_terms" \
    sed -i '/^    # owner \/ brand \/ project$/{n;d}'

# 353: G-BRAND-1: HFVG_PROJECT must be a plain name (no path traversal)
test_mutation 353 "HFVG_PROJECT name validated" \
    "hfvg/gates.py" \
    "tests/test_no_brand_content.py::test_hfvg_project_rejects_path_like_names[../demo]" \
    sed -i 's/^    if not _PROJECT_RE.fullmatch(project or ""):$/    if False:  # MUTATED/'

# 354: G-AUD-COST: the header name is matched case-insensitively
test_mutation 354 "character-cost header case-insensitive" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_cost.py::test_character_cost_header_parsing" \
    sed -i 's/^        if str(k).lower() == CHARACTER_COST_HEADER:$/        if str(k) == CHARACTER_COST_HEADER:  # MUTATED/'

# 355: G-AUD-COST: the reservation is the larger of the API estimate and the credit upper bound
test_mutation 355 "reservation upper bound" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_cost.py::test_reservation_is_the_documented_upper_bound" \
    sed -i 's/^    return max(estimate_usd_micros(job_type, params),$/    return min(estimate_usd_micros(job_type, params),  # MUTATED/'

# 356: G-AUD-COST: the job row exists (reserved) during the provider call
test_mutation 356 "job row recorded before the call" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_cost.py::test_job_row_is_reserved_during_the_provider_call" \
    sed -i 's/^    await ledger.record_job(episode_id, job_id, asset_id, model, "final", line, usd_micros)$/    pass  # MUTATED/'

# 357: G-AUD-COST: ELEVENLABS_USD_PER_CREDIT fails closed on zero/negative
test_mutation 357 "usd per credit fails closed" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_cost.py::test_usd_per_credit_is_configurable_and_fails_closed" \
    sed -i 's/^    if not math.isfinite(rate) or rate <= 0:$/    if not math.isfinite(rate):  # MUTATED/'

# 358: G-AUD-COST: a bad rate is a non-retryable refusal before any ledger row
test_mutation 358 "bad rate refused non-retryable" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_cost.py::test_bad_rate_refused_before_any_ledger_row" \
    sed -i '/^def _reservation/,/^$/s/^    except ValueError as e:$/    except ZeroDivisionError as e:  # MUTATED/'

# 359: G-AUD-COST: credits convert to micro-dollars rounding up
test_mutation 359 "credits to usd rounds up" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_cost.py::test_credits_to_usd_micros_rounds_up" \
    sed -i 's/math.ceil(credits \* usd_per_credit() \* 1_000_000 - 1e-9)/int(credits * usd_per_credit() * 1_000_000)/'

# 360: G-AUD-COST: the default rate is the highest paid-plan rate (never under-reserves)
test_mutation 360 "default usd per credit" \
    "hfvg/providers/elevenlabs.py" \
    "tests/test_audio_cost.py::test_default_usd_per_credit_is_the_highest_paid_plan_rate" \
    sed -i 's/^DEFAULT_USD_PER_CREDIT = "0.0002"$/DEFAULT_USD_PER_CREDIT = "0.0001"  # MUTATED/'

# 361: G-AUD-COST: an actual above the reservation is flagged, never hidden
test_mutation 361 "over-reservation flagged" \
    "hfvg/activities/audio.py" \
    "tests/test_audio_cost.py::test_header_above_the_reservation_is_committed_honestly" \
    sed -i 's/^    over = actual > usd_micros$/    over = False  # MUTATED/'

# 362: G-CI-1: the shard coverage check fails when a declared id never ran
test_mutation 362 "coverage check catches a dropped id" \
    "scripts/redgreen_coverage.py" \
    "tests/test_redgreen_coverage.py::test_dropped_mutation_fails" \
    sed -i 's/^    if missing:$/    if False:  # MUTATED/'

# 363: G-CI-2: a rejected child records its own terminal status (late still_complete write race)
test_mutation 363 "rejected child records 'rejected' after a late still_complete write" \
    "hfvg/workflows/shot.py" \
    "tests/test_gate_correctness_round1.py::TestPerShotG2Decisions::test_rejected_status_survives_a_late_still_complete_write" \
    sed -i 's/^                    "rejected",$/                    "still_complete",  # MUTATED/'

# 364: G-CI-1: an INVALID id fails the shard (not only FAIL)
test_mutation 364 "redgreen summary fails a shard with INVALID ids" \
    "scripts/redgreen.sh" \
    "tests/test_redgreen_coverage.py::test_shard_summary_fails_on_invalid_ids" \
    sed -i 's/^    if \[ \$FAIL_COUNT -eq 0 \] \&\& \[ \$INVALID_COUNT -eq 0 \]; then$/    if [ $FAIL_COUNT -eq 0 ]; then  # MUTATED/'

# Print final summary (no `set -e` exit here: the results file and the clean-tree check must run)
if print_summary; then exit_code=0; else exit_code=1; fi

# Per-id results for scripts/redgreen_coverage.py (CI uploads this file as an artifact).
mkdir -p "$(dirname "$RESULTS_FILE")"
{
    printf '# shard\t%s\t%s\t%s\t%s\n' "$SHARD_NUM" "$TOTAL_SHARDS" "$CURRENT_COMMIT" "$((DECL_ORDINAL + 1))"
    for i in "${!MUTATION_IDS[@]}"; do
        printf '%s\t%s\t%s\n' "${MUTATION_IDS[$i]}" "${MUTATION_RESULTS[$i]}" "${MUTATION_REASONS[$i]//$'\t'/ }"
    done
} > "$RESULTS_FILE"
echo "Per-mutation results written to $RESULTS_FILE (${#MUTATION_IDS[@]} entries, declared ordinals: $((DECL_ORDINAL + 1)))"

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

# 365: B1: pre-approved shots skip still generation but go through clip
test_mutation 365 "pre-approved still skips generation" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_anchor_still.py" \
    sed -i 's/if shot_id in pre_approved_stills:/if False:  # MUTATED/'

# 366: B2: gate policy path resolved from HFVG_GATE_POLICY env var
test_mutation 366 "gate policy env var" \
    "hfvg/gates.py" \
    "tests/test_gate_policy_path.py" \
    sed -i 's/env_path = os.getenv("HFVG_GATE_POLICY")/env_path = None  # MUTATED/'

# 367: B3: owner_approval_points key missing causes clear error
test_mutation 367 "owner approval points required" \
    "hfvg/gates.py" \
    "tests/test_gate_policy_schema.py" \
    sed -i 's/raise ValueError(/pass; return  # MUTATED; raise ValueError(/'

# 368: B4: G1 dry mode uses real provider estimates
test_mutation 368 "G1 real estimates in dry mode" \
    "hfvg/activities/g1_plan.py" \
    "tests/test_g1_estimates.py" \
    sed -i 's/# Both dry and live modes/if dry_run: return {"shots": [], "total_credits": 0}  # MUTATED\n    /'

# 369: B5: prompt lint runs in preflight
test_mutation 369 "prompt lint in preflight" \
    "hfvg/activities/preflight.py" \
    "tests/test_preflight_lint.py" \
    sed -i 's/violations = lint_violations(/violations = []  # MUTATED; lint_violations(/'

# 370: B6: ref resolution validated against model caps
test_mutation 370 "ref resolution validation" \
    "hfvg/activities/ref_generation.py" \
    "tests/test_ref_resolution.py" \
    sed -i 's/if resolution not in allowed:/if False:  # MUTATED/'

# 371: B7: dry mode audio duration from plan
test_mutation 371 "dry mode audio plan duration" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_audio_dry_mode.py" \
    sed -i 's/picture_s += (trim_out - trim_in)/picture_s += 5.0  # MUTATED/'

# 372: B8: balance check required in live mode
test_mutation 372 "balance check live mode" \
    "hfvg/activities/balance_check.py" \
    "tests/test_balance_check.py" \
    sed -i 's/if not row:/if False:  # MUTATED/'

# 373: Trims: trim_head_s validated against duration
test_mutation 373 "trim validation" \
    "hfvg/workflows/episode_v2.py" \
    "tests/test_trims.py" \
    sed -i 's/if trim_in < 0 or trim_out > duration/if False  # MUTATED/'
