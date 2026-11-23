# Episode Input Files

An episode workflow requires several input files in `data/episodes/{episode_id}/`:

## Required Files

### 1. `beatmap.txt`
- **Purpose:** Timeline structure defining shot IDs, scenes, and screen times
- **Format:** Text file with shot definitions (e.g., `A01 (2.5s)`)
- **Validation:** Must be parseable, shot IDs must match `shots.json`
- **Used by:** Episode workflow to structure timeline

### 2. `shots.json`
- **Purpose:** Per-shot generation parameters (prompts, model, duration, aspect ratio)
- **Format:** JSON object with shot_id keys
- **Validation:** 
  - Shot IDs must match beatmap
  - Each shot must have `prompt`, `params.model`, `params.duration`, `params.aspect_ratio`
  - Live mode: refs in prompts must exist or be in `to_generate`
- **Used by:** Shot generation activities

### 3. `refs.json`
- **Purpose:** Reference image specifications for character sheets and scene refs
- **Format:** JSON object with `available` and optional `to_generate` arrays
- **Fields:**
  - `key`: Unique ref identifier (e.g., `r_duck`)
  - `output_path`: Where the ref image is/will be stored
  - `type`: "character" or "scene"
  - For `to_generate`: `prompt` and `version` (default 1)
- **Validation:**
  - All `available` refs must have existing files at `output_path`
  - All `to_generate` refs must have valid prompts
  - Combined set must cover all refs used in `shots.json`
- **Used by:** Ref generation and shot prompts

### 4. `prompt_kit.json`
- **Purpose:** Prompt engineering guide and templates
- **Format:** JSON object with prompt guidance, style templates, and ref usage examples
- **Validation:** Must be valid JSON
- **Used by:** Shot generation for prompt formatting
- **Prompt lint (`"lint"`):** an object holds the series' own lint rules; `"default"` or no `"lint"`
  key selects the built-in default profile (draft-from-idea kits are written with `"lint": "default"`).
  Any other value is refused. See docs/CONTENT-POLICY.md "Prompt Lint Rules".

### 5. Reference Images (if using pre-existing refs)
- **Purpose:** Character sheets, scene references, style guides
- **Location:** Paths specified in `refs.json` `available` entries
- **Format:** PNG or JPG images
- **Validation:** 
  - Files must exist at specified paths
  - Must pass dimension/format QC (min 512px, valid image)
- **Used by:** Shot generation prompts

## Optional Files

### `show.json` (show profile)
- **Purpose:** everything show-specific the engine reads as data: body presets and the sheet lead,
  identity rules, shot flags, beatmap character codes, lint clauses and near-word rules, playbook
  rules, the plate's scale reference, the gate policy file and the real-people stance
  (schema `hfvg.show/1`, docs/SHOW-PROFILE.md)
- **Validation:** strict; every problem of the file is reported together. Optional: an episode
  without one is read through the legacy adapter (its `prompt_kit.json` and `shots.json`, exactly
  as before), or gets the neutral default profile when it has neither.
  `HFVG_SHOW_PROFILE_PATH` names one profile for every episode without its own.
- **Used by:** prompt lint, playbook, shot data, cast sheets, headcount and identity judges

## Configuration Files

### `.cursor/environment.json` (for live mode)
- **Purpose:** Environment configuration including API keys
- **Required for live mode:** `HIGGSFIELD_API_KEY` environment variable
- **Format:** JSON with `env` object
- **Validation:** API key must be present and valid format in live mode

## Preflight Validation

Before spending credits at gate 1, the episode workflow validates:
1. ✅ Beatmap exists and is parseable
2. ✅ `shots.json` exists, shot IDs match beatmap
3. ✅ `refs.json` exists and is valid
4. ✅ `prompt_kit.json` exists and is valid JSON; live mode: its prompt lint kit loads
   - `show.json` (optional) validates when present; the API preflight reports the profile's
     source (`episode`, `env`, `legacy-adapter` or `default`) and digest
5. ✅ All required refs (used in shots) either exist or are in `to_generate`
6. ✅ Model configs are valid (supported model, valid params)
7. ✅ Live mode: API key is configured

If validation fails, all problems are reported in one error before any generation starts.

## Example Structure

```
data/episodes/ep05/
├── beatmap.txt          # Required: timeline structure
├── shots.json           # Required: shot prompts and params
├── refs.json           # Required: ref specifications
├── prompt_kit.json     # Required: prompt engineering guide
├── show.json           # Optional: show profile (docs/SHOW-PROFILE.md)
├── refs/               # Optional: pre-existing refs
│   ├── r_duck.png
│   └── r_scene_park.png
└── outputs/            # Created during generation
    ├── A01.mp4
    └── A02.mp4
```

## Validation Errors

### Missing Files
```
PreflightError: Episode ep05 missing required files:
  - prompt_kit.json
  - refs.json
```

### Shot/Beatmap Mismatch
```
PreflightError: shots.json IDs differ from beatmap:
  - Only in beatmap: ['A03']
  - Only in shots.json: ['B04']
```

### Missing Refs
```
PreflightError: Refs used in shots but not available or to_generate:
  - r_duck (used in shots: A01, A02)
  - r_scene_park (used in shots: A03)
```

### Invalid Model Config
```
PreflightError: Invalid model configs:
  - Shot A01: unsupported model 'invalid-model'
  - Shot A02: invalid aspect_ratio '16:9:1'
```

### Missing API Key (live mode)
```
PreflightError: Live mode requires HIGGSFIELD_API_KEY environment variable
```

## Round 7 shot and episode options (`shots.json`)

### Pre-approved start stills (`pre_approved_still`)

Any number of shots may carry a start still the owner already approved, for example from an
earlier run:

```json
{"shot_id": "A01", "pre_approved_still": "A01.jpeg", ...}
{"shot_id": "A02", "pre_approved_still": {"path": "stills/A02.jpg", "sha256": "<64 hex>", "approved_by": "owner"}, ...}
```

- The value is a local path, either absolute or relative to the episode directory. URLs and `file:`
  are refused. The file must exist, and if `sha256` is given it must match.
- The shot **skips the still spend**: there is no still estimate and no still reservation, and G1
  shows $0 for it. The approved still is recorded as the shot's G2 still, flagged
  `pre_approved: true`, and is still **shown at G2**.
- After that, the shot goes through clip generation, motion QC, clip-only retry and assembly like
  every other shot.
- A refs.json entry `{"key": "...", "pre_approved_still_of": "A01"}` resolves to that shot's
  pre-approved still, so it can be a continuity ref for other shots.

### Trims (`trim_head_s` / `trim_tail_s`)

`trim_head_s` / `trim_tail_s` are the seconds cut from the start and end of the clip. They map
onto the existing `trim_in` / `trim_out` window, so do not use both forms on one shot.

- Both values must be numbers >= 0, and together they must be shorter than the shot duration.
- Assembly checks the window again against the real probed clip duration and refuses a trim that
  exceeds the clip.
- The planned picture length (G1) and the assembled picture length (assembly manifest) are both
  reported as `picture_seconds`.

### Clip QC constraints (`qc_fail_if`)

`qc_fail_if` is a list of free-text failure conditions per shot, for example
`["the prop ends in water", "the prop vanishes"]`. Each one is checked on every clip of the shot by a
`ClipConstraintChecker` (`hfvg.qc.clip_constraints`). The result is recorded per clip (the shot
outcome `qc_constraints` and `qc_constraints_by_clip` in the ShotWorkflow state).

- A violated constraint is a FAIL: the shot fails, and nothing is rerolled automatically.
- The check **fails closed**. No configured checker, a missing clip, a checker error or low
  confidence all give `NEEDS_REVIEW`, and the shot is escalated to a human. None of these is ever a
  silent pass.
- Dry-run placeholder clips are recorded as `DRY_RUN` (not checked).
- Constraints are checked after motion QC passes.
- The vision checker of the automated QC judges ([QC-JUDGES.md](QC-JUDGES.md)) answers every
  condition of a clip in one call from six frames across the clip. It is registered only with
  `HFVG_QC_JUDGES_ENABLED=true` (default off: no checker, so every condition of a live clip is
  `NEEDS_REVIEW`).

### Continuity hard vs advisory checks (`continuity_hard_checks`, top level)

```json
{"continuity_hard_checks": ["relative_size", "headcount"], "shots": [...]}
```

The checks are `headcount`, `relative_size` and `left_to_right_order`. The listed checks are hard:

- A FAIL makes the assembly verdict FAIL.
- A check that nothing could verify is NEEDS_REVIEW.

Every unlisted check is advisory and gives WATCH at worst. Without the key, only `headcount` is
hard (the earlier behaviour), and the size and order checks are not run.

### Dry-mode audio lengths

In dry runs, audio placement, ducking and loudness are checked against planned lengths:

- Each VO line runs about 15 characters per second, clamped to 0.8 to 12 s (`dry_vo_seconds`).
- Music matches the planned picture length, after trims.

Live delivery without a real picture refuses.

## Cast options (`shots.json`, `prompt_kit.json`)

The cast library is described in docs/CAST.md, and the Cast panel in docs/CAST-HOWTO.md. The panel
writes these keys for you.

### Who is in frame (`cast`)

```json
{"shot_id": "A03", "cast": ["mara", {"character": "tobi", "version": 2}], ...}
```

- Each entry is a character id (its slug in the episode's cast, which is the series cast when the
  episode is in a series) or `{"character": ..., "version": n}`. Without a version, the character's
  default version is used: the first locked version, else version 1. At most 6 entries, no repeats.
- Planning runs when the run starts (in an activity), and preflight and G1 preview it. Per character
  in frame, it adds the identity refs (the locked sheet, else the uploads, primary first). With two or
  more characters it adds a lineup sheet. When the refs would go over the still model's max refs
  (`hfvg/data/model_caps.json`, one slot kept free), the rest are merged into a grid sheet. The keys
  go after the shot's own `ref_keys`, and refs.json needs no entry for them.
- Every still prompt for the shot gets each character's description (the locked description for a
  locked version).
- An unknown character or version, or a locked sheet whose files changed, refuses the run. Preflight
  reports these in `cast_errors`.
- `ref_keys` and refs.json keep working as before, alone or together with `cast`.

### What the QC judges check against (`cast`, `count`, `props`)

With the automated QC judges on ([QC-JUDGES.md](QC-JUDGES.md)), the shot plan is what they check:

- Each `cast` entry is one figure, counted by its id and expected once, whatever the character is (a
  person, a creature, a robot, a puppet, a prop that acts). Each locked entry also gets the identity
  check.
- `count` is the number of main characters in frame. When it is larger than the cast list, the main
  characters without a cast entry are counted together as one group. A cast list longer than `count`
  goes to a person.
- The shot's `props` and `place` are not figures. A prop that acts as a character (a talking kettle)
  belongs in the cast library, so it is counted and checked like any other character.
- Any other figure standing out in the shot fails the headcount; figures only in the background go
  to a person.
- The first series' `duck_present` still declares its host outside `count`: the host is counted as
  one more figure, or as one of the cast entries when the cast list is longer than `count`.

### Words in the picture (`allow_text`, `prompt_kit.json` `text_policy`)

- A shot with a `cast` gets "No text, no captions, no speech bubbles, no lettering." in every still
  prompt, and lint rule 13 refuses a prompt that asks for words (docs/CONTENT-POLICY.md).
- `"allow_text": true` on a shot (a sign, a title card) turns the constraint off for that shot.
  `"allow_text": false` turns it on, even for a shot without a cast.
- In `prompt_kit.json`, `"text_policy": "no_text"` applies it to every shot, and `"allow_text"` turns
  it off for every shot. A shot's own `allow_text` wins over the kit. Other values are refused.
- Shots without `cast` or `allow_text`, in kits without `text_policy`, are planned and prompted
  exactly as before.

## Round 7 environment

| Variable | Meaning |
|---|---|
| `HFVG_GATE_POLICY` / `HFVG_RUN_DIR` | Where the gate policy is loaded from (see docs/GATE-POLICY.md). |
| `HFVG_BALANCE_MAX_AGE_HOURS` | Maximum age of the manually recorded Higgsfield balance (default 24). If the balance is missing or older, live preflight and live reservations refuse. Record it with `hfvg budget set-balance <usd>` (`hfvg budget show-balance` shows it) or `POST /api/budget/balance` (admin auth). The Studio credits panel shows the balance and how fresh it is. |
| `HFVG_PLAYBOOK_HARD_RULES` | Comma-separated playbook rules (`duck_role`, `animal_poses`, `same_resort`, `moderation_routing`) that are made **hard**. By default every rule is advisory: the moderation lint runs before every paid still and clip submit, and its decision is recorded in the shot state (`moderation_lint`), but it never blocks. A live hard violation refuses before estimate, reservation or provider call. A dry run records `would_refuse`. Unknown names fail preflight. |
| `HFVG_PLAYBOOK_WORLD_REFS` | Comma-separated locked world ref keys for the `same_resort` rule. Without it, that rule is not evaluated. |

`duck_role` only judges shots that explicitly declare `duck_present`. A shot without the key
makes no claim either way.
