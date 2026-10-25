# Local Development

Complete guide to setting up, running, and testing the Hands-Free Video Generator locally.

## Prerequisites

### Required

- **Python 3.11+** ([download](https://www.python.org/downloads/))
- **Temporal CLI** ([install](https://docs.temporal.io/cli#install))
- **Node.js 20+** ([download](https://nodejs.org/)) - Next.js 16; CI uses Node 20
- **pip** (included with Python)

### Optional

- **ffmpeg** - Required for motion QC tests (set `REQUIRE_FFMPEG=1`)
  ```bash
  # macOS
  brew install ffmpeg
  
  # Ubuntu/Debian
  sudo apt-get install ffmpeg
  
  # Windows
  # Download from https://ffmpeg.org/download.html
  ```

## Initial Setup

### 1. Clone the Repository

```bash
git clone https://github.com/<owner>/DuckDuckGoose.git
cd DuckDuckGoose
```

### 2. Install Python Dependencies

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify installation
python -c "import hfvg; print(hfvg.__name__)"
```

**Dependencies installed:**
- `temporalio>=1.8.0` - Workflow orchestration
- `aiohttp>=3.9.0` - Async HTTP client
- `aiosqlite>=0.20.0` - Async SQLite
- `click>=8.1.0` - CLI framework
- `pydantic>=2.0.0` - Data validation
- `fastapi>=0.100.0` - API server (dev extra)
- `pytest>=8.0.0` - Testing framework
- `respx>=0.21.0` - HTTP mocking

### 3. Install Web UI Dependencies

```bash
cd web
npm install
cd ..
```

### 4. Set Up Environment

```bash
# Copy example env file
cp .env.example .env

# Generate a secure admin secret
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# Edit .env with your secrets
nano .env  # or your editor of choice

# Nothing loads .env automatically: export it in each terminal
set -a; source .env; set +a
```

**Minimum required:**
```bash
ADMIN_SECRET=<your-generated-secret>  # Min 32 chars, REQUIRED
```

**For live mode:**
```bash
HIGGSFIELD_API_KEY=id:secret  # Format: id:secret
ELEVENLABS_API_KEY=...
G1_ESTIMATE_PLACEHOLDER_IMAGE_URL=https://example.com/placeholder-1080x1920.jpg
# OPENAI_API_KEY is not read by the current code
```

### 5. Create Data Directories

```bash
mkdir -p data/episodes data/normalized
```

## Running the Services

### Option A: All Services (Recommended)

Use separate terminals for each service:

**Terminal 1: Temporal Dev Server**
```bash
temporal server start-dev
```

**Terminal 2: Worker**
```bash
python -m hfvg.worker
```

**Terminal 3: API Server**
```bash
cd api
uvicorn main:app --reload --port 8000
```

**Terminal 4: Web UI (Optional)**
```bash
cd web
npm run dev
```

### Option B: Background Services

```bash
# Start Temporal in background
temporal server start-dev &

# Wait for Temporal to be ready
sleep 5

# Start worker in background
python -m hfvg.worker &

# Start API
cd api && uvicorn main:app --reload
```

### Verify Services

```bash
# Check Temporal
temporal workflow list

# Check API
curl http://localhost:8000/api/health

# Check Web UI
curl http://localhost:3000
```

## Running Tests

### Quick Test (Smoke)

```bash
# Fast tests, no Temporal required
pytest tests/test_smoke_test.py -v
```

Expected output (smoke-test activity with Higgsfield mocked by respx):
```
tests/test_smoke_test.py::test_smoke_test_end_to_end PASSED
tests/test_smoke_test.py::test_smoke_test_nsfw_terminal PASSED
tests/test_smoke_test.py::test_smoke_test_canceled_terminal PASSED
tests/test_smoke_test.py::test_smoke_test_presubmit_exception_released PASSED
tests/test_smoke_test.py::test_smoke_test_postsubmit_timeout_pending_reconcile PASSED
```

### Unit Tests

```bash
# All tests
pytest

# Specific test file
pytest tests/test_episode_v2.py -v

# Specific test
pytest tests/test_episode_v2.py::test_episode_v2_approval_gates -v

# With output
pytest tests/test_caps_preflight.py -v -s
```

### Integration Tests

Integration tests do NOT need a running Temporal server: they use
`temporalio.testing.WorkflowEnvironment.start_time_skipping()` (the SDK downloads its
test server on first use).

```bash
pytest tests/test_temporal_integration.py -v
```

**Key integration tests:**
- `test_episode_v2.py` - Full pipeline with gates
- `test_shot_live.py` - Shot workflow with QC
- `test_retry_shot.py` - Shot retry system
- `test_g2_api_route_update.py` - G2 approval with validator

### Mutation Tests (Red-Green)

**Mutation testing verifies that safety protections cannot be removed without tests failing.**

```bash
# All mutations (requires ffmpeg)
REQUIRE_FFMPEG=1 ./scripts/redgreen.sh

# Without REQUIRE_FFMPEG=1 the motion QC tests are skipped
./scripts/redgreen.sh

# One shard, the way CI runs it (4 shards; each writes one result row per id)
REQUIRE_FFMPEG=1 REDGREEN_SHARD=2 REDGREEN_TOTAL_SHARDS=4 \
  REDGREEN_RESULTS_FILE=/tmp/rg-2.tsv ./scripts/redgreen.sh

# The CI summary check: the union of all shard files must run every declared id once
python scripts/redgreen_coverage.py --markdown /tmp/redgreen-status.md /tmp/rg-*.tsv
```

Mutations run in a git worktree of the HEAD **commit**, so commit before running them. The
red-green status is not committed: CI publishes it as the `redgreen-status` artifact and in the
`redgreen-summary` job summary.

**Expected output:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RED-GREEN SAFETY TEST SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Test                                              Result     Reason
──────────────────────────────────────────────── ────────── ──────────────────────────────
1. Live mode required for generation              ✓ PASS     Red → Green confirmed
2. G1.08 credit plan approval required            ✓ PASS     Red → Green confirmed
...
218. ICC conversion covers RGBA refs              ✓ PASS     Red → Green confirmed

Total tests: 219
Passed: 216
Skipped: 3
Invalid: 0
Failed: 0

✓✓✓ ALL RED-GREEN PROOFS PASSED ✓✓✓
```

`Total tests` is 219 = 215 numbered mutations (M-1..M-218 without M-21, M-22 and the
commented-out M-137) + 4 harness probes (PC, P1, P2, P3). M-23, M-24 and M-93 are
skipped via `scripts/redgreen_skip.conf`.

### Test Coverage

```bash
# Run with coverage (pytest-cov is not in the dev extra)
pip install pytest-cov
pytest --cov=hfvg --cov-report=html

# View HTML report
open htmlcov/index.html
```

### Continuous Testing (Watch Mode)

```bash
# Install pytest-watch
pip install pytest-watch

# Watch for changes
ptw -- tests/test_episode_v2.py -v
```

## Development Workflow

### 1. Create a Feature Branch

```bash
git checkout -b feature/my-feature
```

### 2. Make Changes

Edit code in `hfvg/`, `api/`, or `web/`.

### 3. Run Tests

```bash
# Quick smoke test
pytest tests/test_smoke_test.py

# Full test suite
pytest

# Mutation tests (before committing)
./scripts/redgreen.sh
```

### 4. Run Linting (Optional)

```bash
# Install ruff
pip install ruff

# Check
ruff check hfvg/ api/

# Format
ruff format hfvg/ api/
```

### 5. Commit and Push

```bash
git add .
git commit -m "feat: add my feature"
git push origin feature/my-feature
```

### 6. Create Pull Request

See [CONTRIBUTING.md](../CONTRIBUTING.md) for PR guidelines.

## CLI Usage

### Episode Management

```bash
# start-v2 posts to the API (HFVG_API_BASE, default http://localhost:8000) and needs
# ADMIN_SECRET exported. Episode IDs must match ^ep\d{2}$.

# Start episode (dry run, the default)
hfvg start-v2 ep01 --beatmap tests/fixtures/sample_beatmap.md --dry-run

# Start episode (live mode; also needs DRY_RUN=false and POST /api/episodes/ep01/set-live)
hfvg start-v2 ep01 --beatmap tests/fixtures/sample_beatmap.md --live

# Query state
hfvg query-state ep01

# Lint episode
hfvg lint ep01
```

### Gate Approvals

EpisodeWorkflowV2 gates are approved through the API (there is no CLI command for them;
`hfvg approve` only drives the legacy EpisodeWorkflow gates
`readback|character-locks|storyboard|stills|final`):

```bash
# Approve G1_SHOT_PLAN (workflow update; refused while preflight errors exist)
curl -X POST http://localhost:8000/api/episodes/ep01/approve \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" -H "Content-Type: application/json" \
  -d '{"gate_id": "g1_shot_plan"}'

# Approve G2_SCENE_STILLS (workflow update; refused while a retry is pending)
#   -d '{"gate_id": "g2_scene_stills"}'

# Approve G_FINAL_CUT (signal)
#   -d '{"gate_id": "g_final_cut"}'
```

### Shot Retry

```bash
# Retry failed shot (only while parked at G1/G2)
hfvg retry-shot ep01 A02
```

End-frame backfill has no CLI command or API route yet; it is the `retry_end_still`
update on EpisodeWorkflowV2 (`handle.execute_update("retry_end_still", args=["A02", "Duck surprised"])`).

### Preflight Validation

```bash
# Validate episode against model limits (default: $EPISODES_DIR/<id>/shots.json)
hfvg preflight ep01

# Validate custom shots.json
hfvg preflight ep01 --shots custom-shots.json
```

## API Development

### Start API Server

```bash
cd api
uvicorn main:app --reload --port 8000 --log-level debug
```

### API Documentation

Open [http://localhost:8000/docs](http://localhost:8000/docs) for interactive Swagger docs.

### Example API Calls

```bash
# Login (set admin cookie)
curl -X POST http://localhost:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"admin_secret": "YOUR_ADMIN_SECRET"}'

# Start episode (episode_id must match ^ep\d{2}$; beatmap text goes in beatmap_content)
curl -X POST http://localhost:8000/api/episodes \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile b tests/fixtures/sample_beatmap.md \
        '{episode_id: "ep01", beatmap_content: $b, dry_run: true}')"

# Query state
curl http://localhost:8000/api/episodes/ep01 \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET"

# Approve G1
curl -X POST http://localhost:8000/api/episodes/ep01/approve \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"gate_id": "g1_shot_plan"}'

# Retry shot
curl -X POST http://localhost:8000/api/episodes/ep01/shots/A02/retry \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET"
```

## Web UI Development

### Start Dev Server

```bash
cd web
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

### Build for Production

```bash
cd web
npm run build
npm start  # Serve production build
```

### Linting

```bash
cd web
npm run lint
```

## Debugging

### Temporal Web UI

Open [http://localhost:8233](http://localhost:8233) to view:
- Workflow executions
- Activity logs
- Workflow state
- Timeline visualization

### Python Debugger

```python
# Add breakpoint in code
import pdb; pdb.set_trace()

# Or use built-in breakpoint()
breakpoint()
```

### Workflow Debugging

```python
# Add logging in workflows
workflow.logger.info(f"Current state: {self.state}")

# Query workflow state
hfvg query-state ep01
```

### Activity Debugging

```python
# Activity logs appear in worker output
activity.logger.info(f"Submitting still: {shot_id}")
activity.logger.error(f"Provider error: {error}")
```

### Database Inspection

```bash
# Open SQLite database
sqlite3 data/studio.db

# Query episodes
SELECT * FROM episodes;

# Query budget
SELECT * FROM budget_lines;

# Query jobs
SELECT * FROM job_ledger WHERE status='pending_reconcile';
```

## Common Issues

### Issue: `ADMIN_SECRET not set`

**Error:**
```
ValueError: ADMIN_SECRET environment variable is required
```

**Fix:**
```bash
# Generate secret
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# Add to .env
echo "ADMIN_SECRET=<generated-secret>" >> .env
```

### Issue: `temporal server` not found

**Error:**
```
bash: temporal: command not found
```

**Fix:**
```bash
# Install Temporal CLI
curl -sSf https://temporal.download/cli.sh | sh

# Add to PATH
export PATH="$HOME/.temporalio/bin:$PATH"
```

### Issue: Worker fails to connect

**Error:**
```
Failed to connect to Temporal server at localhost:7233
```

**Fix:**
```bash
# Ensure Temporal dev server is running
temporal server start-dev

# Verify connection
temporal workflow list
```

### Issue: Tests fail with socket errors

**Error:**
```
pytest_socket.SocketBlockedError: A test tried to use socket.socket
```

**Fix:**
```bash
# Tests are configured to block sockets by default
# This is expected - provider calls must be mocked with respx

# If you need to allow network (NOT recommended):
pytest tests/test_example.py --force-enable-socket
```

### Issue: `ffmpeg not found` for motion tests

**Error:**
```
FileNotFoundError: ffmpeg not found
```

**Fix:**
```bash
# Install ffmpeg
brew install ffmpeg  # macOS
sudo apt-get install ffmpeg  # Ubuntu

# Or skip motion tests
pytest -k "not motion"
```

### Issue: SQLite database locked

**Error:**
```
sqlite3.OperationalError: database is locked
```

**Fix:**
```bash
# Close all connections
pkill -f "python -m hfvg.worker"

# Or use a new database
rm data/studio.db
```

## Performance

### Fast Test Subset

```bash
# Smoke tests only (~5 seconds)
pytest tests/test_smoke_test.py

# Unit tests only (~30 seconds)
pytest tests/ -k "not temporal and not integration"

# Skip mutation tests
pytest tests/ --ignore=tests/test_redgreen_safety.py
```

### Parallel Testing

```bash
# Install pytest-xdist
pip install pytest-xdist

# Run tests in parallel
pytest -n auto
```

### Skip Slow Tests

```bash
# Mark slow tests
@pytest.mark.slow
def test_long_running():
    ...

# Skip them
pytest -m "not slow"
```

## Environment Variables

### Core

- `ADMIN_SECRET` - API admin secret (REQUIRED)
- `TEMPORAL_ADDRESS` - Temporal server address used by the API (default: localhost:7233)
- `TEMPORAL_HOST` - Temporal server address used by the worker and CLI (default: localhost:7233)
- `TASK_QUEUE` - Worker task queue (default: hfvg-tasks)
- `TEMPORAL_NAMESPACE` - Temporal namespace (default: default)
- `DATABASE_PATH` - SQLite database path (default: ./data/studio.db)
- `DRY_RUN` - Dry run mode (default: true)

### Provider API Keys

- `HIGGSFIELD_API_KEY` - Higgsfield API key (format: `id:secret`)
- `ELEVENLABS_API_KEY` - ElevenLabs API key (read by the ElevenLabs SDK)
- `OPENAI_API_KEY` - not read by the current code (no OpenAI judge wired)

### Model Configuration

- `MODEL_PATH_STILL` - Still image model (default: xai/grok-imagine-image-2.0)
- `MODEL_PATH_SOUL_V2` - Soul V2 still model (default: higgsfield-ai/soul/v2/standard)
- `MODEL_PATH_KLING` - Kling video model (default: kling-video/v3.0/pro/image-to-video)
- `MODEL_PATH_SEEDANCE` - Seedance video model (default: bytedance/seedance-2.5/image-to-video)
- `G1_ESTIMATE_PLACEHOLDER_IMAGE_URL` - Placeholder for clip estimates (REQUIRED for live G1)

### Episode Data

- `EPISODES_DIR` - Episode data directory (default: ./data/episodes)
- `NORMALIZED_CACHE_DIR` - Normalized reference cache (default: ./data/normalized)

### Model Capabilities

- `MODEL_CAPS_PATH` - Model capabilities JSON path (default: hfvg/data/model_caps.json)
- `MODEL_MAX_REFS_JSON` - Per-model ref cap overrides (JSON: `{"model": cap}`; lower only unless `MODEL_CAPS_ALLOW_RAISE=1`)

### Budget Configuration

- `EPISODE_CAP_USD` - Episode cap (default: "60.00")
- `EPISODE_STOP_PERCENT` - Episode/line stop percent (default: 80 -> $48.00 stop)
- `GC01_HEADROOM_USD` - GC.01 headroom (default: "5.00")
- `REVISION_RESERVE_USD` - Revision reserve line `L7_revision`, outside the cap (default: "10.00")
- `MAX_SHOT_RETRIES` - Retries per shot (default: 1)

See [.env.example](../.env.example) for the complete list.

### Testing

- `REQUIRE_FFMPEG` - Require ffmpeg for motion tests (default: unset, optional)

## Useful Commands

### Reset Everything

```bash
# Kill all services
pkill -f temporal
pkill -f "python -m hfvg.worker"
pkill -f uvicorn

# Clean database
rm -rf data/

# Recreate directories
mkdir -p data/episodes data/normalized

# Restart
temporal server start-dev &
python -m hfvg.worker &
cd api && uvicorn main:app --reload
```

### View Logs

```bash
# Worker logs
python -m hfvg.worker 2>&1 | tee worker.log

# API logs
cd api && uvicorn main:app --log-level debug 2>&1 | tee api.log

# Temporal UI logs
temporal server start-dev --log-level debug
```

### Monitor Resources

```bash
# Watch worker CPU/memory
watch -n 1 'ps aux | grep "python -m hfvg.worker"'

# Watch database size
watch -n 5 'ls -lh data/studio.db'

```

## Next Steps

- Read [ARCHITECTURE.md](ARCHITECTURE.md) for system design
- Read [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidelines
- See [ROADMAP.md](ROADMAP.md) for planned features
- Join discussions in GitHub Issues
