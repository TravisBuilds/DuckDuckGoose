# Hands-Free Video Generator - Milestone 1

A Temporal-based workflow harness for turning ideas into finished short videos with human approval gates.

## Architecture

- **Temporal** as the durable workflow backbone
- **OpenAI Agents SDK** (via Temporal integration) for judgment steps
- **Python** implementation with typed error classes and retry policies
- **SQLite** for job and credit ledgers (swappable for Postgres)

## Milestone 1 Scope

This milestone delivers a **skeleton** with no real generation or network calls:

- ✅ EpisodeWorkflow covering all pipeline stages from the spec
- ✅ Child ShotWorkflow fan-out and PostingWorkflow stub
- ✅ Human gates as Temporal Updates/Signals with Queries for state
- ✅ Stub Activities with heartbeats, heartbeat timeouts, and typed errors
- ✅ submit_job/await_job pattern with idempotency keys and job ledger
- ✅ Credit ledger with balance guard (stubbed)
- ✅ Dry-run mode: configurable delays and failure injection
- ✅ CLI to start episodes, inspect state, and send approvals
- ✅ Tests: time-skipping, heartbeat failures, idempotency, credit pauses

## Setup

### Prerequisites

- Python 3.11+
- Temporal dev server

### Installation

1. Install dependencies:

```bash
pip install -e ".[dev]"
```

2. Start the Temporal dev server in a separate terminal:

```bash
temporal server start-dev
```

### Running the Worker

Start the worker that executes workflows and activities:

```bash
python -m hfvg.worker
```

### Using the CLI

Start a new episode (dry-run mode):

```bash
hfvg start-episode --episode-id "ep-test-001" --idea "A duck teaches penguins to cook"
```

Inspect episode state:

```bash
hfvg query-state ep-test-001
```

Approve gates:

```bash
# Approve read-back
hfvg approve ep-test-001 readback

# Approve character locks
hfvg approve ep-test-001 character-locks

# Approve storyboard
hfvg approve ep-test-001 storyboard

# Approve scene stills (for scene_id=1)
hfvg approve ep-test-001 stills --scene-id 1

# Approve final video
hfvg approve ep-test-001 final
```

## Dry-Run Mode

All provider calls are fakes in milestone 1:

- Configurable delays simulate generation times
- Failures can be injected (content blocks, stalled jobs, insufficient credits)
- Configure via environment or code (see `hfvg/config.py`)

Example environment variables:

```bash
export DRY_RUN=true
export DRY_RUN_STILL_DELAY=2.0
export DRY_RUN_CLIP_DELAY=5.0
export DRY_RUN_FAILURE_RATE=0.1
```

## Running Tests

### Smoke Tests (Fast, No Server Required)

```bash
pytest tests/test_smoke.py -v
```

Smoke tests verify:
- All modules import successfully
- Configuration loads correctly
- Error classes work as expected
- Ledger operations (init, idempotency keys, credit operations)
- Credit guard raises InsufficientCreditsError

### Integration Tests (Require Temporal Dev Server)

```bash
pytest -v
```

Integration tests cover:
- Time-skipping test: dry-run episode through all gates
- Heartbeat failure: stalled activity is failed and retried
- Idempotency: duplicate submits don't create duplicate jobs
- Credit guard: InsufficientCredits pauses rather than retries

**Note:** Integration tests use Temporal's time-skipping WorkflowEnvironment and may take longer to run.

## Project Structure

```
hfvg/
├── __init__.py
├── config.py              # Configuration and dry-run settings
├── models.py              # Data models (Pydantic)
├── errors.py              # Typed error classes
├── workflows/
│   ├── __init__.py
│   ├── episode.py         # EpisodeWorkflow (main)
│   ├── shot.py            # ShotWorkflow (per shot)
│   └── posting.py         # PostingWorkflow (stub)
├── activities/
│   ├── __init__.py
│   ├── generation.py      # Generation stubs (stills, clips)
│   ├── qc.py              # QC agent stubs
│   ├── media.py           # ffmpeg stubs
│   ├── audio.py           # ElevenLabs stubs
│   └── posting.py         # Upload/posting stubs
├── ledger.py              # Job and credit ledgers (SQLite)
├── worker.py              # Temporal worker
└── cli.py                 # Command-line interface

tests/
├── test_episode.py        # Episode workflow tests
├── test_idempotency.py    # Idempotency tests
├── test_heartbeat.py      # Heartbeat/stall tests
└── test_credits.py        # Credit guard tests
```

## Next Steps (BUILD-PLAN.md)

See [BUILD-PLAN.md](BUILD-PLAN.md) for:
- What milestone 1 delivers
- Next implementation slices
- Open risks and questions
