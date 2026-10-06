# DuckDuckGoose Studio Console - Setup Guide

Production console for Episode 4 video workflow up to picture lock (G4.09).

## Prerequisites

- **Python 3.11+**
- **Node.js 18+**
- **Temporal CLI** - [Install](https://docs.temporal.io/cli)
- **OpenAI API key** (for duck identity gate)
- **Higgsfield access** (model path TBD from Travis)

## Quick Start

### 1. Clone & Install

```bash
# Install Python dependencies
pip install -r requirements-studio.txt

# Install Node.js dependencies
cd web
npm install
cd ..
```

### 2. Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit .env and set required secrets:
# - STUDIO_ADMIN_SECRET=<your-secure-random-secret>
# - OPENAI_API_KEY=<your-openai-key>
# - MODEL_PATH_STILL=<higgsfield-model-path>  # TBD from Travis

# Optional: Configure paths and limits
```

### 3. Start Backend Services

```bash
# This starts:
# - Temporal dev server (port 8233 UI, 7233 API)
# - Python workflow worker
# - FastAPI Studio API (port 8000)

./scripts/run-backend.sh
```

The script will:
- Validate environment (check for required secrets)
- Create data directories (`data/episodes/`, `data/temporal/`)
- Start all services with logging to `logs/`
- Run until Ctrl+C (graceful shutdown)

### 4. Start Next.js Console

In a new terminal:

```bash
cd web
npm run dev
```

Console available at: http://localhost:3000/studio

### 5. Login

Navigate to `/studio` and enter your `STUDIO_ADMIN_SECRET` to authenticate.

## Directory Structure

```
.
├── api/
│   └── main.py                  # FastAPI backend
├── hfvg/
│   ├── qc/
│   │   ├── duck_identity.py     # Duck identity gate
│   │   └── openai_vision_judge.py  # OpenAI vision judge
│   ├── episode_parser.py        # BEATMAP parser
│   └── workflows/
│       └── episode_v2.py        # Temporal workflows
├── web/
│   ├── app/
│   │   ├── api/studio/          # Server-side API routes
│   │   └── studio/              # Studio console UI
│   └── ...
├── scripts/
│   └── run-backend.sh           # Backend launcher
├── tests/
│   └── test_studio_safety.py   # Safety tests
├── data/                         # Created at runtime
│   ├── episodes/                # Episode data (BEATMAP, etc.)
│   ├── temporal/                # Temporal DB
│   └── studio.db                # Studio SQLite DB
└── logs/                         # Created at runtime
    ├── temporal.log
    ├── worker.log
    └── api.log
```

## Episode Workflow

### 1. Upload Episode Data

Navigate to `/studio/episodes/ep04` and upload:
- **BEATMAP.md** (required) - 31 shots for Ep04
- **CONTINUITY.md** (optional)
- **CREDIT-PLAN.md** (optional)

Files are stored backend-side in `data/episodes/<episode_id>/` and **never committed** to the public repo.

### 2. Review Parsed Shots

After upload, shots are parsed and displayed with:
- Shot ID (A01, A02, etc.)
- Room, TOD, characters
- Status (pending, in_progress, complete, failed)
- Retry count
- QC results (when available)

### 3. Generate Stills & Clips

#### Canary Mode (Testing)
- Select a shot
- Click "Run Canary" to generate **exactly 1 still + 1 clip**
- See estimated credits before confirming
- Review results before proceeding

#### Full Production
- Requires:
  - Episode in **live mode**
  - **G1.08 approved** (credit plan)
  - Budget reservation succeeds
- Enforced backend-side (not just UI)

### 4. Review & Approve

#### Still Strip Review (G2.12)
- View all stills in sequence
- See QC verdicts (duck identity, composition, etc.)
- Approve/reject per still

#### Clip Review (G4.10)
- View clips with motion analysis
- See G4.10 motion results (peak_fd, mean_fd)
- See duck identity results
- Approve/reject per clip

### 5. Gate Approvals

Required Travis approval points:
- G1.01 - Pitch selection ✅ (done)
- G1.03 - Beat map ✅ (done)
- G1.08 - Credit plan ✅ (done)
- G2.12 - Still strip review
- G4.09 - Picture lock

### 6. Audit Trail

Every critical event is logged:
- Episode created
- Files uploaded
- Gate approved
- Live mode toggled
- Canary requested
- Paid submit

View full trail in the Audit panel (right sidebar).

## Budget System

### Lines (from CREDIT-PLAN.md §8)

| Line | Plan | Budget | 80% Stop | Note |
|------|------|--------|----------|------|
| L1 refs | 91 | 120 | 96 | Under stop |
| L2 drafts | 87.5 | 100 | **80** | **Exceeds stop by 7.5** → planned report point |
| L3 final stills | 227.5 | 230 | **184** | **Exceeds stop by 43.5** → planned report point |
| L4 video | 229.4 | 300 | 240 | Under stop |
| L6 reserve | 250 | 250 | 200 | Reserve for re-rolls |

**Total**: 885.4 (first pass + reserve) / 1,000 target / 1,250 cap

### Stops & Reports

- **80% stop**: Generation halts automatically at 80% of any line
- **Planned reports**: L2 and L3 intentionally exceed their stops
  - Requires GC.06 line move approval from Travis before proceeding
- **Hard cap**: 1,250 credits (125% of 1,000 target)

### Safety Features

- **Idempotent retries**: Same idempotency key doesn't double-charge
- **Reservation**: Credits reserved before generation, released on failure
- **Commit**: Credits committed only on successful completion
- **Preflight**: Balance checked before every round (GC.01)

## Quality Gates

### Duck Identity Gate (G2.03, G3.03, G4.02)

**Vision Judge**: OpenAI GPT-4 Vision (temperature 0)

#### Fail Conditions
- Any sign score = 2 (drift)
- Total minor scores ≥ 3
- Neck ring detected (confidence ≥ 0.5)
- Tuque/vest mismatch
- B shots: tuft missing or incorrect
- **Confidence < 0.6** → ESCALATE
- **OPENAI_API_KEY missing** → ESCALATE (fail-closed)

#### Escalation
- Verdict: ESCALATE
- Shown in console
- Blocks automated approval
- Requires Travis manual review

### Motion Gate (G4.10)

**Thresholds**: peak_fd ≥ 0.30, mean_fd ≥ 0.18

#### Fail Conditions
- peak_fd < 0.25 → FAIL
- mean_fd < 0.15 → FAIL
- Blocks clip from proceeding

## Security

### Server-Side Only
- `STUDIO_ADMIN_SECRET` - Never exposed to browser
- `OPENAI_API_KEY` - Backend-to-API only
- All console-to-backend calls proxied through Next.js server routes

### Client-Side
- Authentication via httpOnly cookie
- No `NEXT_PUBLIC_` secrets
- Token verified on every API call

### CORS
- Allowed origins: `https://duckduckgoose-topaz.vercel.app` + `localhost:3000` (dev)
- Configurable via `ALLOWED_ORIGIN` env var

## Testing

### Run Safety Tests

```bash
pytest tests/test_studio_safety.py -v
```

Tests cover:
- Budget 80% stops
- Live mode + G1.08 enforcement
- Idempotent retries
- Duck identity escalation
- Motion gate blocking
- Authentication
- Beatmap parsing (31 shots)

### Dry Run

1. Set all providers to test/fake mode
2. Upload Ep04 BEATMAP (31 shots)
3. Run canary for 1 shot
4. Verify 0 real credits spent
5. Check audit trail
6. Verify all gates block correctly

## Troubleshooting

### Backend won't start

**Check environment**:
```bash
# Required secrets set?
echo $STUDIO_ADMIN_SECRET
echo $OPENAI_API_KEY
echo $MODEL_PATH_STILL

# Temporal installed?
temporal --version

# Python packages?
python3 -c "import temporalio, fastapi, openai"
```

**Check logs**:
```bash
tail -f logs/temporal.log
tail -f logs/worker.log
tail -f logs/api.log
```

### Duck identity gate escalating

- **Cause**: `OPENAI_API_KEY` not set or invalid
- **Expected**: Fail-closed behavior
- **Action**: Set valid key in `.env`, restart backend

### Budget stops unexpectedly

- **L2 drafts**: Stops at 80 credits (planned report point at 87.5)
- **L3 final stills**: Stops at 184 credits (planned report point at 227.5)
- **Action**: Travis approval required to proceed (GC.06 line move)

### Episode upload fails

- **Check file size**: BEATMAP should be < 1MB
- **Check format**: Must be .md files
- **Check backend**: Is FastAPI running? (http://localhost:8000/health)

## API Endpoints

### Episodes
- `GET /api/episodes` - List all episodes
- `POST /api/episodes` - Create new episode
- `GET /api/episodes/{id}` - Get episode state
- `POST /api/episodes/{id}/upload` - Upload episode data files

### Shots
- `GET /api/episodes/{id}/shots` - Get all shots with status

### Budget
- `GET /api/episodes/{id}/budget` - Get budget status

### Approvals
- `POST /api/episodes/{id}/approve` - Approve a gate

### Canary
- `POST /api/episodes/{id}/canary` - Run canary (1 still + 1 clip)

### Audit
- `GET /api/episodes/{id}/audit` - Get audit trail

All endpoints require `Authorization: Bearer <STUDIO_ADMIN_SECRET>` header.

## Architecture

```
┌─────────────────────┐
│  User (Travis)      │
│  Browser            │
└──────────┬──────────┘
           │
           │ HTTPS (Vercel)
           ↓
┌─────────────────────┐
│  Next.js Frontend   │
│  /studio/*          │
│  - Upload UI        │
│  - Shot review      │
│  - Approvals        │
│  - Audit trail      │
└──────────┬──────────┘
           │
           │ Server-side API routes
           │ (secrets injected here)
           ↓
┌─────────────────────┐
│  FastAPI Backend    │  <─── HTTPS tunnel or direct
│  - Episodes         │
│  - Shots            │
│  - Budget           │
│  - Approvals        │
│  - Audit log        │
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│  Temporal           │
│  - EpisodeV2        │
│  - ShotWorkflow     │
│  - Gate checks      │
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│  External APIs      │
│  - OpenAI Vision    │  (duck identity)
│  - Higgsfield       │  (stills, clips)
└─────────────────────┘
```

## Production Deployment

### Backend (Linux Box)
```bash
# 1. Clone repo
# 2. Install dependencies (Python, Temporal, Node)
# 3. Set environment variables
# 4. Run backend in tmux/screen:
tmux new -s studio-backend
./scripts/run-backend.sh

# 5. Set up HTTPS tunnel (e.g., ngrok, cloudflare tunnel)
# 6. Configure CORS in .env with Vercel domain
```

### Frontend (Vercel)
```bash
# 1. Connect GitHub repo to Vercel
# 2. Set environment variables:
#    - STUDIO_ADMIN_SECRET (same as backend)
#    - API_URL (backend HTTPS tunnel URL)
# 3. Deploy
```

## Support

For issues or questions:
- Check logs in `logs/`
- Check audit trail in console
- Review safety test results
- Contact Travis

---

**Version**: 1.0  
**Date**: 2026-10-06  
**Episode**: 04 "Mind the Antlers"
