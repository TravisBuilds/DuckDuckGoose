# Authentication & Authorization

DuckDuckGoose Studio uses role-based access control with two roles: **admin** and **member**.

## Default: Fail Closed

All routes default to **admin-only** access. Routes that allow members must explicitly opt-in with `route_function.allow_members = True`. This ensures new routes without explicit auth checks default to admin-only for security.

## Roles

### Admin
Full access to all endpoints. Admins can:
- Everything members can do (see below)
- Financial controls (raise credit caps, resume balance)
- User management (create/edit/disable users, reset passwords)
- Budget operations (view all balances, sync ledgers)
- Administrative operations (approve G108, canary tests, smoke tests)
- System configuration (lint kits, reference versions, settings)
- Analytics and dashboard

### Member
Creators who make videos end-to-end. Members can:
- List and view episodes
- Start episodes (dry or live)
- Upload episode files (beatmap, credit plan, continuity) via web UI or UPLOAD_TOKEN
- Validate episode uploads before saving
- Generate episode drafts from plain-language ideas (beginner wizard)
- Set episodes to live or dry mode
- View and edit story shape
- Approve or reject stills and gates
- Accept clips
- Retry shots and clips
- Reopen episodes and restore from snapshots
- View audit trails
- View budget estimates for their own episodes (read-only)
- List files and view stills
- Run preflight checks
- View clip plans and resync
- Change their own password

The episode writes above (uploads, start, approvals, retries, reopen, set-live/set-dry, story shape,
resync, final-cut reject) apply to **their own episodes** and to episodes without an owner; see
[Episode ownership](#episode-ownership).

Members **cannot**:
- Raise credit caps or resume balance after insufficient funds (financial controls)
- Access dashboard or spending analytics
- Manage budget ledgers (`/api/budget/*` routes except read-only episode budget)
- Create, edit, or delete users
- Reset other users' passwords
- Access stalls or lint administration
- Set reference versions
- Approve G108
- Run canary or smoke tests
- Modify system settings

## Authentication Methods

### 1. Session Cookies (Browser/Web UI)
- Login via `/api/login` with email + password
- Returns httpOnly session cookie `studio_admin_token`
- Used for browser-based access
- Both admin and member roles supported

### 2. Bearer Token (CLI/API)
- Admin secret: `Authorization: Bearer <ADMIN_SECRET>`
- Always admin-level access
- Used for server-to-server calls and CLI tools

### 3. Upload Token (Scoped)
- Production upload endpoints only
- `Authorization: Bearer <UPLOAD_TOKEN>`
- Scoped to `/api/episodes/upload` and `/api/episodes/upload/validate`
- Equivalent to member-level access for these endpoints; it names no user, so it uploads only to
  episodes without an owner

## Episode ownership

The owner of an episode is `episodes.user_id` in the studio database: the session user who created the
episode's row (the first BEATMAP upload, the create wizard's first upload, or `POST /api/episodes`).

| Caller | Episode it owns | Another member's episode | Episode without an owner |
|---|---|---|---|
| Member session | write and approve | **403** | write and approve (old rule) |
| Admin session, `Bearer <ADMIN_SECRET>` | write and approve | write and approve | write and approve |
| `Bearer <UPLOAD_TOKEN>` (upload route only) | — | **403** | upload |

- **Without an owner** means no row yet, or a row whose `user_id` is NULL or `legacy/shared`: episodes
  created before owners were recorded, or created with a Bearer token. They keep the behaviour from
  before ownership: any member may change them. Nothing assigns them an owner later: the owner is
  only written when the row is created.
- A member's first write (BEATMAP upload or start) on a new episode id makes them its owner. Admin
  sessions own what they create (`user_id` `admin`); Bearer tokens leave the episode without an owner.
- Writes are every member-allowed non-GET route with `{episode_id}` in the path (checked by
  `require_admin_auth`) plus `POST /api/episodes/upload` and `POST /api/episodes` (checked in the
  handler, the id is in the form/body). Routes that only report (`POST /api/episodes/{id}/preflight`)
  set `read_only = True` and are not checked.
- Reads are unchanged: every member still lists and views every episode.
- A refused write answers `403` with `Episode <id> belongs to another member; only its owner or an
  admin can change it` and changes nothing.

## Route Permission Matrix

### Public Routes (No Auth Required)
- `GET /` - Root
- `GET /gates` - Gates page
- `GET /api/health` - Health check
- `POST /api/login` - Login
- `POST /api/logout` - Logout
- `POST /api/auth/set-password` - Set password from invite token

### Member & Admin Routes
- `GET /api/episodes` - List all episodes
- `POST /api/episodes` - Start a new episode
- `POST /api/episodes/{id}/start-again` - Start a failed or terminated run again with the same inputs
- `GET /api/episodes/{id}` - Get episode details
- `POST /api/episodes/{id}/set-live` - Set episode to live mode
- `POST /api/episodes/{id}/set-dry` - Set episode to dry mode
- `GET /api/episodes/{id}/story-shape` - Get story shape
- `PUT /api/episodes/{id}/story-shape` - Update story shape
- `GET /api/episodes/{id}/budget` - View episode budget estimate (read-only)
- `POST /api/episodes/{id}/shots/{shot_id}/approve` - Approve still
- `POST /api/episodes/{id}/shots/{shot_id}/reject` - Reject still
- `POST /api/episodes/{id}/shots/{shot_id}/accept-clip` - Accept clip
- `POST /api/episodes/{id}/approve` - Approve gate
- `POST /api/episodes/{id}/shots/{shot_id}/retry` - Retry shot
- `POST /api/episodes/{id}/shots/{shot_id}/retry-clip` - Retry clip
- `POST /api/episodes/{id}/reopen` - Reopen episode
- `POST /api/episodes/{id}/reopen-from-saved` - Reopen from snapshot
- `GET /api/episodes/{id}/audit` - View audit trail
- `GET /api/episodes/{id}/shots` - List shots
- `GET /api/episodes/{id}/gates` - List gates
- `GET /api/episodes/{id}/files` - List review files
- `GET /api/episodes/{id}/files/{type}` - Get specific review file
- `GET /api/episodes/{id}/shots/{shot_id}/still` - Get still image
- `POST /api/episodes/{id}/preflight` - Run preflight checks
- `GET /api/episodes/{id}/clip-plans` - Get clip plans
- `POST /api/episodes/{id}/resync-plans` - Resync plans
- `POST /api/episodes/{id}/shots/{shot_id}/select-still` - Select still version
- `GET /api/episodes/{id}/still-versions` - List still versions
- `POST /api/episodes/{id}/final-cut/reject` - Reject final cut
- `POST /api/users/me/change-password` - Change own password
- `POST /api/episodes/upload` - Production upload (or via UPLOAD_TOKEN)
- `POST /api/episodes/upload/validate` - Validate upload (or via UPLOAD_TOKEN)
- `POST /api/episodes/draft-from-idea` - Generate episode files from plain-language idea (beginner wizard)

### Admin-Only Routes
- `POST /api/episodes/{id}/raise-cap` - Raise credit cap (financial)
- `POST /api/episodes/{id}/resume-balance` - Resume after balance top-up (financial)
- `GET /api/dashboard` - Analytics dashboard
- `GET /api/spending` - Spending analytics
- `GET /api/budget/balance` - Global budget balance
- `GET /api/budget/all-balances` - All episode balances
- `POST /api/budget/balance` - Top up balance
- `POST /api/budget/{episode_id}/lines/sync` - Sync budget ledger
- `GET /api/users` - List users
- `POST /api/users` - Create user
- `GET /api/users/{user_id}` - Get user details
- `POST /api/users/{user_id}/reset-password` - Reset user password (admin resets others)
- `GET /api/stalls` - View all stalls
- `GET /api/episodes/{id}/stalls` - View episode stalls
- `POST /api/lint` - Lint operations
- `POST /api/episodes/{id}/lint` - Episode lint
- `POST /api/episodes/{id}/set-ref-versions` - Set reference versions
- `POST /api/episodes/{id}/approve-g108` - Approve G108
- `GET /api/canary/{workflow_id}` - Get canary status
- `POST /api/episodes/{id}/canary` - Run canary test
- `POST /api/episodes/{id}/smoke-test` - Run smoke test
- `POST /api/episodes/{id}/upload-prompt-kit` - Upload prompt kit
- `POST /api/episodes/{id}/upload-shots` - Upload shots file
- All other routes not explicitly listed as public or member-allowed

## Implementation

### Middleware
Global middleware (`require_admin_auth`) requires admin access by default. Routes opt-in to allow members by setting:

```python
@app.get("/api/episodes")
async def list_episodes():
    ...

list_episodes.allow_members = True
```

A member-allowed route with `{episode_id}` in its path and a method other than GET is an episode
write: the middleware also requires the caller to own the episode (or be an admin). A POST that only
reports sets `route_function.read_only = True` as well.

### Verification Functions
- `verify_auth_unified(authorization, studio_admin_token, require_admin=True)` - Unified auth check
- `verify_admin_cookie(cookie)` - Verify admin session
- `verify_member_or_admin_cookie(cookie)` - Verify any authenticated session
- `verify_admin_secret(authorization)` - Verify Bearer token

### Security Principles
1. **Fail Closed**: New routes without explicit auth default to admin-only
2. **Explicit Opt-In**: Member access requires explicit `.allow_members = True`
3. **No Bypass**: Upload token only works for specific upload endpoints
4. **Rate Limiting**: Login attempts are rate-limited per email
5. **Secure Sessions**: httpOnly cookies, 7-day expiry, server-side revocation
6. **Invite-Based Onboarding**: Users set their own passwords via 72h single-use tokens
