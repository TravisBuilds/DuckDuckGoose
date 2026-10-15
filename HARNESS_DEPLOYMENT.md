# DuckDuckGoose Harness: Deployment Guide

## Overview

This document describes how to run the full end-to-end harness locally for Ep05 teaser (4-shot, ~24s, all Kling, 500 credit cap).

Branch: `cursor/temporal-harness-end-to-end-b9be`
Base: `cursor/phase1-backend-complete-941e`
PR: Open against base branch (do not merge)

## Features Implemented

### 1. Credit Ledger & Cap-Pause
- **Higgsfield credit ledger** (`hfvg/credit_ledger.py`): Tracks credits (not USD) with per-episode caps
- **Pause on cap**: Workflow pauses at 90% of cap, waits for approval via update handler
- **Single-run lock**: Only one episode can run at a time per Higgsfield account
- **Idempotent job IDs**: Retry/replay never double-charges

### 2. Three Human Gates (Collapsed from 15)
- **G1_SHOT_PLAN**: Beatmap + credit plan approval
- **G2_SCENE_STILLS**: All stills + reference sheets approval
- **G_FINAL_CUT**: Final cut approval after edit

Internal automated gates (motion QC, duck identity, etc.) remain but don't block on human approval.

### 3. Motion QC (Advisory Mode)
- Frame sheets at 0.5s intervals
- Headcount per shot (detects extra/missing animals)
- Flags: floating animals, sliding, walking backwards, objects appearing
- Advisory only: flags issues for gates rather than auto-rerolling

### 4. Automatic Run Report
- Per-stage metrics: time, credits, generations, rerolls, flags, retries, gate waits
- Output: JSON + markdown
- Generated at workflow completion

### 5. CANARY Mode
- 2-shot subset of brief
- Small credit cap (default 100 credits)
- Same live path as full run

### 6. Bug Fixes
- **Auth unified**: All routes accept both Bearer token (`Authorization: Bearer <ADMIN_SECRET>`) and session cookie
- **CORS configurable**: Set `CORS_ORIGINS` env var (comma-separated)
- **refs.json validation**: Checks all shots in `shots.json` have entries in `refs.json` `per_shot`

### 7. Test Suite
- **302 tests passed** (baseline 290), 2 skipped
- New tests: `test_credit_ledger.py`, `test_harness_workflow.py`
- `--disable-socket` remains enabled (no network calls in CI)

## Prerequisites

### System Requirements
- Python 3.11+
- Temporal Server (local or cloud)
- PostgreSQL or SQLite (for studio DB)
- Node.js 18+ (for Next.js web console)

### API Keys (for live mode)
- `HIGGSFIELD_API_KEY`: Format `<id>:<secret>` (e.g., `hf_abc123:sk_xyz789`)
- `ELEVENLABS_API_KEY`: ElevenLabs API key (for audio stage)
- `ADMIN_SECRET`: 32+ character secret (generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)

**Note**: Live mode requires all keys. Dry-run mode (`DRY_RUN=true`) works without keys.

## Local Setup

### 1. Clone and Install

```bash
git clone https://github.com/TravisBuilds/DuckDuckGoose
cd DuckDuckGoose
git checkout cursor/temporal-harness-end-to-end-b9be

# Install Python dependencies
python3 -m pip install --user -e '.[dev]'

# Or use venv (if python3-venv is available)
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### 2. Start Temporal Server

```bash
# Option A: Local Temporal server (Docker)
docker run -p 7233:7233 temporalio/auto-setup:latest

# Option B: Temporal CLI (simpler for testing)
temporal server start-dev
```

### 3. Configure Environment

Create `.env` file (or export in shell):

```bash
# API Settings
ADMIN_SECRET="<generate-32-char-secret>"
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
DATABASE_PATH=./data/studio.db

# Mode
DRY_RUN=true  # Set to 'false' for live Higgsfield/ElevenLabs calls

# Live Mode Keys (optional for dry-run)
HIGGSFIELD_API_KEY=<id>:<secret>
ELEVENLABS_API_KEY=<api-key>

# CORS (optional, defaults to localhost:3000,3001)
CORS_ORIGINS=http://localhost:3000,http://localhost:3001

# Credit Cap (optional, defaults to 500)
EPISODE_CAP_CREDITS=500
```

### 4. Start Services

Terminal 1 - API:
```bash
export ADMIN_SECRET="<your-secret>"
export DRY_RUN=true
cd api
uvicorn main:app --reload --port 8000
```

Terminal 2 - Worker:
```bash
export ADMIN_SECRET="<your-secret>"
export DRY_RUN=true
python3 -m hfvg.worker
```

Terminal 3 - Web Console (optional):
```bash
cd web
npm install
npm run dev
# Opens on http://localhost:3000
```

## Running Episodes

### Ep05 Teaser (Full Run)

```bash
# Using curl (with Bearer token)
curl -X POST http://localhost:8000/api/episodes \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{
    "episode_id": "ep05",
    "dry_run": true
  }'

# Response:
# {
#   "workflow_id": "ep05",
#   "run_id": "...",
#   "episode_id": "ep05",
#   "message": "Episode ep05 started (dry_run=true)"
# }

# Check status
curl http://localhost:8000/api/episodes/ep05 \
  -H "Authorization: Bearer <ADMIN_SECRET>"

# Approve gates (send signals)
curl -X POST http://localhost:8000/api/episodes/ep05/approve \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"gate": "G1_SHOT_PLAN"}'

curl -X POST http://localhost:8000/api/episodes/ep05/approve \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"gate": "G2_SCENE_STILLS"}'

curl -X POST http://localhost:8000/api/episodes/ep05/approve \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"gate": "G_FINAL_CUT"}'
```

### CANARY Mode (2-Shot Subset)

```bash
curl -X POST http://localhost:8000/api/episodes/ep05/canary \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"cap_credits": 100}'

# Returns workflow_id for status polling
```

### Using Temporal CLI

```bash
# Start workflow directly
temporal workflow start \
  --task-queue hfvg-tasks \
  --type EpisodeHarnessWorkflow \
  --workflow-id ep05-test \
  --input '{"episode_id": "ep05", "beatmap_path": "./fixtures/ep05/BEATMAP.md", "cap_credits": 500, "is_canary": false, "dry_run": true}'

# Query state
temporal workflow query \
  --workflow-id ep05-test \
  --type get_state

# Send signals (approve gates)
temporal workflow signal \
  --workflow-id ep05-test \
  --name approve_shot_plan

temporal workflow signal \
  --workflow-id ep05-test \
  --name approve_scene_stills

temporal workflow signal \
  --workflow-id ep05-test \
  --name approve_final_cut

# Increase credit cap (update handler)
temporal workflow update \
  --workflow-id ep05-test \
  --name increase_cap \
  --input '{"new_cap_credits": 750}'
```

## Environment Variables Reference

### Required (Live Mode)
- `ADMIN_SECRET`: Admin secret (32+ chars, no default)
- `HIGGSFIELD_API_KEY`: Higgsfield credentials (`<id>:<secret>`)
- `ELEVENLABS_API_KEY`: ElevenLabs API key

### Optional
- `DRY_RUN`: Mode (`true` = dry-run, `false` = live). Default: `true`
- `TEMPORAL_ADDRESS`: Temporal server address. Default: `localhost:7233`
- `TEMPORAL_NAMESPACE`: Temporal namespace. Default: `default`
- `DATABASE_PATH`: SQLite database path. Default: `./data/studio.db`
- `CORS_ORIGINS`: Comma-separated CORS origins. Default: `http://localhost:3000,http://localhost:3001`
- `EPISODE_CAP_CREDITS`: Episode credit cap. Default: `500`
- `PAUSE_PERCENT`: Pause threshold (% of cap). Default: `90`

### Model Configuration
- `MODEL_PATH_STILL`: Still image model. Default: `xai/grok-imagine-image-2.0`
- `MODEL_PATH_KLING`: Kling video model. Default: `kling-video/v3.0/pro/image-to-video`
- `MODEL_PATH_SEEDANCE`: Seedance model. Default: `bytedance/seedance-2.5/image-to-video`

## Test Suite

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=hfvg --cov-report=html

# Run specific test files
pytest tests/test_credit_ledger.py
pytest tests/test_harness_workflow.py

# Quick smoke test
pytest tests/test_smoke.py -v
```

### Test Summary
- **Total**: 302 passed, 2 skipped
- **Baseline**: 290 passed (12 new tests added)
- **New test files**:
  - `tests/test_credit_ledger.py`: Credit ledger, idempotency, lock
  - `tests/test_harness_workflow.py`: Harness workflow, 3 gates, CANARY

## Troubleshooting

### "No module named hfvg"
```bash
pip install -e .
# Or add to PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### "ADMIN_SECRET not set"
Generate a secret:
```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
export ADMIN_SECRET="<generated-secret>"
```

### "Temporal client not initialized"
Check that Temporal server is running:
```bash
temporal operator cluster health
# Or curl the API
curl http://localhost:8000/api/health
```

### "Episode cap exceeded"
Increase cap via update handler:
```bash
temporal workflow update \
  --workflow-id ep05 \
  --name increase_cap \
  --input '{"new_cap_credits": 1000}'
```

### "No such table: episodes"
Initialize the database:
```bash
python3 -c "import asyncio; from hfvg.studio_db import init_studio_db; asyncio.run(init_studio_db())"
```

## Brief Structure

Ep05 brief lives in `fixtures/ep05/`:
- `BEATMAP.md`: Shot list with timing, action, purpose, cost estimates
- `CONTINUITY.md`: Character descriptions, set details, prop rules
- `VO-AUDIO.md`: Voice-over script, music cues, SFX

This structure is safe for public repo (original characters only).

## Next Steps

After completing a successful run:
1. Check run report in `./data/reports/ep05_*.json` and `*.md`
2. Review credit usage vs cap
3. Inspect motion QC flags (if any)
4. Verify all gates were approved in sequence

For production:
1. Set `DRY_RUN=false`
2. Configure Higgsfield and ElevenLabs keys
3. Run CANARY first to validate with 2 shots
4. Review CANARY report before full run
5. Monitor credit usage during full run

## PR Details

- **Branch**: `cursor/temporal-harness-end-to-end-b9be`
- **Base**: `cursor/phase1-backend-complete-941e`
- **Status**: Open (do not merge per requirements)
- **Tests**: 302 passed, 2 skipped
- **Commits**: 1 (feat: end-to-end Temporal harness...)
