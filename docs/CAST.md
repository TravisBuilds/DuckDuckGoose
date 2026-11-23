# Cast Library and Character Sheets

Recurring characters drift: without shared references, every still is a fresh guess and the same
character looks like a different person from shot to shot. The cast library keeps each character's
description and reference pictures in one place. Approved character sheets are locked. Shots name who
is in frame, and planning feeds those references and descriptions into every still with that character.

For the creator-facing steps (upload a picture, lock a sheet, put people in shots) see
[CAST-HOWTO.md](CAST-HOWTO.md). Props and places (things and settings with reference photos) and the
shot plan editor build on the same library: see [PROPS.md](PROPS.md). A series is shown in the studio
as a Universe, with its profile, saved voices, creation flow and the lock every episode in it needs
before it starts: see [WORLDS.md](WORLDS.md).

| Piece | Code |
|---|---|
| Store: series, characters, versions, images, sheet jobs, audit log | `hfvg/cast_store.py` |
| Migration `2026-11-cast-library-v1` | `hfvg/migrations.py` |
| Prompt rules: `cast` request, character clause, no-text constraint, lint rule 13, draft cast | `hfvg/cast_prompt.py` |
| Planning: `cast` → refs, ref keys, lineup / grid sheets, `shot_meta` | `hfvg/cast_plan.py` (called by `hfvg/shot_data.py`) |
| Sheet planning (prompts, refs, model checks, placeholders) | `hfvg/cast_sheets.py` |
| Sheet generation (guarded paid path) | `hfvg/activities/cast_sheet.py`, `hfvg/workflows/cast_sheet.py` |
| API routes | `api/cast_routes.py` (registered from `api/main.py`) |
| Web UI | `web/components/CastPanel.tsx` (episode page), `web/app/studio/create/page.tsx` (wizard), `web/lib/cast.mjs` |

## Concepts

- **Library.** An episode in a series uses the series library, which every episode of the series shares.
  An episode without a series has its own library. Nothing else changes for episodes without a cast.
- **Character.** `character_id` (internal), `slug` (the id shots use, unique per library, e.g. `mara`),
  `name`, `role`, `description` (how they look, the same in every shot; up to 600 characters).
- **Version.** A character's outfit: `version` (1, 2, ...), `label`, `outfit` (up to 600 characters: it
  goes into still prompts beside the description and never into a clip prompt), `derived_from_version`,
  `sheet_stage` (`none` → `options_ready` → `master_picked` → `turnaround_ready`), `locked`,
  `lock_hash`, `locked_description`. A shot without a version uses the character's default version:
  the first locked version (the main look; an outfit can only derive from a locked, lower version),
  else version 1. Locking an outfit later never changes shots that did not pick it.
- **Images.** Each version has images with a role: `upload` (the creator's reference pictures, one
  marked primary), `master_option` (generated look options), `master` (the picked look),
  `turnaround_front`, `turnaround_three_quarter`, `turnaround_side`, `turnaround_back`, `head_sheet`.
  Generated images replaced by a newer try are kept as `discarded` rows; files are never overwritten.

## Reference uploads

`POST /api/episodes/{id}/cast/{character_id}/versions/{version}/images` (multipart):

| Field | Meaning |
|---|---|
| `image_file` | PNG, JPG or WebP |
| `rights` | Required: `original` (the creator made it) or `owned` (the creator owns the rights to use it) |
| `depicts_real_person` | `true` when the picture shows a real person |
| `likeness_consent` | Required `true` when it shows a real person: that person agreed to their likeness being used (the wizard's `likeness_consent` gate, `hfvg.genre_presets.Gate.LIKENESS_CONSENT`) |
| `note`, `primary` | Optional note; make this the version's primary picture |

Checks, in order: a body over `CAST_UPLOAD_MAX_BYTES` (default 15 MiB) gets **413** before the form is
parsed (`CastUploadLimit` middleware: Content-Length first, then the body is counted as it arrives).
Then the rights answers (**400** without them), a real decode with Pillow (format from the file's
content, not its name or content type), at least `CAST_MIN_IMAGE_PX` (default 128) px on each side, at
most 40 MP, at most 12 uploads per version. A picture whose short side is under `REF_MIN_SHORT_SIDE`
(default 512) is kept as uploaded with a `size_note` ("Small image, we'll sharpen it up."; under 256 px
the note asks for a bigger one), and the copy a model gets is upscaled with Lanczos
(`hfvg/image_normalize.py`). Identical bytes in the same version are de-duplicated by
sha256 (the existing picture is returned with `duplicate: true`). A locked version refuses uploads
(**409**). Every upload, delete, sheet step, lock and series change is written to `cast_audit_log`
with the actor and the attestation.

Files live under `CAST_STORAGE_DIR` (default `<DATA_DIR>/cast`):

```
<cast root>/
├── images/<sha[:2]>/<sha256>.<ext>   # uploads and sheet images, written once (mode 0640)
├── thumbs/<sha256>.jpg               # 384 px previews for the Cast panel
└── composites/<sha256>.png (+ .json) # lineup and grid sheets built by planning, with their sources
```

Nothing serves this directory statically. `GET /api/cast/images/{image_id}` and `.../thumb` stream
files to signed-in studio users only (`Cache-Control: private`).

## Who is in each shot (`shots.json`)

```json
{"id": "S03", "prompt": "...", "cast": ["mara", {"character": "pip", "version": 2}], "allow_text": false}
```

- `cast`: up to 6 entries, a slug or `{character, version}`. The Cast panel's "Who is in each shot"
  writes this (`POST /api/episodes/{id}/cast/shots`), and so does the create wizard.
- `allow_text` (optional): `true` lets this shot's stills contain words (a sign, a title card), and
  `false` forbids them even when the kit allows text.
- `prompt_kit.json` `text_policy` (optional): `"no_text"` or `"allow_text"`. Draft-from-idea kits are
  written with `"no_text"`.

The text policy of a shot is its `allow_text`, then the kit's `text_policy`, and with neither a shot
with a cast gets `no_text`. Shots without a cast in kits without a `text_policy` carry neither key,
so their prompts, lint and refs are exactly as before. Explicit `ref_keys` and `refs.json` keep
working and come first.

## Planning (`hfvg/cast_plan.py`)

Planning runs where `shots.json` is planned: in the run's parse activity (`load_shot_plans`, so the
result is recorded in workflow history), in the API's preflight / start checks, and in the Cast
panel's preview. For each shot with a cast:

1. **Identity refs** per character: the locked sheet of the version (master, head sheet, 3/4, side,
   back, front), or before a lock its uploads (primary first). A live run never uses dry-run
   placeholder images.
2. **Lineup.** With two or more characters in frame, a lineup image (each character's first ref side
   by side) leads, so the model sees who is who.
3. **Room.** The still model's `max_refs` from `hfvg/data/model_caps.json` (lowered by
   `MODEL_MAX_REFS_JSON` if set), minus 1 reserved slot for the start still or anchor that
   `hfvg/still_refs.py` adds, minus the shot's own refs. When the cast refs do not fit, the last slot
   becomes **one grid sheet** of everything left: nothing is dropped silently, and the plan notes say
   what was merged.
4. **Keys.** `cast:img:<sha256>.<ext>` and `cast:comp:<sha256>` resolve from the key alone, so they
   need no `refs.json` entry and the G2 re-check finds them.
5. **Prompt facts.** `shot_meta.cast` holds each character's id, version and locked description
   clause, and `shot_meta.text_policy` holds the policy.

Errors refuse the run, dry or live, like a `refs.json` mismatch: an unknown character, a missing
version, or a locked sheet whose lock hash or files no longer match (`verify_lock`). They appear in
preflight (`cast_errors`), in the shots upload response (`cast_problems`) and in the Cast panel. The
preflight checks the cast only for a caller who may see it ([Who may do what](#who-may-do-what)):
for anyone else it answers `cast_hidden: true` and no `cast_errors`, and the start still refuses a
bad cast.

## Prompts

`submit_still_job_enforced` (dry and live) appends, after the continuity locks and before the
prompt lint:

- once, `"Characters in frame, each exactly as described and as in the reference images:"`, then per
  character in frame `"<Name>: <description>; outfit: <outfit>."` (the locked description for a
  locked version);
- under `no_text`: `"No text, no captions, no speech bubbles, no lettering."`.

Injection is idempotent. Lint rule 13 (`hfvg/prompt_lint.py`, still prompts only) requires both and
names prompt phrases that ask for on-image words (`speech bubble`, `caption`, `lettering`,
`reads: "..."`), so a skipped injection or a contradicting prompt is refused live instead of drifting.
The G1 plan previews the same prompts and shows each shot's cast (`preflight.cast`).

## Character sheets

A version's sheet is made in steps, in the Cast panel:

1. **Options.** Either 1–4 front full-body looks from the primary upload (default 2, the upload is
   the identity reference), or 2–4 from the description alone (default 3). A new outfit of a locked
   version makes its options from that version's **locked master**, never from scene stills.
2. **Pick.** One option becomes the master. Picking another clears the turnaround made from the old one.
3. **Turnaround.** Front, three-quarter, side and back full-body views plus a head sheet, each from
   the master.
4. **Approve.** The version is locked: `locked = 1` plus `lock_hash`, a sha256 over the character
   snapshot and the sheet image hashes. A locked version refuses uploads, sheet steps and image
   deletes. Editing the character's description later does not change it: the version keeps the
   `locked_description` it was locked with. Deleting a character with a locked sheet needs an admin
   with `force` and a reason. Before the lock, any step can be rejected and made again. Each try gets
   new job ids, so a paid job id is never reused.

### Revising a locked look (admins)

`POST /api/episodes/{id}/cast/{character_id}/versions/{version}/revise-lock` with `description`,
`reason` and optionally `outfit` rewrites the words of a locked version. Its `locked_description`
becomes `"<Name>: <description>; outfit: <outfit>."`, using the character's current name and, without
`outfit`, the version's own outfit. `lock_hash` is then made again over the same sheet pictures. The
revise form in the Cast panel shows for admins only (`can_revise_locks` in the cast view).

- Only an admin may revise (**403** for members, with no `allow_members` on the route). Revising is
  accepted while runs are open. Text is checked against the content policy like any character text.
- A lock that no longer verifies (`verify_lock`: the rows or a sheet file changed) is refused with
  **409**, so a revision never re-blesses a changed sheet. Repeating the current words is refused too.
- The audit log records each revision (`version_lock_revise`, with the reason, the words, outfit and
  lock hash before and after), and the version lists them in `lock_revisions`.
- The pictures, the version number and the shots that use the version stay the same. New plans
  (preflight, the Cast panel, a new run) use the new words. A run keeps the words it planned with
  until its plans are resynced.

Sheets use the server's still model (`MODEL_PATH_STILL`, default `xai/grok-imagine-image-2.0`) and only
when `model_caps.json` lists it as routed by the REST provider. `gpt_image_2` (`mcp_only_not_on_api`)
and other models the provider refuses cannot be chosen. A model whose reference limit is too small
for a step is refused with the reason.

### Money: the guarded path

- `POST .../sheet/estimate` asks the provider for each image's price (fail closed: **503** if the provider
  gives none). The panel shows "N pictures, about $X" before anything is made.
- `POST .../sheet/generate` takes `approved_total_usd_micros`. It refuses (**409**) when the fresh
  estimate is above what the creator approved, or when the balance gate
  (`BalanceMonitor.check_preflight_balance`) fails. Then it starts a `CastSheetWorkflow`.
- `generate_cast_sheet_image_activity` repeats the guards for every image, in the order of the still
  submit: provider key, smoke gate, episode live mode, reference hashes, provider estimate within the
  approved share, balance gate, credit reserve, `L1_refs` USD reserve on the requesting episode
  (episode cap, line cap, stop threshold, GC.01 balance required), submit with an idempotency key,
  job ledger row with the episode owner (dashboard accounting), poll with heartbeats, commit with the
  actual cost when reported, store. A failure releases both holds. A poll that runs out keeps them and
  hands the job to the reconciler (job type `cast_sheet`).
- The workflow runs the images one at a time with `maximum_attempts=1` (a paid call is never retried
  automatically) and stops at the first failure or refusal; the rest are marked `skipped`.
- No route or flag skips the estimate or the balance gate. Paid sheets need the episode in live mode.
  In dry-run (`DRY_RUN=true`), every step makes free, deterministic placeholder pictures labelled
  "stand-in". A placeholder sheet can be locked only in dry-run, and live runs never use placeholders.

## Replay safety and runs

- `EpisodeWorkflowV2` code is unchanged. Cast planning happens inside activities (parse, G1 plan,
  still submit), and their results are recorded in history, so open runs replay unchanged.
  `CastSheetWorkflow` is a new workflow type, and the reconcile workflow branches only on the new job
  type `cast_sheet`.
- A run plans its references once, at its start. A sheet locked after a run planned its shots is used
  from the next run (Start again or a new episode). While the episode's run is open, the API refuses
  changes to who is in each shot. Deleting characters or pictures and changing the series are refused
  while any episode that shares the cast has an open run (**409** with the run id).
- The cast store, planning (lock hashes, lineup and grid sheets) and reference normalization are
  synchronous SQLite, file and Pillow work. The API calls them through a thread, and the activities
  (the parse activity's planning, the G2 re-check, sheet images, the still provider's reference
  upload) use `asyncio.to_thread`, so neither a slow plan nor a lock wait holds up the worker's event
  loop and the other activities on it (`tests/test_cast_event_loop.py`). The activities' names,
  arguments and results are unchanged by this.

## Who may do what

Every route requires a signed-in studio user. Episode routes follow [episode ownership](AUTHENTICATION.md#episode-ownership).

**Seeing a cast** (the cast and character views with their versions, sheet jobs and audit, pictures
and thumbnails, the series list and series view, and the preflight's cast check):

- admins (session or Bearer token) see every cast;
- an episode's own cast is seen by whoever may change the episode: its owner, or every member while
  the episode has no owner;
- a series cast is seen by the series' owner and by whoever may change one of the series' episodes.

Everyone else gets the answer a missing item gets, so a refusal does not tell whether a cast,
series, character or picture exists: **404** with `Series <id> not found`, `Image <id> not found`,
or, for an episode's cast and its characters, one message for both. The check comes before anything
is read or made (no thumbnail is made for a refused request). `GET /api/series` leaves out the series
the caller may not see, and linking an episode to such a series answers like a missing series.

**Changing a cast** needs the episode's owner (or an admin), and for a series cast also the series'
owner (or an admin). A series without an owner (a legacy one, or one created with the admin Bearer
token) changes only through an admin (**403**): nobody claims it by editing it first, which would
hand it to whoever comes first. The owners of its episodes still see it. Responses include
`can_edit` so the UI hides what a member cannot change.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CAST_STORAGE_DIR` | `<DATA_DIR>/cast` | Private image storage; the API and the worker must see the same directory |
| `CAST_UPLOAD_MAX_BYTES` | `15728640` | Largest upload (413 above it) |
| `CAST_MIN_IMAGE_PX` | `128` | Smallest width and height of an upload |
| `REF_MIN_SHORT_SIDE` | `512` | Short side a smaller reference is upscaled to before a model sees it; uploads under it get a `size_note` |

Existing settings that apply: `DATA_DIR`, `DATABASE_PATH`, `DRY_RUN`, `MODEL_PATH_STILL`,
`MODEL_MAX_REFS_JSON`, the dollar budget and balance settings.

## Deploy and migration

- **Migration** `2026-11-cast-library-v1` creates new tables only (`series`, `episode_series`,
  `cast_characters`, `cast_versions`, `cast_images`, `cast_sheet_jobs`, `cast_audit_log`) and records
  itself in `schema_migrations`. Existing tables and rows are not touched. It runs at API startup
  (`run_all_migrations`) and on the first cast store connection, and is safe to run again.
- **Rebuild** the `api`, `worker` and `web` images. The worker registers `CastSheetWorkflow` and the
  three sheet activities, so deploy the worker before or with the API, or a sheet request waits in
  the queue until a worker that knows it starts.
- **Storage**: no new volume. With docker-compose, `DATA_DIR=/data` on the existing `./data:/data` bind
  mount, so images land in `./data/cast` on the host. Include it in backups with `studio.db`. Never
  expose it through a static file server or a reverse-proxy file route: the API is the only way in.
- **Live sheets** need what live stills need: the provider key, a passing smoke test, the episode in
  live mode and a recorded Higgsfield balance.

## Extension points (later PRs)

- **Vision identity judge.** Built behind `HFVG_QC_JUDGES_ENABLED` (off by default,
  [QC-JUDGES.md](QC-JUDGES.md)): `review_still` and `review_clip` check every `locked: true` entry of
  `shot_meta.cast` against its lock (locked description, master, turnaround and head sheet,
  `lock_hash`). Drift fails the still or clip, which is re-rolled only through the bounded
  `retry_shot` / `retry_clip`; an unsure check or a lock that changed on disk goes to a person. An
  automatic retry is not part of it.
- **Seedance reference-to-video.** `shot_meta.cast` and the cast ref keys can be passed to a clip
  route. Clip planning ignores the cast today.
- **Higgsfield Elements / Soul ID.** A locked version (master + turnaround + `lock_hash`) is the input
  an element or identity training would take. The `source` and `role` columns can record such assets.
- **Named-IP checks on kits.** Character descriptions and sheet prompts already pass the content
  policy hard-stop and banned-word checks (`hfvg.content_policy`). A named-IP list would plug in there
  and in the kit upload validation.
