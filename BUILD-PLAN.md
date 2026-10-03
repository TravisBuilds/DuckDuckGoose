# Build Plan: Hands-Free Video Generator

## Milestone 1: What Was Delivered

Milestone 1 provides a **complete skeleton** of the Hands-Free Video Generator with no external dependencies. Everything runs locally in dry-run mode against the Temporal dev server.

### ✅ Delivered Components

**1. Core Workflows**
- `EpisodeWorkflow`: Main workflow covering all pipeline stages from idea gathering through posting
- `ShotWorkflow`: Child workflow per shot handling still → review → clip → QC
- `PostingWorkflow`: Batched post approval queue (stub)

**2. Human Approval Gates** (via Temporal Updates/Signals)
- `approve_readback`: Approve read-back summary
- `approve_storyboard`: Approve storyboard + cost estimate
- `approve_scene_stills(scene_id)`: Batched per-scene still approvals
- `approve_final`: Approve final mixed video
- `approve_posts(platforms)`: Approve posting queue (in PostingWorkflow)

**3. State Queries**
- `get_state`: Current stage, approval status, shot counts
- `get_storyboard`: Storyboard with cost estimate
- `get_pending_posts`: Posting queue (in PostingWorkflow)

**4. Activities with Heartbeats**

All activities send heartbeats and have heartbeat timeouts configured:

- **Generation**: `submit_still_job`, `submit_clip_job`, `await_job`
- **QC**: `review_still`, `review_clip` (agent-based, stubbed)
- **Media**: `trim_clips`, `render_edit`, `mix_audio` (ffmpeg, stubbed)
- **Audio**: `generate_voiceover`, `generate_sfx`, `generate_music` (ElevenLabs, stubbed)
- **Posting**: `post_to_platform` (stubbed)

**5. Typed Error Classes + Retry Policies**
- `RetryableError`: transient failures → retry with backoff
- `ContentBlockError`: moderation block → non-retryable, route to recovery branch
- `InsufficientCreditsError`: low balance → non-retryable, pause and notify
- `FatalError`: other fatal errors → non-retryable, escalate

**6. submit_job/await_job Pattern**
- Idempotency keys based on `hash(shot_id, version, prompt, refs, params)`
- Job ledger (SQLite) prevents double submits
- `await_job` polls with heartbeats until terminal state
- Credit ledger with balance guard: estimate deducted on submit

**7. Credit System**
- SQLite tables: `jobs`, `credits`, `credit_balance`
- Initial balance: 10,000 credits (configurable)
- `check_balance` before every submit
- `deduct_credits` on estimate, reconciliation ready for later
- Low-balance threshold configured (1,000 by default)

**8. Dry-Run Mode**
- All provider calls are fakes
- Configurable delays per activity type (stills: 2s, clips: 5s, QC: 1s, etc.)
- Failure injection rates:
  - `DRY_RUN_FAILURE_RATE`: general transient failures
  - `DRY_RUN_CONTENT_BLOCK_RATE`: moderation blocks
  - `DRY_RUN_STALL_RATE`: stalled jobs (not implemented but ready)
- Zero cost, runs entirely locally

**9. CLI**
- `hfvg start-episode`: Start a new episode
- `hfvg query-state`: Inspect current state
- `hfvg query-storyboard`: View storyboard
- `hfvg approve <gate>`: Send approvals (readback, storyboard, stills, final)
- `hfvg approve-posts`: Approve posting queue
- `hfvg wait-result`: Block until episode completes

**10. Tests** (pytest + Temporal time-skipping)
- ✅ `test_episode.py`: Full episode through all gates
- ✅ `test_idempotency.py`: Duplicate submits return same job ID
- ✅ `test_heartbeat.py`: Stalled activity times out and retries
- ✅ `test_credits.py`: InsufficientCredits pauses, sufficient credits proceed

---

## How to Run It

### Setup

1. **Install dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

2. **Start Temporal dev server** (in a separate terminal):
   ```bash
   temporal server start-dev
   ```

3. **Start the worker:**
   ```bash
   python -m hfvg.worker
   ```

### Run a Dry-Run Episode

In another terminal:

```bash
# Start episode
hfvg start-episode --episode-id ep-test-001 --idea "A duck teaches penguins to cook"

# Check state
hfvg query-state ep-test-001

# Approve gates as workflow progresses
hfvg approve ep-test-001 readback
hfvg approve ep-test-001 storyboard
hfvg approve ep-test-001 stills --scene-id 1
hfvg approve ep-test-001 stills --scene-id 2
hfvg approve ep-test-001 final

# Approve posts
hfvg approve-posts ep-test-001 --platforms instagram

# Wait for result
hfvg wait-result ep-test-001
```

### Run Tests

```bash
pytest -v
```

All tests use Temporal's time-skipping environment, so they complete in seconds.

---

## Next Implementation Slices

These are the recommended next steps to build toward a production harness:

### Slice 1: Real Higgsfield Stills + Recovery Test
**Goal:** Prove the submit/await pattern with real generation and recovery from a killed worker.

- Replace `submit_still_job` and `await_job` with real Higgsfield MCP calls
- Use Higgsfield `generate_image_2` → `job_status` → `jobs_wait`
- Test: kill the worker mid-job; verify it resumes and the job completes
- Add retry cap per shot (3 attempts), then escalate
- Archive only after atomic swap (staging → live pointer flip)

**Open risk to resolve first:**
- Can a headless worker hold/refresh Higgsfield's OAuth MCP token?
- If not, use Higgsfield's API/SDK directly instead of MCP for Activities

**Deliverable:** One real still generated, QC'd (stubbed), and saved. Worker recovery test passes.

---

### Slice 2: QC Agent + Contact Sheet UI
**Goal:** Agent-based `still_review` with a visual contact sheet for human approval.

- Implement `review_still` as an OpenAI Agents SDK Activity (or LiteLLM for vision models)
- QC rubric: character drift (A/B crop), height ratios, headcount, role, props
- Generate contact sheet (HTML or simple image grid) per scene
- Expose contact sheet URL in `get_state` query
- Test: QC fails a drift, retries succeed

**Deliverable:** QC agent runs, contact sheet viewable, scene approval works.

---

### Slice 3: Clips + ffmpeg + ElevenLabs
**Goal:** Full shot pipeline: clips generated, trimmed, mixed with audio.

- Real `submit_clip_job` + `await_job` (Seedance or Kling)
- `review_clip` with vision QC (motion stiffness, props, continuity)
- Real `trim_clips`, `render_edit` with ffmpeg
- Real `generate_voiceover`, `generate_music`, `mix_audio` via ElevenLabs MCP
- Moderation-block recovery: route wet/mud shots to Kling instead of Seedance
- Test: end-to-end mute → picture lock → final mix

**Deliverable:** A finished mixed video from 2–3 shots.

---

### Slice 4: Posting Queue + Run Reports + Cost Dashboard
**Goal:** Social posting, run reports, and cost tracking.

- Real `post_to_platform` for Instagram (or one test platform)
- Run report Activity: planned/done/failed/missing/credits per batch
- Cost dashboard: query credit ledger, compare estimate vs actual
- Low-balance warning Signal when balance < threshold
- Reconciliation: update estimator with actual vs estimate deltas
- Test: post approved, report generated, cost accurate

**Deliverable:** Posted video, run report, cost dashboard populated.

---

### Slice 5: (Optional) Hermes Front End
**Goal:** Chat-based operator surface for approvals and status.

- Hermes skill or MCP tool sends Temporal Updates
- Read run status via Queries
- Send daily status reports via `hermes send`
- Approve stills from Telegram/Slack

**Deliverable:** Approval workflow works from chat.

---

## Open Risks and Questions

### 1. **Headless MCP OAuth (CRITICAL)**
**Question:** Can a server-side Temporal worker hold and refresh Higgsfield's OAuth token for its remote MCP server?

**Context:** Higgsfield's MCP endpoint (`https://mcp.higgsfield.ai/mcp`) uses OAuth, initiated via sign-in in Claude or ChatGPT. Temporal workers run headless.

**Options:**
- If YES: Use MCP client in Activities as planned
- If NO: Use Higgsfield's API/SDK directly in Activities, keep MCP for agent-facing use

**Next step:** Test MCP auth flow with a headless worker before slice 1.

---

### 2. **LiteLLM Stability**
**Question:** Is LiteLLM (OpenAI Agents SDK's adapter for non-OpenAI models) stable enough for production QC?

**Context:** The QC vision model (Gemini or Claude) likely isn't an OpenAI model. LiteLLM is marked "beta" in the SDK docs.

**Options:**
- Use LiteLLM and test well
- Call the vision model API directly inside the QC Activity

**Next step:** Prototype `review_still` with both approaches in slice 2.

---

### 3. **Long Gates and History Size**
**Question:** Will very long episodes (days waiting at a gate) cause Temporal history bloat?

**Context:** Temporal workflows are event-sourced. Long waits and many shots could grow history size.

**Mitigation:** Use Continue-As-New for multi-hour or multi-day episodes, as the OpenAI Agents SDK integration docs recommend.

**Next step:** Monitor history size in slice 3; add Continue-As-New if needed.

---

### 4. **Postgres Migration Path**
**Current state:** SQLite for ledgers (dev-friendly, swappable).

**Question:** When to migrate to Postgres?

**Answer:** When:
- Multiple workers need shared state (concurrency > 1)
- Ledger queries become slow (>10k rows)
- Production deployment requires HA

**Mitigation:** Ledger interface is already async and swappable. Migration is a config change + schema port.

---

### 5. **Credit Reconciliation Logic**
**Current state:** Estimate deducted on submit; actual cost from provider known later.

**Question:** How to reconcile estimate vs actual and feed back into the estimator?

**Next step:** In slice 4, query Higgsfield `transactions` and ElevenLabs usage after each job completes. Log delta, adjust estimator weights per model.

---

## Architecture Notes

### Why Temporal + OpenAI Agents SDK?

Per the FRAMEWORK-COMPARISON doc (section 5):

- **Temporal** solves the hardest reliability problem: long jobs that die silently. Activities heartbeat, and the server watches them. If a heartbeat is late, the job is retried. State survives crashes.
- **OpenAI Agents SDK** has first-party Temporal integration (GA since March 2026). Model calls become durable Activities. MCP, HITL, and handoffs all work inside workflows.
- **MCP** servers for Higgsfield and ElevenLabs exist. Temporal's docs say MCP durability is external, so we key our own idempotency off provider job IDs.

### Pipeline Stage → Stack Mapping (from FRAMEWORK-COMPARISON section 5.2)

| Stage | Temporal construct | Agent/tool | Gate |
|-------|-------------------|-----------|------|
| Read-back | `EpisodeWorkflow` step | Writer agent (stubbed) | ✋ `approve_readback` Update |
| Script | Activity (stubbed) | Writer agent | Loop back on fail (capped) |
| Character locks | Activity (stubbed) | Director agent → Higgsfield | ✋ Lock approval → registry write |
| Storyboard | `EpisodeWorkflow` step | Director (stubbed) | ✋ `approve_storyboard` (shows estimate + balance) |
| Stills | `ShotWorkflow` per shot (fan-out) | Director prompt → Higgsfield | `submit_job` + `await_job` Activities |
| `still_review` | Activity → QC agent | Vision model (LiteLLM) | Retry cap; rubric failures fed to retry prompt |
| Human still review | Parent waits on `approve_scene_stills(scene_id)` | Contact-sheet UI | Batched per scene; only flagged stills escalated |
| Clips | Same `submit_job`/`await_job` on video queue | Director picks model from card | Heartbeat timeout ≈ 2–3× poll interval |
| `clip_qc` | Activity: sample frames + QC agent | Same rubric | Fail → retry (cap) → route back per spec |
| Mute edit | `media` Activities | Editor agent outputs EDL | ✋ Optional picture-lock review |
| Voice/SFX/music | `elevenlabs` Activities | Sound agent prompts | Voice locked at storyboard |
| Final mix | `media` Activity | — | ✋ `approve_final` |
| Post approval | `PostingWorkflow` | Agent drafts captions | ✋ Every post approved via batched queue |

---

## Measuring Success

From milestone 1 forward, the harness logs these metrics (per REVIEW.md "Measurable targets"):

| Metric | Target (starting) | How logged |
|--------|-------------------|-----------|
| Manual revision rounds | 0 | Episode result + run report |
| First-pass still rate | Tracked, rising | QC pass/fail per shot |
| QC escape rate | Near 0 | User flags that QC passed |
| Cost per finished second | Tracked per format | Credit ledger ÷ final duration |
| Estimate accuracy | Within ±20% | Actual cost ÷ storyboard estimate |
| Silent failures | 0 | Heartbeat timeouts + stall detection |

These are queryable from the credit and job ledgers.

---

## Summary

**Milestone 1 is complete.** The skeleton runs end to end in dry-run mode. All gates, heartbeats, retries, and error classes are in place. Tests pass.

**Next:** Slice 1 (real Higgsfield stills + kill-a-worker recovery test). Resolve the headless MCP auth question first.

**Open questions:** Headless MCP OAuth, LiteLLM stability, Postgres migration timing, history size for long episodes.

**The architecture is sound.** Temporal handles the reliability layer. The agent SDK handles judgment. MCP connectors go in Activities. The ledger tracks everything.

**Ready to build.**

---

## Milestone 2: Production Readiness for Episode 4

**Goal**: Prepare the harness for its first real production run of episode 4.  
**Target Date**: October 5, 2026  
**Status**: In Progress

### ✅ Completed

1. **Provider Authentication Research** (`docs/PROVIDERS.md`)
   - Higgsfield: Direct REST API with `HIGGSFIELD_API_KEY`
   - ElevenLabs: Standard API key via `ELEVENLABS_API_KEY`
   - Both support headless server workflows
   - No interactive OAuth required
   - Decision: API key authentication approved

2. **Provider Adapters** (`hfvg/providers/`)
   - `HiggsfieldProvider`: Image/video generation with cost estimates
   - `ElevenLabsProvider`: Audio generation (TTS, SFX, music)
   - Both default to `DRY_RUN=true` (zero credits spent)
   - Mock responses in dry-run mode with realistic job IDs
   - Cost estimation from PIPELINE-LESSONS.md model card
   - 8 provider tests passing

3. **Canary CLI Command** (`hfvg canary`)
   - Generate ONE draft still with real API (manual only)
   - Requires `DRY_RUN=false` explicitly set
   - Shows cost estimate and balance before generation
   - Confirmation prompt before spending credits
   - NOT run in CI, manual testing only

4. **Asset Registry** (`hfvg/registry.py`)
   - Load CHARACTER-LOCK.md for series identity
   - Load refs/apparel/<lock-id>/ for locked assets
   - Load per-episode refs/ and envs/ plates
   - Validate shots only reference locked assets
   - Track media IDs for idempotency
   - Support for superseded version detection

5. **Playbook Rule Validators** (`hfvg/playbook.py`)
   - Credit cap enforcement (§3.5)
   - Draft-first tier validation (§5.6)
   - Same-resort environment check (§6)
   - Duck role validation (§5)
   - No upright/carrying poses (§7)
   - Moderation-safe routing recommendations (§3.2)

### 🚧 In Progress

6. **Episode Input Parsing**
   - Load BRIEF/BEATMAP format docs
   - Parse beat maps with transitions
   - Extract character counts, duck roles, tags
   - Generate shot plans from beats

7. **Integration with Existing Activities**
   - Wire provider adapters to submit_job/await_job
   - Add registry validation to shot planning
   - Add playbook validation to pre-submit checks

8. **CI/Testing**
   - Full test suite with mocked providers
   - No real API calls in CI
   - Documentation tests

---

## Episode 4 Readiness Checklist

This is the **order of first real production run** for episode 4 of "Mid-Mountain Rest / Quacked Concierge Peak Glow Up".

### Phase 0: Setup (Travis)

**What Travis Must Provide:**

1. **API Credentials**
   ```bash
   export HIGGSFIELD_API_KEY=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   export ELEVENLABS_API_KEY=sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   - Higgsfield API key from: Dashboard → Account → API Keys
   - ElevenLabs API key from: Profile Settings → API Key
   - Store in `.env` file (not committed) or environment

2. **Credit Balance**
   - Higgsfield: ≥1,500 credits (hard cap for Ep04)
   - ElevenLabs: ≥4,000 credits (audio after picture lock)
   - Check balances: `hfvg canary` (aborts before spending)

3. **Series Folder Structure**
   ```
   series/
   ├── CHARACTER-LOCK.md              # Series identity lock
   ├── refs/
   │   ├── apparel/
   │   │   ├── concierge-mallard-tuque-v3/
   │   │   │   ├── AB02.png ... AB05.png
   │   │   │   └── media-ids.json
   │   │   └── [other locked apparel]/
   │   └── world/
   │       └── media-ids.json         # Resort frames (Ep01/Ep02)
   └── logo/
       └── locked/
           └── logo.png
   ```
   - Set `SERIES_PATH=/path/to/series` in environment

4. **Episode 4 Folder Structure**
   ```
   ep04-<guests>/
   ├── BRIEF.md                       # Episode brief (like Ep03)
   ├── BEATMAP.md                     # Beat-by-beat shot plan
   ├── CONTINUITY.md                  # Per-room continuity bible
   ├── CREDIT-PLAN.md                 # Budget breakdown
   ├── STATUS.md                      # Production tracking
   ├── refs/
   │   ├── guests/                    # Guest character refs
   │   └── props/                     # Episode-specific props
   └── envs/
       └── [room plates]              # Episode environment plates
   ```
   - Set `EPISODE_PATH=/path/to/ep04-<guests>` in environment

### Phase 1: Canary Test (Manual)

**Before any production work, verify setup:**

```bash
# Test 1: Dry-run mode (no credits)
DRY_RUN=true hfvg canary

# Test 2: Check balance
DRY_RUN=false hfvg canary
# (abort at confirmation prompt)

# Test 3: Generate ONE draft still (1 credit)
DRY_RUN=false hfvg canary \
  --model gpt_image_2 \
  --resolution 1k \
  --quality medium \
  --prompt "A cozy mountain resort lobby with timber beams"
# Confirm when prompted

# Expected: Job completes successfully, URL returned, 1cr deducted
```

**Success Criteria:**
- Balance check works
- Cost estimate matches (1.0cr for 1k medium)
- Job submits and completes
- Output URL accessible
- Balance decremented correctly

**On Failure:**
- Check `HIGGSFIELD_API_KEY` is set and valid
- Check API key has not expired
- Check account has credits
- Check network can reach api.higgsfield.ai

### Phase 2: Episode 4 Document Prep (Travis)

**Before starting the harness:**

1. **Write Episode 4 Docs**
   - `BRIEF.md`: Story, characters, climax, VO outline
   - `BEATMAP.md`: Shot-by-shot plan with transitions, tags, duck roles
   - `CONTINUITY.md`: Room masters, prop counts, costume per character
   - `CREDIT-PLAN.md`: Budget breakdown using Ep03 template

2. **Lock Episode 4 Guest Characters**
   - Generate guest identity refs (2k high)
   - Create head sheets and turnarounds
   - Lock in `ep04-<guests>/refs/guests/GUEST-LOCK.md`
   - Generate height board with duck + guests

3. **Generate Room Plates**
   - Use series world refs (ONE-RESORT RULE §6)
   - QC each plate side-by-side vs resort frames
   - Save as `ep04-<guests>/envs/ROOM01.png` etc.

### Phase 3: Pre-Production (G1 in Credit Plan)

**Harness runs in draft mode:**

```bash
# Start episode from docs
hfvg start-episode \
  --episode-id ep04-<guests> \
  --episode-path ./ep04-<guests> \
  --series-path ./series \
  --platforms instagram

# Wait for pre-production refs gate
hfvg query-state ep04-<guests>
```

**What Runs:**
1. Parse BRIEF + BEATMAP
2. Load asset registry
3. Validate shot references
4. Generate character ABs (2k high)
5. Generate height board
6. QC character drift
7. Report pre-production cost

**Manual Approval:**
- Travis reviews character ABs
- Travis approves or requests regeneration
- Once approved: `hfvg approve ep04-<guests> pre-production`

### Phase 4: Still Drafts (G2 in Credit Plan)

**Harness generates draft stills (1k medium, 1cr each):**

```bash
# Still drafts run automatically after pre-production approval

# Monitor progress
hfvg query-state ep04-<guests>

# Wait for draft strip
hfvg query-strip ep04-<guests>
```

**What Runs:**
1. Generate draft stills (1k medium)
2. QC each draft (character drift, headcount, height, duck role, props)
3. Auto-retry failures (max 2 retries per shot)
4. Assemble contact strip in edit order
5. Annotate cuts (last frame A vs first frame B)

**Manual Approval:**
- Travis reviews draft strip
- Travis marks shots for revision or approval
- `hfvg approve ep04-<guests> stills`

### Phase 5: Final Stills (G3 in Credit Plan)

**Harness generates finals (2k high, 6.5cr each):**

```bash
# Finals run automatically after still approval
# Only approved shots rendered at final resolution
```

**What Runs:**
1. Render finals for approved shots only
2. QC finals (texture, small props, text legibility)
3. Report cost

### Phase 6: Video Drafts (G4 in Credit Plan)

**Harness generates draft videos:**

```bash
# Video drafts run automatically after final stills
```

**What Runs:**
1. Route shots by content:
   - Wet/mud/pool → Kling 3.0 pro (6cr)
   - Dry/complex → Seedance 480p draft (3cr/s)
2. QC drafts (motion, props, continuity)
3. Auto-retry failures
4. Assemble draft mute

**Manual Approval:**
- Travis reviews draft mute shot-by-shot
- `hfvg approve ep04-<guests> video-drafts`

### Phase 7: Finals & Finalize (G5 in Credit Plan)

**First: Finalize pilot to verify motion match:**

```bash
# Pilot: one shot finalized to test matching
# 60cr total (12cr draft + 48cr finalize)
```

**If pilot matches 1:1:**
- Finalize approved hero shots (~2 shots)
- Upscale remaining Seedance drafts
- Assemble picture lock mute

**Manual Approval:**
- Travis reviews final mute
- `hfvg approve ep04-<guests> picture-lock`

### Phase 8: Audio (G6 in Credit Plan)

**After picture lock, generate audio:**

```bash
# Audio runs automatically after picture lock
```

**What Runs:**
1. Generate VO (Japanese, Sora voice)
2. Generate BGM (reuse series beds)
3. Generate SFX
4. Mix audio to picture
5. Burn hard subs

**Manual Approval:**
- Travis reviews mixed episode
- `hfvg approve ep04-<guests> mix`

### Phase 9: Hand-Off Package

**Harness does NOT post. Instead:**

```bash
# Generate hand-off package
hfvg export ep04-<guests>
```

**Package Contents:**
1. Final mixed video (MP4)
2. Locked still frames
3. Credit log (actual spend)
4. Asset manifest
5. Distribution metadata

**Travis distributes manually** (never automated posting per hard rule).

---

## Development Workflow

### Local Development
```bash
# Clone repo
git clone <repo-url>
cd hands-free-video-generator

# Install dependencies
pip install -e .

# Run in dry-run mode (no credits)
export DRY_RUN=true
hfvg start-episode --episode-id test --idea "Test" --platforms instagram

# Run tests
pytest tests/ -v
```

### Environment Variables Summary

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DRY_RUN` | No | `true` | Dry-run mode (no real API calls) |
| `HIGGSFIELD_API_KEY` | For real gen | - | Higgsfield API key |
| `ELEVENLABS_API_KEY` | For real gen | - | ElevenLabs API key |
| `SERIES_PATH` | No | `./series` | Path to series folder |
| `EPISODE_PATH` | No | `./episode` | Path to episode folder |
| `TEMPORAL_HOST` | No | `localhost:7233` | Temporal server |
| `TEMPORAL_NAMESPACE` | No | `default` | Temporal namespace |

### Public Repository Safety

**NO secrets committed:**
- All API keys from environment only
- `.env` in `.gitignore`
- No reference images with private metadata
- Config files point at local paths only

**CI Safety:**
- `DRY_RUN=true` hardcoded in CI
- No API keys in GitHub Secrets
- Mocked providers for all tests
- Zero credits spent in CI

---

## Production Budget (Episode 4)

Based on PIPELINE-LESSONS.md §5.7 and Ep03 CREDIT-PLAN:

| Phase | Higgsfield | ElevenLabs |
|-------|------------|------------|
| Pre-production refs | ≤350cr | - |
| Still drafts | ≤120cr | - |
| Final stills | ≤260cr | - |
| Video drafts | ≤440cr | - |
| Finals/finalize | ≤120cr | - |
| Reserve | ≤100cr | - |
| **Total Higgsfield** | **≤1,500cr** | - |
| Audio (after picture lock) | - | ≤4,000cr |

**Hard caps:**
- Higgsfield: 1,500cr (stop at 80% per line)
- ElevenLabs: 4,000cr
- Revision budget: ≤25% of spend

---

## Next Steps (Completing Milestone 2)

1. ✅ Provider auth research → `docs/PROVIDERS.md`
2. ✅ Provider adapters → `hfvg/providers/`
3. ✅ Canary CLI → `hfvg canary`
4. ✅ Asset registry → `hfvg/registry.py`
5. ✅ Playbook validators → `hfvg/playbook.py`
6. 🚧 Episode input parser → `hfvg/episode.py`
7. 🚧 Integration tests with mocks
8. 🚧 Full test suite passing
9. 🚧 CI green
10. 🚧 Open PR #2

**Target**: Ready by October 5, 2026 for Episode 4 production.
