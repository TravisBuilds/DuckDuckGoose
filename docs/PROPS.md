# Props and Places, and the Shot Plan Editor

Props drift like characters do. Without a reference, every still guesses again what the umbrella or
the cabin looks like, so a prop changes colour, shape or size from shot to shot. The props and places
library keeps each thing's and setting's name, short description and reference photos next to the
cast (docs/CAST.md). Each shot picks its place and props in the shot plan. Planning then sends their
photos with that shot's still and adds their descriptions to its prompt.

The shot plan editor is where the picks happen. Before the run starts, and while the run waits for
shot plan approval (G1), the episode's owner (or an admin) can change each shot's place, props, cast
and prompts. Every change is kept in the shot's history.

For the creator-facing steps see [CAST-HOWTO.md](CAST-HOWTO.md#7-props-and-places).

| Piece | Code |
|---|---|
| Store: props and places, their photos | `hfvg/prop_store.py` (files in the cast image store, `hfvg/cast_store.py`) |
| Migration `2026-11-props-library-v1` | `hfvg/migrations.py` |
| `shots.json` `props` / `place`, the prompt clauses, the clip "keep" line, lint rule 13 | `hfvg/cast_prompt.py`, `hfvg/shot_data.py`, `hfvg/prompt_lint.py` |
| Planning: photos into refs, `shot_meta.props` | `hfvg/cast_plan.py` |
| Clip prompt length at G1 with the "keep" line | `hfvg/preflight.py` |
| Shot plan edits: the G1 re-plan, the history | `hfvg/plan_resync.py` (`plan_replan`), `hfvg/workflows/episode_v2.py` (`replan_shots`), `hfvg/shot_plan_edits.py` |
| API routes | `api/prop_routes.py`, `api/shot_plan_routes.py` (registered from `api/main.py`) |
| Web UI | `web/components/PropsPanel.tsx`, `web/components/ShotPlanEditor.tsx`, `web/components/RightsQuestions.tsx` (shared with the Cast panel), `web/lib/props.mjs` |

## Concepts

- **Library.** The cast's library: an episode in a series uses the series library, shared by every
  episode of the series. An episode without a series has its own library, which moves into the series
  with the cast (`cast_store.link_episode`).
- **Item.** `prop_id` (internal, `pr_…`), `kind`, `slug` (the id shots use, unique per library across
  both kinds, e.g. `red-umbrella`), `name`, `description` (what it looks like, the same in every shot,
  at most 300 characters). The kind is set when the item is made:
  - `prop` is a thing in the shot;
  - `place` is where the shot happens.
- **Photos.** The creator's reference photos (`pi_…`), at most 8 per item, one marked main (used
  first). At most 60 items per library.

## Adding and photos

- `POST /api/episodes/{id}/props/with-photo` (multipart) adds an item from a photo in one step. The
  request carries `kind`, `name`, `description` and `image_file`, plus the cast upload's rights
  answers: `rights`, `depicts_real_person`, `likeness_consent` and `note`. The item and its photo are
  recorded in one transaction, so a refused photo leaves no item.
- `POST /api/episodes/{id}/props` (JSON) adds an item without a photo. A shot that picks it gets its
  description only, and the plan notes say so.
- `POST /api/episodes/{id}/props/{prop_id}/photos` adds a further photo.

Photos get the cast upload checks (docs/CAST.md, Reference uploads), in this order:

1. A body over `CAST_UPLOAD_MAX_BYTES` gets **413** before the form is parsed. The `CastUploadLimit`
   middleware covers the prop photo paths.
2. Without the rights answers the upload gets **400**. A real person needs their consent.
3. The image is decoded with Pillow.
4. Each side must be at least `CAST_MIN_IMAGE_PX`, and the image at most 40 MP.

The same bytes twice on one item are one photo (`duplicate: true`). Every create, edit, photo upload,
main-photo change and delete is written to `cast_audit_log` with the actor and the attestation.

Files share the cast image store: the same bytes are one file under `CAST_STORAGE_DIR`, written
once. A file is removed only when neither a cast picture nor a prop photo uses it. Photos are served
only by `GET /api/props/photos/{image_id}` and `.../thumb` (`Cache-Control: private`), never as
static files.

Deleting an item or a photo is refused (**409** with the run id) while any episode that shares the
library has an open run, because the run planned its references from it. Adding and editing are not
refused: a run keeps the words and photos it planned with.

## Picking them in a shot (`shots.json`)

```json
{"id": "S03", "prompt": "...", "cast": ["mara"], "place": "harbour-cabin", "props": ["red-umbrella"]}
```

- `place` is one place id. `props` is up to 4 prop ids.
- The shot plan editor writes these keys (below). They can also be written by hand.
- The run refuses the plan, dry or live, like a cast error, for any of these:
  - an id the library does not have;
  - a prop picked as the place, or the other way round;
  - the same id twice;
  - the place also listed as a prop.
- These problems appear in the preflight (`cast_errors`), in the shots upload response and in the
  editor.
- Shots without `props` and `place` carry no `shot_meta.props`, so their refs, prompts and lint are
  exactly as before.

## Planning (`hfvg/cast_plan.py`)

The same planning as the cast, in the same places: the run's parse activity, the API's preflight and
start checks, and the editor's preview. For a shot with a place or props:

1. **Refs.** The order is:
   - the cast lineup (two or more characters);
   - each character's first picture;
   - the place's main photo, then each prop's main photo;
   - the characters' further pictures;
   - the further photos.

   The room is the cast's: the still model's `max_refs`, minus the reserved anchor slot, minus the
   shot's own refs. What does not fit goes into the one grid sheet, and the plan notes say so. The
   keys are the cast image keys (`cast:img:<sha256>.<ext>`), so they resolve without `refs.json` and
   the G2 re-check finds them.
2. **Prompt facts.** `shot_meta.props` holds the place, then the props, each as `{slug, kind, name,
   clause}`.

## Prompts

- **Still.** `submit_still_job_enforced` (dry and live) appends text after the characters' clauses:
  once `"Also in this shot, each exactly as described and as in the reference photos:"`, then
  `"<Name> (the setting): <description>."` for the place and `"<Name>: <description>."` for each prop.
  The injection is idempotent. Lint rule 13 requires each clause, so a skipped injection is refused
  live instead of drifting. The G1 plan previews the same prompts and lists each shot's props
  (`preflight.cast[shot].props`).
- **Clip.** One continuity lock with the names: `"Keep these exactly as they look in the first
  frame: Harbour cabin, Red umbrella."`. The routed clip models (Kling 3.0 Pro and Seedance 2.5
  image-to-video) take a start frame and an end frame, not reference images. So the clip gets the
  props through the still it starts from, which was made with their photos. The G1 preflight
  measures the clip prompt with this line (Kling's 2,500-character limit), so a clip that would be
  too long is refused at G1, before any still is paid for.

## Editing the shot plan before approval

`GET /api/episodes/{id}/shot-plan` returns:

- every shot's prompts (`prompt`, `end_prompt`, `clip_prompt`) and its `cast`, `props` and `place`;
- what the picks plan to (`refs` labels, `notes`, `errors`) and the shot's `history` (latest 20);
- the cast and the props and places to pick from;
- `phase`, `may_change` and `can_edit`.

`POST /api/episodes/{id}/shot-plan` takes `{"shots": {"S01": {...changes}}, "note": "..."}`:

- **Fields.** Only the fields sent change.
  - `prompt` (the still prompt) cannot be empty.
  - An empty `end_prompt` or `clip_prompt` removes it; the still prompt is used.
  - An empty `cast`, `props` or `place` removes the pick.
- **Limits.** At most 6,000 characters per prompt and 300 for the note.
- **Checks before the write.** The prompts get the content checks of the cast text. The new
  `shots.json` must pass the run's own parse and cast planning (unknown ids and the wrong kind answer
  **400**).
- **Phases** (`phase.state`):
  - `open`: no run. The change is saved to `shots.json` for the next run.
  - `g1`: a run waits for shot plan approval and the plan is not approved. The API writes
    `shots.json`, then calls the workflow's `replan_shots` update. That update:
    1. re-parses the package with the same parser and path as at the start;
    2. checks the difference (`plan_replan`): only the prompts and the cast, props and place may
       change. These stay as they are: the shot list, each shot's settings (model, length, frames,
       aspect), trim, tags, pre-approved stills, the rest of `shot_meta` and the refs.json refs;
    3. runs the preflight and computes the G1 plan again, with its estimates;
    4. only then applies it.

    When the update refuses, the API puts the previous `shots.json` back and answers **409**, and
    nothing changes.
  - `locked`: the plan is approved, the run is still preparing it, or the run was reopened. The API
    answers **409** with the reason.
- **G1 approval.**
  - It is refused while a change is being checked.
  - Preflight errors from the new plan hold it until they are fixed (no override).
  - The approval checks the recomputed estimates through the estimates guard and the balance gate.
  - The editor keeps **Approve the shot plan** disabled while there are unsaved changes.
- **History.** Each changed field is one `shot_plan_edits` row: `field`, `old_value`, `new_value`,
  `actor`, `actor_name`, `run_id`, `note` and the time. The change is also written to the episode
  audit (`shot_plan_edit`), and the G1 re-plan to the workflow's `replan_log`.

## Who may do what

Every route requires a signed-in studio user and follows
[episode ownership](AUTHENTICATION.md#episode-ownership).

- **Seeing** the library, its photos and the shot plan follows the cast rule (docs/CAST.md, Who may
  do what):
  - admins see everything;
  - an episode's own library is seen by whoever may change the episode;
  - a series library is seen by the series' owner and by whoever may change one of the series'
    episodes.

  Everyone else gets **404**, the answer a missing item gets: one message for the library or the
  shot plan, and `Image <id> not found` for a photo. The check comes before the file is read or a
  thumbnail made.
- **Changing the library** needs the episode's owner (or an admin). A series library also needs the
  series' owner (or an admin), and a series without an owner changes only through an admin (**403**).
- **Changing the shot plan** follows the episode write rule: its owner or an admin, or any member
  while the episode has no owner. Picking from a series' cast and props changes this episode, not
  the series library, so no series rule applies. The view's `may_change` and `can_edit` let the UI
  hide what a member cannot change.

## Replay safety and runs

- The migration only adds tables. Episodes already on disk have no `props` or `place` keys, so they
  plan, prompt and lint exactly as before. Archived runs open as they did; ep98cle still opens as
  finished (`tests/test_archived_run_state.py`).
- `EpisodeWorkflowV2` has these additions:
  - **One new update, `replan_shots`.** Old histories never contain it.
  - **A new refusal in the G1 approval validator** while a re-plan runs. Validators do not run on
    replay.
  - **New state attributes** (`replan_log`, `_replanning`, `_shots_subset`), set without commands.
    `_replanning` is not carried by a reopen snapshot.

  Open runs replay unchanged.
- The re-plan runs existing activities with their names, arguments and results unchanged: the parse
  (a read), the preflight and the G1 plan (estimates only). Activity retries repeat the same reads.
  The workflow applies nothing until all of them succeed. A second change waits for the first: the
  API holds a lock per episode, and the workflow refuses a change while another one is checked.
- A run plans its references at its start, and again at each G1 re-plan. A photo added after that
  is used by the next run (or the next re-plan at G1).

## Money

Nothing in this feature spends:

- prop photos are uploads, not generations;
- picks and prompt edits are file changes;
- a G1 re-plan recomputes the estimates with the existing G1 plan activity.

The approval of the new plan goes through the same estimates guard (`validate_g1_approval`) and
balance gate as before. No route uses `override_unavailable_estimates`, and no cap was added.

## Deploy and migration

- **Migration** `2026-11-props-library-v1` creates new tables only (`prop_items`, `prop_images`,
  `shot_plan_edits`) and records itself in `schema_migrations`. Existing tables and rows are not
  touched. It runs at API startup (`run_all_migrations`) and on the first cast store connection
  (worker, tools), and is safe to run again.
- **Rebuild** the `worker`, `api` and `web` images.
- **Deploy the worker before or with the API.** A shot plan change at G1 calls the workflow's
  `replan_shots` update. With an old worker the update is unknown, so the API puts the previous
  `shots.json` back and answers 409 with "The studio is updating. Try again in a minute." (any
  update a worker does not know yet reads that way). Changes before a run do not need the worker.
- **Storage:** no new volume and no new settings. Photos go into `CAST_STORAGE_DIR` with the cast
  images. The cast limits apply (`CAST_UPLOAD_MAX_BYTES`, `CAST_MIN_IMAGE_PX`).

## Not in this change (follow-ups)

- **Suggest props.** The studio would read the shot prompts and suggest props and places to add (text
  only). The OpenAI calls are not priced by the estimate flow or recorded in the dollar ledger, so a
  new one would be a paid call outside `approved_usd_micros`. It needs a price for text calls first,
  or an explicit decision that they are exempt.
- **Seedance reference-to-video** for clips with props. The model is in `model_caps.json` but not
  routed, and it has no USD rate, so the G1 plan cannot price it and a live approval would be
  refused. It needs a rate and routing.
- **gpt_image_2 references.** The model is MCP-only and the repository has no REST adapter for it.
- **Kit continuity locks in the G1 clip length check.** This gap predates the feature. The preflight
  now counts the props "keep" line, but not the kit's own clip locks, which the clip submit also
  appends.
