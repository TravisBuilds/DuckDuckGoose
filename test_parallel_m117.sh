#!/bin/bash
# Parallel stress test for M117: run the test concurrently like CI does

set -e

TEST_NAME="tests/test_retry_shot.py::test_retry_shot_respects_max_retries"
PARALLEL=8
RUNS=50
PASS=0
FAIL=0

echo "Running $TEST_NAME $RUNS times with $PARALLEL parallel workers..."

for i in $(seq 1 $RUNS); do
    echo -n "Run $i/$RUNS: "
    # Run with pytest-xdist to simulate concurrent execution
    if timeout 60 python3 -m pytest "$TEST_NAME" -n $PARALLEL -x > /tmp/parallel_$i.log 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL"
        FAIL=$((FAIL + 1))
        # Save failed run log
        cp /tmp/parallel_$i.log /tmp/parallel_fail_$i.log
        # Show the error
        echo "=== FAILURE LOG ==="
        tail -50 /tmp/parallel_$i.log
        break  # Stop on first failure to inspect
    fi
done

echo ""
echo "Results: $PASS passed, $FAIL failed out of $RUNS runs"
if [ $FAIL -eq 0 ]; then
    echo "SUCCESS: All runs passed under parallel load"
    exit 0
else
    echo "FAILURE: Found flaky behavior under parallel execution"
    exit 1
fi
