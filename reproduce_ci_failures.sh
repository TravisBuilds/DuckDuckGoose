#!/bin/bash
# Reproduce CI baseline failures under CI-like load: 4 parallel processes on 2 CPUs

set -e

# Tests that failed in CI
TESTS=(
    "tests/test_redgreen_safety.py::test_approval_signal_reaches_episode_workflow"
    "tests/test_audio_delivery_workflow.py::TestDeliveryInEpisode::test_audio_waits_for_picture_lock"
    "tests/test_retry_shot.py::test_retry_shot_increments_version_for_unique_job_id"
    "tests/test_gate_correctness_round1.py::TestClipRetry::test_retry_clip_reuses_approved_still_and_is_bounded"
)

TEST_NAMES=(
    "M10_approval_signal"
    "M23_audio_waits_picture"
    "M121_retry_increments_version"
    "M228_retry_clip_bounded"
)

# Check if taskset is available
if ! command -v taskset &> /dev/null; then
    echo "taskset not found, running without CPU restriction"
    TASKSET=""
else
    # Limit to 2 CPUs (0 and 1) to simulate CI resource constraints
    TASKSET="taskset -c 0,1"
    echo "Using taskset to limit to 2 CPUs"
fi

# Function to run a test multiple times under load
test_under_load() {
    local test_path=$1
    local test_name=$2
    local runs=$3
    
    echo ""
    echo "============================================"
    echo "Testing: $test_name"
    echo "Path: $test_path"
    echo "Runs: $runs"
    echo "============================================"
    
    local pass=0
    local fail=0
    
    # Start 4 background load processes (simulating 4 redgreen shards)
    local bg_pids=()
    for shard in 1 2 3 4; do
        (
            while true; do
                $TASKSET python3 -m pytest tests/test_budget.py -q > /dev/null 2>&1 || true
                sleep 0.05
            done
        ) &
        bg_pids+=($!)
    done
    
    trap "for pid in \${bg_pids[@]}; do kill \$pid 2>/dev/null || true; done" EXIT
    
    sleep 2  # Let background load start
    
    for i in $(seq 1 $runs); do
        echo -n "Run $i/$runs: "
        if $TASKSET timeout 60 python3 -m pytest "$test_path" -xvs > /tmp/${test_name}_$i.log 2>&1; then
            echo "PASS"
            pass=$((pass + 1))
        else
            echo "FAIL"
            fail=$((fail + 1))
            cp /tmp/${test_name}_$i.log /tmp/${test_name}_fail_$i.log
            echo "=== FAILURE LOG (last 30 lines) ==="
            tail -30 /tmp/${test_name}_fail_$i.log
            # Stop on first failure to analyze
            break
        fi
    done
    
    # Kill background load
    for pid in ${bg_pids[@]}; do
        kill $pid 2>/dev/null || true
    done
    trap - EXIT
    
    echo ""
    echo "Results for $test_name: $pass passed, $fail failed out of $runs runs"
    
    if [ $fail -gt 0 ]; then
        return 1
    fi
    return 0
}

# Test each one
for i in ${!TESTS[@]}; do
    if ! test_under_load "${TESTS[$i]}" "${TEST_NAMES[$i]}" 20; then
        echo ""
        echo "STOPPING: Found failure in ${TEST_NAMES[$i]}"
        exit 1
    fi
done

echo ""
echo "SUCCESS: All tests passed 20/20 times under CI-like load"
