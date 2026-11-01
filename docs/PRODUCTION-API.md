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
- `UPLOAD_TOKEN` (scoped: upload/validate endpoints only)
- `ADMIN_SECRET` (full access: all endpoints)

**Configuration:**
```bash
export UPLOAD_TOKEN="your_upload_token_min_32_chars"
export ADMIN_SECRET="your_admin_secret_min_32_chars"
```

**Security requirements:**
- Tokens must be at least 32 characters
- Tokens must be ASCII (non-ASCII rejected with 403)
- Constant-time comparison prevents timing attacks
- Missing/short tokens fail closed (500 error on startup)

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
    "continuity": false
  }
}
```

**Response (422 Unprocessable Entity):** Validation failed

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

**Guarantees:**
- No files written to `EPISODES_DIR`
- No database writes
- Same validation as actual upload

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
| `409` | Conflict | Workflow is running (upload blocked) |
| `422` | Unprocessable Entity | Validation failed (beatmap parse error, invalid UTF-8) |
| `500` | Internal Server Error | Server misconfiguration (UPLOAD_TOKEN < 32 chars) |

## Episode ID Format

**Required format:** `ep##` where `##` is a two-digit number (e.g., `ep01`, `ep04`, `ep99`)

**Validation regex:** `^ep\d{2}$`

**Examples:**
- ✅ Valid: `ep01`, `ep04`, `ep12`, `ep99`
- ❌ Invalid: `ep1` (one digit), `ep100` (three digits), `episode04` (wrong prefix)

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

### v1.0 (Current)
- Bearer token auth with `UPLOAD_TOKEN` scope
- Validate endpoint (dry-run)
- Upload endpoint with idempotency
- Content hashing for deduplication
- Stale file cleanup
- Shot replacement (not INSERT OR IGNORE)
- Workflow lock (409 if running)
- Explicit UTF-8 encoding (Windows compatibility)
