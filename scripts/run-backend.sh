#!/bin/bash
#
# DuckDuckGoose Studio Backend Launcher
# 
# Starts:
# 1. Temporal dev server
# 2. Python workflow worker
# 3. FastAPI Studio API
#
# Configuration via environment variables:
#   STUDIO_ADMIN_SECRET - Admin authentication secret
#   STUDIO_DB_PATH - SQLite database path (default: data/studio.db)
#   EPISODE_DATA_PATH - Episode data storage (default: data/episodes)
#   ALLOWED_ORIGIN - CORS allowed origin (default: https://duckduckgoose-topaz.vercel.app)
#   MODEL_PATH_STILL - Higgsfield still model path
#   OPENAI_API_KEY - OpenAI API key for vision judge
#   DEV_MODE - Enable development mode (adds localhost to CORS)
#
# Usage:
#   ./scripts/run-backend.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== DuckDuckGoose Studio Backend ===${NC}"
echo

# Check environment
if [ -z "$STUDIO_ADMIN_SECRET" ]; then
    echo -e "${RED}ERROR: STUDIO_ADMIN_SECRET not set${NC}"
    echo "Set it in your environment or .env file"
    exit 1
fi

if [ -z "$MODEL_PATH_STILL" ]; then
    echo -e "${YELLOW}WARNING: MODEL_PATH_STILL not set${NC}"
    echo "Still generation will fail without a configured Higgsfield model"
fi

if [ -z "$OPENAI_API_KEY" ]; then
    echo -e "${YELLOW}WARNING: OPENAI_API_KEY not set${NC}"
    echo "Duck identity gate will ESCALATE for manual review"
fi

# Create data directories
mkdir -p data/episodes
mkdir -p data/temporal

# Check dependencies
if ! command -v temporal &> /dev/null; then
    echo -e "${RED}ERROR: temporal CLI not found${NC}"
    echo "Install from: https://docs.temporal.io/cli"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}ERROR: python3 not found${NC}"
    exit 1
fi

# Check Python packages
if ! python3 -c "import temporalio" 2>/dev/null; then
    echo -e "${RED}ERROR: temporalio package not installed${NC}"
    echo "Run: pip install temporalio[opentelemetry]"
    exit 1
fi

if ! python3 -c "import fastapi" 2>/dev/null; then
    echo -e "${RED}ERROR: fastapi package not installed${NC}"
    echo "Run: pip install fastapi uvicorn aiosqlite"
    exit 1
fi

echo -e "${GREEN}✓ Environment checks passed${NC}"
echo

# Cleanup function
cleanup() {
    echo
    echo -e "${YELLOW}Shutting down...${NC}"
    
    if [ ! -z "$TEMPORAL_PID" ]; then
        kill $TEMPORAL_PID 2>/dev/null || true
    fi
    
    if [ ! -z "$WORKER_PID" ]; then
        kill $WORKER_PID 2>/dev/null || true
    fi
    
    if [ ! -z "$API_PID" ]; then
        kill $API_PID 2>/dev/null || true
    fi
    
    exit 0
}

trap cleanup SIGINT SIGTERM

# Start Temporal dev server
echo -e "${GREEN}Starting Temporal dev server...${NC}"
temporal server start-dev \
    --db-filename data/temporal/default.db \
    --log-level error \
    > logs/temporal.log 2>&1 &
TEMPORAL_PID=$!

sleep 3

if ! kill -0 $TEMPORAL_PID 2>/dev/null; then
    echo -e "${RED}ERROR: Temporal server failed to start${NC}"
    echo "Check logs/temporal.log for details"
    exit 1
fi

echo -e "${GREEN}✓ Temporal dev server started (PID $TEMPORAL_PID)${NC}"
echo "  UI: http://localhost:8233"

# Start Python worker
echo -e "${GREEN}Starting workflow worker...${NC}"
python3 -m hfvg.worker > logs/worker.log 2>&1 &
WORKER_PID=$!

sleep 2

if ! kill -0 $WORKER_PID 2>/dev/null; then
    echo -e "${RED}ERROR: Worker failed to start${NC}"
    echo "Check logs/worker.log for details"
    cleanup
fi

echo -e "${GREEN}✓ Worker started (PID $WORKER_PID)${NC}"

# Start FastAPI
echo -e "${GREEN}Starting Studio API...${NC}"
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000 > logs/api.log 2>&1 &
API_PID=$!

sleep 2

if ! kill -0 $API_PID 2>/dev/null; then
    echo -e "${RED}ERROR: API failed to start${NC}"
    echo "Check logs/api.log for details"
    cleanup
fi

echo -e "${GREEN}✓ Studio API started (PID $API_PID)${NC}"
echo "  API: http://localhost:8000"
echo "  Docs: http://localhost:8000/docs"

echo
echo -e "${GREEN}=== All services running ===${NC}"
echo
echo "Press Ctrl+C to stop all services"
echo

# Wait for any process to exit
wait -n

cleanup
