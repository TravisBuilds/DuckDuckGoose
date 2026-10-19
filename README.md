# Hands-Free Video Generator

A production-ready Temporal Python workflow harness for turning story ideas into finished short-form videos with automated generation, human approval gates, and budget enforcement.

## What Is This?

The Hands-Free Video Generator is a **workflow orchestration harness** that coordinates AI video generation providers (Higgsfield/Kling/Seedance for video, ElevenLabs for audio) through a durable Temporal workflow with:

- **Automated pipeline**: Parse beatmap → generate stills → human review (G2) → generate clips → edit → audio → delivery
- **Human gates**: Three approval points (`G1_SHOT_PLAN`, `G2_SCENE_STILLS`, `G_FINAL_CUT`)
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
                 │  Higgsfield  │        │  ElevenLabs  │    │  Human QC  │
                 │ (Still/Video)│        │   (Audio)    │    │ (escalate) │
                 └──────────────┘        └──────────────┘    └────────────┘
```

**Key Components:**

- **EpisodeWorkflowV2**: Main orchestrator (G1 → G2 → clips → audio → final)
- **ShotWorkflow**: Per-shot child workflow (still → review → clip → QC)
- **Gates**: `G1_SHOT_PLAN` (shot plan + credit plan), `G2_SCENE_STILLS` (scene stills), `G_FINAL_CUT` (final cut)
- **Budget Ledger**: Atomic reserve/commit/release in integer usd_micros: $60.00 episode cap, $48.00 stop (80%), $5.00 GC.01 headroom, $10.00 revision reserve (`L7_revision`, outside the cap)
- **Model Caps Registry**: Per-model limits (refs, prompt, duration, aspect, resolution)
- **Preflight**: Validate all shots before spend (G1 plan, per-submit defense)

## Quickstart

### Prerequisites

- **Python 3.11+**
- **Temporal dev server** ([download](https://docs.temporal.io/cli#install))
- **Node.js 20+** (for web UI; Next.js 16, CI uses Node 20)
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

# Nothing loads .env automatically: export it in every terminal
# (API, worker, CLI) before starting them
set -a; source .env; set +a
```

**Required:**
- `ADMIN_SECRET`: Min 32 chars (generate: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)

**Optional (for live mode):**
- `HIGGSFIELD_API_KEY`: Format `id:secret`
- `ELEVENLABS_API_KEY`: For audio generation
- `OPENAI_API_KEY`: listed in `.env.example` but not read by the current code (no OpenAI judge is wired; live QC escalates to human review)

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
# CLI approach (posts to the API at HFVG_API_BASE, default http://localhost:8000;
# needs the API running and ADMIN_SECRET exported). Episode IDs must match ^ep\d{2}$.
hfvg start-v2 ep01 --beatmap tests/fixtures/sample_beatmap.md --dry-run

# Or via API (send the beatmap text as beatmap_content, or upload it first
# with POST /api/episodes/upload)
curl -X POST http://localhost:8000/api/episodes \
  -H "Authorization: Bearer YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile b tests/fixtures/sample_beatmap.md \
        '{episode_id: "ep01", beatmap_content: $b, dry_run: true}')"
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
  ├─ 🚦 GATE G_FINAL_CUT: Human approves final cut
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

Switch to live (all of these are required; `DRY_RUN=true` is a kill switch that forces dry mode):
```bash
# 1. DRY_RUN=false in the API and worker environment
# 2. Enable live for the episode (cookie auth from POST /api/login):
#    POST /api/episodes/{id}/set-live  {"confirmation": "ENABLE_LIVE_MODE"}
# 3. Start with --live (G1 approval then approves the G1.08 credit plan)
hfvg start-v2 ep01 --beatmap tests/fixtures/sample_beatmap.md --live
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

- **Hard USD caps**: Episode cap $60.00 (`EPISODE_CAP_USD`); every budget line also has its own cap. Over a cap a reserve raises
- **Atomic reserves**: SQL `WHERE` clause ensures no overspend
- **Stop**: $48.00 (80% of the cap, `EPISODE_STOP_PERCENT`); each line also has its own 80% stop. Past the stop a reserve returns `False`
- **GC.01 headroom**: $5.00 (`GC01_HEADROOM_USD`) must remain on the manually entered Higgsfield balance after all holds
- **Revision reserve**: $10.00 line `L7_revision` (`REVISION_RESERVE_USD`), outside the episode cap, usable only with the `revision` approval tag
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

# Same via API: POST /api/episodes/{id}/shots/{shot_id}/retry
# CLI returns: queued, refused (with reason)
# MAX_SHOT_RETRIES retries per shot (default 1, i.e. 2 total attempts)
```

### End-Frame Generation

Shots with `end_frame: true` generate:
1. **Start still** → QC → recorded
2. **End still** (uses start still as first ref) → recorded
3. Both land before G2 (human reviews both)
4. Clip generation uses `last_image_url` (Kling)

Backfill for runs parked at G2 without an end still: there is no CLI command or API route yet.
It is the `retry_end_still(shot_id, end_prompt=None)` update on `EpisodeWorkflowV2`
(child workflow `{episode_id}-shot-{shot_id}-end{n}`), callable from a Temporal client:
```python
await handle.execute_update("retry_end_still", args=["A02", "Duck looking surprised"])
```

## Testing

```bash
# All tests (unit + integration)
pytest

# Smoke-test activity tests (Higgsfield mocked with respx, no Temporal required)
pytest tests/test_smoke_test.py

# Red-green mutation tests (requires REQUIRE_FFMPEG=1)
REQUIRE_FFMPEG=1 ./scripts/redgreen.sh

# Specific test
pytest tests/test_episode_v2.py::test_episode_v2_approval_gates -v
```

See [docs/LOCAL-DEV.md](docs/LOCAL-DEV.md) for detailed testing instructions.

## Environment Variables

See [`.env.example`](.env.example) for all available environment variables.

**Core:**
- `ADMIN_SECRET`: API admin secret (REQUIRED, min 32 chars)
- `TEMPORAL_ADDRESS`: Temporal server used by the API (default: localhost:7233)
- `TEMPORAL_HOST`: Temporal server used by the worker and CLI (default: localhost:7233)
- `DATABASE_PATH`: SQLite path (default: ./data/studio.db)
- `DRY_RUN`: Dry run mode (default: true)

**Provider API Keys:**
- `HIGGSFIELD_API_KEY`: Format `id:secret`
- `ELEVENLABS_API_KEY`: For audio generation (read by the ElevenLabs SDK)
- `OPENAI_API_KEY`: not read by the current code

**Budget (USD decimal strings):**
- `EPISODE_CAP_USD` (default 60.00), `EPISODE_STOP_PERCENT` (default 80 -> $48.00), `GC01_HEADROOM_USD` (default 5.00), `REVISION_RESERVE_USD` (default 10.00)

**Model Paths:**
- `MODEL_PATH_STILL`: Still model (default: xai/grok-imagine-image-2.0)
- `MODEL_PATH_KLING`: Kling video model (default: kling-video/v3.0/pro/image-to-video)
- `MODEL_PATH_SEEDANCE`: Seedance video model (default: bytedance/seedance-2.5/image-to-video)

**Episode Data:**
- `EPISODES_DIR`: Episode data directory (default: ./data/episodes)

## CLI Reference

```bash
# Start episode (EpisodeWorkflowV2, via the API)
hfvg start-v2 <episode_id> [--beatmap PATH] [--dry-run | --live] [--shots A01,A02] \
    [--credit-cap N] [--attempt-overrides '{"A02": 2}']

# Query state
hfvg query-state <episode_id>

# Approve EpisodeWorkflowV2 gates: there is no CLI command; use the API
#   POST /api/episodes/{id}/approve  {"gate_id": "g1_shot_plan" | "g2_scene_stills" | "g_final_cut"}
# (`hfvg approve <episode_id> {readback|character-locks|storyboard|stills|final}` targets the
#  legacy EpisodeWorkflow only)

# Retry failed shot (while parked at G1/G2)
hfvg retry-shot <episode_id> <shot_id>

# Preflight validation
hfvg preflight <episode_id> [--shots PATH]

# Lint episode
hfvg lint <episode_id>

# Other commands: start-episode, query-storyboard, approve-posts, wait-result,
# canary, smoke-test, set-anchor  (see `hfvg --help`)
```

## API Reference

See [api/README.md](api/README.md) for full API documentation.

**Key endpoints:**
- `POST /api/login`: Set admin cookie
- `POST /api/episodes`: Start episode
- `GET /api/episodes/{id}`: Query state
- `POST /api/episodes/{id}/approve`: Send approval (`{"gate_id": "g1_shot_plan"}`, `g2_scene_stills`, `g_final_cut`)
- `POST /api/episodes/{id}/shots/{shot_id}/retry`: Retry shot
- `GET /api/episodes/{id}/budget`: Budget lines (usd_micros)
- `POST /api/episodes/{id}/set-live`: Enable live mode (`{"confirmation": "ENABLE_LIVE_MODE"}`)
- `GET /api/health`: Health check
- `POST /api/episodes/{id}/preflight`: Validate shot plans

## License

[LICENSE](LICENSE) - License TBD by owner

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Quick rules:**
- Never delete, skip, or weaken tests/mutations
- One coding agent at a time (process rule; the account-level run lock separately allows one episode run per account at a time)
- Never commit secrets or private assets
- Keep CI green (all 3 jobs must pass)

## Support

- **Issues**: [GitHub Issues](../../issues)
- **Discussions**: [GitHub Discussions](../../discussions)
- **Documentation**: [docs/](docs/)

---

**Built with:** Temporal • Python 3.11 • FastAPI • Next.js • SQLite • Higgsfield • ElevenLabs • OpenAI
