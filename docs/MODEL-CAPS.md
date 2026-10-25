# MODEL-CAPS: per-model limits, enforced before any spend

Status: research and design only. Nothing is coded into the repo yet. Checked **2026-10-19** (Europe/Paris).
Data file: `model_caps.json` (same folder; schema v1, 16 models, 37 cited sources). Raw doc snapshots: `sources/`.
Acceptance spec for the implementation: `ACCEPTANCE.md`.

## Why

The harness is a tool for creators. On Ep05, three paid-path still jobs ended as an uncharged but opaque
`"Generation failed"`. All three sent 6–7 image refs to Grok Image 2.0. Higgsfield's schema says 10;
xAI, the model's vendor, says **5**. Each model's limits must be known in advance, shown to the creator,
and checked on every shot **before estimate/reserve**. A violation must come back as a plain sentence,
not as a provider failure.

## Rules used in the data file

* Every non-unknown value cites at least one source (URL plus check date) in `sources`. Harness observations are
  labelled as such (`obs_*`, `harness_*`) and never presented as vendor docs.
* `basis` says where a value comes from:
  * `higgsfield` / `upstream`: only that publisher documents it.
  * `stricter_upstream` / `stricter_higgsfield`: both publish a value; the stricter one wins.
    Both raw values are kept in `higgsfield` / `upstream`.
  * `stricter_upstream_pages`: two official pages of the same vendor disagree; the stricter one wins.
  * `conservative_upstream`: the vendor's own docs disagree across endpoints; the conservative value is used until confirmed.
  * `intersection`: formats both layers accept.
  * `harness_policy`: our own cap, below the documented one.
  * `unknown`: nothing official. Preflight never refuses on an unknown; the UI shows "not published".
* Where Higgsfield and the vendor differ, the stricter value is used and noted (marked † below).

## Summary: image and video models

`max_refs` = image references the request may carry. Frames (start/end) are not refs.

| Model (Higgsfield endpoint id) | Harness status | Max refs | Prompt max chars | Aspect ratios | Resolutions | Duration (s) | Frames |
|---|---|---|---|---|---|---|---|
| `xai/grok-imagine-image-2.0` | active_default_still | 5 † | UNKNOWN | auto, 1:1, 1:2, 2:1, 3:2, 2:3, 4:3, 3:4, 16:9, 9:16 | 1k, 2k | n/a | — |
| `gpt_image_2` | mcp_only_not_on_api | 16 | 32000 | 1:1, 4:3, 3:4, 16:9, 21:9, 9:16, 3:2, 2:3 | 1k, 2k, 4k | n/a | — |
| `higgsfield-ai/soul/v2/standard` | active_ref_generation | 0 | UNKNOWN | 9:16, 16:9, 4:3, 3:4, 1:1, 2:3, 3:2 | 720p, 1080p | n/a | — |
| `alibaba/qwen-image-3/edit` | configured_cap_only_not_routed | 3 | UNKNOWN | 1:1, 2:3, 3:2, 3:4, 4:3, 7:9, 9:7, 9:16, 16:9, 21:9 | 1k, 2k | n/a | — |
| `kling-video/v3.0/pro/image-to-video` | active_default_clip | 0 | 2500 † | follows_start_frame | UNKNOWN | 3–15 int | start req / end opt |
| `bytedance/seedance-2.5/image-to-video` | active_routed | 0 | UNKNOWN | follows_start_frame | 480p, 720p, 1080p | 4–30 int | start req / end opt |
| `bytedance/seedance-2.5/reference-to-video` | configured_cap_only_not_routed | 8 † | UNKNOWN | 16:9, 4:3, 1:1, 3:4, 9:16, 21:9 | 480p, 720p, 1080p | 4–30 int | — |

† = stricter-of-two or harness-policy value. See the per-model notes.

## Summary: audio (ElevenLabs)

| Harness key | Upstream model id | Limit | Cost (API list) | Note |
|---|---|---|---|---|
| `eleven_v3` | eleven_v3 | 5,000 chars/request | $0.08 / 1k chars | default TTS (audio round VO) |
| `eleven_multilingual_v2` | eleven_multilingual_v2 | 10,000 chars/request (~10 min) | $0.08 / 1k chars | |
| `eleven_flash_v2_5` | eleven_flash_v2_5 | 40,000 chars | $0.04 / 1k chars | replacement for turbo |
| `eleven_turbo_v2_5` | eleven_turbo_v2_5 (deprecated) | 40,000 chars | $0.04 / 1k chars | migrate to `eleven_flash_v2_5` |
| `sound_generation` | eleven_text_to_sound_v2 | 0.5–30 s † (API ref 0.5, capabilities page 0.1) | $0.12/min; 40 credits/s when duration set | text max UNKNOWN |
| `music_v1` / `music_v2` / `music_v2_5` | same (POST /v1/music; v1 is the API default) | 3 s–5 min † (API ref allows 10 min; FAQ and pricing say 5) | $0.15/min | one music rate on the pricing page |
| `eleven_music` | **not a valid id**: legacy harness alias, mapped to music_v1 | 3 s–5 min † | $0.15/min | lyrics ≤30 lines/section, ≤200 chars/line |

## Per-model notes (the values that matter for preflight)

### Grok Image 2.0: `xai/grok-imagine-image-2.0` (default still model)
* **Max refs 5** (stricter upstream). Higgsfield schema: `image_urls` maxItems 10. xAI multi-image editing:
  "Use up to five source images" (docs.x.ai, updated 2026-08-28; the Imagine overview and launch post say the same).
  Ep05 evidence:
  * failed: b37d054e (7 refs), 97be983c (6), 8b427df8 (6)
  * succeeded: 626c6461 and 3fe9f971 (5 each)
* Body fields: prompt, `image_urls` (ordered public URIs), quality `low|medium` (xAI also has `auto`; Higgsfield doesn't),
  resolution `1k|2k`, aspect_ratio `auto,1:1,1:2,2:1,3:2,2:3,4:3,3:4,16:9,9:16`. One image per request.
* Ref formats: Higgsfield upload types jpeg/png/webp/gif. Ref byte/pixel limits: UNKNOWN; Higgsfield says refs
  "may be resized during preprocessing". The harness already normalizes refs to JPEG ≤1280 px, ≤1 MB.
* Prompt max: **UNKNOWN** (neither publisher documents one). The UI shows "not published". Preflight does not refuse.
* Cost:
  * Higgsfield `/estimate` (free, authoritative) observed 0.96 credits / $0.06 at 1k medium.
  * xAI list price is $0.04 per output image, and edits are also billed per input image.
* Quirks:
  * Over-limit input ends as `failed` with only "Generation failed", uncharged, so the only safe handling is refusing in preflight.
  * The xAI acceptable-use policy (real-people edits) applies.

### GPT Image 2: `gpt_image_2` (MCP only, not on the API path)
* There is no Higgsfield API endpoint (absent from `docs.higgsfield.ai/llms-full.txt`). hfvg's `MODEL_PATH_GPT_IMAGE_2` defaults to "".
  It is still the CLI `--model` default and appears in legacy cost tables. **Preflight must refuse it on the API path** with a
  creator message. Via the MCP connector it accepts `medias` with role `image`; 1k/2k/4k; low/medium/high;
  aspects 1:1, 4:3, 3:4, 16:9, 21:9, 9:16, 3:2, 2:3.
* Upstream OpenAI:
  * ≤16 input images, each png/webp/jpg <50 MB
  * prompt ≤32,000 chars
  * output ratio 1:3–3:1; >2560x1440 is experimental, max 3840x2160
  * no transparent background on gpt-image-2
* This is the model behind the Studio audit's "6–16 refs, zero failures". The figure does not transfer to Grok.

### SOUL V2: `higgsfield-ai/soul/v2/standard` (reference generation)
* Text-to-image only. 0 image refs; identity comes from a trained `custom_reference_id`.
* 720p/1080p; aspects 9:16, 16:9, 4:3, 3:4, 1:1, 2:3, 3:2; batch 1 or 4.
* `custom_reference_strength` must be >0: the schema accepts 0, but the runtime fails with it.
* Prompt max UNKNOWN. The docs' cost example (1.5 cr / $0.094) is illustrative only.

### Qwen Image 3 edit: `alibaba/qwen-image-3/edit` (cap configured, not routed)
* 1–3 refs (required); 1k/2k; 10 aspects; Alibaba recommends ≤4,500 tokens. Upstream Alibaba doc not checked: UNKNOWN.

### Kling 3.0 Pro image-to-video: `kling-video/v3.0/pro/image-to-video` (default clip model)
* Frames: `image_url` start (required) and `last_image_url` end (optional). Last-frame-only is not supported. **0 image refs**;
  `elements` are Kling element ids owned by the account. The harness cap table's "3" is unused.
* **Prompt ≤2,500 chars** (stricter Higgsfield: "longer prompts are truncated"; Kling allows 3,072, recommends 2,500).
  Refuse instead of silent truncation.
* Duration: integer **3–15** (the hfvg docstring says 3–12; fix it). multi_prompt ≤6 items × 512 chars.
* Frame files: jpg/jpeg/png only († Kling, stricter than Higgsfield uploads); ≥300 px; aspect 1:2.5–2.5:1.
  Max size: the official Kling docs disagree (3.0 Omni i2v says 50 MB, the image API says 10 MB). The conservative **10 MB** is used.
* No aspect parameter: output follows the start frame. Resolution isn't a parameter on this endpoint (UNKNOWN; the harness assumes 1080p).
* `sound` defaults to **on** at Higgsfield. The harness sends `off`; preflight asserts it.
* Cost: `/estimate` returns usd; the live canary gave $0.056/s.

### Seedance 2.5 image-to-video: `bytedance/seedance-2.5/image-to-video`
* Frames: `image_url` (required) and `end_image_url` (optional). If the ratios differ, the end frame is cropped to fit.
* Duration: integer **4–30**. Resolutions 480p/720p/1080p. Output follows the start frame. `generate_audio` defaults to true
  (the harness sends false). Media must be public https; `asset://` is rejected.
* Frames (upstream BytePlus): 300–6000 px, aspect 0.4–2.5, <30 MB, body ≤64 MB. Formats: the intersection with Higgsfield uploads,
  i.e. jpeg/png/webp/gif.
* Prompt: no hard limit. BytePlus recommends ≤500 Chinese characters / 1,000 English words, so this is a warning, not a refusal.
* **Moderation quirk:** Seedance 2.x rejects reference images or videos with real human faces (verified portrait assets only).
* Harness policy (existing routing): ≥4 s, never `wet`, count>1 goes to Kling; draft 480p, final 1080p.
* Cost: Higgsfield list text 480p $0.2056/s, 720p $0.4622/s, 1080p $1.1372/s (from the hfvg config comment; not re-found today, so re-verify).

### Seedance 2.5 reference-to-video (cap configured, not routed)
* ≤30 images / 10 videos / 10 audios / 50 total (both layers). The harness policy cap is 8. 6 aspects; 4–30 s.

### ElevenLabs
* TTS character limits are from the models page and the API pricing page (re-read 2026-10-25: eleven_v3 5,000; multilingual_v2 10,000; flash_v2_5 40,000). `eleven_turbo_v2_5` is deprecated (use `eleven_flash_v2_5`).
* TTS endpoint `POST /v1/text-to-speech/{voice_id}` (text, model_id, voice_settings, seed); billed per character.
* SFX endpoint `POST /v1/sound-generation`, model `eleven_text_to_sound_v2`: duration 0.5–30 s; loop only on v2; prompt_influence 0–1.
* Music endpoint `POST /v1/music`: `music_length_ms` 3,000–600,000 per the API reference, but 5 minutes per the capabilities FAQ and pricing page,
  so 300,000 is used. prompt XOR composition_plan. Uploaded reference audio is screened for copyright.
  **The harness id `eleven_music` is not a valid model_id.**
* Harness `AUDIO_COSTS` are credit estimates; the API bills USD per char/min (see table).

### Shared Higgsfield facts
* Upload content types: jpeg, jpg, png, webp, gif, wav, mp4. The upload URL is valid for 1 h. Outputs are kept ≥7 days.
* `failed`/`nsfw` are not charged.
* Concurrency limit: 400 "Maximum number of concurrent requests (N)". There are no rate-limit headers.
* Record `X-Correlation-ID` with each request_id (now logged by PR #11).

## Findings that need a decision (found while mapping the repo)
1. `gpt_image_2` is the CLI default and appears in cost tables, but it has no API endpoint. Make it refused or MCP-only.
2. `eleven_music` is not a real ElevenLabs model id. Map it to `music_v1`, or pin `music_v2_5`.
3. `eleven_turbo_v2_5` is deprecated upstream.
4. The Kling docstring says 3–12 s; the docs say 3–15. `shots.json` allows float durations, but Kling and Seedance need integers.
5. `DEFAULT_MODEL_MAX_REFS` lists Kling i2v = 3, but that endpoint has no image-ref field (frames only).
6. Kling prompts over 2,500 chars are **silently truncated** by Higgsfield, which today is invisible to the creator.

## Harness design

### 1. Capabilities registry: `hfvg/model_caps.py` + `hfvg/data/model_caps.json`
* The JSON is shipped in the package (the file in this folder is the seed). `MODEL_CAPS_PATH` may point elsewhere (tests).
* Load once and validate with pydantic (schema v1: `models{id: {kind, provider, status, aliases, request_fields, limits{field: {value,
  basis, sources[], higgsfield?, upstream?, note?}}, cost, moderation[]}}, sources{id: {url, publisher, checked}}`).
  Fail closed at worker/API start if the file is missing or invalid.
* API:
  * `resolve(model_or_alias) -> ModelCaps`. Raises `UnknownModel`; never falls back to a default cap.
  * `ModelCaps.limit(name) -> Limit | None`, where Limit has `.value`, `.unknown` and `.source_urls`.
  * `creator_view(model) -> dict` for the UI/API: label, limits, "not published" for unknowns, and sources with dates.
* `config.max_refs_for()` becomes a thin wrapper over `resolve(path).limit("max_refs")`.
  * `MODEL_MAX_REFS_JSON` may only **lower** a cap. Raising one needs `MODEL_CAPS_ALLOW_RAISE=1` and logs a warning naming the source being overridden.
  * `DEFAULT_MODEL_MAX_REFS` and the `STILL_MAX_REFS` fallback for unknown models are removed. An unknown model is an error, not "3".
* Statuses with behavior:
  * `mcp_only_not_on_api`, `alias_invalid_upstream_id`: refuse on submit paths.
  * `deprecated_upstream`: warn.
  * `configured_cap_only_not_routed`: allowed but flagged in the UI.

### 2. Preflight: `hfvg/preflight.py`
* `preflight_shot(shot_plan, route, caps_registry, *, phase) -> list[Violation]` and `preflight_episode(...)`.
  `Violation = {shot_id, frame: start|end|clip|audio, model, field, limit, actual, severity: error|warning, message, source_url, checked}`.
* Pure function, no network.
* It uses the **same ref-assembly function as submit**: the anchor/lead logic, including "the `_end` lead drops the anchor first".
  So the count it checks is exactly the count that would be sent.
* Checks per shot (every rule reads the registry; nothing is hard-coded):
  * still: ref count ≤ max_refs (start and end separately); prompt and end_prompt ≤ prompt_max_chars when known;
    aspect ∈ aspect_ratios; resolution ∈ resolutions; quality ∈ quality; model status allowed.
  * ref files (local, after normalization): format ∈ ref_formats; bytes ≤ ref_max_bytes; px within min/max when known.
  * clip:
    * integer duration within duration_s
    * start frame present when `required`
    * end frame only if `end_frame` ≠ unsupported
    * no last-frame-only
    * prompt ≤ max (Kling 2,500)
    * audio flag equals the harness value (sound off / generate_audio false)
    * frame format/bytes/px/aspect when the frame file exists (at clip time)
    * routing policy (Seedance ≥4 s, no wet, count>1 goes to Kling)
  * audio: TTS text ≤ text_max_chars; SFX duration in range; music length in range; valid upstream id.
  * Unknown limits never refuse. Optional `warn_over` advisories (e.g. Seedance recommended prompt length) are severity=warning.
* **Where it runs (before any estimate/reserve):**
  1. On `upload-shots` / BEATMAP load (fast feedback).
  2. At G1 plan validation, before G1 can be approved and before the G1.08 credit-plan estimate.
  3. In `submit_still_job_enforced` / `submit_clip_job_enforced` / audio submit as the **first** step, before
     `/estimate`, `reserve` and upload. This is defense in depth for retries and backfills (e.g. `retry_end_still`).
  4. `POST /api/episodes/{id}/preflight` (read-only) for the UI and CLI (`hfvg preflight <episode>`).
* A violation raises `PreflightError` (non-retryable `ApplicationError`). The shot goes to `failed_shots` with the creator message.
  No provider call, no estimate, no ledger rows.
* Creator message template (plain English, actionable, cites the source):
  "Shot A02 (end frame) uses 6 reference images, but Grok Image 2.0 accepts at most 5. Remove 1 reference in shots.json
  (ref_keys) and try again. Limit source: docs.x.ai multi-image editing, checked 2026-10-19."
  "Shot A03 clip is 4.5 s, but Kling 3.0 Pro needs a whole number of seconds from 3 to 15."
  "Shot A04 prompt is 2,731 characters; Kling 3.0 Pro accepts 2,500 (Higgsfield silently cuts the rest). Shorten it by 231."

### 3. Web UI
* `GET /api/models` → `creator_view` for every model with status ∈ offered: label, kind, limits as human text,
  "not published" for unknowns, source links and checked dates, status badges (deprecated / MCP only).
* Episode page (`web/app/studio/episodes/[id]/page.tsx`):
  * Shot list: each shot shows its still and clip model with a compact limits line (e.g. "Grok Image 2.0 · ≤5 refs · 1k/2k · 9:16")
    and per-shot preflight state (✓ / ✗ with the message).
  * A model picker (new; there is none today) for the still and clip model per episode, with per-shot override. Selecting a model shows
    its limits card *before* selection is saved, and re-runs preflight live (`POST /preflight` with the candidate model).
  * The Approve G1 and Start live buttons are disabled while any error-severity violation exists. A tooltip lists them.
* Pricing page: show the cost unit and source per model from the registry (not the hard-coded `web/lib/pricing.ts` numbers).

### 4. Coverage test
`tests/test_model_caps_registry.py::test_every_configured_model_has_caps` collects every model id the harness can use or offer:
* still_model_path() default
* `Config.MODEL_PATH_*` defaults
* SeedanceVideoProvider/KlingVideoProvider model_path
* routing.MODELS aliases
* `credit_pricing` table keys
* `providers.higgsfield.MODEL_COSTS` model names
* `providers.elevenlabs.AUDIO_COSTS` keys
* `base.py` defaults
* the CLI `--model` default
* `MODEL_PATH_STILL` env values used in scripts/fixtures

It asserts `model_caps.resolve()` succeeds for each. Adding a model anywhere without a caps entry fails CI.

### 5. Periodic doc re-check
* `model_caps.json.checked` + `recheck_interval_days` (30).
  `tests/test_model_caps_registry.py::test_caps_freshness_warns` emits a pytest **warning** (never a failure: CI must not break on the calendar)
  when stale. The API `GET /api/models` includes `stale: true`, and the UI shows "limits last checked N days ago".
* `scripts/recheck_model_caps.py` (manual, or a monthly scheduled workflow that only uploads a report artifact and never auto-edits):
  1. fetches `https://docs.higgsfield.ai/llms-full.txt`
  2. parses each endpoint's "Complete JSON schema" (maxItems, enum, min/max)
  3. diffs it against `higgsfield` values in the JSON
  4. lists upstream URLs to re-read by hand (xAI, OpenAI, Kling, BytePlus, ElevenLabs)

  Output is a markdown diff for a human to apply. Offline tests run it against the saved snapshot in `tests/fixtures/`.
  The snapshot `tests/fixtures/higgsfield_llms_full_2026-10-19.txt` is the real dump (fetched 2026-10-19 01:44 Europe/Paris), not synthetic.
  The script itself never fetches; save a fresh dump and pass `--fixture`. Limits with a deliberately tighter basis
  (`stricter_*`, `conservative_*`, `harness_policy`, `intersection`) only drift when upstream drops below them; untracked upstream
  endpoints (77 on 2026-10-19) are informational unless `--strict`.
* Owner checklist (in this doc): on every Higgsfield or vendor model announcement, and at least monthly, run the script, update values plus
  `checked`, and re-run the coverage tests.

## Unknowns to resolve (ask Higgsfield support / vendors)
* Grok Image 2.0 prompt max length; ref byte/pixel limits; why Higgsfield documents 10 refs when xAI allows 5.
* Kling 3.0 Pro output resolution on the Higgsfield endpoint; frame max bytes (50 vs 10 MB).
* Seedance hard prompt limit.
* ElevenLabs SFX and music prompt max lengths.
* Qwen upstream limits.
* Current Seedance USD/s (re-verify the list text).
