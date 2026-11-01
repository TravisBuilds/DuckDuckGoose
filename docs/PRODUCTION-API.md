# Production API - StoryBuilder Integration

**Contract for Jack (CTO)**: Endpoints, authentication, schemas, and error handling for the StoryBuilder Production tab integration.

## Overview

The Production API provides upload and validation endpoints for integrating StoryBuilder with the video generation harness. These endpoints support:

- **Scoped authentication** via `UPLOAD_TOKEN` (upload-only access)
- **Validation without saving** for client-side feedback
- **Idempotent uploads** with automatic deduplication
- **Windows compatibility** with explicit UTF-8 encoding

---

## Authentication

### Token Types

| Token | Scope | Usage |
|-------|-------|-------|
| `ADMIN_SECRET` | Full API access | All endpoints |
| `UPLOAD_TOKEN` | Upload operations only | `/api/episodes/upload*` endpoints |

### Setup

Generate tokens with:
```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Configure in `.env`:
```bash
ADMIN_SECRET=your_admin_secret_min_32_chars
UPLOAD_TOKEN=your_upload_token_min_32_chars  # Optional, for scoped access
```

### Request Format

Include the token in the `Authorization` header:
```http
Authorization: Bearer <UPLOAD_TOKEN>
```

Or use session cookie authentication (browser/UI):
```http
Cookie: studio_admin_token=<session_token>
```

**Note**: Admin secret (`ADMIN_SECRET`) works for all endpoints. Use `UPLOAD_TOKEN` for least-privilege production integrations.

---

## Endpoints

### 1. Upload Episode (`POST /api/episodes/upload`)

Upload episode files (BEATMAP required, CREDIT-PLAN and CONTINUITY optional).

**Auth**: Bearer `UPLOAD_TOKEN` or `ADMIN_SECRET`, OR session cookie

**Request** (multipart/form-data):
```http
POST /api/episodes/upload
Authorization: Bearer <UPLOAD_TOKEN>
Content-Type: multipart/form-data

episode_id: ep04
beatmap_file: <BEATMAP.md>
credit_plan_file: <CREDIT-PLAN.md> (optional)
continuity_file: <CONTINUITY.md> (optional)
```

**Request Parameters**:
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `episode_id` | string | Yes | Episode ID (format: `ep##` or `ep##suffix`, e.g., `ep04`, `ep05full`) |
| `beatmap_file` | file | Yes | BEATMAP.md markdown file |
| `credit_plan_file` | file | No | CREDIT-PLAN.md budget file |
| `continuity_file` | file | No | CONTINUITY.md series continuity file |

**Success Response** (200 OK):
```json
{
  "success": true,
  "episode_id": "ep04",
  "shots_count": 42,
  "upload_hash": "a3f5c8e1b2d4f6a0",
  "is_duplicate": false,
  "files_uploaded": {
    "beatmap": true,
    "credit_plan": true,
    "continuity": false
  },
  "beatmap_path": "./data/episodes/ep04/BEATMAP.md"
}
```

**Response Fields**:
| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Always `true` on success |
| `episode_id` | string | Confirmed episode ID |
| `shots_count` | integer | Number of shots parsed from beatmap |
| `upload_hash` | string | SHA-256 hash prefix for deduplication |
| `is_duplicate` | boolean | `true` if this upload matches a previous upload (idempotent retry) |
| `files_uploaded` | object | Which optional files were provided |
| `beatmap_path` | string | Server path where beatmap was saved |

**Idempotency**: Uploading the same episode ID with the same beatmap content multiple times is safe. The `upload_hash` is computed from the beatmap content, and `is_duplicate: true` indicates a retry with identical content (no-op). Changed content overwrites the previous upload.

---

### 2. Validate Upload (`POST /api/episodes/upload/validate`)

Validate episode files **without saving** (dry-run validation for client-side feedback).

**Auth**: Bearer `UPLOAD_TOKEN` or `ADMIN_SECRET`, OR session cookie

**Request** (multipart/form-data):
```http
POST /api/episodes/upload/validate
Authorization: Bearer <UPLOAD_TOKEN>
Content-Type: multipart/form-data

episode_id: ep04
beatmap_file: <BEATMAP.md>
credit_plan_file: <CREDIT-PLAN.md> (optional)
continuity_file: <CONTINUITY.md> (optional)
```

**Request Parameters**: Same as `/api/episodes/upload`

**Success Response** (200 OK, all validations passed):
```json
{
  "success": true,
  "valid": true,
  "errors": [],
  "episode_id": "ep04",
  "shots_count": 42,
  "files_validated": {
    "beatmap": true,
    "credit_plan": true,
    "continuity": false
  }
}
```

**Validation Failed Response** (200 OK, errors present):
```json
{
  "success": false,
  "valid": false,
  "errors": [
    "Beatmap parsing error: Missing shot ID on line 15",
    "Credit plan parsing error: Invalid budget format"
  ],
  "shots_count": 38
}
```

**Response Fields**:
| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | `true` if request succeeded (even if validation failed) |
| `valid` | boolean | `true` if all validations passed, `false` if errors |
| `errors` | array[string] | Validation error messages (empty if valid) |
| `episode_id` | string | The validated episode ID |
| `shots_count` | integer | Number of shots parsed (0 if beatmap invalid) |
| `files_validated` | object | Which files were checked |

**Use Case**: Call this endpoint before calling `/api/episodes/upload` to show validation errors to the user without creating episode records or side effects.

---

## Error Responses

### 400 Bad Request
Invalid episode ID format or missing required fields.

```json
{
  "detail": "Invalid episode_id format: ep1. Must match ep## (e.g., ep04)"
}
```

### 401 Unauthorized
Missing or malformed `Authorization` header and no valid session cookie.

```json
{
  "detail": "Authorization header required. Set 'Authorization: Bearer <TOKEN>'"
}
```

### 403 Forbidden
Invalid token.

```json
{
  "detail": "Invalid upload token"
}
```

### 422 Unprocessable Entity
FastAPI validation error (e.g., missing form field).

```json
{
  "detail": [
    {
      "loc": ["body", "episode_id"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

### 500 Internal Server Error
Server-side error (e.g., database failure, file I/O error).

```json
{
  "detail": "Server error: <error message>"
}
```

---

## Episode ID Format

Valid episode IDs match the pattern: `ep[0-9]{2}(?:[a-z][a-z0-9]{0,11})?`

**Examples**:
- `ep04` ✅
- `ep99` ✅
- `ep05full` ✅ (full episode vs. teaser)
- `ep05v2` ✅ (versioned cut)
- `ep001` ❌ (three digits not supported)
- `ep5` ❌ (must be two digits)
- `ep04-draft` ❌ (no hyphens in suffix)

**Suffix rules** (optional):
- Must start with a lowercase letter (so `ep001` stays invalid)
- May contain lowercase letters and digits
- Maximum 12 characters (suffix only)
- No `-`, `/`, `.`, or special characters (path safety)

---

## Windows Compatibility

All text file operations use explicit UTF-8 encoding:

```python
# Reading
content = Path(file_path).read_text(encoding='utf-8')
with open(file_path, 'r', encoding='utf-8') as f:
    data = f.read()

# Writing
Path(file_path).write_text(content, encoding='utf-8')
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(data)
```

Files affected:
- `hfvg/continuity_parser.py`: CONTINUITY.md and prompt_kit.json parsing
- `hfvg/model_caps.py`: model_caps.json registry loading
- `hfvg/cli.py`: Beatmap reading for `hfvg start-v2`
- `hfvg/edit_plan.py`: shots.json and edit_plan.json read/write

This ensures proper handling of Unicode characters (e.g., smart quotes, em dashes, non-ASCII names) on Windows systems.

---

## Deduplication & Idempotency

### Upload Hash

Each upload computes a SHA-256 hash (16-character prefix) from the beatmap content:

```python
import hashlib
upload_hash = hashlib.sha256(beatmap_content).hexdigest()[:16]
```

This hash is stored in `<episode_dir>/.upload_hash`.

### Retry Behavior

| Scenario | Behavior | `is_duplicate` |
|----------|----------|----------------|
| First upload | Creates episode, saves files | `false` |
| Retry with same content | No-op, returns cached result | `true` |
| Retry with changed content | Overwrites files, updates hash | `false` |

**Use Case**: Network failures, timeout retries, or resubmissions from the UI are safe. The client can check `is_duplicate: true` to skip redundant downstream operations.

---

## Example Client Integration (Python)

```python
import requests
from pathlib import Path

API_URL = "https://api.example.com"
UPLOAD_TOKEN = "your_upload_token_here"

def validate_episode(episode_id, beatmap_path, credit_plan_path=None):
    """Validate episode files before uploading."""
    files = {
        "beatmap_file": open(beatmap_path, "rb"),
    }
    if credit_plan_path:
        files["credit_plan_file"] = open(credit_plan_path, "rb")
    
    response = requests.post(
        f"{API_URL}/api/episodes/upload/validate",
        headers={"Authorization": f"Bearer {UPLOAD_TOKEN}"},
        data={"episode_id": episode_id},
        files=files
    )
    
    for f in files.values():
        f.close()
    
    result = response.json()
    if not result.get("valid"):
        print(f"Validation errors: {result['errors']}")
        return False
    print(f"Valid: {result['shots_count']} shots")
    return True

def upload_episode(episode_id, beatmap_path, credit_plan_path=None):
    """Upload episode files."""
    # Validate first
    if not validate_episode(episode_id, beatmap_path, credit_plan_path):
        return None
    
    # Upload
    files = {
        "beatmap_file": open(beatmap_path, "rb"),
    }
    if credit_plan_path:
        files["credit_plan_file"] = open(credit_plan_path, "rb")
    
    response = requests.post(
        f"{API_URL}/api/episodes/upload",
        headers={"Authorization": f"Bearer {UPLOAD_TOKEN}"},
        data={"episode_id": episode_id},
        files=files
    )
    
    for f in files.values():
        f.close()
    
    response.raise_for_status()
    result = response.json()
    
    if result.get("is_duplicate"):
        print(f"Upload is duplicate (hash: {result['upload_hash']})")
    else:
        print(f"Uploaded {result['shots_count']} shots (hash: {result['upload_hash']})")
    
    return result

# Usage
upload_episode("ep04", "episodes/ep04/BEATMAP.md", "episodes/ep04/CREDIT-PLAN.md")
```

---

## Testing

### Generate Test Tokens

```bash
# Generate ADMIN_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# Generate UPLOAD_TOKEN (scoped)
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

### Test with curl

```bash
# Validate without saving
curl -X POST http://localhost:8000/api/episodes/upload/validate \
  -H "Authorization: Bearer $UPLOAD_TOKEN" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md"

# Upload episode
curl -X POST http://localhost:8000/api/episodes/upload \
  -H "Authorization: Bearer $UPLOAD_TOKEN" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md" \
  -F "credit_plan_file=@CREDIT-PLAN.md"

# Check idempotency (retry with same files)
curl -X POST http://localhost:8000/api/episodes/upload \
  -H "Authorization: Bearer $UPLOAD_TOKEN" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md" \
  -F "credit_plan_file=@CREDIT-PLAN.md"
# Should return is_duplicate: true
```

### Test Auth Failures

```bash
# Missing token
curl -X POST http://localhost:8000/api/episodes/upload \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md"
# Expected: 401 Unauthorized

# Invalid token
curl -X POST http://localhost:8000/api/episodes/upload \
  -H "Authorization: Bearer invalid_token_here" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md"
# Expected: 403 Forbidden

# Admin secret should also work (full access)
curl -X POST http://localhost:8000/api/episodes/upload \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -F "episode_id=ep04" \
  -F "beatmap_file=@BEATMAP.md"
# Expected: 200 OK
```

---

## Security Considerations

1. **Token Management**:
   - Store `ADMIN_SECRET` and `UPLOAD_TOKEN` in secure vaults (not in code/git)
   - Rotate tokens periodically
   - Use environment variable injection in production

2. **Scope Separation**:
   - `UPLOAD_TOKEN` cannot start/stop episodes or approve gates
   - `UPLOAD_TOKEN` cannot access sensitive data (budget, logs, state)
   - Use `UPLOAD_TOKEN` for production integrations (least privilege)

3. **Rate Limiting** (future):
   - Consider adding rate limits per token
   - Log token usage for audit trails

4. **HTTPS Required**:
   - Always use HTTPS in production
   - Never send tokens over HTTP

---

## Changelog

- **2026-11-01**: Initial Production API contract for StoryBuilder integration
  - Added `UPLOAD_TOKEN` scoped authentication
  - Added `/api/episodes/upload/validate` endpoint
  - Added upload deduplication with `upload_hash` and `is_duplicate`
  - Added Windows UTF-8 encoding fixes
  - Documented episode ID format (including optional suffixes)

---

## Support

For issues or questions:
- Create a ticket in the harness repository
- Tag @TravisBuilds for API changes
- Escalate to Jack (CTO) for integration blockers
