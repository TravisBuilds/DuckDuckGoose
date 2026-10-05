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
