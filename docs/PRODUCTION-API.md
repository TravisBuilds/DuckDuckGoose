# Production API Documentation

StoryBuilder Production Tab integration endpoints for episode upload and validation.

## Overview

The Production API provides endpoints for the StoryBuilder Production tab to upload episode files (beatmaps, credit plans, continuity notes) with validation, idempotency, and deduplication.

**Base URL:** Same as Studio API (default: `http://localhost:8000`)

## Authentication

### Bearer Token (Recommended for Production Tab)

Use the `Authorization` header with a Bearer token:

```
Authorization: Bearer <token>
```

**Supported tokens:**
- `UPLOAD_TOKEN` (scoped: the routes below, nothing else)
- `ADMIN_SECRET` (full access: all endpoints)

**What `UPLOAD_TOKEN` reaches** (`UPLOAD_TOKEN_ROUTES` in `api/main.py`): everything the Production tab needs to
upload an episode's four files and check them, and nothing that starts, approves or spends.

| Route | With `UPLOAD_TOKEN` |
|-------|---------------------|
| `POST /api/episodes/upload/validate` | Validate the files, saving nothing |
| `POST /api/episodes/upload` | Upload BEATMAP.md. 409 once the episode has a workflow, running or finished |
| `POST /api/episodes/{id}/upload-prompt-kit` | Upload prompt_kit.json. 409 once the episode has a workflow |
| `POST /api/episodes/{id}/upload-shots` | Upload shots.json + refs.json. 409 once the episode has a workflow |
| `POST /api/episodes/{id}/lint` | Lint the uploaded files, prompts included |
| `POST /api/episodes/{id}/preflight` | Preflight + offline cost estimate |

Every other route refuses it with 403. A session or `ADMIN_SECRET` keeps the console's behaviour: uploads are
refused only while a workflow runs. The token can never replace the files (and, through `/upload`, the shot rows) of
an episode whose run has happened; it has to use a new episode id. A workflow past Temporal's retention reads as
never started.

**Configuration:**
```bash
export UPLOAD_TOKEN="your_upload_token_min_32_chars"
export ADMIN_SECRET="your_admin_secret_min_32_chars"
```

**Security requirements:**
- Tokens must be at least 32 characters
- Tokens must be ASCII (non-ASCII tokens rejected with 403 Forbidden, not 500)
- Missing token: 401 Unauthorized
- Invalid token (including non-UTF-8): 403 Forbidden
- Constant-time comparison prevents timing attacks
- Missing/short tokens fail closed (500 error on startup)

**Token validation behavior:**
- Non-ASCII bytes in Authorization header: 403 (fail closed, never 500)
- Empty Bearer token: 403
- Malformed header (not "Bearer <token>"): 401

### Session Cookie (Alternative)

Session-based auth via `studio_admin_token` cookie (login required).

## Endpoints

### POST /api/episodes/upload/validate

Validate episode files **without saving** (dry-run).

**Purpose:** Pre-flight validation before upload to catch errors early.

**Auth:** Bearer token (UPLOAD_TOKEN or ADMIN_SECRET) OR session cookie

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `episode_id` | string | Yes | Episode ID (format: `ep##`, e.g., `ep04`) |
| `beatmap_file` | file | Yes | BEATMAP.md (markdown table format) |
| `credit_plan_file` | file | No | CREDIT-PLAN.md |
| `continuity_file` | file | No | CONTINUITY.md |
| `shots_file` | file | No | shots.json. With it, the per-shot files are checked together (below) |
| `refs_file` | file | No | refs.json |
| `prompt_kit_file` | file | No | prompt_kit.json |

**With `shots_file`** (`hfvg/episode_files.py`), validate also runs what the upload routes, `/lint`, preflight and
the live submit would, so one call says whether the set would get through a run:
- `parse_shots`, and BEATMAP.md and shots.json listing the same shots on the same models
- refs.json: a `per_shot` list for every shot, keys equal to its `ref_keys` in order (a path not on the server
  is a warning)
- `plan_route` on every shot, with the beatmap's text tags merged in as the workflow does
- the prompt lint on every prompt the live submit would send, continuity locks injected first as the submit
  does (`lint_plans`)
- `preflight_plans`, and the offline cost estimate

Each problem is in `errors` prefixed with its shot id, and per shot in `shots`. Without `refs_file` or
`prompt_kit_file` those checks are skipped with a warning.

**Response (200 OK):** Success

```json
{
  "success": true,
  "valid": true,
  "errors": [],
  "episode_id": "ep04",
  "shots_count": 12,
  "files_validated": {
    "beatmap": true,
    "credit_plan": true,
    "continuity": false,
    "shots": true,
    "refs": true,
    "prompt_kit": true
  },
  "warnings": [],
  "shots": {"SH01": {"errors": [], "warnings": []}},
  "cost_estimate": {"total_usd": "12.7794", "...": "as POST /preflight"}
}
```

**Response (200 OK):** Validation failed (valid: false)

```json
{
  "success": false,
  "valid": false,
  "errors": [
    "Beatmap parsing error: No shots found in beatmap",
    "Credit plan file is not valid UTF-8"
  ],
  "shots_count": 0
}
```

**Note:** Validation errors return 200 OK with `valid: false`, not 422. The endpoint only returns 4xx for authentication/authorization failures.

**Guarantees:**
- No files written to `EPISODES_DIR`
- No database writes
- Same validation as actual upload

**File encoding:**
- Files are read and written with explicit UTF-8 encoding
- Empty optional files (credit_plan, continuity) are treated as None/not provided
- Validate endpoint checks UTF-8 encoding and reports errors
- Upload endpoint accepts any byte content (parsing errors caught during validation stage)

**Example (curl):**

```bash
curl -X POST http://localhost:8000/api/episodes/upload/validate \
  -H "Authorization: Bearer $UPLOAD_TOKEN" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md"
```

---

### POST /api/episodes/upload

Upload episode files (BEATMAP required, CREDIT-PLAN and CONTINUITY optional).

**Purpose:** Persist episode files and populate database with shots.

**Auth:** Bearer token (UPLOAD_TOKEN or ADMIN_SECRET) OR session cookie

**Request:** `multipart/form-data` (same as validate)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `episode_id` | string | Yes | Episode ID (format: `ep##`) |
| `beatmap_file` | file | Yes | BEATMAP.md |
| `credit_plan_file` | file | No | CREDIT-PLAN.md |
| `continuity_file` | file | No | CONTINUITY.md |

**Response (200 OK):** Success

```json
{
  "success": true,
  "episode_id": "ep04",
  "shots_count": 12,
  "upload_hash": "a3f5d8c921ef6742",
  "is_duplicate": false,
  "files_uploaded": {
    "beatmap": true,
    "credit_plan": true,
    "continuity": false
  },
  "beatmap_path": "/data/episodes/ep04/BEATMAP.md"
}
```

**Response (200 OK):** Duplicate (idempotent retry)

```json
{
  "success": true,
  "episode_id": "ep04",
  "shots_count": 12,
  "upload_hash": "a3f5d8c921ef6742",
  "is_duplicate": true,
  "files_uploaded": {
    "beatmap": true,
    "credit_plan": true,
    "continuity": false
  },
  "beatmap_path": "/data/episodes/ep04/BEATMAP.md"
}
```

**Response (409 Conflict):** Workflow running

```json
{
  "detail": "Episode ep04 workflow is currently running. Cannot upload while workflow is active."
}
```

**Response (422 Unprocessable Entity):** Validation failed

```json
{
  "detail": "Upload validation failed: No shots found in beatmap"
}
```

**Idempotency:**
- Content hash computed from all provided files (SHA-256, truncated to 16 hex chars)
- Identical re-upload returns `is_duplicate: true` and skips all writes
- Changed content triggers full re-upload with file/DB replacement
- `.upload_hash` written **last** after all operations succeed

**File Management:**
- Files staged to temp directory, validated, then moved atomically
- Stale files removed on re-upload:
  - If `CREDIT-PLAN.md` exists but not provided → deleted
  - If `CONTINUITY.md` exists but not provided → deleted
  - `BEATMAP.md` always replaced

**Database:**
- Shots replaced (not `INSERT OR IGNORE`):
  1. `DELETE FROM shots WHERE episode_id = ?`
  2. `INSERT` new shots from beatmap
- Episode record created/updated
- Budget initialized if credit plan provided

**Workflow Locking:**
- Returns 409 if Temporal workflow is `RUNNING` or `CONTINUED_AS_NEW`
- Prevents concurrent modifications during active workflow

**Example (curl):**

```bash
curl -X POST http://localhost:8000/api/episodes/upload \
  -H "Authorization: Bearer $UPLOAD_TOKEN" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md" \
  -F "credit_plan_file=@CREDIT-PLAN.md" \
  -F "continuity_file=@CONTINUITY.md"
```

---

## Error Codes

| Code | Meaning | Common Causes |
|------|---------|---------------|
| `200` | Success | Request completed |
| `400` | Bad Request | Invalid episode_id format |
| `401` | Unauthorized | Missing Authorization header or cookie |
| `403` | Forbidden | Invalid token, non-ASCII token, or wrong token scope |
| `409` | Conflict | Workflow is running (upload blocked); with `UPLOAD_TOKEN`, the episode already has a workflow |
| `422` | Unprocessable Entity | Validation failed (beatmap parse error) |
| `500` | Internal Server Error | Server misconfiguration (UPLOAD_TOKEN < 32 chars) |

## Episode ID Format

**Required format:** `ep##` where `##` is a two-digit number (e.g., `ep01`, `ep04`, `ep99`), with an optional
lowercase suffix for a cut or version (`ep05full`, `ep04v2`)

**Validation regex:** `EPISODE_ID_PATTERN` in `api/main.py`, `ep[0-9]{2}(?:[a-z][a-z0-9]{0,11})?`, fully matched

**Examples:**
- ✅ Valid: `ep01`, `ep04`, `ep12`, `ep99`, `ep04v2`, `ep05full`
- ❌ Invalid: `ep1` (one digit), `ep100` (three digits), `episode04` (wrong prefix), `ep04V2` (capital in the suffix)

## Beatmap Format

**Required:** Markdown table with specific columns.

**Minimum columns:** `#`, `ID`, `Screen (gen)`, `Room`, `TOD`, `Chars`, `Action`, `Purpose`, `In`, `Out`, `Model`

**Example:**

```markdown
# Episode 04 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | A red ball rolls | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Studio | Day | A | Ball stops | Close | - | - | K |
```

**Parser:** `hfvg.episode_parser.parse_beatmap()`

## Credit Plan Format

**Optional.** Markdown with credit allocation per shot.

**Example:**

```markdown
# Credit Plan

S01: 100 credits
S02: 150 credits
```

**Parser:** `hfvg.credit_plan_parser.parse_credit_plan()`

## Continuity Format

**Optional.** Markdown with character descriptions, sets, lighting notes.

**Example:**

```markdown
# Continuity

## Characters
- A: Red sphere, glossy surface

## Sets
- Studio: White backdrop, wooden table

## Lighting
- Day: Soft natural light from window left
```

**Parser:** `hfvg.continuity_parser.parse_continuity()`

## Integration Example (JavaScript)

```javascript
async function uploadEpisode(episodeId, files) {
  const formData = new FormData();
  formData.append('episode_id', episodeId);
  formData.append('beatmap_file', files.beatmap);
  if (files.creditPlan) formData.append('credit_plan_file', files.creditPlan);
  if (files.continuity) formData.append('continuity_file', files.continuity);

  const response = await fetch('http://localhost:8000/api/episodes/upload', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${UPLOAD_TOKEN}`
    },
    body: formData
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(`Upload failed: ${error.detail || JSON.stringify(error)}`);
  }

  return await response.json();
}
```

## Rate Limiting

**Not currently enforced.** Idempotency and content hashing prevent duplicate work.

## Changelog

### v1.1 (Current)
- `UPLOAD_TOKEN` reaches the prompt kit and shots uploads, `/lint` and `/preflight` (`UPLOAD_TOKEN_ROUTES`)
- Validate takes shots.json, refs.json and prompt_kit.json and runs the shot, ref, routing, prompt lint and
  preflight checks on them, with per-shot results and the cost estimate
- `/lint` lints the prompts (`lint_report`); a missing kit is a warning, a broken one an issue
- `lint_report` injects continuity locks before linting, as the live submit does
- With `UPLOAD_TOKEN`, the uploads are refused (409) once the episode has any workflow, not only a running one

### v1.0
- Bearer token auth with `UPLOAD_TOKEN` scope
- Validate endpoint (dry-run)
- Upload endpoint with idempotency
- Content hashing for deduplication
- Stale file cleanup
- Shot replacement (not INSERT OR IGNORE)
- Workflow lock (409 if running)
- Explicit UTF-8 encoding (Windows compatibility)
