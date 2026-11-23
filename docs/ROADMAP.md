# Roadmap

Planned features and improvements for the Hands-Free Video Generator.

## Current Status (v0.1.0)

**✅ Completed:**
- Core pipeline (G1 → stills → G2 → clips → final)
- Budget ledger with atomic reserves
- Model capabilities registry with preflight
- Shot retry system
- End-frame generation
- Web UI (basic)
- CLI tools
- Audio stage: voiceover, SFX, music, mix and delivery QC before the final cut (PRs #17 and #18)
- Red-green mutation testing (783 tests: 778 mutations + 5 probes, as of 2026-11-23)
- Automated QC judges, off by default behind `HFVG_QC_JUDGES_ENABLED` ([QC-JUDGES.md](QC-JUDGES.md))

**🚧 In Progress:**
- Improved error messages

## Upcoming Features

### High Priority

#### 1. Automated QC judges (top priority, approved Nov 19 2026)

**Status:** Built, off by default (`HFVG_QC_JUDGES_ENABLED=false`, [QC-JUDGES.md](QC-JUDGES.md)).
With the flag off nothing is registered and every check gives the result the **Now** notes below
describe (new runs already send the still rubric, which nothing reads without a judge). With it on:

- (a) the still reviewer judges each live still against its prompt and references, which
  `ShotWorkflow` now sends in the rubric behind `workflow.patched("qc-still-rubric")`; a FAIL
  fails the shot for the bounded `retry_shot`, anything unsure goes to a person;
- (b) one headcount call per still or clip counts every cast entry the shot declares by its id, the
  other main characters as one group, and the figures nobody declared (unexpected, background); a
  counter error is NEEDS_REVIEW at G2, at recompose and in assembly QC;
- (c) one call per clip answers every `qc_fail_if` condition from six frames across the clip;
- (d) `hfvg/qc/identity.py` is one identity gate for every locked character, with criteria from the
  lock, real crops and the same signs for any kind of character; the first series' duck host gate
  is a compatibility shim over it;
- every judge works for any show (people, creatures, robots, puppets, props that act): no judge
  instruction, parser, cache key or budget assumes a kind of character or a look;
- cost: the provider and model from `HFVG_QC_JUDGE_MODEL` (OpenAI through `OPENAI_API_KEY`, the only
  judge client), a `qc_judges` budget line ($0.50, or `auto`: sized from the episode's shots and
  their locked characters) with reserve and commit around every call inside the $48 stop, the
  balance gate for the model's provider (unknown balance: the line bounds the spend), verdicts cached
  by content, and a "judge budget used up" reason when the line runs out;
- tests: fake-judge unit tests for every path, shows without a duck (`tests/test_qc_any_show.py`), a
  workflow test, a replay test of a run parked at review before the patch, red-green M-730 to M-758
  and M-764 to M-785.

Left: turning the flag on after a look at the first live verdicts, with the chosen model's prices in
`HFVG_QC_JUDGE_USD_PER_MTOK_IN` / `_OUT` (required for any model but the default); deciding whether
`auto` becomes the default line size; per-character identity rules in the cast library.

**Why:** Live QC has no vision judge yet. Motion QC and the stillgate measure each clip, but the
still reviewer, the counter and the clip checker below are interfaces with fail-closed wiring and
no real implementation, so every live still, every headcount and every live clip with `qc_fail_if`
conditions waits for a human. The identity gate is a duck-only scaffold that nothing calls.

**Rule for every judge:** a judge that is missing, unsure (below its confidence threshold), refused
by the budget or broken gives NEEDS_REVIEW (escalate to a human), never PASS. `DRY_RUN` calls no
model.

##### a. Still reviewer

- **Now:** `review_still` in `hfvg/activities/qc.py` passes every still in `DRY_RUN`. Live, it
  escalates when the episode can't be verified (no `episode_id`, unknown episode, not in live mode
  or G1.08 not approved, DB error), and otherwise escalates every still with "Real QC agent not
  implemented - escalate to human review". `hfvg/workflows/shot.py` then records the still as
  `needs_review` and waits for the human G2 approval. The workflow calls `review_still` with an
  empty rubric (`{}`), so the prompt and the references never reach the review.
- **Interface:** `review_still(asset_url, rubric, episode_id, shot_id)` returns `passed`, `issues`
  and `escalate`. The references are already in the shot plan (`hfvg/cast_plan.py`): the locked
  cast sheets as `cast:` ref keys plus `shot_meta.cast` (each character's locked description), and
  the place and prop photos with `shot_meta.props`. [CAST.md](CAST.md#extension-points-later-prs)
  names `review_still` as the place for this verdict.
- **Done when:**
  - a vision judge scores each live still against the shot's still prompt (after any moderation
    reword) and those character and setting references. The workflow passes them in the rubric,
    behind `workflow.patched(...)` so runs parked at review replay unchanged;
  - PASS is recorded as `still_complete`, like today's pass branch;
  - FAIL returns `escalate: False` with the judge's reasons, so the shot ends `failed` and is
    re-rolled through the bounded `retry_shot` (`MAX_SHOT_RETRIES`, the G2 round budget). Commit
    10e9fd2 (P0.1b) removed the automatic QC retry loop on purpose; bringing one back is a separate
    decision and must go through the guarded submit;
  - low confidence, no judge or key, a judge error or timeout, a malformed answer, or a missing
    still or reference escalates. The fail-closed episode checks stay in front of the judge.
- **Tests:**
  - fake-judge unit tests: PASS, FAIL, low confidence, error, timeout, malformed answer;
  - `tests/test_shot_live.py::test_qc_escalates_in_live_mode` (red-green M-31) keeps proving that
    live review without a judge escalates;
  - a workflow test that the rubric carries the prompt and references, and that a FAIL can be
    re-rolled with `retry_shot`; a replay test for a run parked at review before the patch;
  - red-green mutations for the new branches (a low-confidence verdict must never pass).

##### b. Headcount counter

- **Now:** with the flag off no counter is registered (`get_cast_counter()` is `None`), so counts
  come from a reviewer or the check is NEEDS_REVIEW. Two places use it:
  - `hfvg/qc/g2_headcount.py` counts, in each kept still, every figure the shot declares (each
    cast entry by its id, the other main characters as one group) and the figures nobody declared.
    A FAIL blocks G2 approval (409) unless it is overridden with a reason. Without a counter it uses
    a reviewer's recorded headcount (per figure id, or the first series' cast / duck / other form),
    else NEEDS_REVIEW. It runs on `GET /api/episodes/{id}/g2-headcount`, on every G2 approval and
    on the recompose headcount route.
  - `hfvg/qc/continuity.py` lists it as the `headcount` continuity check (hard by default), which
    `check_assembly_qc` runs on each shot of the mute edit. Without a counter it is NEEDS_REVIEW
    and holds `G_FINAL_CUT` until a passing rebuild or `accept_assembly_qc`.
- **Interface:** `CastCounter.count_cast(media_path, shot_context)` in `hfvg/qc/headcount.py`
  answers, per figure id, a count and whether it matches the declared character, plus unexpected
  and background figures, a confidence and a method; `FakeCastCounter` is for tests only;
  `set_cast_counter()` registers one per process. The first series' species interface
  (`AnimalCounter.count_animals` in `hfvg/qc/animal_count.py`) is an adapter of it.
- **Done when:**
  - a real counter implements `CastCounter` and is registered at startup in both processes that
    read it: the worker (assembly QC) and the API (G2 and recompose headcount);
  - a count below `DEFAULT_CONFIDENCE_THRESHOLD` (0.7) stays NEEDS_REVIEW and a counter error is
    NEEDS_REVIEW for that shot, never a 500 or a retry of the whole assembly QC activity that would
    repeat every paid count;
  - a reviewer's recorded headcount still wins over the model (`ReviewerHeadcountCounter`);
  - the counter knows who is declared: it gets the shot's cast entries, their descriptions and
    references, and counts any kind of character.
- **Tests:**
  - registration: a counter when one is configured, NEEDS_REVIEW (as now) when not;
  - low confidence and counter errors are NEEDS_REVIEW in the G2 headcount, the recompose
    headcount and assembly QC; a reviewer count beats the model;
  - a figure that may not be the declared character is NEEDS_REVIEW at G2 and in assembly QC;
  - keep `tests/test_g2_headcount.py` and `tests/test_picture_assembly_round3.py::TestAnimalChecks`
    (M-273) passing; add mutations for low confidence or an error turning into PASS.

##### c. Clip "fail if" checker

- **Now:** no production checker ships or is registered (`get_constraint_checker()` is `None`),
  so every live clip of a shot that declares `qc_fail_if` in shots.json is NEEDS_REVIEW once
  motion QC and the stillgate pass. A reviewer keeps it (`accept_clip_review`) or re-rolls it
  (`retry_clip`). Dry-run clips record their conditions as DRY_RUN rows.
- **Interface:** `ClipConstraintChecker.check(media_path, constraint, shot_context)` in
  `hfvg/qc/clip_constraints.py` returns `violated`, `confidence` and `detail`;
  `FakeClipConstraintChecker` is for tests only. `review_clip` (`hfvg/activities/qc.py`) downloads
  the clip and calls `evaluate_clip_constraints` with the shot's prompt and duration, one checker
  call per declared condition.
- **Done when:**
  - a real vision checker answers each condition from frames sampled across the whole clip, and
    is registered at worker startup;
  - it keeps `DEFAULT_CONFIDENCE_THRESHOLD` (0.7) and the fail-closed rules of
    `evaluate_clip_constraints`: no checker, a missing file, a checker error or low confidence is
    NEEDS_REVIEW; a violated condition is FAIL, re-rolled only through the bounded `retry_clip`;
  - frames are extracted through `hfvg.subproc`, like `review_clip`'s other ffmpeg calls, not with
    a blocking `subprocess.run`.
- **Tests:** checker tests on small synthetic clips with a fake model client (violated, clean, low
  confidence, error, malformed answer, frames taken across the clip); registration; keep
  `tests/test_round7_qc_constraints.py` and M-384 to M-386 passing; a mutation for low confidence
  turning into PASS.

##### d. Identity gate for any locked character

- **Now:** with the flag off no identity judge is registered, so no still or clip review checks a
  character's identity. `hfvg/qc/duck_identity.py`, the first series' duck host gate, is a
  compatibility shim over the generic gate (its rules as one profile); no production code calls
  it, only `tests/test_qc_duck_identity.py` and `tests/test_e2e_dryrun.py` do.
- **Interface:** `IdentityGate` with `CharacterDetector.detect(image, criteria)` and
  `IdentityJudge.judge_identity(composite, shot_context, criteria)` in `hfvg/qc/identity.py`;
  `DuckIdentityGate` and `create_gate()` in the shim.
- **Done when:** one gate works for any locked character, with its criteria taken from the
  character's lock in the cast library (locked description, master, turnaround and head sheet,
  `lock_hash`) instead of code. A live gate without a judge escalates (the always-pass fakes are
  for dry run only). The composite uses real crops of the locked sheet. It runs as part of the
  still and clip review, and low confidence escalates (below 0.6 today).
- **Tests:** criteria built from a locked version; live without a judge escalates; low confidence
  escalates; the current duck rules as one configured character, so the existing
  `tests/test_qc_duck_identity.py` cases keep passing; `test_create_gate_default` and
  `test_e2e_duck_identity_gate` stay as dry-run tests, with live tests added next to them.

##### Cost and credits

Every judge verdict is a paid model call: QC spends nothing today and will spend on every still and
clip attempt.
The existing OpenAI text calls (story shaping, draft from idea, the voice picker) are not on the
ledger. The judges must be:

- **Budget ledger** (`hfvg/budget.py`, [DOLLAR-LEDGER.md](DOLLAR-LEDGER.md)): estimate in integer
  micro-dollars, reserve before the call, commit the real cost or release on failure, idempotent
  per call, as `hfvg/activities/audio.py` does for ElevenLabs. There is no QC budget line yet, and
  the default line caps already add up to $47.50 of the default $48.00 episode stop, so a QC line
  needs room made for it.
- **Balance gate** (`BalanceMonitor.check_preflight_balance`,
  [BALANCE-ALERTS.md](BALANCE-ALERTS.md)): checked before each paid call. It only knows
  `higgsfield` and `elevenlabs` and is a no-op for any other provider, so a judge provider needs a
  balance source (or a written decision) first.
- **Refusals:** a refused reserve, a failed balance check or a missing key makes the verdict
  NEEDS_REVIEW, never PASS.
- **Volume:** one still can take several calls (review, identity, three headcount counts at G2),
  and each clip takes one call per `qc_fail_if` condition, on every attempt, moderation resubmit
  and retry. The G2 headcount is recomputed on every `GET /g2-headcount` and every G2 approval, and
  assembly QC on every edit rebuild. Cache verdicts by media content hash (the edit manifest
  already records each clip's `source_sha256`) so a re-read or a retried activity does not pay
  twice.
- **Credits:** judge calls only use Higgsfield or ElevenLabs credits if the judge runs there. A
  stricter judge does mean more paid re-rolls; those stay bounded by `MAX_SHOT_RETRIES`,
  `MAX_PAID_RETRIES_PER_SHOT_PER_ROUND` and the per-round caps.
- **Dry run and CI:** no model calls in `DRY_RUN`; CI keeps spending nothing and has no keys.
- **Provider:** not chosen yet. `api/README.md` calls `OPENAI_API_KEY` the vision judge key, but
  no QC code reads it.

**Files:**
- `hfvg/activities/qc.py`, `hfvg/workflows/shot.py` (still and clip review)
- `hfvg/qc/headcount.py`, `hfvg/qc/vision_counter.py`, `hfvg/qc/g2_headcount.py`,
  `hfvg/qc/continuity.py` (counter; `hfvg/qc/animal_count.py` keeps the first series' adapters)
- `hfvg/qc/clip_constraints.py` (clip conditions)
- `hfvg/qc/identity.py` (identity gate; `hfvg/qc/duck_identity.py` is a compatibility shim)
- `hfvg/worker.py`, `api/main.py` (registering the judges at startup)
- `hfvg/budget.py`, `hfvg/balance_monitor.py` (spend)

#### 2. Audio Pipeline

**Status:** Shipped. The audio round (voiceover, SFX, music, mix, delivery QC) came in
PRs #17 and #18 (merged 2026-10-25) and runs on the picture locked by the mute-edit QC.
PR #8 was not merged.
**Scope:**
- Voiceover generation (ElevenLabs)
- Music generation
- SFX generation
- Audio mixing
- Picture lock gate

**Files:**
- `hfvg/activities/audio.py` (voiceover, SFX, music, mix and delivery QC)
- `hfvg/workflows/episode_v2.py` (audio round on the locked picture, before `G_FINAL_CUT`)

#### 3. CORS Tests

**Status:** CORS is already implemented: `api/main.py` adds `CORSMiddleware` from `CORS_ORIGINS`
(default `http://localhost:3000,http://localhost:3001`), listed in `.env.example`.
**Remaining:** tests for the CORS headers

**Good First Issue:** ⭐

**Tests:**
- `tests/test_api_cors.py` (new)

#### 4. 500 Error Handling

**Status:** Not started
**Issue:** API returns generic 500 errors without details
**Fix:** Add exception handlers with structured error responses

**Good First Issue:** ⭐

```python
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "type": type(exc).__name__}
    )
```

**Files to change:**
- `api/main.py` (add exception handlers)
- Tests for error responses

#### 5. Per-User Spend Caps

**Status:** Not started
**Issue:** Only episode-level caps exist
**Fix:** Add user_id to budget ledger, enforce per-user caps

**Requirements:**
- User authentication (OAuth or JWT)
- User table with `user_id`, `cap_usd_micros`
- Budget queries aggregate by `user_id`
- Reserve check: `user_total + episode_total + amount <= caps`

**Files to change:**
- `hfvg/studio_db.py` (add users table)
- `hfvg/budget.py` (user cap checks)
- `api/main.py` (auth middleware)
- Tests for user caps

### Medium Priority

#### 6. Authentication System

**Status:** Not started
**Scope:**
- OAuth (Google, GitHub)
- JWT tokens
- User sessions
- Role-based access (admin, creator, viewer)

**Good First Issue:** ⭐ (OAuth integration)

**Files:**
- `api/auth.py` (new)
- `hfvg/models.py` (User model)
- Web UI login page

#### 7. PostgreSQL Support

**Status:** Not started
**Issue:** SQLite doesn't support concurrent writes
**Fix:** Add PostgreSQL adapter, migration scripts

**Requirements:**
- `asyncpg` dependency
- Connection pooling
- Schema migration tool (Alembic)
- Backward compatibility flag

**Files to change:**
- `hfvg/studio_db.py` (abstract DB layer)
- `pyproject.toml` (add asyncpg)
- Migration scripts

#### 8. Webhook Notifications

**Status:** Not started
**Scope:**
- Episode state changes (started, gate reached, completed, failed)
- Shot events (still ready, clip ready, retry needed)
- Budget alerts (80% spent, cap reached)

**Good First Issue:** ⭐

**Example:**
```python
# webhook_config.json
{
    "url": "https://example.com/webhook",
    "events": ["episode.completed", "budget.cap_reached"],
    "secret": "webhook-secret"
}

# POST to webhook URL
{
    "event": "episode.completed",
    "episode_id": "ep-test-001",
    "timestamp": "2026-10-19T15:30:00Z",
    "data": {...}
}
```

**Files:**
- `hfvg/webhooks.py` (new)
- `hfvg/config.py` (webhook config)
- Tests

#### 9. OpenTelemetry Tracing

**Status:** Not started
**Scope:**
- Distributed tracing (Temporal → activities → providers)
- Metrics (latency, success rate, cost)
- Span attributes (episode_id, shot_id, provider)

**Requirements:**
- `opentelemetry-api` dependency
- `opentelemetry-sdk`
- `opentelemetry-instrumentation-temporal`
- Jaeger or Honeycomb export

**Good First Issue:** ⭐ (basic setup)

**Files:**
- `hfvg/otel.py` (new)
- `hfvg/worker.py` (instrument)
- `hfvg/providers/` (add spans)

### Low Priority

#### 10. Multi-Account Support

**Status:** Not started
**Scope:**
- Teams and workspaces
- Shared budgets
- Member roles (owner, admin, contributor)

#### 11. Advanced QC

**Status:** Not started
**Note:** prompt adherence for stills is now part of item 1 (still reviewer).
**Scope:**
- Automated motion analysis (enhanced)
- Style consistency checks
- Prompt adherence scoring
- Audio sync validation

#### 12. Batch Processing

**Status:** Not started
**Scope:**
- Process multiple episodes in parallel
- Batch retry (multiple shots at once)
- Scheduled processing

#### 13. API Rate Limiting

**Status:** Not started
**Scope:**
- Per-user request limits
- Sliding window rate limiter
- 429 responses with Retry-After header

**Good First Issue:** ⭐

## Good First Issues

These issues are great for new contributors:

### 1. Add CORS Tests ⭐

**Difficulty:** Easy
**Time estimate:** 1-2 hours
**Skills:** Python, FastAPI, HTTP

**Tasks:**
- CORS middleware and `CORS_ORIGINS` already exist in `api/main.py` / `.env.example`
- Write tests for CORS headers (allowed and disallowed origins)

**Files:**
- `tests/test_api_cors.py` (new)

### 2. Improve 500 Error Messages ⭐

**Difficulty:** Easy
**Time estimate:** 2-3 hours
**Skills:** Python, FastAPI, error handling

**Tasks:**
- Add exception handlers to `api/main.py`
- Return structured error responses
- Log errors with details
- Write tests for error cases

**Files:**
- `api/main.py`
- `tests/test_api_errors.py` (new)

### 3. OAuth Login (Google) ⭐

**Difficulty:** Medium
**Time estimate:** 4-6 hours
**Skills:** Python, FastAPI, OAuth 2.0

**Tasks:**
- Add `authlib` dependency
- Implement Google OAuth flow
- Store user sessions
- Add login/logout routes
- Write tests for auth flow

**Files:**
- `api/auth.py` (new)
- `api/main.py` (add routes)
- `pyproject.toml` / `api/requirements.txt`
- `tests/test_auth.py` (new)

### 4. Webhook Notifications ⭐

**Difficulty:** Medium
**Time estimate:** 3-4 hours
**Skills:** Python, async HTTP, webhooks

**Tasks:**
- Implement webhook sender
- Add webhook configuration (env or JSON)
- Send episode/budget events
- Add signature verification (HMAC)
- Write tests with mock webhook server

**Files:**
- `hfvg/webhooks.py` (new)
- `hfvg/config.py`
- `hfvg/workflows/episode_v2.py`
- `tests/test_webhooks.py` (new)

### 5. OpenTelemetry Basic Setup ⭐

**Difficulty:** Medium
**Time estimate:** 3-5 hours
**Skills:** Python, distributed tracing, OpenTelemetry

**Tasks:**
- Add OpenTelemetry dependencies
- Instrument worker startup
- Add spans to activity calls
- Export to console (dev) or Jaeger (optional)
- Document setup in `docs/LOCAL-DEV.md`

**Files:**
- `hfvg/otel.py` (new)
- `hfvg/worker.py`
- `pyproject.toml`
- `docs/LOCAL-DEV.md`

## Experimental Ideas

These are ideas that need more discussion:

### Video Editing Features

- Automated scene transitions
- Title cards generation
- Subtitle generation (from VO transcript)
- Color grading

### AI Enhancements

- Shot prompt auto-generation (from brief)
- Style consistency enforcement (model fine-tuning)
- Automated storyboard generation

### Integrations

- YouTube upload
- TikTok upload
- Discord notifications
- Slack notifications

### Developer Experience

- Visual workflow builder (drag-and-drop)
- Playground mode (test prompts without budget)
- A/B testing (compare models/providers)

## Deprecated / Won't Fix

### SQLite-only Concurrency

**Issue:** SQLite doesn't support concurrent writes
**Decision:** Migrate to PostgreSQL for production
**Reason:** SQLite is fine for development/testing, but production needs multi-writer support

### Client-side Polling for G2

**Issue:** Web UI polls API every 5s for shot status
**Decision:** Keep polling, add WebSocket support later
**Reason:** Polling is simple and works. WebSockets add complexity.

## Contributing

Want to work on something from the roadmap?

1. Comment on the related issue (or create one)
2. Ask questions in GitHub Discussions
3. Read [CONTRIBUTING.md](../CONTRIBUTING.md)
4. Submit a PR!

**Good First Issues** are marked with ⭐ and have detailed task lists.

## Feature Requests

Have an idea not on the roadmap?

1. Open a [Feature Request](../../issues/new?template=feature_request.md)
2. Describe the use case and proposed solution
3. Discuss with maintainers and community
4. Implement if approved!

---

**Last updated:** 2026-11-20
