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

- The References step becomes "Story & refs": first a story box (plain words, rough is fine), then
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
  episode's still line, idempotent submit, job ledger, commit; stalled jobs go to the reconciler.
  No override skips the estimate or the balance gate.
- The pick is copied into the episode (`concept_frames/`) and written as the shot's `pre_approved_still`
  (path, sha256, approved_by), only before the run starts; the run uses it as the start frame and skips that
  still. A pick is refused when the shot changed since its options were made. In a Universe the shot's
  characters must be locked first, so the frame shows the look the video keeps.
- New tables: `concept_frame_sets`, `concept_frame_options`, `concept_frame_picks`.

## Deploy

API, worker and web. Worker first: the concept frames queue (`<TASK_QUEUE>-concept-frames`) and the
reconciler's new job type ship with it; until a worker polls that queue the API refuses live concept
frames with a plain 409. The migrations are additive and run on first use; no manual database edits.

## Tests

pytest for each store, route, guard and adapter (with red-green mutations for the new money and pick
guards), web unit tests for the journey, checklist and size notes, the copy rules over the new screens,
and a Playwright run (dry-run backend, fake language model, no credits) through story, checklist, upload
and a concept pick, with screenshots at 1440 and 390 px.
