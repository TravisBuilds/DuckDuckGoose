# Architecture

## System Overview

The Hands-Free Video Generator is a **workflow orchestration harness** built on Temporal that coordinates multiple AI providers to transform story ideas into finished videos.

```mermaid
graph TB
    subgraph "Client Layer"
        Web[Web UI<br/>Next.js]
        CLI[CLI<br/>Python Click]
    end
    
    subgraph "API Layer"
        API[FastAPI Server<br/>api/main.py]
    end
    
    subgraph "Orchestration Layer"
        Temporal[Temporal Server]
        Worker[Worker<br/>hfvg/worker.py]
    end
    
    subgraph "Workflow Layer"
        EpV2[EpisodeWorkflowV2<br/>Main Pipeline]
        Shot[ShotWorkflow<br/>Per-Shot Child]
        EpV2 --> Shot
    end
    
    subgraph "Activity Layer"
        G1[G1 Plan<br/>Estimates]
        Preflight[Preflight<br/>Validation]
        Still[Still Activity<br/>submit/poll]
        Clip[Clip Activity<br/>submit/poll]
        QC[QC Activity<br/>review]
        Audio[Audio Activities<br/>VO/Music/SFX]
    end
    
    subgraph "Provider Layer"
        HF[Higgsfield<br/>Still/Video]
        EL[ElevenLabs<br/>Audio]
        Human[Human review<br/>live QC escalation]
    end
    
    subgraph "Data Layer"
        DB[(SQLite<br/>studio.db)]
        FS[Filesystem<br/>Episodes/Assets]
        Registry[Model Caps<br/>Registry JSON]
    end
    
    Web --> API
    CLI --> API
    API --> Temporal
    Temporal --> Worker
    Worker --> EpV2
    EpV2 --> G1
    EpV2 --> Preflight
    Shot --> Still
    Shot --> Clip
    Shot --> QC
    EpV2 --> Audio
    
    G1 --> HF
    G1 --> EL
    Still --> HF
    Clip --> HF
    QC --> Human
    Audio --> EL
    
    G1 --> Registry
    Preflight --> Registry
    Still --> DB
    Clip --> DB
    API --> DB
    Worker --> FS
    
    style EpV2 fill:#e1f5ff
    style Shot fill:#e1f5ff
    style G1 fill:#fff4e1
    style Preflight fill:#fff4e1
    style DB fill:#e8f5e9
```

## EpisodeWorkflowV2 Pipeline

```mermaid
stateDiagram-v2
    [*] --> Init: start_v2
    Init --> G1_Plan: parse BEATMAP.md
    
    G1_Plan --> G1_Preflight: compute credits
    G1_Preflight --> G1_Wait: preflight passed
    G1_Wait --> Refs: G1 approved
    
    Refs --> Stills: generate refs
    Stills --> G2_Wait: launch shot workflows
    
    G2_Wait --> Clips: G2 approved
    Clips --> Edit: await all clips
    Edit --> Audio: trim/QC
    Audio --> G_Final_Wait: generate VO/music/SFX
    
    G_Final_Wait --> Report: G_FINAL_CUT approved
    Report --> [*]: complete
    
    note right of G1_Wait
        Gate 1: Shot Plan
        - Beatmap + credit plan
        - Preflight errors block
    end note
    
    note right of G2_Wait
        Gate 2: Scene Stills
        - All stills landed + QC'd
        - Pending retries block
    end note
    
    note right of G_Final_Wait
        Gate 3: Final Cut
        - Picture lock + audio
    end note
```

### G1_SHOT_PLAN: Shot Plan Gate

**Inputs:**
- Parsed beatmap (shot list)
- Credit plan (per-shot + total estimates)
- Preflight validation results

**Validation:**
1. **Preflight errors refuse** (no override) - shots must pass model limits
2. **Live mode + estimates unavailable** refuses (unless `override_unavailable_estimates=true`)

**On approval:**
- Marks G1.08 credit plan approved (DB flag)
- Proceeds to reference generation

**Implementation:** `EpisodeWorkflowV2.approve_g1_shot_plan()` with validator

### G2_SCENE_STILLS: Scene Stills Gate

**Inputs:**
- All shot start stills (QC passed or escalated+approved)
- All shot end stills (for end-frame shots)
- Shot completion status

**Validation:**
1. **Pending retries refuse** - unreviewed retry stills must land first
2. All stills must be recorded (needs_review or still_complete)

**On approval:**
- Signals all shot workflows to proceed to clips
- Marks shots as approved in DB

**Implementation:** `EpisodeWorkflowV2.approve_g2_scene_stills()` with validator

**Safety design:**
- Retry children send `retry_still_ready` signal after stills land
- Validator refuses while `pending_retries` non-empty
- Prevents unreviewed retry stills from going to paid clips

### G_FINAL_CUT: Final Cut Gate

**Inputs:**
- Picture lock (all clips QC'd)
- Audio mix complete

**On approval:**
- Generates run report
- Marks episode complete

**Implementation:** `EpisodeWorkflowV2.approve_g_final_cut()` (signal, no validator)

## ShotWorkflow

Per-shot child workflow that handles:
1. **Start still**: submit → poll → QC → record
2. **End still** (if `end_frame: true`): submit → poll → record
3. **G2 wait**: parent approval before clips
4. **Clip**: submit → poll → QC → record

```mermaid
stateDiagram-v2
    [*] --> Preflight_Still: run(shot_plan)
    
    Preflight_Still --> Submit_Still: checks pass
    Submit_Still --> Poll_Still: reserve + submit
    Poll_Still --> QC_Still: completed
    
    QC_Still --> End_Still: passed, end_frame=true
    QC_Still --> Escalated: passed=false, escalate=true (live)
    QC_Still --> Failed: passed=false, escalate=false
    
    End_Still --> Record_Stills: end still completed
    Escalated --> Record_Review: mark needs_review
    Record_Review --> Notify_Parent: retry_still_ready signal
    Notify_Parent --> Human_Review: wait for human approval
    Human_Review --> Record_Approved: approved
    
    Record_Stills --> Notify_Parent2: retry_still_ready (retries only)
    Record_Approved --> Notify_Parent2
    Notify_Parent2 --> G2_Wait
    
    G2_Wait --> Preflight_Clip: stills_approved signal
    
    Preflight_Clip --> Submit_Clip: checks pass
    Submit_Clip --> Poll_Clip: reserve + submit
    Poll_Clip --> QC_Clip: completed
    
    QC_Clip --> Record_Clip: passed
    Record_Clip --> [*]: completed
    
    QC_Clip --> Escalated_Clip: escalate (live)
    Escalated_Clip --> [*]: needs_review
    
    Failed --> [*]: failed
    
    note right of Notify_Parent
        Retry signal sent AFTER
        stills land, BEFORE G2 wait.
        Unblocks G2 approval.
    end note
    
    note right of G2_Wait
        Clips NEVER start before
        parent G2 approval.
        Safety: unreviewed stills
        never go to paid clips.
    end note
```

## Budget Ledger

**Design:** Atomic SQL reserves prevent overspend

```mermaid
sequenceDiagram
    participant Activity
    participant Ledger
    participant DB
    participant Provider
    
    Activity->>Ledger: reserve(amount)
    Ledger->>DB: UPDATE ... WHERE spent+reserved+amount <= cap
    DB-->>Ledger: rows_affected=1 (success) or 0 (cap hit)
    alt past stop (returns False) or over a cap / GC.01 headroom (raises ValueError)
        Ledger-->>Activity: False / ValueError
    else success
        Ledger-->>Activity: job_id
        Activity->>Provider: submit(job_id, idempotency_key)
        Provider-->>Activity: provider_job_id
        Activity->>Ledger: persist_provider_job(job_id, provider_job_id)
        Activity->>Ledger: poll_job_status(job_id)
        Provider-->>Activity: completed + cost
        Activity->>Ledger: commit(job_id, actual_cost)
        Ledger->>DB: UPDATE spent += actual, reserved -= reserved
    end
```

**Atomic reserve SQL:**
```sql
UPDATE budget_lines
SET reserved_usd_micros = reserved_usd_micros + ?
WHERE line_id = ?            -- "{episode_id}:{line_name}"
  AND (spent_usd_micros + reserved_usd_micros + ?) <= stop_usd_micros
```

**Defaults (hfvg/budget.py, docs/DOLLAR-LEDGER.md):** episode cap $60.00, episode stop $48.00 (80%),
GC.01 headroom $5.00, revision reserve $10.00 (`L7_revision`, outside the episode cap/stop, stop == cap).

**Key properties:**
- **Stop / cap**: Reserve only succeeds if `spent + reserved + amount <= stop` (80% of the line cap) and the episode total stays at or under the $48.00 episode stop; past a stop `reserve()` returns `False`, over the episode cap or a line cap it raises `ValueError`
- **Idempotent**: Same `job_id` (shot_id + version + suffix) → same provider idempotency key
- **Reconciliation**: Poll exhaustion keeps reservation, marks `pending_reconcile` (never releases)
- **Release on failure**: Confirmed provider failure (failed/blocked/canceled) releases reservation

## Model Capabilities Registry

**File:** `hfvg/data/model_caps.json`

**Schema:**
```json
{
  "schema_version": 1,
  "checked": "2026-10-19",
  "recheck_interval_days": 30,
  "precedence": "...stricter of Higgsfield and upstream vendor...",
  "shared": { ... },
  "models": {
    "xai/grok-imagine-image-2.0": {
      "kind": "image",
      "provider": "higgsfield",
      "status": "active_default_still",
      "aliases": ["grok", "grok_image_2_0", "grok-imagine-image-2.0"],
      "limits": {
        "max_refs": {"value": 5, "basis": "stricter_upstream", "sources": ["xai_multi", "hf_grok", "..."], "higgsfield": 10, "upstream": 5},
        "prompt_max_chars": {"value": null, "basis": "unknown", "sources": ["hf_grok", "xai_prompting", "obs_ep05"]},
        "aspect_ratios": {"value": ["auto", "1:1", "1:2", "2:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"], "basis": "higgsfield", "sources": ["hf_grok", "hf_mcp_grok"]}
      }
    }
  },
  "sources": {
    "hf_grok": {
      "url": "https://docs.higgsfield.ai/docs/models/grok-image-2/generate-and-edit",
      "publisher": "Higgsfield",
      "checked": "2026-10-19"
    }
  }
}
```

**Preflight checks:**
1. **G1 plan** - all shots, all phases (start/end/clip)
2. **Submit activities** - defense-in-depth before reserve/submit

**Enforcement:**
- **Errors refuse** (no retries) - PreflightError is non-retryable
- **Warnings log** (e.g. deprecated models, quality tier mappings)
- **G1 approval validator refuses** while preflight errors exist

## Worker Registration

**File:** `hfvg/worker.py`

Activities registered (`PRODUCTION_ACTIVITIES`):
- Budget (dollar ledger): `init_episode_budget_lines_activity`, `commit_job_budget`, `release_job_budget`, `mark_job_pending_reconcile` (reserves happen inside the `*_enforced` submit activities)
- Credit (legacy): `init_episode_credit_line_activity`, `reserve_credits_activity`, `commit_credits_activity`, `release_credits_activity`, `update_credit_cap_activity`
- Generation: `submit_still_job_enforced`, `submit_clip_job_enforced`, `await_job_enforced`, `poll_job_status`
- QC: `precheck_still_qc`, `precheck_clip_qc`, `review_still`, `review_clip`
- G1 / live: `compute_g1_plan_activity`, `approve_g108_activity`, `check_live_mode_and_g108`, `approve_g2_shots`
- Preflight: `preflight_validate_episode_activity`
- Refs: `load_refs_to_generate_activity`, `generate_ref_activity`, `recheck_refs_activity`, `list_anchor_stills`
- Assets: `save_asset_locally`, `record_shot_result`, `upsert_episode_activity`
- Edit / audio / posting: `trim_clips`, `render_edit`, `generate_voiceover`, `generate_music`, `generate_sfx`, `mix_audio`, `post_to_platform`, `generate_run_report`
- Workflow support: `parse_beatmap_activity`, `load_gate_policy_activity`, `verify_episode_files_activity`
- Run lock: `acquire_run_lock`, `release_run_lock`
- Retries: `resolve_landed_start_still` (end-frame backfill)

Workflows registered:
- `EpisodeWorkflow` (legacy; target of `hfvg approve`)
- `EpisodeWorkflowV2`
- `ShotWorkflow`
- `PostingWorkflow` (stub)

## Data Model

### Database Schema (SQLite)

Abridged from `hfvg/studio_db.py` and `hfvg/budget.py` (other tables: `audit_log`,
`budget_transactions`, `manual_balance`, `sessions`, `run_locks`, `upload_cache`,
`smoke_gate_overrides`, plus the legacy credit tables).

**episodes** (`hfvg/studio_db.py`)
```sql
CREATE TABLE episodes (
    episode_id TEXT PRIMARY KEY,
    beatmap_path TEXT,
    live_mode INTEGER DEFAULT 0,  -- 0=dry, 1=live
    g108_approved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
```

**budget_lines** (`hfvg/budget.py`)
```sql
CREATE TABLE budget_lines (
    line_id TEXT PRIMARY KEY,        -- "{episode_id}:{line_name}"
    episode_id TEXT NOT NULL,
    provider TEXT NOT NULL,          -- 'higgsfield' | 'elevenlabs'
    line_name TEXT NOT NULL,
    cap_usd_micros INTEGER NOT NULL,
    stop_usd_micros INTEGER NOT NULL,  -- 80% of cap (L7_revision: == cap)
    spent_usd_micros INTEGER NOT NULL DEFAULT 0,
    reserved_usd_micros INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
)
```

**job_ledger** (`hfvg/budget.py`)
```sql
CREATE TABLE job_ledger (
    job_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    shot_id TEXT,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,        -- 'draft' | 'final' | 'retry' | 'revision'
    line_name TEXT NOT NULL,
    usd_micros INTEGER NOT NULL,
    status TEXT NOT NULL,      -- e.g. 'reserved', 'committed', 'released', 'pending_reconcile'
    verdict TEXT,
    used INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

**shots** (`hfvg/studio_db.py`)
```sql
CREATE TABLE shots (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    still_url TEXT,
    clip_url TEXT,
    qc_results TEXT,
    retries INTEGER DEFAULT 0,
    prompt TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
```

**anchor_stills** (`hfvg/studio_db.py`)
```sql
CREATE TABLE anchor_stills (
    episode_id TEXT NOT NULL,
    anchor_shot_id TEXT NOT NULL,
    target_shot_id TEXT NOT NULL,
    local_path TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (episode_id, target_shot_id)
)
```

### Filesystem Layout

```
data/
├── studio.db                      # SQLite database
├── episodes/
│   ├── ep04/
│   │   ├── BEATMAP.md            # Shot list with prompts
│   │   ├── CONTINUITY.md         # Character/location descriptions
│   │   ├── shots.json            # Per-shot params (model, duration, refs)
│   │   ├── refs.json             # Reference image registry
│   │   ├── prompt_kit.json       # Lint rules, character codes, etc.
│   │   ├── stills/
│   │   │   ├── A01_v1.jpeg       # Start stills
│   │   │   ├── A01_end_v1.jpeg   # End stills
│   │   │   └── ...
│   │   ├── clips/
│   │   │   ├── A01_v1.mp4
│   │   │   └── ...
│   │   └── refs/
│   │       ├── duck-profile.jpeg
│   │       └── ...
│   └── ep05/
│       └── ...
├── normalized/                    # Normalized reference cache
│   └── <sha256>_<size>.jpeg
└── lead_refs/                     # Downloaded start stills used as lead refs ($DATA_DIR)

(The Higgsfield upload URL cache is the `upload_cache` table in `studio.db`.)
```

## Retry System

### Shot Retry

**Endpoint:** `POST /api/episodes/{id}/shots/{shot_id}/retry` (CLI: `hfvg retry-shot <episode_id> <shot_id>`)

**Workflow:** `EpisodeWorkflowV2.retry_shot()` update handler

**Flow:**
1. Validator checks:
   - Parked at G1 or G2 (not after G2 approval)
   - Shot in `failed_shots` or previously retried
   - No pending retry for this shot
   - Under `MAX_SHOT_RETRIES` limit (default 1 retry = 2 total attempts)
2. Increment `shot_attempts[shot_id]`
3. Launch child with new workflow ID: `{episode}-shot-{shot_id}-retry{n}`
4. Add to `pending_retries` (blocks G2 until still lands)
5. Background task tracks completion, clears `pending_retries`

**Idempotency:**
- Each retry has unique workflow ID and version
- Job IDs include attempt: `still-{episode}-{shot}-v{version}` for the original, `still-{episode}-{shot}-r{attempt-1}` for retries (plus optional `-lead<sha8>` / `-anchor<sha8>`)
- Provider idempotency key includes attempt

### End-Frame Backfill

**Endpoint:** none yet (no API route or CLI command); call the workflow update from a Temporal client

**Workflow:** `EpisodeWorkflowV2.retry_end_still()` update handler

**Use case:** Shot's start still landed but _end failed

**Flow:**
1. Resolve landed start still (committed status)
2. Launch child `{episode_id}-shot-{shot_id}-end{n}` with `reuse_start_still` flag
3. Child skips start still submit (cost=0)
4. Generates _end still with start still as lead ref
5. Uses same attempt/version (deterministic job_id)

## Testing Strategy

### Test Pyramid

```
┌─────────────────────────┐
│   Red-green (219)       │  ← 215 mutations + 4 probes
├─────────────────────────┤
│  Integration Tests      │  ← Real Temporal workflows
│  (pytest, time-skip)    │
├─────────────────────────┤
│   Unit Tests            │  ← Fast, no external deps
│  (pytest)               │
└─────────────────────────┘
```

**Mutation testing:** `./scripts/redgreen.sh`
- 219 red-green tests: 215 numbered mutations (M-1..M-218) + 4 harness probes; 3 skipped (M-23, M-24, M-93)
- Each mutation must cause test failure (red)
- Restoration must pass test (green)
- Classifies failures (AssertionError vs other)

**Integration tests:** `pytest`
- Real Temporal workflows with time-skipping
- SQLite in-memory or temp file
- Mocked provider APIs (respx)

**Smoke tests:** `pytest tests/test_smoke_test.py`
- No Temporal required
- Smoke-test activity end to end with Higgsfield mocked (respx): success, NSFW, canceled, pre-submit exception, post-submit timeout

## Security & Safety

### Budget Safety

1. **Atomic SQL WHERE clause** - cap checked in UPDATE, not before
2. **Stop threshold** - $48.00 (80% of the $60.00 cap) blocks new reserves; revision reserve $10.00 sits outside the cap
3. **Idempotent keys** - retry never double-charges
4. **Poll exhaustion** - keeps reservation, never releases
5. **GC.01 headroom** - $5.00 balance required after holds

### Auth

- **API:** `Authorization: Bearer {ADMIN_SECRET}` header (min 32 chars) or the httpOnly session cookie set by `POST /api/login`
- **Fail closed:** No default secret, server refuses to start
- **Session:** Stored in DB, 24h expiry

### Content Policy

- **No named IP** - no franchises, brands, real people
- **Prompt lint** - forbids filter evasion, brand mentions
- **Moderation** - provider-level content filters

### Live Mode Gates

1. **DRY_RUN check** - activities refuse live calls in dry mode
2. **G1.08 check** - generation refuses without credit plan approval
3. **Preflight** - refuses before spend if model limits broken
4. **QC escalation** - live mode escalates to human, never auto-passes

## Monitoring & Observability

### Temporal Visibility

- **Workflow ID format:** episode workflow = bare `{episode_id}` (e.g. `ep04`; `ep04-episode` is accepted and normalized by the API), shots `{episode_id}-shot-{shot_id}`, retries `...-retry{n}`, end-still backfill `...-end{n}`
- **Search attributes:** none custom
- **Queries:** `get_state()` exposes current stage, gates, shot status

### Logging

- **logging / structlog** - Python logging in the worker, structlog in config
- **Provider evidence** - raw response bodies + correlation IDs on failures
- **Audit trail** - budget reserves, commits, releases logged

### Metrics (Future)

- Credits spent per episode/shot
- Generation success rate
- Gate approval latency
- Provider failure rates
- QC escalation rates

## Deployment

### Production Checklist

- [ ] Set `ADMIN_SECRET` (min 32 chars, from secrets manager)
- [ ] Set provider API keys (`HIGGSFIELD_API_KEY`, etc.)
- [ ] Configure `DATABASE_PATH` (persistent volume)
- [ ] Set `EPISODES_DIR` (persistent volume)
- [ ] Deploy Temporal server (prod cluster, not dev); set `TEMPORAL_ADDRESS` (API) and `TEMPORAL_HOST` (worker/CLI)
- [ ] Scale workers (multiple replicas)
- [ ] Set `DRY_RUN=false` for live mode (and enable each episode with `POST /api/episodes/{id}/set-live`)
- [ ] Configure budget caps (`EPISODE_CAP_USD`, `EPISODE_STOP_PERCENT`, `GC01_HEADROOM_USD`, `REVISION_RESERVE_USD`)
- [ ] Set up monitoring (Temporal Cloud or self-hosted)
- [ ] Configure backup/restore for SQLite
- [ ] Set `G1_ESTIMATE_PLACEHOLDER_IMAGE_URL` for live clip estimates

### Scaling

- **Workers:** Horizontal - launch multiple worker processes
- **Database:** SQLite → PostgreSQL for multi-writer (requires migration)
- **Temporal:** Self-hosted cluster or Temporal Cloud
- **Provider concurrency:** Controlled by worker count + activity timeouts

## Future Architecture

See [ROADMAP.md](ROADMAP.md) for planned features:
- PostgreSQL support (multi-writer)
- OpenTelemetry tracing
- Per-user spend caps
- Webhook integrations
- Audio stage (PR #8)
