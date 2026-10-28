#!/bin/bash
# Concurrent stress test: run M117 test while other tests run in parallel

set -e

TEST_NAME="tests/test_retry_shot.py::test_retry_shot_respects_max_retries"
RUNS=50
PASS=0
FAIL=0

echo "Running $TEST_NAME $RUNS times while other tests run concurrently..."

# Start background test load (like CI running multiple shards)
echo "Starting background test load..."
for shard in 1 2 3 4; do
    (
        while true; do
            python3 -m pytest tests/test_budget.py -q > /dev/null 2>&1 || true
            sleep 0.1
        done
    ) &
    BG_PIDS[$shard]=$!
done

trap "for pid in \${BG_PIDS[@]}; do kill \$pid 2>/dev/null || true; done" EXIT

sleep 2  # Let background load start

for i in $(seq 1 $RUNS); do
    echo -n "Run $i/$RUNS: "
    if timeout 30 python3 -m pytest "$TEST_NAME" -xvs > /tmp/concurrent_$i.log 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL"
        FAIL=$((FAIL + 1))
        cp /tmp/concurrent_$i.log /tmp/concurrent_fail_$i.log
        echo "=== FAILURE LOG (last 50 lines) ==="
        tail -50 /tmp/concurrent_$i.log
        break
    fi
done

# Kill background load
for pid in ${BG_PIDS[@]}; do
    kill $pid 2>/dev/null || true
done

echo ""
echo "Results: $PASS passed, $FAIL failed out of $RUNS runs"
if [ $FAIL -eq 0 ]; then
    echo "SUCCESS: All runs passed under concurrent load"
    exit 0
else
    echo "FAILURE: Found flaky behavior"
    exit 1
fi
