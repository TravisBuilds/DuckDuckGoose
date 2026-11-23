# Show profile (`show.json`)

The engine makes videos of any kind: a satire with invented people, a cooking show, a montage with
nobody in it, a series with a talking robot. It holds no show's rules of its own. Everything one
show needs that the engine does not know lives in that show's **show profile**, a JSON file with
schema `hfvg.show/1`, loaded and validated by `hfvg/show_profile.py`.

A new show needs its own data and nothing else: a `show.json` next to its `shots.json`,
`prompt_kit.json` and beatmap, and no code change. `tests/fixtures/shows/human_satire/` is a
complete example (two invented candidates, a moderator, an audience, a prop), and
`tests/test_show_profile_all_human.py` runs it through every stage that reads a profile.

## Where a profile comes from

For an episode, first match wins (`resolve_show_profile`):

| Order | Source | `source` value |
|---|---|---|
| 1 | `<EPISODES_DIR>/<episode>/show.json` | `episode` |
| 2 | the file named by `HFVG_SHOW_PROFILE_PATH` (one profile for every episode without its own) | `env` |
| 3 | an episode with a `prompt_kit.json` or a `shots.json` (or the `SHOTS_JSON_PATH` / `PROMPT_LINT_KIT_PATH` overrides): the **legacy adapter** | `legacy-adapter` |
| 4 | the neutral default profile | `default` |

Loading reads files, so it happens in activities and the API only, never in Temporal workflow code
(running workflows replay the same way whatever profile a show has). A `show.json` that does not load
fails closed: the preflight refuses the episode and lists every problem; a judge that cannot read the
profile sends the shot to a person (NEEDS_REVIEW), it never passes it.

What an episode gets is readable without opening files:

- `GET /api/episodes/{id}/show-profile` (members): source, digest (sha256 of the canonical JSON),
  body presets, shot flags, identity profile ids;
- `GET /api/cast/body-presets?episode_id=`: the body choices of the Cast panel;
- the preflight reports the source and digest with its other checks.

## Keys

Every key is optional except `schema`. Unknown keys and wrong types are refused, all problems of a
file in one list.

| Key | What it holds | Read by |
|---|---|---|
| `schema` | `"hfvg.show/1"` | loader |
| `name` | a label for people | API, logs |
| `body_presets` | `{id: {label, frame, head, aspects, placeholder}}`: extra body types for character sheets (full-body framing, head-sheet framing, allowed aspect ratios, placeholder size `[w, h]`), next to the built-in `humanoid`, `four_legged` and `other` | `hfvg/cast_sheets.py`, Cast panel |
| `sheet.lead` | the sentence every text-mode master sheet prompt starts with (default `Character design reference.`) | `hfvg/cast_sheets.py` |
| `cast` | `{slug: {role, tags, body, identity_profile}}`: show data about cast-library slugs (the characters themselves stay in the cast library, docs/CAST.md) | lint `role` predicates, identity gate |
| `identity_profiles` | `{id: {name, label, signs, apparel, hint_signs, state_key, state_features, min_height_fraction, presence, presence_note}}`: the rules the identity judge scores a character by (`signs`: sign -> what the judge compares). A locked character whose `cast` entry names one is judged with it; others use the generic profile | `hfvg/qc/identity.py` |
| `judges.style_note` | one sentence about the show's look, appended to the headcount and identity judges' instructions and part of their cache key; empty by default (instructions unchanged) | `hfvg/qc/vision_counter.py`, `hfvg/qc/identity.py` |
| `flag_figures` | `[{flag, figure_id, name, description}]`: a shot flag that adds one figure to the headcount outside `count` (the figure id starts with `_`; the flag must be one of `shot_flags`) | `hfvg/qc/headcount.py` (`register_flag_figures`) |
| `shot_flags` | `[{key, meta, type, declared_meta, default, top_level}]`: `shots.json` keys copied into the plan's `shot_meta.flags.<meta>` (bool, str or int); a recompose may change only declared flags | `hfvg/shot_data.py`, recompose route |
| `beatmap` | `character_codes` (`{code: {slug, role, key, absent}}`; `key` and `absent`: the shot key a role is written to, and its value when the code is not in the row) and `role_prefix`: what a beatmap's character codes stand for | `hfvg/episode_parser.py` |
| `lint` | `count_clause {template, required}`, `realtime_pattern`, `locked_camera_clause`, `forbidden_phrases`, `banned_words` (+ `banned_words_message`), `content_policy_terms`, `forbid_bare_waves`, `required_clauses`, `forbid_near`, `continuity_locks` | `hfvg/prompt_lint.py` |
| `playbook` | `rules` (types `role_exclusion`, `pose_words`, `world_refs`, each with its own parameters and message templates) and `hard` (rule ids that refuse a live submit) | `hfvg/playbook.py`, `hfvg/moderation_lint.py` |
| `motion_suggestions` | `[{subject, suggestions, tag_match}]`: ambient motion suggested for shots naming a subject | `hfvg/qc/prompt_validation.py` |
| `plate.scale_reference` | the role whose height is the unit of a composited start plate | `hfvg/plate_composite.py` |
| `gates.policy_file` | a gate policy file in the show's folder: the preflight refuses one that is missing or invalid and the show-profile route reports which policy the show gets; the workflow's gate lookup still uses the deployment's policy ([GATE-POLICY.md](GATE-POLICY.md)) | `hfvg/show_profile.py`, preflight, API |
| `policy.real_people` | `"refuse"`, the only value (below) | loader, preflight |

### Conditions (`when`)

Lint clauses, near-word rules and continuity locks apply when their `when` matches the shot:
`{"always": true}`, `{"flag": "<meta>"}` (with an optional `"default"` for shots that do not set it),
`{"characters_any": [...]}`, `{"count_gt": n}`, `{"tag": "..."}`, `{"role": "..."}` (a role of the
shot's cast entries), combined with `{"all": [...]}`, `{"any": [...]}` and `{"not": {...}}`.

### Messages

Each rule carries its own message template (`{text}`, `{word}`, `{subject}`, `{sentence}`, `{id}`,
`{shot_id}` as the rule offers them), so a show's lint and playbook speak its own words. Without one
a rule uses a neutral default (`missing required clause {id!r}: {text!r}`, `word {word!r} near
{subject!r}: {sentence!r}`, `banned word: {word!r}`).

## A show with no animals

The smallest useful profile names the show and says what its prompts must carry:

```json
{
  "schema": "hfvg.show/1",
  "name": "Kitchen Hour",
  "lint": {
    "count_clause": {"template": "{n} cooks in frame.", "required": true},
    "required_clauses": [
      {"id": "hands_clean", "text": "Hands stay clean and dry.", "when": {"count_gt": 0}}
    ],
    "banned_words": ["logo"]
  },
  "policy": {"real_people": "refuse"}
}
```

Its cooks are ordinary cast-library characters (`shots.json` `cast`); the headcount counts them by
id, the identity gate judges their locked looks with the generic signs (or an `identity_profiles`
entry the show adds), and nothing in its messages or judge requests is about anything it did not
declare. A montage with nobody in it needs only `{"schema": "hfvg.show/1", "name": "..."}`: every
other key takes the neutral default (content-policy terms, the clip motion rules, no clause to
require, no shot flag or flag figure, the neutral sheet lead).

A new show ships a `show.json`, even that minimal one (or the deployment sets
`HFVG_SHOW_PROFILE_PATH`): an episode package with a `prompt_kit.json` or a `shots.json` and no
profile is read through the legacy adapter, so that every episode recorded before profiles keeps
its behaviour.

## The legacy adapter (the first series)

Episodes recorded before show profiles have no `show.json`; the legacy adapter
(`hfvg/compat/legacy_show.py`) reads their `prompt_kit.json` and `shots.json` exactly as the code
did before. `examples/first-series/show.json` is the adapter's profile written out, the readable
specification of that series as data (its host's identity profile, its `duck` flag figure, its
`duck_present` and `antler_character` shot flags, its lint and playbook rules, its plate scale
reference); a test keeps the two equal. The goldens under `tests/fixtures/legacy_golden/` pin every
surface the adapter feeds (lint and playbook messages, shot metadata keys, headcount rosters and
reviewer rows, sheet prompts, judge prompts and cache keys) byte for byte.

What the adapter keeps for stored data and older clients, in `hfvg/compat/`:

- `species_counter.py`: the first series' counter interfaces (`AnimalCounter`, `FakeAnimalCounter`,
  `check_shot_animals`, the `set_animal_counter` / `get_animal_counter` names, the reader of
  reviewer rows recorded as `cast` / `duck` / `other`). `hfvg.qc.animal_count` re-exports it for one
  release with a DeprecationWarning;
- `host_identity.py`: that series' host identity gate (formerly `hfvg.qc.duck_identity`);
- `legacy_playbook.py`, `legacy_plate.py`: its playbook validator and plate key spelling.

The reviewer headcount API (`POST /api/episodes/{id}/shots/{shot}/headcount`) takes per-figure
counts, or the older `cast` + `flag_counts` + `other` form; `duck` is accepted for `flag_counts` (the
same value; both with different values are refused). Stored rows keep the shape they had, so a
rollback reads them. The assembly QC result
still carries `animal_results` next to `headcount_results` (deprecated mirror for recorded workflow
histories).

## Real people

`policy.real_people` records a show's stance and has one value, `"refuse"`: shows use invented
characters only (docs/CONTENT-POLICY.md). There is no allow mode. The world, story and draft
prompts turn real people into originals, the cast store takes an uploaded likeness only with a
consent attestation, the `satire-sketch` card blocks real-person likeness, and a video model refuses
reference images of real faces. A mode for consented real figures would be a separate decision of
the product owner and a separate change.

## Keeping the engine free of show words

`tests/test_core_has_no_show_vocabulary.py` scans `hfvg/`, `api/` and the web app's source (code,
comments and file names) for species and first-series words. A hit passes only through an entry of
`tests/show_vocabulary_allowlist.json` with a reason and a `remove_by` date: the compatibility
layer, the deprecated module name, the optional archetype catalogue (cards tagged
`catalogue: "animal-pack"`), two motion-QC word lists and the replay-pinned recompose validator.
The test also proves it can fail (a planted word) and that the neutral default and the all-human
fixture are free of those words.
