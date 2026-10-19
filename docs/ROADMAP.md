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
- Mutation testing (218 tests)

**🚧 In Progress:**
- Audio stage (PR #8)
- Improved error messages

## Upcoming Features

### High Priority

#### 1. Audio Pipeline (PR #8)

**Status:** In development
**Scope:**
- Voiceover generation (ElevenLabs)
- Music generation
- SFX generation
- Audio mixing
- Picture lock gate

**Files:**
- `hfvg/activities/audio.py` (stubs exist)
- `hfvg/workflows/episode_v2.py` (placeholder comments)

#### 2. CORS Configuration

**Status:** Not started
**Issue:** API blocks cross-origin requests from web UI
**Fix:** Add configurable CORS middleware

**Good First Issue:** ⭐

```python
# api/main.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Files to change:**
- `api/main.py` (add middleware)
- `.env.example` (add CORS_ORIGINS)
- `docs/LOCAL-DEV.md` (document)

**Tests:**
- `tests/test_api_cors.py` (new)

#### 3. 500 Error Handling

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

#### 4. Per-User Spend Caps

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

#### 5. Authentication System

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

#### 6. PostgreSQL Support

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
- `requirements.txt` (add asyncpg)
- Migration scripts

#### 7. Webhook Notifications

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

#### 8. OpenTelemetry Tracing

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

#### 9. Multi-Account Support

**Status:** Not started
**Scope:**
- Teams and workspaces
- Shared budgets
- Member roles (owner, admin, contributor)

#### 10. Advanced QC

**Status:** Not started
**Scope:**
- Automated motion analysis (enhanced)
- Style consistency checks
- Prompt adherence scoring
- Audio sync validation

#### 11. Batch Processing

**Status:** Not started
**Scope:**
- Process multiple episodes in parallel
- Batch retry (multiple shots at once)
- Scheduled processing

#### 12. API Rate Limiting

**Status:** Not started
**Scope:**
- Per-user request limits
- Sliding window rate limiter
- 429 responses with Retry-After header

**Good First Issue:** ⭐

## Good First Issues

These issues are great for new contributors:

### 1. Add CORS Configuration ⭐

**Difficulty:** Easy
**Time estimate:** 1-2 hours
**Skills:** Python, FastAPI, HTTP

**Tasks:**
- Add CORS middleware to `api/main.py`
- Add `CORS_ORIGINS` env var to `.env.example`
- Write tests for CORS headers
- Document in `docs/LOCAL-DEV.md`

**Files:**
- `api/main.py`
- `.env.example`
- `docs/LOCAL-DEV.md`
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
- `requirements.txt`
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
- `requirements.txt`
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
3. Read [CONTRIBUTING.md](CONTRIBUTING.md)
4. Submit a PR!

**Good First Issues** are marked with ⭐ and have detailed task lists.

## Feature Requests

Have an idea not on the roadmap?

1. Open a [Feature Request](../../issues/new?template=feature_request.md)
2. Describe the use case and proposed solution
3. Discuss with maintainers and community
4. Implement if approved!

---

**Last updated:** 2026-10-19
