# Automated QC judges

Four vision judges check live stills and clips, so a person only looks at what they flag. They work
for any show: its cast can be people, creatures, robots, puppets, props that act, or a mix, in any
look. The judges know a show only through what its shots declare (the cast entries, the number of
main characters in frame, the props and places, the `qc_fail_if` conditions) and the locked sheets
of its cast library.

They are **off by default**: with `HFVG_QC_JUDGES_ENABLED` unset or false nothing is registered and
QC works exactly as before (every live still, every headcount without a reviewer count and every
live clip with `qc_fail_if` conditions waits for a person).

**Rule for every judge:** a judge that is missing, unsure, refused by the budget or the balance gate,
timed out, broken or answering nonsense gives **NEEDS_REVIEW** (a person decides), never PASS. When
the judge budget ran out, the reason says so: it starts with "judge budget used up, a person checks
this". `DRY_RUN` makes no model call. The judges never re-roll anything: a FAIL is re-rolled only
through the bounded `retry_shot` / `retry_clip`. Nor does a FAIL ever pass on its own: a failed still
waits at G2 for a person (a).

## Turn it on

Set these for **both** the worker and the API (the API runs the G2 and recompose headcount), then
restart both:

| Env | Default | Meaning |
|---|---|---|
| `HFVG_QC_JUDGES_ENABLED` | `false` | `true` registers the judges |
| `OPENAI_API_KEY` | none | the judge key; without it every check is NEEDS_REVIEW |
| `HFVG_QC_JUDGE_MODEL` | `DEFAULT_MODEL` in `hfvg/qc/judges.py` | `provider/model`, or a bare OpenAI model id; never shown on a screen. The provider names the budget line and the balance gate. Only OpenAI has a judge client: a model of another provider is NEEDS_REVIEW without a call |
| `HFVG_QC_JUDGE_USD_PER_MTOK_IN`, `..._OUT` | `0.40`, `1.60` | the model's price per million input / output tokens. The defaults price the default model only: for any other model set both, or no call is made |
| `HFVG_QC_LINE_CAP_USD` | `0.50` | cap of each episode's `qc_judges` budget line (its stop is 80 %), or `auto` to size each new line from the episode's shots ([Budget line](#what-it-costs-estimates)) |
| `HFVG_QC_JUDGE_TIMEOUT_S` | `60` | one judge call |
| `HFVG_QC_REVIEW_DEADLINE_S` | `200` | all judge calls of one still or clip review |
| `HFVG_QC_JUDGE_RETRY_AFTER_S` | `300` | a failed call is not paid again for the same inputs before this |
| `HFVG_QC_JUDGE_IMAGE_TOKENS` | `1600` | tokens held per image (an upper bound) |
| `HFVG_QC_JUDGE_MAX_OUTPUT_TOKENS` | `600` | longest answer (held in full) |
| `HFVG_QC_JUDGE_TEMPERATURE` | `0` | `none` for a model without a temperature setting |

The worker log then says `QC judges enabled in the worker`, followed by `(no judge API key: every
check needs review)` when the key is missing. An invalid or missing price, cap, limit or timeout
refuses every call (NEEDS_REVIEW); no call is ever made unpriced. The judges only act in live mode:
with `DRY_RUN` on (the default) no judge is called, `review_still` and `review_clip` work as before
and a headcount left to the counter is NEEDS_REVIEW. To turn the judges off, unset the flag and
restart.

## What a shot declares

The judges read the shot plan: `shots.json` after the cast planning of [CAST.md](CAST.md) (keys in
[EPISODE-INPUTS.md](EPISODE-INPUTS.md#cast-options-shotsjson-prompt_kitjson)).

| In the shot plan | The judges use it as |
|---|---|
| `cast` (`shot_meta.cast`) | the declared characters, each by its id (the cast slug) with its name, description and sheet |
| a locked version in `cast` (`locked: true`) | a character whose identity the gate (d) checks |
| `count` | the number of main characters in frame; those without a cast entry are one group, counted together |
| `props` and `place` (`shot_meta.props`) | not figures: the counter is told not to count them |
| `qc_fail_if` | the clip conditions (c) |

Any figure in frame that the plan does not declare is **unexpected** when it stands out as a subject
of the shot, or **background** (passers-by, a crowd). A prop that acts as a character (a talking
kettle) belongs in the cast library, not in `props`: then it is counted, and its identity judged,
like any other character. The plan cannot declare background figures, so they always go to a person.

## How each judge decides

All four go through `JudgeService.ask` (`hfvg/qc/judges.py`): one request with the images, a JSON
answer that is checked before use (a malformed answer is NEEDS_REVIEW), and the money rules below.
No instruction to a judge names a kind of character or a visual style.

### a. Still reviewer (`review_still`)

- **Sees:** the still, the shot's still prompt (after any moderation reword, with the locked cast
  clauses) and up to six of the shot's references (cast sheets, place and prop photos).
  `ShotWorkflow` sends them in the rubric behind `workflow.patched("qc-still-rubric")`; a run parked
  at review before that patch keeps its empty rubric, which is NEEDS_REVIEW.
- **Answers:** pass, fail (naming the issues) or unsure, with a confidence.
- **Decides:** confidence below 0.7, or unsure: NEEDS_REVIEW. Fail: FAIL with `escalate: false`: the
  still waits at G2 for a person, marked judge-failed with the judge's reasons (DB status
  `qc_failed`, `qc_failed` and `needs_decision` in `GET /shot-states`). G2 approval is refused until
  a person decides on it: keep it (approve the shot or select its version; it is then animated like
  any kept still), make it again (`retry_shot`) or reject it. A decision made before the verdict
  does not count, and G2 approval alone never animates it. Pass: the
  identity gate (d) for each locked character, worst result wins; PASS is recorded as
  `still_complete`. The episode checks (episode id, live mode, G1.08) still run first.
- **Replay:** the wait is behind `workflow.patched("qc-fail-parks-at-g2-v1")` (and the parent's keep
  signal behind `"g2-keep-qc-failed-still-v1"`): a shot a FAIL ended before keeps that ending, and is
  made again through `retry_shot`. A canary still and a recompose still end `failed` on a FAIL, as
  before.

### b. Headcount (`CastCounter`, `hfvg/qc/headcount.py`)

- **Where:** the G2 headcount (`GET /g2-headcount`, every G2 approval, the recompose headcount) in
  the API, and the headcount check of assembly QC in the worker. A reviewer's recorded headcount
  still wins and makes no call.
- **Figures** (`shot_roster`): one per cast entry, by its id, expected once. When `count` is larger
  than the cast list, the other main characters are one more figure (id `_cast`, expected `count`
  minus the cast list). A cast list longer than `count` is a plan a person confirms.
- **Sees:** the still (or three frames of the clip inside its trim window), every figure by its id
  and name with its cast description (the locked one for a locked version), the first sheet image
  of up to four declared characters, each labelled with the same id and name, and the shot's props
  and place, named as not figures.
- **Answers:** in one call (`count_cast(media, shot_context)`), for every figure id: how many are in
  frame and whether they look like the declared character (`matches`); how many unexpected and
  background figures; a confidence. For a clip, the largest number seen in one frame.
- **Decides**, the worst row winning:

| Answer | Result |
|---|---|
| a figure counted another number of times than declared | FAIL ("Ben: expected 1, counted 2") |
| an unexpected figure | FAIL ("unexpected figures: expected 0, counted 1") |
| a figure counted, but it may not be the declared character (`matches: false`) | NEEDS_REVIEW |
| background figures | NEEDS_REVIEW |
| a declared figure the counter did not count | NEEDS_REVIEW |
| confidence below 0.7 | NEEDS_REVIEW |
| a cast list longer than `count` | NEEDS_REVIEW |
| no counter, no still or clip, a counter error, the judge budget used up | NEEDS_REVIEW |
| every figure as declared, nothing else | PASS |

At G2 a FAIL blocks approval unless overridden with a reason. In assembly QC the headcount is a hard
check (it holds `G_FINAL_CUT`) unless the episode makes it advisory. A counter error is NEEDS_REVIEW
for that shot, never a 500 or an activity retry.

**A reviewer's count** has the shape of the counter's answer. `GET /g2-headcount` lists each shot's
figures (`expected.figures`: id, name, expected count), and the reviewer sends a count per id:

```http
POST /api/episodes/{id}/shots/{shot}/headcount

{"version": 2, "figures": {"ana": 1, "ben": 1, "pip": 1}, "unexpected": 0, "background": 0, "note": ""}
```

The count must name every figure that still's plan declares and no other: a mistyped id, or a
declared one left out, is refused (422) when it is recorded. The plan is the still version's, or
the shot's latest recompose still's; when the workflow cannot be read, nothing is recorded (409 or
503). A count stored before the plan changed is read as before: an id the shot no longer declares
is an unexpected figure, a declared id it lacks is not counted (NEEDS_REVIEW). The first series'
form (`cast`, `duck`, `other`) is still accepted, never mixed with `figures`, and needs no plan
([Compatibility](#compatibility)).

### c. Clip "fail if" checker (`ClipConstraintChecker`)

- **Where:** live `review_clip` of a shot with `qc_fail_if` conditions, once motion QC and the
  stillgate pass.
- **Sees:** six frames spread across the whole clip (ffmpeg through `hfvg.subproc`), the shot's
  prompt and the numbered conditions.
- **Answers:** in one call, for every condition: violated or not, a confidence and what it saw.
- **Decides** (`evaluate_clip_constraints`): confidence below 0.7: NEEDS_REVIEW. Violated: FAIL with
  `escalate: false`, re-rolled through `retry_clip`. Otherwise PASS. The worst condition wins; a
  person keeps a NEEDS_REVIEW clip (`accept_clip_review`) or re-rolls it.

### d. Identity gate for locked characters (`hfvg/qc/identity.py`)

- **Where:** after a still passes (a), and in `review_clip` once every other check passed, for each
  character the shot declares with `locked: true` in `shot_meta.cast`.
- **Criteria:** the character's lock in the cast library: locked description, sheet images (master,
  turnaround, head) and lock hash. A lock that changed on disk or cannot be read: NEEDS_REVIEW
  without a call.
- **Per frame** (the still; for a clip 0.1 s in, the middle and 0.15 s before the end of its real
  duration): one call finds every locked character (found, confidence, the box of the character and
  of its head, or its top part when it has none). A composite of real crops (the sheet images next
  to the character cut out of the frame) goes to one call per character, which scores each sign
  0 (on model), 1 (minor) or 2 (drift). The signs are the same for every kind of character:

| Sign | Compared with the sheet |
|---|---|
| `face` | the face or front: its shape and features |
| `surface` | what the character is made of or covered in: colour, texture, length and style |
| `colours` | the colours and markings |
| `outfit` | the clothes and accessories of the locked outfit (0 when the sheet shows none and the frame adds none) |
| `proportions` | shape, build and proportions of the whole character and its parts |

- **Decides:** detector or judge confidence below 0.6, or a declared character not found:
  NEEDS_REVIEW. A sign at 2, or minor scores adding up to 3: FAIL (a still's waits at G2 as in (a)),
  re-rolled through `retry_shot` / `retry_clip`. Otherwise the judge's own verdict. The worst frame
  and the worst character win.
- **Rules are data** (`IdentityProfile`: the signs, apparel that must match when present, a sign that
  fails already at 1, a feature some shots must show, a size floor). Every locked character uses
  `GENERIC_PROFILE`; the cast library cannot give a character its own rules yet.
- The first series' duck host gate (`hfvg/qc/duck_identity.py`) is a compatibility shim: its rules
  are one profile (`DUCK_HOST_PROFILE`) run by the same gate, for the code and tests written against
  it. The pipeline does not call it, and never did: a duck host that is a locked cast entry is
  checked like any other character. The always-pass fakes of `create_gate()` are for dry run only:
  a live check with them escalates.

No verdict, issue or ledger entry names the model; the counter reports its method as `vision_judge`.

## What it costs (estimates)

Every paid call: the balance gate for the model's provider, a hold of the call's estimate on the
episode's `qc_judges` budget line (created for that provider), the call, then a commit of the real
cost from the response's token usage (or a release when no answer came back). The line's own stop,
the episode cap and stop and the GC.01 headroom all apply. OpenAI has no balance endpoint for an API
key, so the gate reads its balance as unknown and lets the line bound the spend. The judges do not
rely on `override_unavailable_estimates`: they compute their own estimate, and without one (a
missing or invalid price setting) the call is refused.

**Calls per take.** One take of a shot (its still and its clip, again for every re-roll) can ask,
with N the locked characters the shot declares:

| Judge | Calls |
|---|---|
| still review | 1 |
| still identity | 1 + N (one call finds every locked character, one judges each) |
| G2 headcount | 1 |
| clip "fail if" | 1, when the shot declares conditions |
| clip identity | 3 × (1 + N): the first, middle and last frame |
| assembly headcount | 1 |

That is 8 + 4N calls for a shot with conditions and N ≥ 1, and 4 for a shot with conditions and no
locked character.

**Per call.** "Held" is the reservation the code computes at the default settings (1,600 tokens per
image, the full 600-token answer), an upper bound. "Typical" is an estimate that assumes about 1,000
input tokens per 768-px image or composite, about 450 per 512-px image (references, cast sheets, the
frames of the "fail if" check) and a 150-token answer. Both use the default prices; the model's own
token count and price sheet decide the real cost, which is what the ledger commits.

| Call | Held | Typical (estimate) |
|---|---|---|
| Still review: the still and 4 references | $0.0045 | $0.0017 |
| Headcount at G2: the still and 3 cast sheets | $0.0039 | $0.0016 |
| Headcount in assembly QC: 3 frames and 3 cast sheets | $0.0052 | $0.0024 |
| Clip "fail if": 6 frames, every condition | $0.0051 | $0.0016 |
| Identity, finding the characters: 1 frame | $0.0018 | $0.0008 |
| Identity, judging one character: 1 composite | $0.0019 | $0.0009 |

**Per take (estimate)**, one still and one clip attempt of a shot with `qc_fail_if` conditions and
three cast sheets (four when four characters are locked):

| Locked characters (N) | Calls | Typical | Held |
|---|---|---|---|
| 0 | 4 | $0.007 | $0.019 |
| 1 | 12 | $0.014 | $0.033 |
| 2 | 16 | $0.018 | $0.041 |
| 3 | 20 | $0.022 | $0.048 |
| 4 | 24 | $0.025 | $0.058 |

**Budget line.** By default every episode's line has the fixed $0.50 cap (stop $0.40): the room left
under the $48.00 episode stop by the default line caps ($44.00 Higgsfield + $3.50 ElevenLabs =
$47.50), so the episode stop does not move. It fits a small show: if every call cost its full hold,
the stop would cover one take of about 12 shots with one locked character, 10 with two or 8 with
three (about 28, 22 or 18 at the typical cost), fewer with re-rolls. After that, every check of the
episode is NEEDS_REVIEW with "judge budget used up".

`HFVG_QC_LINE_CAP_USD=auto` sizes the line from the show instead (`hfvg/qc/judge_budget.py`). When an
episode's first judge call creates its line, the cap is set so that the stop holds two takes of every
shot of its `shots.json`, each call at its held amount with its text counted at a 6,000-character
upper bound; never less than $0.50. For shots with `qc_fail_if` conditions and three cast entries
(four in the last row):

| Episode | `auto` cap (stop) |
|---|---|
| 12 shots, 1 locked character each | $1.26 ($1.01) |
| 20 shots, 1 locked character each | $2.10 ($1.68) |
| 20 shots, 3 locked characters each | $3.09 ($2.47) |
| 30 shots, 4 locked characters each | $5.47 ($4.38) |

A shot plan that cannot be read or planned gets the $0.50 cap, with a warning in the log. A fixed
cap stays as set, but when the episode's first judge call creates its line with a fixed cap smaller
than the `auto` size, the log warns, naming both amounts. `auto` is not the default: on a show
bigger than the $0.50 line holds, it would raise what the judges may spend without anyone changing
a setting. An existing line keeps its cap: `auto`, like a new number, applies to lines created
after the change.
A cap above $0.50, fixed or `auto`, has a trade-off: the default line caps then add up to more than
the $48 stop, which the ledger's design otherwise avoids. The episode stop still bounds the total,
so in an episode whose lines run close to their caps, judge spend and generation compete for the
last dollars under the stop: a late generation hold can be refused there, and so can a judge call
(NEEDS_REVIEW, "judge budget used up ... (the episode stop)"). Raising `EPISODE_CAP_USD` makes room
for both.

**When the judge budget is used up.** A call refused at the `qc_judges` line's stop or cap, or at the
episode's, is NEEDS_REVIEW with a reason that starts "judge budget used up, a person checks this"
and names the limit, for example "(the qc_judges line: $0.40 spent or held of its $0.40 stop)" or
"(the episode stop)". The G2 headcount report and the assembly QC result list those shots in
`judge_budget_used_up`, so the reviewer knows which checks no judge looked at. A refusal for another
reason (the account's GC.01 headroom, the balance gate) is NEEDS_REVIEW with its own reason.

**Paid once.** A verdict is cached by the content hashes of the media and the references plus every
text input (the prompt, the declared figures, the props), the model id and the judge version, so
page loads, G2 approvals, retried activities and edit rebuilds with the same clips do not pay again.
A new still or clip, a changed reference, prompt or cast list is judged again.

**Credits.** The judges spend dollars of the judge model's provider (OpenAI by default), not
Higgsfield or ElevenLabs credits. A stricter QC means more re-rolls, which stay bounded by
`MAX_SHOT_RETRIES`, `MAX_PAID_RETRIES_PER_SHOT_PER_ROUND` and the per-round caps.

## Compatibility

The first series' episodes (including the live ones) and their stored data keep working without
any edit:

- A shot that declares its host with `duck_present` (`shot_meta.duck`) outside `count` gets the host
  as one more figure (id `_duck_host`, counted alone, `hfvg/qc/animal_count.py`). This is read when
  the shot is counted, not when `shots.json` is parsed, because a running episode's workflow holds
  the plans it was started with, which declare the host only by `duck: true`. When the cast list
  is already longer than `count`, the host is one of the listed entries and nothing is added. A new
  show with such a host declares it as an ordinary cast entry, locked so that its identity is judged.
- Reviewer counts stored as `cast` / `duck` / `other` in `g2_headcounts.json`, and the same form of
  the POST body, are read in the general shape: `cast` counts the declared figures together, `duck`
  the host figure (or the host in the cast list; anywhere else a duck is an unexpected figure),
  `other` the unexpected figures.
- `AnimalCounter.count_animals(media, expected_type, shot_context)`, `FakeAnimalCounter`,
  `DuckPresenceCheck`, `check_shot_animals`, `create_duck_check`, `set_animal_counter` /
  `get_animal_counter` and `VisionAnimalCounter` still work as adapters: an `AnimalCounter` answers
  the general check through `count_cast`, asking the questions the older checks asked, and its
  `type_match: false` is NEEDS_REVIEW, like `matches: false`.
- Assembly QC still returns `animal_results`, the same list as `headcount_results`.
- `JUDGE_VERSION` is 2: a verdict cached under the earlier instructions is judged again once, on the
  same budget line.

## Code and tests

- `hfvg/qc/judges.py` (calls, cache, money, registration), `hfvg/qc/judge_budget.py` (the `auto`
  line), `hfvg/qc/still_review.py`, `hfvg/qc/headcount.py` (figures and the headcount rules),
  `hfvg/qc/vision_counter.py`, `hfvg/qc/vision_constraints.py`, `hfvg/qc/identity.py`; wired in
  `hfvg/activities/qc.py`, `hfvg/workflows/shot.py`, `hfvg/qc/g2_headcount.py`, `hfvg/worker.py` and
  `api/main.py`. Compatibility: `hfvg/qc/animal_count.py`, `hfvg/qc/duck_identity.py`.
- `tests/test_qc_judges*.py`, `tests/test_qc_still_review.py`, `tests/test_qc_vision_counter.py`,
  `tests/test_qc_vision_constraints.py`, `tests/test_qc_identity.py`, `tests/test_g2_headcount.py`
  and `tests/test_qc_any_show.py` use a fake judge client (or a mocked OpenAI endpoint): no test
  makes a network call or pays. `tests/test_qc_any_show.py` runs the judges on shows without a duck
  (two people and a robot, a person with a lamp, a prop that acts) and sizes the budget for N locked
  characters; `tests/test_qc_duck_identity.py` keeps the compatibility shim working. Red-green
  mutations M-730 to M-758 and M-764 to M-792 cover the fail-closed rules.
