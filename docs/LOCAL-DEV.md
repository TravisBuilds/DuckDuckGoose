# Local Development

Complete guide to setting up, running, and testing the Hands-Free Video Generator locally.

## Prerequisites

### Required

- **Python 3.11+** ([download](https://www.python.org/downloads/))
- **Temporal CLI** ([install](https://docs.temporal.io/cli#install))
- **Node.js 18+** ([download](https://nodejs.org/))
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
git clone <repository-url>
cd hands-free-video-generator
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
- `fastapi>=0.100.0` - API server
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
```

**Minimum required:**
```bash
ADMIN_SECRET=<your-generated-secret>  # Min 32 chars, REQUIRED
```

**For live mode:**
```bash
HIGGSFIELD_API_KEY=id:secret  # Format: id:secret
OPENAI_API_KEY=sk-...
ELEVENLABS_API_KEY=...
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
curl http://localhost:8000/health

# Check Web UI
curl http://localhost:3000
```

## Running Tests

### Quick Test (Smoke)

```bash
# Fast tests, no Temporal required
pytest tests/test_smoke_test.py -v
```

Expected output:
```
tests/test_smoke_test.py::test_all_modules_import PASSED
tests/test_smoke_test.py::test_config_loads PASSED
tests/test_smoke_test.py::test_error_classes_work PASSED
```

### Unit Tests

```bash
# All tests
pytest

# Specific test file
pytest tests/test_episode_v2.py -v

# Specific test
pytest tests/test_episode_v2.py::test_g1_approval -v

# With output
pytest tests/test_caps_preflight.py -v -s
```

### Integration Tests

Integration tests require Temporal dev server running:

```bash
# Terminal 1: Start Temporal
temporal server start-dev

# Terminal 2: Run integration tests
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

# Individual mutation
./scripts/redgreen.sh  # Will run without ffmpeg, skipping motion tests
```

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

Total tests: 218
Passed: 216
Skipped: 2
Invalid: 0
Failed: 0

✓✓✓ ALL RED-GREEN PROOFS PASSED ✓✓✓
```

### Test Coverage

```bash
# Run with coverage
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
# Start episode (dry run)
hfvg start-v2 ep-test-001 --beatmap fixtures/ep05/BEATMAP.md --dry-run

# Start episode (live mode)
hfvg start-v2 ep-test-001 --beatmap fixtures/ep05/BEATMAP.md --no-dry-run

# Query state
hfvg query-state ep-test-001

# Lint episode
hfvg lint ep-test-001
```

### Gate Approvals

```bash
# Approve G1 (shot plan)
hfvg approve ep-test-001 g1-shot-plan

# Approve G2 (scene stills)
hfvg approve ep-test-001 g2-scene-stills

# Approve G_FINAL (final cut)
hfvg approve ep-test-001 g-final-cut
```

### Shot Retry

```bash
# Retry failed shot
hfvg retry-shot ep-test-001 A02

# Backfill end-frame still
hfvg retry-end-still ep-test-001 A02 --end-prompt "Duck surprised"
```

### Preflight Validation

```bash
# Validate episode against model limits
hfvg preflight ep-test-001

# Validate custom shots.json
hfvg preflight ep-test-001 --shots-path custom-shots.json
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

# Start episode
curl -X POST http://localhost:8000/api/episodes \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "episode_id": "ep-test-001",
    "beatmap_path": "fixtures/ep05/BEATMAP.md",
    "dry_run": true
  }'

# Query state
curl http://localhost:8000/api/episodes/ep-test-001 \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET"

# Approve G1
curl -X POST http://localhost:8000/api/episodes/ep-test-001/approve \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"signal_name": "approve_g1_shot_plan"}'

# Retry shot
curl -X POST http://localhost:8000/api/episodes/ep-test-001/retry-shot/A02 \
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
hfvg query-state ep-test-001
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
SELECT * FROM episode_budget_lines;

# Query jobs
SELECT * FROM budget_jobs WHERE status='pending_reconcile';
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
pytest tests/test_example.py --disable-socket=false
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
- `TEMPORAL_ADDRESS` - Temporal server address (default: localhost:7233)
- `TEMPORAL_NAMESPACE` - Temporal namespace (default: default)
- `DATABASE_PATH` - SQLite database path (default: ./data/studio.db)
- `DRY_RUN` - Dry run mode (default: true)

### Provider API Keys

- `HIGGSFIELD_API_KEY` - Higgsfield API key (format: `id:secret`)
- `OPENAI_API_KEY` - OpenAI API key
- `ELEVENLABS_API_KEY` - ElevenLabs API key

### Model Configuration

- `MODEL_PATH_STILL` - Still image model (default: xai/grok-imagine-image-2.0)
- `MODEL_PATH_STILL_FALLBACK` - Fallback still model
- `MODEL_PATH_KLING` - Kling video model
- `MODEL_PATH_SEEDANCE` - Seedance video model
- `G1_ESTIMATE_PLACEHOLDER_IMAGE_URL` - Placeholder for clip estimates (REQUIRED for live G1)

### Episode Data

- `EPISODES_DIR` - Episode data directory (default: ./data/episodes)
- `NORMALIZED_CACHE_DIR` - Normalized reference cache (default: ./data/normalized)

### Model Capabilities

- `MODEL_CAPS_PATH` - Model capabilities JSON path (default: hfvg/data/model_caps.json)
- `MODEL_MAX_REFS_JSON` - Per-model ref cap overrides (JSON: `{"model": cap}`)

### Budget Configuration

- `DEFAULT_EPISODE_CAP_USD` - Default episode cap (default: "60.00")
- `GC01_HEADROOM_USD` - GC.01 headroom (default: "12.00")

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

# Watch Temporal metrics
curl http://localhost:8233/metrics
```

## Next Steps

- Read [ARCHITECTURE.md](ARCHITECTURE.md) for system design
- Read [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidelines
- See [ROADMAP.md](ROADMAP.md) for planned features
- Join discussions in GitHub Issues
