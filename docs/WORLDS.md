# Universes (world first, phase 1)

A Universe keeps what every episode shares: its characters with one locked look each, its places and
props with their photos, its story bible and style notes, and its voices. The studio starts from it:
the home lists the Universes first, a new episode starts from a Universe page with its cast already
matched, and the episode page shows which Universe it is in and what it uses from it.

In the UI the concept is called **Universe**. In the code and the API it is a **series** (`series`
table, `sr_` ids, the series cast and props libraries of [CAST.md](CAST.md) and [PROPS.md](PROPS.md))
and its routes live under `/api/worlds`. Nothing about series changes: a Universe is a series with a
profile.

| Piece | Code |
|---|---|
| Store: profile, saved voices, creation notes and photos, mood boards and live board requests, bible, used-in, unlocked cast, list summaries | `hfvg/world_store.py` |
| Migrations `2026-11-world-profile-v1` and `2026-11-universe-board-requests-v1` | `hfvg/migrations.py` (`UNIVERSE_MIGRATION`, `UNIVERSE_BOARD_MIGRATION`) |
| Live mood boards: the pictures of a board, their reference photos, the budget line and ledger key | `hfvg/universe_boards.py` |
| Live mood boards: the paid picture, the end of a request, the reconciler's pass | `hfvg/activities/universe_board.py` |
| Live mood boards: the workflow and its task queue (registered by `production_workers` in `hfvg/worker.py`) | `hfvg/workflows/universe_board.py` |
| Draft context and the test drafter's Universe names | `hfvg/world_store.py` (`draft_context`), `hfvg/genre_drafts.py` (`fake_draft`) |
| API routes | `api/world_routes.py` (registered from `api/main.py`) |
| Required lock (start, start again, shot plan approval, picks at the shot plan) | `api/main.py` (`_refuse_unlocked_universe_cast`), `api/shot_plan_routes.py` |
| Web pages | `web/app/studio/page.tsx` (home), `web/app/studio/universes/new/page.tsx`, `web/app/studio/universes/[id]/page.tsx`, `web/components/UniverseCreation.tsx` |
| Episode page pieces | `web/components/UniverseEpisode.tsx` (breadcrumb, Add to a Universe, strip, Start card) |
| Wizard (`/studio/create?world=<id>`) | `web/app/studio/create/page.tsx`, `web/components/VoiceSlotPicker.tsx` |
| Pure views (node tests) | `web/lib/consistency.mjs`, `web/lib/world.mjs`, `web/lib/wizard.mjs` (`savedVoiceChoices`) |

## Concepts

- **Universe.** A series with an owner (`series.owner_user_id`) and a profile (`series_profiles`):
  logline, genre (the default video card, `preset_id`), cover picture, story bible, style notes and the
  narrator voice. A series without a profile row reads as the empty profile.
- **Characters, places, props.** The series cast library and the series props library. Their routes
  under `/api/worlds/{id}` are the episode routes (same checks, limits, rights attestation, private
  storage and audit) on the library `("series", id)`. No character sheet is made from the Universe
  page: a look is locked from an episode in the Universe, where the paid sheet steps, their estimates
  and the balance gate already are.
- **Saved voices.** A character's voice (`character_voices`) and the Universe's narrator voice
  (`series_profiles.narrator_*`): the voice id, name, description and preview link that
  `POST /api/voices/pick` returned. Saving calls no provider and stores no key. Deleting a character
  deletes its saved voice.
- **Creation references.** Notes by topic (premise, characters, places, tone; at most 40, 2,000
  characters each) and reference photos (at most 12, the cast upload checks and rights questions).
- **Mood board.** Four pictures made from the references (in live mode, paid pictures the worker
  makes: see [Live mood boards](#live-mood-boards)). Each board is a version: `ready`, then
  `approved` or `redo_requested` (with the creator's feedback, which the next version answers). Only
  the latest board can be approved, and a board made meanwhile refuses an older plan. A live board
  that stopped early is a version with the pictures it made and the reason it stopped.
- **Bible.** Written once per approved board from the board and the references: up to 8 characters,
  6 places and 8 props are added (names the Universe already has stay as they are), plus the story
  bible and style notes when they are still empty. `DRAFT_LLM_FAKE=1` writes a deterministic test
  bible from the "Name: description" lines of the notes (dry run only).
- **Consistency.** Each character version shows one badge: "Locked vN", "Not locked yet", "Older look"
  (an older locked look than the newest) or "Look changed since lock" (`cast_store.verify_lock` found
  changed pictures). Places and props show "Photo set" or "No photo" (phase 1 has no locks for them).
  The health line reads "3 of 4 characters locked", and "Used in N episodes" counts each episode once
  per item (versions named in shots are counted per version).
- **Required lock.** An episode in a Universe does not start (and does not start again) before every
  Universe character its shots use has a locked look, its shot plan is not approved before that, and
  at the shot plan a character without a locked look cannot be put in a shot. The refusal is a 409
  that names the characters and says where to lock them, and the episode page shows the same as a
  Start card with a link to Cast. Episodes outside a Universe are not affected. A run plans its
  references when it starts, so a look locked later would not reach it.

## Who may do what

| Action | Who |
|---|---|
| See a Universe (page, card, its pictures that are not private) | Admins, its owner, and whoever may change one of its episodes (an episode editor). Everyone else gets the 404 a missing Universe gets (`Universe <id> not found`), with no names or pictures. |
| Change it (profile, characters, places, props, saved voices, notes, reference photos, mood board, bible) | Its owner or an admin. An episode editor gets 403. A Universe without an owner (made by a Bearer request, or before series had owners) changes only through an admin. |
| See its story bible, style notes, saved voices, notes, reference photos, open mood boards and the creation flow | Its owner and admins. An episode editor sees the cover and an approved mood board, not the references. |
| Start a new episode from it (button on its page) | Its owner and admins. An episode editor sees a note instead. |
| Put an episode in it ("Add to a Universe", `POST /api/episodes/{id}/series`) | Whoever may change the episode, if they also own the Universe (or are an admin). |
| See an episode's Universe, strip and locks (`GET /api/episodes/{id}/universe`) | Whoever may see the episode's cast. Everyone else gets only who made the episode and that they may not act on it. |
| Create a Universe | Any signed-in member (they own it). |

Nobody else receives prompt text: the mood board prompt is in the owner's creation flow only, and the
episode page reads its "Made with" chips from the shot plan only for whoever may act on the episode.

## Routes

Every route allows members and runs under the app-wide guard. "Viewer" means the "see" row above,
"owner" the "change" row.

| Route | Who may call it |
|---|---|
| `GET /api/worlds` | Any member: only the Universes they may see |
| `POST /api/worlds` | Any member |
| `GET /api/worlds/{id}` | Viewer (private parts for the owner) |
| `POST /api/worlds/{id}/profile` | Owner |
| `POST /api/worlds/{id}/cast`, `/cast/with-picture`, `/cast/{character_id}`, `/cast/{character_id}/delete` | Owner |
| `POST /api/worlds/{id}/cast/{character_id}/versions/{version}/images` (and `/{image_id}/primary`, `/{image_id}/delete`) | Owner |
| `POST /api/worlds/{id}/cast/{character_id}/voice` | Owner |
| `POST /api/worlds/{id}/narrator-voice` | Owner |
| `POST /api/worlds/{id}/props`, `/props/with-photo`, `/props/{prop_id}`, `/props/{prop_id}/delete` | Owner |
| `POST /api/worlds/{id}/props/{prop_id}/photos` (and `/{image_id}/primary`, `/{image_id}/delete`) | Owner |
| `POST /api/worlds/{id}/notes`, `/notes/{note_id}`, `/notes/{note_id}/delete` | Owner |
| `POST /api/worlds/{id}/photos`, `/photos/{image_id}/delete` | Owner |
| `POST /api/worlds/{id}/board/estimate` (with `feedback` for a redo) | Owner |
| `POST /api/worlds/{id}/board` (with `feedback` for a redo) | Owner, with `approved_total_usd_micros` at or above the estimate |
| `POST /api/worlds/{id}/board/{board_id}/approve` | Owner |
| `POST /api/worlds/{id}/bible` | Owner |
| `GET /api/worlds/images/{image_id}` and `/thumb` | Owner; a viewer only for the cover and an approved board |
| `GET /api/episodes/{id}/universe` | Any member (see the table above for what each gets) |

Deletes are refused (409) while any episode of the Universe is being made. Uploads keep the cast
limits (`CAST_UPLOAD_MAX_BYTES`, `CAST_MIN_IMAGE_PX`) and get 413 before anything is stored.

In live mode `POST /api/worlds/{id}/board` answers `{"board": null, "request": ...}` and the worker
makes the board. `GET /api/worlds/{id}` then carries, for the owner, `creation.board_request` (the
board being made with its pictures so far, the last one that failed, or why the latest board stopped
early) and `creation.board.unavailable` (why a live board cannot be started now). The request has no
prompt text.

Changed routes: `POST /api/episodes/draft-from-idea` takes an optional `world_id` (below);
`POST /api/episodes`, `POST /api/episodes/{id}/start-again`, the G1 approval of
`POST /api/episodes/{id}/approve` and `POST /api/episodes/{id}/shot-plan` at G1 apply the required lock.

## Making a Universe

`/studio/universes/new` asks for a name, a genre and a logline. The Universe page then walks its
owner through four steps: references (notes and photos), the mood board (the price first, then make
it, then approve it or ask for a redo with feedback), the characters and places (the bible), and the
pictures and voices of each. A member without a Universe or an episode lands on this from the home
(first run).

**Money.** The mood board is the only paid step. Its estimate comes first and the board is refused
when the estimate is above the approved total; no route uses `override_unavailable_estimates`. In
dry-run mode (`DRY_RUN=true`) boards are free placeholder pictures made by the API. A live board is
made by the worker, picture by picture, as below. The owner can also add characters, places and
props and write the bible by hand. The bible is written by the language model
(`STORY_SHAPING_MODEL`) like a draft, outside the dollar ledger.

## Live mood boards

A live board is four paid still pictures, made one at a time by the worker with the provider adapter
of the episode stills and character sheets (`HiggsfieldStillProvider`) and the server's still model
(`MODEL_PATH_STILL`, only when `model_caps.json` lists it as routed). Each picture gets the board
prompt after its own focus line (the characters together, the main place, the light, the details)
and up to the model's reference limit of the Universe's reference photos, taken in turn so that
every photo reaches some picture.

**Price first.** `POST /api/worlds/{id}/board/estimate` asks the provider for each picture's price
(`POST /estimate/{model}`; 503 when it gives none) and the page shows, before anything is made:
the number of pictures and the provider's price of each, that they are made from the owner's notes
and N reference photos with the still model, and what is left on the Universe's mood board budget
before its stop. Nothing is generated or charged.

**Make.** `POST /api/worlds/{id}/board` with `approved_total_usd_micros`. Before anything is recorded
or charged, the API refuses with a plain message:

1. no worker polls the mood board task queue: **409** (a worker older than live boards), or **503**
   when Temporal cannot be asked;
2. the still model cannot make the board (**409**), the words are refused by the content policy
   (**400**), or the provider gives no price (**503**);
3. the provider's fresh price is above the approved total: **409**;
4. the board costs more than is left before the budget line's stop: **409**;
5. no recorded Higgsfield balance, or one too old (GC.01, `balance_freshness`): **409**;
6. the balance gate (`BalanceMonitor.check_preflight_balance`): **409**.

Then it records the request and its picture jobs (each picture may cost up to its price plus its
share of the headroom the owner approved) and starts `UniverseBoardWorkflow` on the task queue
`hfvg-tasks-universe` (the queue the API starts studio workflows on, `hfvg-tasks`, plus `-universe`;
the worker polls its `TASK_QUEUE` plus `-universe`). The page reads "Making the mood board: 2 of 4
pictures made" and updates itself every 4 seconds.

**Each picture** (`generate_universe_board_image_activity`, one attempt, heartbeats) repeats the
guards of a still, in the same order: provider key, reference photos unchanged on disk, provider
estimate (fail closed) within the picture's approved share, balance gate, credit reserve, USD reserve
on the Universe's budget line (line cap, stop threshold, GC.01 balance required), submit with an
idempotency key from the job id, job ledger row in the Universe owner's name, poll with heartbeats,
commit with the actual cost when the provider reports it, download, keep. A failure before the
provider completes releases both holds. A poll that runs out keeps them and hands the job to the
reconciler (job type `universe_board`, job ids `uboard-...`). A retried activity returns a picture
it already kept (no spend) and resumes a provider job it already submitted instead of submitting
again. No route or flag skips the estimate or the balance gate.

**Stopping early.** The workflow stops at the first refusal or failure. The pictures already made
(and paid for) become the next board version, and the owner reads why under it, for example "The
price of a picture went up after you approved it, so the studio stopped before paying more. Check
the price again." A request that made no picture fails with that reason. The technical error stays
in the job row, the workflow result and the worker log. The owner can approve the shorter board or
ask for a redo.

**Redo with feedback.** A redo is a new request that answers the feedback on the latest board, with
its own estimate, approval and checks. While a board is being made, another board, an approval and
deleting a reference photo are refused (409).

**Budget line.** `U1_mood_board`, booked under the ledger key `universe-<series id>` (never an
episode id, so no episode's cap, stop or spend sees it): cap $4.00 (`LINE_CAP_USD_MICROS` in
`hfvg/universe_boards.py`, the default of the reference line `L1_refs`), stop at
`EPISODE_STOP_PERCENT` of the cap ($3.20 by default). It is created when the first live picture is
paid for and is never reset.

**Cost truth.** Each picture has a job ledger row, so mood board spend appears like any paid job
([ACTUAL-COSTS.md](ACTUAL-COSTS.md)): the stage `mood_boards` in actual vs estimate, the Universe's
owner in spend by person, in-flight holds, the month-end projection, and the usage report match (as
generations of the still model).

**When the worker goes away.** A board whose workflow ended without finishing it (a crashed or
rolled-back worker, a cancelled run, or its 2-hour execution timeout) is finished the next time its
owner opens the Universe page or uses a board route: a board of the pictures made, else failed. A
workflow Temporal does not know counts as ended only 2 minutes after its request was recorded (the
request is recorded just before the workflow starts). The reconciler settles any money still held
for its pictures.

## New episodes from a Universe

The Universe page's "New episode in this Universe" opens `/studio/create?world=<id>`:

- The Universe's genre is the card, and the draft request carries `world_id`. The model gets the
  Universe's name, logline, characters (name, role, look), places and props, and for its owner and
  admins the story bible and style notes. A caller who may not see the Universe gets its 404 and the
  model is not asked. A draft without `world_id` asks the model byte for byte what it asked before
  (`tests/fixtures/draft_input_golden.json`, captured on main before this change).
- In a dialogue card the model is told to give every line to the Universe's first two characters
  (the test drafter does the same).
- Drafted characters are matched to the Universe's by name (case and spaces around it ignored; a name
  two Universe characters share is left for the creator), shown as "Same as in the Universe: Mira ·
  Locked v1" or "New to this Universe".
- Each speaker named like a Universe character starts with that character's saved voice, and the
  narrator with the Universe's narrator voice ("Saved voice: <name>", with "Use a different voice for
  this episode"). Without `?world=` nothing is prefilled and the requests are the same as before.
- When every look the episode uses is locked, the episode starts as before. Otherwise it is set up in
  the Universe and its page opens on Cast, with a Start card that waits for the locks. The start
  request (card, idea, length, dry run) is kept in the browser (`localStorage`
  `ddg:start:<episode id>`) for that card; without it the card starts a free test run.

## Episode page and home

- **Episode page.** A breadcrumb (Universe › episode, or "Add to a Universe" for an episode in none),
  the "From your Universe" strip (the characters, places and props it uses with their badges), the
  Start card while looks are unlocked, and "Made with" chips under each scene picture. A member who
  may not act on the episode sees no "Waiting for you" and no decision buttons.
- **Home.** "Your Universes" first (cover, health line, counts, latest episode), then the episodes:
  Continue, Recently finished (with a player and a download link) and Unfiled (episodes in no
  Universe, each with "Add to a Universe").

## Migration

`2026-11-world-profile-v1` creates new tables only and records itself in `schema_migrations`:

| Table | What |
|---|---|
| `series_profiles` | One row per Universe: logline, bible, style notes, genre, cover, narrator voice, which board the bible was written from |
| `character_voices` | One saved voice per character |
| `universe_notes` | Creation notes by topic |
| `universe_photos` | Reference photos (files in `CAST_STORAGE_DIR`, shared by hash with the cast images) |
| `universe_boards`, `universe_board_images` | Mood boards and their pictures |

`2026-11-universe-board-requests-v1` (live mood boards) does the same, after it:

| Table | What |
|---|---|
| `universe_board_requests` | A live board being made or one that ended: its prompt, the estimate and approval, its workflow id, the board it became, and the reason it stopped early or failed |
| `universe_board_jobs` | Its paid picture jobs: status, estimate, approved share, number of reference photos, and the picture kept (files in `CAST_STORAGE_DIR`) |

Existing tables and rows are not touched and no data is edited. Both run at API startup
(`run_all_migrations`) and on the first cast store connection (worker, tools), after the cast and
props library migrations whose tables they reference, and are safe to run again. The budget line
`U1_mood_board` is not a migration: it is created in the ledger when a Universe's first live
picture is paid for.

## Deploy

This change has worker code (live mood boards), so the worker goes first:

1. **Back up** the studio database and `CAST_STORAGE_DIR` first.
2. Deploy only when **no episode is mid-run** (the episode list shows none on its way).
3. **Worker first** (or together with the API). The new worker polls `hfvg-tasks` and the mood
   board queue `hfvg-tasks-universe`, runs
   `UniverseBoardWorkflow`, and its reconciler settles `uboard-...` jobs. No existing workflow
   changes what it does: the reconcile workflow only gains a branch for the new job type, so open
   runs replay unchanged.
4. **Then the API.** It applies both migrations on startup. While no worker polls the mood board
   queue (an older worker), a live board is a plain 409 ("the worker running now cannot make them
   yet. Nothing was charged.") before any price is asked or anything is recorded; dry-run boards work
   either way.
5. **Then the web.** The new pages call the new routes; an old web keeps working with the new API.

No new settings and no new volume.

**Rolling the worker back** below this change: first make sure no mood board picture holds money (no
`uboard-...` job reserved or waiting for the reconciler; the dashboard's in-flight list shows none),
because an older reconciler does not know `uboard-` jobs and stops at them. After a rollback, live
boards are the plain 409 again, and a board started just before it ends as described in
[Live mood boards](#live-mood-boards).

## Not in this change (follow-ups)

- **A setting for the mood board budget.** The cap of `U1_mood_board` is a code constant ($4.00);
  there is no route to change it.
- **Film from episodes.** The Universe page shows the episodes it would use and a disabled button.
- **Locks for places and props.** Phase 1 shows only whether they have a photo.
- **Dialogue beyond phase 1** (per-episode casting of voices to new characters, voice lines per
  Universe): see the dialogue plan.
