# First frames (concept frames)

Before a video is filmed, its owner can pick the picture each shot starts from. The studio writes the
picture prompt itself from what the episode already knows (the shot's story moment, its words, its
characters, place and props and their reference photos), makes a few options, and the owner picks one,
tries again, or asks for a change in plain words ("make it night", "closer on her face"). The picked
option becomes the shot's start frame: the run starts the shot from it and does not pay for that still.
First frames are optional; a shot without one gets its start still from the run, as before.

In the UI the step is called **First frames**. In the code and the API it is **concept frames**.

| Piece | Code |
|---|---|
| Store: the brief of a shot, the prompt adapters, rounds and options, the pick, the view | `hfvg/concept_frames.py` |
| Migration `2026-11-concept-frames-v1` | `hfvg/migrations.py` (`CONCEPT_FRAMES_MIGRATION`) |
| Paid option, end of a round, the reconciler's pass | `hfvg/activities/concept_frame.py` |
| The workflow and its task queue (registered by `production_workers` in `hfvg/worker.py`) | `hfvg/workflows/concept_frame.py` |
| Reconciler routing for `cframe-...` jobs | `hfvg/activities/reconcile.py`, `hfvg/workflows/reconcile.py` |
| API routes | `api/concept_frame_routes.py` (registered from `api/main.py`) |
| Episode page section | `web/components/ConceptFrames.tsx`, pure views in `web/lib/frames.mjs` (node tests) |
| Start card (a video that waits on its page) and the wizard's choice | `web/components/UniverseEpisode.tsx` (`StartCard`), `web/app/studio/create/page.tsx` |

## For the owner

- **Where.** The episode page has a First frames section after the shot plan, while no run is going:
  before the video starts, and after a run ended (for the next run). While a run is filming, the
  section only shows the frames it started from.
- **Each shot** says what its start frame shows in a few plain words ("Wren at Harbour cabin, with
  Brass key, dusk"; after "make it night" and "closer on her face": "..., night, close on her face"),
  the story moment (a camera label in front of the beat, such as "cinematic shot:", is left out), how
  many reference photos guide it, naming once who has no photo yet ("No reference photos of Lead yet:
  the words guide it"), and where it stands: no start frame yet (optional), options ready, being
  made, start frame locked, or the shot changed since its options were made. The shot's picture
  words (shots.json `prompt`) are never sent to the page: the view carries `description`, built on
  the server (`describe`), and no `scene`.
- **The price first.** "See 3 options" asks for the price; a test run shows "Free". "Make 3 options"
  makes them; live, the button carries the price ("Make 3 options for $0.09") and the price block shows
  what is left in the episode's draft pictures budget.
- **Pick, try again, or say what to change.** "Use this one" locks an option as the shot's start frame.
  "Try again" makes another round of the same shot. A change in plain words ("make it night", the
  example buttons fill the box), sent with "Try this change", is written into that round and every
  later one (the last four changes are kept) until "Start over", which goes back to the shot as it is.
  Earlier rounds stay under "Earlier tries".
- **The wizard** (`/studio/create`) has "Pick each shot's first frame before filming", off by default.
  On, the video waits on its page (like a Universe video waiting for its locks) and the start card
  there starts it with the request the wizard kept: the same test run or the live run whose price was
  confirmed. The start card says how many shots start from a frame the owner picked.
- **Nobody picks or sees a model.** The page, the API responses and the audit never name the picture
  model, the adapter or the prompt.

## What the studio writes

The brief of a shot (`shot_brief`) comes from the files the run itself reads:

- the story moment: the shot's beat in BEATMAP.md (action, purpose, time of day);
- the shot's picture words (shots.json `prompt`) and its own reference pictures (refs.json);
- its characters, place and props through the run's own cast planning (`hfvg.cast_plan`): each one's
  description and reference picture, with the same picks (the shot plan's) and the same reference
  limit as the run;
- the Universe's logline and the episode's style line.

A **prompt adapter**, chosen from the server's still model (`MODEL_PATH_STILL`, by id or alias in the
model caps registry), lays the brief out the way that model reads best:

| Adapter | Models | Shape | Length limit |
|---|---|---|---|
| story | `xai/grok-imagine...` | a short paragraph: the moment, who, where, the references, the camera | 2,000 |
| sections | `openai/gpt-image...` | labelled lines (scene, characters, place, props, references, changes, frame) | 4,000 |
| edit | `alibaba/qwen-image...` | one instruction line that names each reference image by number | 1,200 |
| tags | `higgsfield-ai/soul...` | comma-separated phrases | 600 |
| plain | any other model | like story | 1,500 |

A lower `prompt_max_chars` in the model's caps wins. When a prompt is too long the scene words are
clipped first, so the characters, the references and the owner's changes always reach the model. A
change that names a time of day replaces the beat's time of day. Each option of a round tries its own
camera idea on the same moment. The model's caps are checked before anything is priced (routed
status, reference count, resolution, quality and aspect ratio), and the content policy reads every
prompt (`prompt_problems`): words it refuses are a plain 400 before any spend.

## Money

**Test run** (`DRY_RUN=true`): the API makes free placeholder options at once (a sky, a ground and one of
the shot's reference pictures, 576 by 1024 for 9:16, no text). Nothing is reserved, submitted or
recorded in the ledgers. A placeholder can never become a live run's start frame.

**Live**, the API checks, in this order, before anything is recorded or started (each refusal is a plain
409 or 503 and nothing is charged):

1. the episode is in live mode;
2. a worker polls the first frames task queue (`<TASK_QUEUE>-frames`): none is a 409, Temporal not
   answering a 503;
3. the provider's own price of every option (fail closed: no price is a 503);
4. the total is within what the owner approved (`approved_total_usd_micros`, the estimate they saw);
5. it fits what the episode's draft pictures line (`L2_drafts`) has left before its stop;
6. a recorded balance that is fresh enough (GC.01);
7. the balance gate (`BalanceMonitor.check_preflight_balance`).

Then the round and its options are recorded, each option gets its share of the approved total, and
`ConceptFrameWorkflow` starts on the first frames queue. A workflow that cannot start ends the round at
once (nothing was made or charged).

**On the worker** every option is one paid still on the guarded path of the character sheets: the
episode's gates (credentials, smoke test, live mode), the reference photos unchanged since the plan,
the provider estimate within the option's approved share, the balance gate, a credit hold and a dollar
hold on `L2_drafts` (`require_balance`), an idempotent submit (`<option id>-<hash>`), the provider job
persisted, a job ledger row in the episode owner's name (tier `draft`), poll, commit, the actual cost,
and the picture kept. The first refusal or failure stops the round; the options already made stay. A
call whose result is not known after polling keeps its holds and goes to the reconciler
(`cframe-...` jobs, settled by `reconcile_concept_frame_job_activity`). No step skips the provider
estimate or the balance gate, and nothing here sets `override_unavailable_estimates`.

## The pick

- `POST .../pick` copies the option into the episode (`concept_frames/<shot>-<sha12>.png` next to
  shots.json) and writes it as the shot's `pre_approved_still` (path, sha256, approved_by). The run
  reads it like any pre-approved still (`hfvg.pre_approved`): the shot starts from it and its still
  is not paid for.
- Refused: an option that was not made; a test-run placeholder when the studio runs live; an option
  made before the shot changed (its brief no longer matches: the shot's words, cast, place, props or
  their reference photos); a start frame someone set in shots.json by hand (it stays; the view says so).
- `POST .../unpick` takes a frame picked here off the shot; the run makes that still again.
- Only while no run is going (the shot plan's phase is "open"), under the shot list lock.
- In a Universe, every character in the shot must have a locked look first, so the frame shows the look
  the video keeps.

## Who may do what

| What | Who |
|---|---|
| See the shots, their options and picks | Whoever may see the episode's cast (else the episode's 404) |
| See each shot's story moment and words | Whoever may read the episode's prompts (admins, the Universe's owner, whoever may change the episode) |
| Price, make, pick and take off a first frame | The episode's owner or an admin (any member for an episode without an owner) |

## Routes

| Route | What |
|---|---|
| `GET /api/episodes/{id}/concept-frames` | Every shot: what it shows, its rounds and options, its pick, and whether the caller may change them now |
| `POST /api/episodes/{id}/concept-frames/{shot}/estimate` (`{"change", "keep_changes"}`) | The next round's options and price; nothing is made |
| `POST /api/episodes/{id}/concept-frames/{shot}` (`{"change", "keep_changes", "approved_total_usd_micros"}`) | Make the round the owner approved |
| `POST /api/episodes/{id}/concept-frames/{shot}/pick` (`{"option_id"}`) | Use an option as the shot's start frame |
| `POST /api/episodes/{id}/concept-frames/{shot}/unpick` | Stop using it |
| `GET /api/episodes/{id}/concept-frames/images/{option}` and `.../thumb` | An option's picture and its thumbnail (private, never a static file) |

## Settings

| Setting | Default | What |
|---|---|---|
| `CONCEPT_FRAME_OPTIONS` | 3 | Options per round, kept within 1 to 4 |

The still model is the server's (`MODEL_PATH_STILL`); resolution and quality are the run's still defaults.

## Migration

`2026-11-concept-frames-v1` creates new tables only and records itself in `schema_migrations`:

| Table | What |
|---|---|
| `concept_frame_sets` | A round of options for a shot: its changes, the brief it was made from, the adapter and model, dry run or live, the estimate and approval, its workflow, status and the reason it stopped early |
| `concept_frame_options` | Its options: prompt, status, estimate and approved share, the picture kept (files in `CAST_STORAGE_DIR`) |
| `concept_frame_picks` | Which option is a shot's start frame, and the ones it replaced or that were taken off |

Existing tables and rows are not touched and no data is edited. Episodes from before have no rounds and
no picks: the section offers first frames for their shots, and their runs read shots.json as before. A
start frame set in shots.json by hand shows as "set in the shot list" and is never replaced.

## Deploy

API, worker and web. **Worker first** (or together with the API): it polls the first frames queue
`<TASK_QUEUE>-frames` (`hfvg-tasks-frames` by default) with `ConceptFrameWorkflow`, and its reconciler
settles `cframe-...` jobs. No running workflow changes what it does: the reconcile workflow only gains a
branch for the new job type. **Then the API**, which applies the migration on startup; while no worker
polls the first frames queue, live first frames are a plain 409 before any price is asked; test runs work
either way. **Then the web.**

**Rolling the worker back** below this change: first make sure no first frame option holds money (no
`cframe-...` job reserved or waiting for the reconciler), because an older reconciler does not know them.
