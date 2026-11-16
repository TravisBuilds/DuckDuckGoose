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
- See the scene pictures' review state, the credit line, the episode's stall alerts and the model
  limits that the episode page shows (read-only)
- See who they are signed in as (`GET /api/me`, for the studio's name and role chip)
- Change their own password

The episode writes above (uploads, start, start again, approvals, retries, reopen, set-live/set-dry,
story shape, resync, final-cut reject) apply to **their own episodes** and to episodes without an
owner; see [Episode ownership](#episode-ownership).

Members **cannot**:
- Raise credit caps or resume balance after insufficient funds (financial controls)
- Access dashboard or spending analytics
- Manage budget ledgers (`/api/budget/*` routes except read-only episode budget)
- Create, edit, or delete users
- Reset other users' passwords
- Access the stalls of all episodes (`GET /api/stalls`) or lint administration
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
episode's row (the first BEATMAP upload, the create wizard's first upload, `POST /api/episodes`, or
`POST /api/episodes/{id}/start-again` for an episode that has no row).

| Caller | Episode it owns | Another member's episode | Episode without an owner |
|---|---|---|---|
| Member session | write and approve | **403** | write and approve (old rule) |
| Admin session, `Bearer <ADMIN_SECRET>` | write and approve | write and approve | write and approve |
| `Bearer <UPLOAD_TOKEN>` (upload route only) | — | **403** | upload |

- **Without an owner** means no row yet, or a row whose `user_id` is NULL or `legacy/shared`: episodes
  created before owners were recorded, or created with a Bearer token. They keep the behaviour from
  before ownership: any member may change them. Nothing assigns them an owner later: the owner is
  only written when the row is created.
- A member's first write (BEATMAP upload, start or start again) on a new episode id makes them its
  owner. Admin sessions own what they create (`user_id` `admin`); Bearer tokens leave the episode
  without an owner.
- Start again (`POST /api/episodes/{id}/start-again`) is a write like the others: a member starts only
  their own episode again, or one without an owner; an admin any. It keeps the owner of an existing
  row (starting an ownerless episode again does not claim it) and records the row only when it is
  missing, as a start does. The `UPLOAD_TOKEN` cannot start anything.
- The ownership check comes before the route's own checks: a member gets the `403` for another
  member's episode whether its run is open or has ended. The run checks are for callers who may act on
  the episode: while a run is open its files are refused (`409`), once it has ended its actions are
  refused (`409`, "This run has ended ...") and its files may change before it is started again.
- Writes are every member-allowed non-GET route with `{episode_id}` in the path (checked by
  `require_admin_auth`) plus `POST /api/episodes/upload` and `POST /api/episodes` (checked in the
  handler, the id is in the form/body). Routes that only report (`POST /api/episodes/{id}/preflight`)
  set `read_only = True` and are not checked.
- Reads are unchanged: every member still lists and views every episode (a cast has its own read
  rule, below).
- A refused write answers `403` with `Episode <id> belongs to another member; only its owner or an
  admin can change it` and changes nothing.

## Cast library and series ownership

The cast routes (docs/CAST.md) follow the episode rules above, plus rules for series and for who may
see a cast:

- Every non-GET route under `/api/episodes/{id}/cast...` is an episode write: the owner check above
  applies (member: own or ownerless episodes; admin: any). That includes the sheet estimate, which
  only the people who may make the sheet need.
- An episode in a series edits the **series** cast, so those writes also need the series' owner
  (`series.owner_user_id`, the member who created the series). Admins may change any series. A refused
  series write answers `403` with `Series <name> belongs to another member; only its owner or an admin
  can change it`.
- A series **without an owner** (`owner_user_id` NULL, empty or `legacy/shared`: a legacy series, or
  one created with the admin Bearer token) changes only through an admin. A member's write answers
  `403` with `Series <name> has no owner; only an admin can change it`. Nobody claims such a series
  by editing it first (that would hand it to whoever comes first), and nothing assigns it an owner
  later.
- `POST /api/episodes/{id}/series` with a `series_id` needs the episode's owner and the series'
  owner (an admin for a series without an owner); a series the caller may not see answers `404`
  like a missing one. Taking the episode out (`series_id: null`) needs only the episode's owner.
- Deleting a character with a locked sheet needs an admin session (or the admin Bearer token) with
  `force` and a reason. Members get `403`, even on their own episode.
- Reads (`GET /api/series`, `GET /api/series/{id}`, `GET /api/episodes/{id}/cast...`, cast images)
  follow who may see the cast, not the episode read rule. Admins (session or Bearer) see every cast.
  An episode's own cast is seen by whoever may change the episode: its owner, or every member while it
  has no owner. A series cast is seen by the series' owner and by whoever may change one of the
  series' episodes.
- Everyone else gets the `404` a missing item gets, so a refusal does not tell whether the cast,
  series, character or image exists: `Series <id> not found`, `Image <id> not found`, or for
  `GET /api/episodes/{id}/cast...` one message for the cast and its characters. `GET /api/series`
  leaves out the series the caller may not see. The responses say whether the caller may change the
  cast (`can_edit`).
- The preflight (`POST /api/episodes/{id}/preflight`, read-only) checks the shots' cast only for a
  caller who may see it. Anyone else gets `cast_hidden: true` and no `cast_errors`, which would name
  the cast's characters.
- The shot states (`GET /api/episodes/{id}/shot-states`, read-only) keep the statuses and the
  moderation steps for everyone, but the free text that can quote a shot's prompt (still and clip
  errors, the escalation's provider message and reason, the lint messages) only for a caller who may
  see the cast. Anyone else gets `cast_hidden: true` and those fields empty.
- Cast images are only served by `GET /api/cast/images/{image_id}` and `.../thumb`, under the same
  rule as their cast (`401` without a session, `404` for a member who may not see it, checked before
  the file is read or a preview made). They are never served as static files. The `UPLOAD_TOKEN`
  reaches no cast route.
- **Props and places** (`/api/episodes/{id}/props...`, `/api/props/photos/...`, docs/PROPS.md) follow
  the cast rules above:
  - the same library;
  - the same owner checks for changes (episode owner, plus series owner for a series library);
  - the same read rule, with `404` for whoever may not see it;
  - photos served only through the authenticated routes.
- **The shot plan** (`GET`/`POST /api/episodes/{id}/shot-plan`) is seen by whoever may see the
  episode's cast; everyone else gets `404`. Changes (prompts, and each shot's cast, props and place)
  follow the episode write rule only: the owner or an admin, or any member while the episode has no
  owner. Picking from a series' library changes the episode, not the series. A refused change answers
  `403` like any episode write and changes nothing.

## Route Permission Matrix

### Public Routes (No Auth Required)
- `GET /` - Root
- `GET /gates` - Gates page
- `GET /api/health` - Health check
- `POST /api/login` - Login
- `POST /api/logout` - Logout
- `POST /api/auth/set-password` - Set password from invite token

### Member & Admin Routes
- `GET /api/me` - Who is signed in: `user_id`, `name`, `email`, `role` (the shared admin login has no user row: `name` `Admin`, `email` null; `Bearer <ADMIN_SECRET>` also `user_id` null)
- `GET /api/episodes` - List all episodes, each with its owner (`user_id`, `owner_name`; null without an owner)
- `POST /api/episodes` - Start a new episode
- `POST /api/episodes/{id}/start-again` - Start a failed or terminated run again with the same inputs
- `GET /api/episodes/{id}` - Get episode details (why a scene picture was turned down only for whoever may see the cast)
- `POST /api/episodes/{id}/set-live` - Set episode to live mode
- `POST /api/episodes/{id}/set-dry` - Set episode to dry mode
- `GET /api/episodes/{id}/story-shape` - Get story shape
- `PUT /api/episodes/{id}/story-shape` - Update story shape
- `GET /api/episodes/{id}/budget` - View episode budget estimate (read-only)
- `GET /api/episodes/{id}/shot-states` - Scene pictures' and clips' review state (free text only for whoever may see the cast)
- `GET /api/episodes/{id}/credits` - The run's credit line and the recorded Higgsfield balance (read-only; raising the cap stays admin-only)
- `GET /api/episodes/{id}/stalls` - The episode's stall alerts
- `GET /api/models` - Model limits
- `POST /api/episodes/{id}/shots/{shot_id}/approve` - Approve still
- `POST /api/episodes/{id}/shots/{shot_id}/reject` - Reject still
- `POST /api/episodes/{id}/shots/{shot_id}/accept-clip` - Accept clip
- `POST /api/episodes/{id}/approve` - Approve gate
- `POST /api/episodes/{id}/shots/{shot_id}/retry/estimate` - Price a redo of a scene picture (spends nothing)
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
- `GET /api/series`, `POST /api/series`, `GET /api/series/{series_id}` - List the series the caller may see, create (owned by the caller) and view one (`404` when the caller may not see it)
- `POST /api/episodes/{id}/series` - Put the episode in a series or take it out (series owner too; an admin for a series without an owner)
- `GET /api/episodes/{id}/cast` - The episode's cast, shots and sheet status (whoever may see the cast)
- `POST /api/episodes/{id}/cast` - Add a character
- `POST /api/episodes/{id}/cast/with-picture` - Add a character with its reference picture in one step (multipart, rights attestation)
- `POST /api/episodes/{id}/cast/import` - Add the characters a draft wrote (create wizard)
- `POST /api/episodes/{id}/cast/shots` - Choose who is in each shot
- `GET /api/episodes/{id}/cast/{character_id}` - One character (whoever may see the cast)
- `POST /api/episodes/{id}/cast/{character_id}` - Edit name, role or description
- `POST /api/episodes/{id}/cast/{character_id}/delete` - Delete a character (locked: admin with `force`)
- `POST /api/episodes/{id}/cast/{character_id}/versions` - Add an outfit (version)
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}` - Edit an unlocked version
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/images` - Upload a reference image (multipart, rights attestation)
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/images/{image_id}/primary` - Make an upload the main picture
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/images/{image_id}/delete` - Delete an image
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/sheet/estimate` - Price a sheet step (no spend)
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/sheet/generate` - Make a sheet step (guarded paid path)
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/sheet/pick` - Pick the main look
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/sheet/reject` - Clear a step before the lock
- `POST /api/episodes/{id}/cast/{character_id}/versions/{version}/approve` - Approve and lock the sheet
- `GET /api/cast/images/{image_id}`, `GET /api/cast/images/{image_id}/thumb` - Cast image and preview (whoever may see its cast)
- `GET /api/episodes/{id}/props` - The episode's props and places with their photos (whoever may see the cast)
- `POST /api/episodes/{id}/props` - Add a prop or place
- `POST /api/episodes/{id}/props/with-photo` - Add a prop or place with its first photo in one step (multipart, rights attestation)
- `POST /api/episodes/{id}/props/{prop_id}` - Edit a prop's or place's name or description
- `POST /api/episodes/{id}/props/{prop_id}/delete` - Delete a prop or place (refused during a run)
- `POST /api/episodes/{id}/props/{prop_id}/photos` - Upload a reference photo (multipart, rights attestation)
- `POST /api/episodes/{id}/props/{prop_id}/photos/{image_id}/primary` - Make a photo the main one
- `POST /api/episodes/{id}/props/{prop_id}/photos/{image_id}/delete` - Delete a photo (refused during a run)
- `GET /api/props/photos/{image_id}`, `GET /api/props/photos/{image_id}/thumb` - Prop or place photo and preview (whoever may see its library)
- `GET /api/episodes/{id}/shot-plan` - Each shot's prompts, cast, props and place with their edit history (whoever may see the cast)
- `POST /api/episodes/{id}/shot-plan` - Change shots' prompts and picks before the shot plan is approved (episode write rule; no series rule)

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
