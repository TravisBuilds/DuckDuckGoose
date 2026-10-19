# Hands-Free Video Generator

A production-ready Temporal Python workflow harness for turning story ideas into finished short-form videos with automated generation, human approval gates, and budget enforcement.

## What Is This?

The Hands-Free Video Generator is a **workflow orchestration harness** that coordinates AI video generation providers (Higgsfield/Kling/Seedance for video, ElevenLabs for audio) through a durable Temporal workflow with:

- **Automated pipeline**: Parse beatmap → generate stills → human review (G2) → generate clips → edit → audio → delivery
- **Human gates**: Three approval points (G1: shot plan, G2: scene stills, G_FINAL: final cut)
- **Budget enforcement**: Hard USD caps, atomic SQL reservations, idempotent retries
- **Model capabilities registry**: Preflight validation against documented provider limits before spend
- **Quality gates**: Automated QC with human escalation in live mode
- **Shot retry system**: Single-shot re-generation with version tracking
- **Web UI**: Next.js dashboard for episode management and approvals

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│   Web UI    │────▶│  FastAPI     │────▶│    Temporal    │
│  (Next.js)  │     │  (api/)      │     │   Workflows    │
└─────────────┘     └──────────────┘     └────────────────┘
                           │                       │
                           ▼                       ▼
                    ┌──────────────┐     ┌────────────────┐
                    │   SQLite     │     │   Activities   │
                    │  (studio.db) │     │  (providers)   │
                    └──────────────┘     └────────────────┘
                                                  │
                        ┌─────────────────────────┼────────────────────┐
                        ▼                         ▼                    ▼
                 ┌──────────────┐        ┌──────────────┐    ┌────────────┐
                 │  Higgsfield  │        │  ElevenLabs  │    │   OpenAI   │
                 │ (Still/Video)│        │   (Audio)    │    │   (Judge)  │
                 └──────────────┘        └──────────────┘    └────────────┘
```

**Key Components:**

- **EpisodeWorkflowV2**: Main orchestrator (G1 → G2 → clips → audio → final)
- **ShotWorkflow**: Per-shot child workflow (still → review → clip → QC)
- **Gates**: G1 (shot plan approval), G2 (scene stills approval), G_FINAL (final cut)
- **Budget Ledger**: Atomic reserve/commit/release with 80% stop threshold
- **Model Caps Registry**: Per-model limits (refs, prompt, duration, aspect, resolution)
- **Preflight**: Validate all shots before spend (G1 plan, per-submit defense)

## Quickstart

### Prerequisites

- **Python 3.11+**
- **Temporal dev server** ([download](https://docs.temporal.io/cli#install))
- **Node.js 18+** (for web UI)
- **ffmpeg** (for motion QC)

### 1. Install Dependencies

```bash
# Python dependencies
pip install -e ".[dev]"

# Web UI dependencies
cd web && npm install && cd ..
```

### 2. Set Up Environment

```bash
cp .env.example .env
# Edit .env with your ADMIN_SECRET and provider API keys
```

**Required:**
- `ADMIN_SECRET`: Min 32 chars (generate: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)

**Optional (for live mode):**
- `HIGGSFIELD_API_KEY`: Format `id:secret`
- `OPENAI_API_KEY`: For vision QC
- `ELEVENLABS_API_KEY`: For audio generation

### 3. Start Temporal Dev Server

```bash
temporal server start-dev
```

### 4. Start the Worker

```bash
python -m hfvg.worker
```

### 5. Start the API Server

```bash
cd api && uvicorn main:app --reload
```

### 6. Start the Web UI (Optional)

```bash
cd web && npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

### 7. Run Your First Episode (Dry Run)

```bash
# CLI approach
hfvg start-v2 ep-test-001 --dry-run

# Or via API
curl -X POST http://localhost:8000/api/episodes \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "episode_id": "ep-test-001",
    "beatmap_path": "fixtures/ep05/BEATMAP.md",
    "dry_run": true
  }'
```

## Pipeline Overview

### Full Pipeline (7 Steps)

```
Step 0: BIBLE
  ├─ Asset registry (refs.json)
  └─ Playbook (prompt_kit.json)

Step 1: STORY
  ├─ Parse BEATMAP.md → shot list
  ├─ Compute G1 plan (credit estimates)
  └─ 🚦 GATE G1: Human approves shot plan + credit plan

Step 2: STILLS
  ├─ Generate reference images (character sheets, lineup)
  ├─ Generate start stills for all shots (parallel)
  ├─ Generate end stills for end-frame shots
  ├─ Automated QC (live mode escalates to human)
  └─ 🚦 GATE G2: Human approves all scene stills

Step 3: VIDEO
  ├─ Generate clips from stills (parallel, after G2)
  └─ Automated motion QC

Step 4: MUTE
  ├─ Trim clips to exact durations
  ├─ Motion QC
  └─ Picture lock

Step 5: SCRIPT
  └─ VO script lock (PR #8)

Step 6: AUDIO
  ├─ Generate voiceover (ElevenLabs)
  ├─ Generate music
  ├─ Generate SFX
  └─ Mix audio (PR #8)

Step 7: DELIVERY
  ├─ 🚦 GATE G_FINAL: Human approves final cut
  ├─ Encode final video
  └─ Upload to Drive (HOLD by default)
```

### Dry Run vs Live Mode

**Dry run** (default):
- No provider API calls
- Fake delays simulate generation
- No spend, no credits required
- Instant stills/clips with fake URLs

**Live mode**:
- Real provider API calls
- Real spend (USD budget ledger)
- QC escalates to human review
- Preflight validates against model limits

Switch to live:
```bash
hfvg start-v2 ep-test-001 --no-dry-run
# or set DRY_RUN=false in .env
```

## Documentation

- **[LOCAL-DEV.md](docs/LOCAL-DEV.md)**: Development setup, running tests, debugging
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)**: Detailed architecture with mermaid diagrams
- **[ROADMAP.md](docs/ROADMAP.md)**: Planned features and good-first-issues
- **[CONTRIBUTING.md](CONTRIBUTING.md)**: Contribution guidelines and testing rules
- **[CONTENT-POLICY.md](docs/CONTENT-POLICY.md)**: Content moderation and safety policy
- **[MODELS.md](docs/MODELS.md)**: Available models and pricing
- **[PROVIDERS.md](docs/PROVIDERS.md)**: Provider integration guide
- **[MODEL-CAPS.md](docs/MODEL-CAPS.md)**: Model capabilities and limits

## Key Features

### Budget System

- **Hard USD caps**: Episode default $60.00, configurable per-run
- **Atomic reserves**: SQL `WHERE` clause ensures no overspend
- **80% stop threshold**: Blocks new reserves at 80% of cap
- **Idempotent retries**: Same job_id → same provider request
- **Reconciliation**: Poll exhaustion keeps reservation, marks `pending_reconcile`

### Model Capabilities

- **Registry**: `hfvg/data/model_caps.json` (per-model limits from provider docs)
- **Preflight**: Validate all shots before spend (G1 plan + submit defense)
- **Checks**: refs, prompt length, duration, aspect ratio, resolution, frame formats
- **Error messages**: Creator-facing with source URLs and dates

### Shot Retry

```bash
# Retry a failed shot at G1 or G2
hfvg retry-shot ep-test-001 A02

# CLI returns: queued, refused (with reason)
# Max 3 retries per shot (configurable)
```

### End-Frame Generation

Shots with `end_frame: true` generate:
1. **Start still** → QC → recorded
2. **End still** (uses start still as first ref) → recorded
3. Both land before G2 (human reviews both)
4. Clip generation uses `last_image_url` (Kling)

Backfill for runs parked without an end still:
```bash
hfvg retry-end-still ep-test-001 A02 --end-prompt "Duck looking surprised"
```

## Testing

```bash
# All tests (unit + integration)
pytest

# Smoke tests only (no Temporal required)
pytest tests/test_smoke_test.py

# Red-green mutation tests (requires REQUIRE_FFMPEG=1)
REQUIRE_FFMPEG=1 ./scripts/redgreen.sh

# Specific test
pytest tests/test_episode_v2.py::test_g1_approval -v
```

See [docs/LOCAL-DEV.md](docs/LOCAL-DEV.md) for detailed testing instructions.

## Environment Variables

See [`.env.example`](.env.example) for all available environment variables.

**Core:**
- `ADMIN_SECRET`: API admin secret (REQUIRED, min 32 chars)
- `TEMPORAL_ADDRESS`: Temporal server (default: localhost:7233)
- `DATABASE_PATH`: SQLite path (default: ./data/studio.db)
- `DRY_RUN`: Dry run mode (default: true)

**Provider API Keys:**
- `HIGGSFIELD_API_KEY`: Format `id:secret`
- `OPENAI_API_KEY`: For vision QC
- `ELEVENLABS_API_KEY`: For audio generation

**Model Paths:**
- `MODEL_PATH_STILL`: Still model (default: xai/grok-imagine-image-2.0)
- `MODEL_PATH_KLING`: Kling video model
- `MODEL_PATH_SEEDANCE`: Seedance video model

**Episode Data:**
- `EPISODES_DIR`: Episode data directory (default: ./data/episodes)

## CLI Reference

```bash
# Start episode (EpisodeWorkflowV2)
hfvg start-v2 <episode_id> [--beatmap PATH] [--no-dry-run] [--shots A01,A02]

# Query state
hfvg query-state <episode_id>

# Approve gates
hfvg approve <episode_id> g1-shot-plan
hfvg approve <episode_id> g2-scene-stills

# Retry failed shot
hfvg retry-shot <episode_id> <shot_id>

# Preflight validation
hfvg preflight <episode_id> [--shots-path PATH]

# Lint episode
hfvg lint <episode_id>
```

## API Reference

See [api/README.md](api/README.md) for full API documentation.

**Key endpoints:**
- `POST /api/login`: Set admin cookie
- `POST /api/episodes`: Start episode
- `GET /api/episodes/{id}`: Query state
- `POST /api/episodes/{id}/approve`: Send approval
- `POST /api/episodes/{id}/retry-shot/{shot_id}`: Retry shot
- `POST /api/episodes/{id}/preflight`: Validate shot plans

## License

[LICENSE](LICENSE) - License TBD by owner

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Quick rules:**
- Never delete, skip, or weaken tests/mutations
- One coding agent at a time (account-level run lock)
- Never commit secrets or private assets
- Keep CI green (all 3 jobs must pass)

## Support

- **Issues**: [GitHub Issues](../../issues)
- **Discussions**: [GitHub Discussions](../../discussions)
- **Documentation**: [docs/](docs/)

---

**Built with:** Temporal • Python 3.11 • FastAPI • Next.js • SQLite • Higgsfield • ElevenLabs • OpenAI
