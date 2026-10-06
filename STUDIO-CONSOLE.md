# Studio Console Implementation Status

This document tracks the implementation of the studio console for Episode 4 production.

## ✅ What's Built (MVP)

### Backend API (`/api`)

**Service**: FastAPI backend wrapping the Temporal workflow
- **Location**: `api/main.py`
- **Access Control**: Admin secret (env: `ADMIN_SECRET`, min 32 chars)
- **Routes**:
  - `POST /api/episodes`: Start episode workflow
  - `GET /api/episodes/{id}`: Get episode state
  - `POST /api/episodes/{id}/approve`: Send approval signals
  - `GET /api/episodes/{id}/budget`: Get budget status with 80% stop tracking
  - `POST /api/episodes/{id}/set-live`: Switch to live mode
- **Safety**: All routes except health check require admin secret
- **Fail-Closed**: Returns 503 if Temporal disconnected

**Deployment**:
- Runs standalone: `python api/main.py`
- Requires: Temporal worker running (`python -m hfvg.worker`)
- Containerizable (Docker/Podman) - see `api/README.md`
- Can run on persistent Linux machine with systemd

### Frontend Console (`/web/app/studio`)

**Login**: `/studio/login`
- Admin secret authentication
- Sets secure cookie
- Redirects to console

**Dashboard**: `/studio`
- API health check
- Quick actions
- Safety warnings
- Episode list (stub)

**Episode View**: `/studio/episodes/[id]`
- Real-time episode state polling (3s)
- **Budget meters** per line vs cap/stop with visual indicators
- **Approval gates** with Approve buttons (15 Travis gates)
- Shot list (stub - needs workflow integration)
- Still strip review (stub)
- Clip review with QC (stub)

### Access Control

**Public pages** (no auth): `/`, `/archetypes`, `/pricing`
**Protected**: `/studio/**` requires admin secret in cookie
**API**: All routes except `/` and `/api/health` require `Authorization: Bearer <secret>` header

**Middleware**: `web/middleware.ts` enforces auth
**Login flow**: Cookie-based, server-side verified against API

### Existing Infrastructure (Already on main)

- ✅ `hfvg/gates.py`: Gate policy loader (71 gates, 15 Travis approvals)
- ✅ `hfvg/budget.py`: Per-line budget ledger with 80% stop
- ✅ `hfvg/ledger.py`: Idempotent job tracking
- ✅ `hfvg/workflows/episode_v2.py`: EpisodeWorkflowV2 with approval signals
- ✅ `hfvg/qc/motion.py`: G4.10 motion check (wraps stillgate.py)
- ✅ `hfvg/qc/duck_identity.py`: Duck identity gate scaffold
- ✅ `hfvg/qc/prompt_validation.py`: Prompt validation rules

## 🚧 What Needs Implementation

### Critical for Ep04 Dry Run

1. **Real Vision Judge** (`hfvg/qc/duck_identity.py`)
   - Replace `FakeVisionJudge` with OpenAI vision implementation
   - Read `OPENAI_API_KEY` from env
   - If key missing: ESCALATE (fail-closed), never silently pass
   - Temperature 0, structured output
   - See existing interface in `duck_identity.py:30-60`

2. **Episode Data Loading** (NOT committed to public repo)
   - Upload via console UI or mount path
   - Parse BEATMAP.md → shots
   - Load CONTINUITY.md, CREDIT-PLAN.md
   - Store in `./data/episodes/{episode_id}/`
   - See attached files: `BEATMAP_dd05.md`, `CREDIT-PLAN_4753.md`

3. **Shot Status Tracking**
   - Link ShotWorkflow results to episode state
   - Track: prompt → still generation → still QC → clip generation → clip QC
   - Store media URLs (or paths for dry-run)
   - QC results per shot (duck identity, motion check)
   - Retry count per shot

4. **Still Strip Review UI**
   - Grid of all stills for G2.12 approval
   - Show QC verdicts: PASS/WATCH/FAIL with reasons
   - Duck identity results per still
   - Approve/Reject + note

5. **Clip Review UI**
   - List of clips for G4.09 picture lock
   - G4.10 motion check results (mean_fd, peak_fd, PASS/WATCH/FAIL)
   - Duck identity results per clip
   - Retry mechanism

6. **Canary Action** (`POST /api/episodes/{id}/canary`)
   - Generate exactly 1 still + 1 clip for specified shot
   - Let Travis verify before full round
   - Show cost estimate before triggering

7. **Live Mode Enforcement**
   - Track dry_run vs live per episode
   - Require G1.08 approval + live mode switch before ANY paid submit
   - Block paid generation in dry-run
   - Add "Switch to Live" confirmation dialog

8. **Audit Trail**
   - Log all gate results to database
   - Track: timestamp, gate_id, shot_id, verdict, reason, judge_output
   - Visible in console per shot
   - Export to JSON

### Still Model Configuration

**Problem**: `MODEL_PATH_GPT_IMAGE_2` is unset because GPT Image isn't in Higgsfield's API.

**Solution**:
- Make still model configurable via env: `STILL_MODEL` (default: fail fast if missing)
- Add console UI to select still model from Higgsfield's available models
- Fail fast with clear message if model not found
- See `hfvg/providers/higgsfield.py` for model routing

### Budget Enforcement

Already implemented in `hfvg/budget.py`:
- ✅ Reserve before generation
- ✅ 80% stop check
- ✅ Hard cap
- ✅ Release on failure

**Needs wiring**:
- Call `BudgetLedger.reserve()` before every paid submit
- If `reserve()` returns False: stop and show "80% stop reached" in console
- Commit actual spend after generation
- Release on provider failure or moderation block

### Idempotency Enforcement

Already implemented in `hfvg/ledger.py`:
- ✅ Idempotency key generation from request
- ✅ Job deduplication

**Needs wiring**:
- Pass idempotency key to Higgsfield provider
- Check existing job before submitting
- Return existing result if found

## 🔐 Environment Variables

### Backend API

**Required**:
- `ADMIN_SECRET`: Admin secret (min 32 chars, generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)

**Optional**:
- `TEMPORAL_ADDRESS`: Temporal server (default: `localhost:7233`)
- `TEMPORAL_NAMESPACE`: Temporal namespace (default: `default`)
- `DATABASE_PATH`: SQLite path (default: `./data/studio.db`)
- `OPENAI_API_KEY`: OpenAI API key for vision judge (escalates if missing)
- `DRY_RUN`: Default dry-run mode (default: `true`)
- `PORT`: API port (default: `8000`)

### Frontend Console

**Required**:
- `STUDIO_ADMIN_SECRET`: Same as backend `ADMIN_SECRET` (for cookie verification)

**Optional**:
- `NEXT_PUBLIC_API_URL`: API URL (default: `http://localhost:8000`)

### Vercel Deployment

**Console** (`web/` directory):
- Framework: Next.js
- Root Directory: `web`
- Build Command: `npm run build`
- Environment Variables:
  - `STUDIO_ADMIN_SECRET`: Admin secret
  - `NEXT_PUBLIC_API_URL`: Backend API URL (e.g., `https://api.example.com`)

**Note**: Vercel serverless functions cannot host the Temporal worker or API. Deploy those separately.

## 📋 Verification Checklist

### Before First Dry Run

- [ ] Generate and set `ADMIN_SECRET` in both API and console
- [ ] Start Temporal dev server or connect to Temporal Cloud
- [ ] Start Temporal worker: `python -m hfvg.worker`
- [ ] Start API: `python api/main.py`
- [ ] Start console: `cd web && npm run dev`
- [ ] Upload Ep04 BEATMAP.md via console
- [ ] Initialize episode budget: `BudgetLedger.init_episode_budget('ep04')`

### Dry Run Test

- [ ] Load beat map (31 shots visible)
- [ ] Approve G1.01 pitch pick
- [ ] Approve G1.03 beatmap
- [ ] Approve G1.08 credit plan
- [ ] Approve GC.02 budget tracking
- [ ] See stills placeholders (dry-run mode)
- [ ] Approve G2.12 still strip
- [ ] See clips placeholders (dry-run mode)
- [ ] Check QC verdicts (all PASS in dry-run)
- [ ] Check budget meters (reserved but not spent)
- [ ] Approve G4.09 picture lock
- [ ] Verify workflow stops at picture lock

### Safety Tests

- [ ] Reject console request without admin secret (401)
- [ ] Reject API request without admin secret (403)
- [ ] Block paid generation without live mode switch
- [ ] Block paid generation without G1.08 approval
- [ ] Hit 80% stop on a budget line (auto-stop)
- [ ] Retry same shot (idempotency: returns existing job)
- [ ] Judge missing (escalates, never passes)
- [ ] G4.10 motion fail (blocks clip, shows in console)

## 🎯 Episode 4 Budget

From `CREDIT-PLAN_4753.md`:

**Higgsfield** (all Kling 3.0 Pro):
- L1 refs: 91 / 96 stop / 120 cap
- L2 drafts: 87.5 / 80 stop / 100 cap ⚠️
- L3 final stills: 227.5 / 184 stop / 230 cap ⚠️
- L4 video: 229.4 / 240 stop / 300 cap
- L6 reserve: 250 / 200 stop / 250 cap
- **Total**: 885.4 with reserve / 1,000 target / 1,250 cap

**ElevenLabs** (deferred after picture lock):
- 3,500 / 4,000 cap

**Cloud API Hard Cap**: $60 (covers Higgsfield 1,250 cr + OpenAI vision + buffer)

⚠️ **L2 and L3 planned totals sit above their 80% stops**. These are report points per BEATMAP §8. GC.06 line move report to Travis required.

## 📝 Decisions Needed from Travis

1. **Still model**: Which Higgsfield still model to use? (GPT Image 2 not available)
2. **Episode data upload**: Console UI upload or mount from private path? (NOT committed to repo)
3. **Paid services**: OK to use OpenAI vision API for duck identity judge? (~$0.01/image)
4. **Temporal hosting**: Use Temporal Cloud or self-host dev server for now?
5. **API deployment**: Container host recommendation or run on persistent Linux machine?

## 🚀 Next Steps

1. Implement real vision judge with OpenAI (or escalate path if key missing)
2. Wire shot status tracking from ShotWorkflow to console
3. Build still strip and clip review UIs
4. Add canary action endpoint
5. Enforce live mode + G1.08 before paid generation
6. Add audit trail logging
7. Implement episode data upload
8. Full dry-run test with Ep04 data
9. Screenshots of console in action
10. Update PR with verification checklist results

## 📚 References

- **HARNESS-GATES v1.1**: `examples/mid-mountain-rest/HARNESS-GATES.json`
- **Episode Parser**: `hfvg/episode_parser.py`
- **Workflow**: `hfvg/workflows/episode_v2.py`
- **Budget Ledger**: `hfvg/budget.py`
- **QC Gates**: `hfvg/qc/`
- **API Docs**: `api/README.md`
- **Ep04 Beat Map**: Upload required (31 shots, 120s, all Kling)
