#!/bin/bash
# Stress test for M117: run test_retry_shot_respects_max_retries under CPU load

set -e

TEST_NAME="tests/test_retry_shot.py::test_retry_shot_respects_max_retries"
RUNS=50
PASS=0
FAIL=0

echo "Running $TEST_NAME $RUNS times under CPU load..."
echo "Starting 4 parallel stress processes to simulate CI load..."

# Start CPU stress in background (4 processes like 4 redgreen shards)
stress --cpu 4 --timeout 600s &
STRESS_PID=$!

trap "kill $STRESS_PID 2>/dev/null || true" EXIT

for i in $(seq 1 $RUNS); do
    echo -n "Run $i/$RUNS: "
    if timeout 30 python3 -m pytest "$TEST_NAME" -xvs > /tmp/stress_$i.log 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL"
        FAIL=$((FAIL + 1))
        # Save failed run log
        cp /tmp/stress_$i.log /tmp/stress_fail_$i.log
        # Show the error
        tail -20 /tmp/stress_$i.log
        break  # Stop on first failure to inspect
    fi
done

kill $STRESS_PID 2>/dev/null || true

echo ""
echo "Results: $PASS passed, $FAIL failed out of $RUNS runs"
if [ $FAIL -eq 0 ]; then
    echo "SUCCESS: All runs passed under load"
    exit 0
else
    echo "FAILURE: Found flaky behavior"
    exit 1
fi
