# Creation flow fixes

Three fixes to the creation flow from an admin walkthrough of the new studio. This file is the plan;
the user-facing behaviour is described in docs/WORLDS.md, docs/CAST.md and docs/CONCEPT-FRAMES.md.

## Where things are today

- **Universe creation**: `/studio/universes/{id}/create?step=spark|references|moodboard|bible|lock`
  (`web/components/UniverseCreation.tsx`, `web/lib/journey.mjs`), API `api/world_routes.py`, store
  `hfvg/world_store.py`. References are notes on four topics plus up to 12 photos; the mood board opens at
  one note or three photos; the bible is written by the language model from the notes and the approved board.
- **Episodes**: the wizard `/studio/create` (idea, `draft-from-idea`, review, start as a test run or live).
  A Universe episode waits on its page (`StartCard`) until every look it uses is locked.
- **Uploads**: `cast_store.inspect_image` checks type, decoding, animation, 40 MP, the short side
  (`CAST_MIN_IMAGE_PX`, default 512) and the size (`CAST_UPLOAD_MAX_BYTES`, 413). It serves cast pictures,
  props and places photos and Universe photos; `pictureProblem` (`web/lib/cast.mjs`) checks the same first.
- **Model inputs**: every reference goes through `hfvg/image_normalize.normalize_reference_image` (JPEG,
  long side at most 1280, about 1 MB) before it is uploaded to the provider.
- **Stills**: one still model per server (`config.still_model_path()`). Paid stills outside a run use the
  guarded path of `hfvg/activities/cast_sheet.py` and `hfvg/activities/universe_board.py`: provider
  estimate (fail closed), the approved price, the balance gate, credit and USD reserves, an idempotent
  submit, the job ledger, poll, commit, and the reconciler for stalled jobs. A shot can carry a
  `pre_approved_still` in shots.json; the run then skips that still's spend (fixed once G1 is reached).

## Fix 2: small references

- `CAST_MIN_IMAGE_PX` becomes the floor for unusable pictures: default 128 (was 512). The check stays where
  it is. Pictures from 128 to 255 px on the short side get "Very small image" and 256 to 511 px get "Small
  image, we'll sharpen it up"; both are accepted. Size, type and decoding checks are unchanged.
- `normalize_reference_image` upscales a reference whose short side is under `REF_MIN_SHORT_SIDE` (512)
  with Lanczos before any model sees it (capped by the long-side limit). The original stays stored as
  uploaded; the upscaled copy is the cached normalized file (its cache key names the upscale, so files
  of bigger pictures keep their old key).
- The API returns `size_note` with each upload and with stored pictures; the cast, props, Universe and
  checklist upload forms show it as a plain note, not an error.

## Fix 3: story first, then a reference checklist

- The References step becomes "Story & photos": first a story box (plain words, rough is fine), then
  "Make my reference list". The language model (`STORY_SHAPING_MODEL`, the existing chat path;
  `DRAFT_LLM_FAKE` gives a deterministic list in tests) reads the story and returns the references it
  needs: each character (face and outfit angles), each place, each key prop, why, ideal and minimum
  photo counts, optional or not.
- Photos upload onto a checklist item (they are Universe reference photos, so the mood board uses them
  as today); the list shows "4 of 7 ready". The owner can add and remove items, and redo the list.
- A written story opens the mood board (like a note does), and the story and the list's names go into the
  board prompt and the bible, so the bible uses the same names.
- Back-compat: new tables only (`universe_stories`, `universe_checklist_items`,
  `universe_checklist_photos`), applied by the migration runner on first use. Existing Universes have no
  story: their notes, photos, boards and bibles work as before and their References step still shows them.

## Fix 1: help making the concept image

- A "First frames" panel on an episode's page before it starts (and a wizard choice that leads there).
  For a shot, the app writes the image prompt from the shot's beat, its characters, place and props and
  their references (the run's own planning: `plan_cast`, continuity locks, cast injection), through a
  per-model prompt adapter chosen from the server's still model. Nobody picks or sees a model.
- It makes several options (default 3, `CONCEPT_FRAME_OPTIONS`, 1 to 4); the owner picks one, tries again,
  or adds a change in plain words ("make it night", "closer on her face").
- Spend: the estimate is shown first. A test-run server makes free placeholders. A live server needs the
  episode in live mode, then the provider estimate (fail closed), the approved total, the budget line,
  the balance check, and on the worker (its own task queue, started only while a worker polls it) the
  sheet path's guards: estimate within the approval, balance gate, credit and USD reserves on the
  episode's draft pictures line (`L2_drafts`), idempotent submit, job ledger, commit; stalled jobs go to
  the reconciler. No override skips the estimate or the balance gate.
- The pick is copied into the episode (`concept_frames/`) and written as the shot's `pre_approved_still`
  (path, sha256, approved_by), only before the run starts; the run uses it as the start frame and skips that
  still. A pick is refused when the shot changed since its options were made. In a Universe the shot's
  characters must be locked first, so the frame shows the look the video keeps.
- New tables: `concept_frame_sets`, `concept_frame_options`, `concept_frame_picks`.

## Follow-ups from the first verification

- **The kept start lives in the studio** (N1). The wizard's start for an episode that waits on its
  page (First frames, or a Universe look to lock) was kept in the browser, so another device had no
  Start button. `POST /api/episodes/{id}/kept-start` now keeps it server-side in `episode_kept_starts`
  (owner or admin only, never while a run is going, nothing starts or is charged), `GET
  /api/episodes/{id}/universe` returns it as `kept_start` to whoever may start the episode, and the
  start clears it. A start an older page left in the browser still works on that browser.
- **Each First frames shot in plain words** (N2). The card shows who and what is in it, the place,
  the time of day and the changes ("Mara at Harbour, dusk, close on her face"), the moment without the
  camera wording, and the reference photos once ("No reference photos of Lead yet: the words guide
  it"). The picture prompt stays on the server; the API no longer sends it.
- **One line at 320, 390 and 1440 px** (N3). Buttons, option names and "Changes so far:" never wrap
  or break a word: a narrow column lists the options with each picture beside its button.
- **Prices that differ read differently** (N4): "The estimate is now $0.183 (you approved $0.180)".
  A picture under the size floor is refused with "choose a bigger one".
- **"Make my reference list" ten times an hour** (N5) for each person (`REFERENCE_LISTS_PER_HOUR`),
  then a plain 429 that says when to try again.

## Deploy

API, worker and web. Worker first: the first frames queue (`<TASK_QUEUE>-frames`) and the
reconciler's new job type ship with it; until a worker polls that queue the API refuses live first
frames with a plain 409. The migrations are additive and run on first use; no manual database edits.
The follow-ups add one more additive migration, `2026-11-kept-starts-v1` (table
`episode_kept_starts`), applied by the API and the worker on first use. They need the API and the
web; deploy the worker with them so all three run the same `hfvg` code (no worker job reads the new
table or depends on the follow-ups).

## Tests

pytest for each store, route, guard and adapter (with red-green mutations for the new money and pick
guards), web unit tests for the journey, checklist and size notes, the copy rules over the new screens,
and a Playwright run (dry-run backend, fake language model, no credits) through story, checklist, upload
and a concept pick, with screenshots at 1440 and 390 px. The follow-ups add a fresh browser (no local
storage) that finds the start card and the picked frame, and layout checks at 320, 390 and 1440 px:
no sideways scroll, no button or label on two lines or cut off, no word broken across lines.
