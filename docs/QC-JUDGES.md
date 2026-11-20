# Automated QC judges

Four vision judges check live stills and clips, so a person only looks at what they flag. They are
**off by default**: with `HFVG_QC_JUDGES_ENABLED` unset or false nothing is registered and QC works
exactly as before (every live still, every headcount without a reviewer count and every live clip
with `qc_fail_if` conditions waits for a person).

**Rule for every judge:** a judge that is missing, unsure, refused by the budget or the balance gate,
timed out, broken or answering nonsense gives **NEEDS_REVIEW** (a person decides), never PASS.
`DRY_RUN` makes no model call. The judges never re-roll anything: a FAIL is re-rolled only through
the bounded `retry_shot` / `retry_clip`.

## Turn it on

Set these for **both** the worker and the API (the API runs the G2 and recompose headcount), then
restart both:

| Env | Default | Meaning |
|---|---|---|
| `HFVG_QC_JUDGES_ENABLED` | `false` | `true` registers the judges |
| `OPENAI_API_KEY` | none | the judge key; without it every check is NEEDS_REVIEW |
| `HFVG_QC_JUDGE_MODEL` | `DEFAULT_MODEL` in `hfvg/qc/judges.py` | vision model id, never shown on a screen |
| `HFVG_QC_JUDGE_USD_PER_MTOK_IN`, `..._OUT` | `0.40`, `1.60` | the model's price per million input / output tokens: change them with the model |
| `HFVG_QC_LINE_CAP_USD` | `0.50` | cap of each episode's `qc_judges` budget line (its stop is 80 %) |
| `HFVG_QC_JUDGE_TIMEOUT_S` | `60` | one judge call |
| `HFVG_QC_REVIEW_DEADLINE_S` | `200` | all judge calls of one still or clip review |
| `HFVG_QC_JUDGE_RETRY_AFTER_S` | `300` | a failed call is not paid again for the same inputs before this |
| `HFVG_QC_JUDGE_IMAGE_TOKENS` | `1600` | tokens held per image (an upper bound) |
| `HFVG_QC_JUDGE_MAX_OUTPUT_TOKENS` | `600` | longest answer (held in full) |
| `HFVG_QC_JUDGE_TEMPERATURE` | `0` | `none` for a model without a temperature setting |

The worker log then says `QC judges enabled in the worker`, followed by `(no judge API key: every
check needs review)` when the key is missing. An invalid price, cap, limit or timeout refuses every
call (NEEDS_REVIEW); no call is ever made unpriced. The judges only act in live mode: with `DRY_RUN`
on (the default) no judge is called, `review_still` and `review_clip` work as before and a headcount
left to the counter is NEEDS_REVIEW. To turn the judges off, unset the flag and restart.

## How each judge decides

All four go through `JudgeService.ask` (`hfvg/qc/judges.py`): one request with the images, a JSON
answer that is checked before use (a malformed answer is NEEDS_REVIEW), and the money rules below.

### a. Still reviewer (`review_still`)

- **Sees:** the still, the shot's still prompt (after any moderation reword, with the locked cast
  clauses) and up to six of the shot's references (cast sheets, place and prop photos).
  `ShotWorkflow` sends them in the rubric behind `workflow.patched("qc-still-rubric")`; a run parked
  at review before that patch keeps its empty rubric, which is NEEDS_REVIEW.
- **Answers:** pass, fail (naming the issues) or unsure, with a confidence.
- **Decides:** confidence below 0.7, or unsure: NEEDS_REVIEW. Fail: FAIL with `escalate: false`, so
  the shot ends `failed` with the judge's reasons and is re-rolled through `retry_shot`. Pass: the
  identity gate (d) for each locked character, worst result wins; PASS is recorded as
  `still_complete`. The episode checks (episode id, live mode, G1.08) still run first.

### b. Character and animal counter (`AnimalCounter`)

- **Where:** the G2 headcount (`GET /g2-headcount`, every G2 approval, the recompose headcount) in
  the API, and the headcount check of assembly QC in the worker. A reviewer's recorded headcount
  still wins and makes no call.
- **Sees:** the still (or three frames of the clip inside its trim window), the shot's declared
  cast (`shot_meta.cast`) and their sheet images.
- **Answers:** in one call, how many cast characters, ducks and animals are in the shot. The duck
  host counts as a duck and an animal, never as cast; without a declared cast, every distinct
  character that is a subject of the shot counts as cast. The three counts share that one answer.
- **Decides:** confidence below 0.7: NEEDS_REVIEW. A wrong count: FAIL (at G2 it blocks approval
  unless overridden with a reason; in assembly QC it is the hard headcount check). A counter error:
  NEEDS_REVIEW for that shot, never a 500 or an activity retry.

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
  duration): one call finds every locked character (found, confidence, body and head box). A
  composite of real crops (the sheet images next to the character cut out of the frame) goes to one
  call per character, which scores face, hair or fur, colours, outfit and proportions 0 (on model),
  1 (minor) or 2 (drift).
- **Decides:** detector or judge confidence below 0.6, or a declared character not found:
  NEEDS_REVIEW. A sign at 2, or minor scores adding up to 3: FAIL, re-rolled through `retry_shot` /
  `retry_clip`. Otherwise the judge's own verdict. The worst frame and the worst character win.
- The duck host's rules (bill, head, neck ring and body; tuque and vest; the tuft in "B" shots; the
  0.12 height floor) are one configured profile of the same gate (`DuckIdentityGate`), which the
  pipeline does not call, as before; a duck host that is a locked cast character is checked like any
  other. The always-pass fakes of `create_gate()` are for dry run only: a live check with them
  escalates.

No verdict, issue or ledger entry names the model; the counter reports its method as `vision_judge`.

## What it costs (estimates)

Every paid call: the balance gate, a hold of the call's estimate on the episode's `qc_judges` budget
line (provider `openai`), the call, then a commit of the real cost from the response's token usage
(or a release when no answer came back). The line's own stop, the episode cap and stop and the GC.01
headroom all apply. OpenAI has no balance endpoint for an API key, so the gate reads its balance as
unknown and lets the line bound the spend. The judges do not rely on `override_unavailable_estimates`:
they compute their own estimate, and without one (an invalid price setting) the call is refused.

**Budget line.** The default line caps were $44.00 (Higgsfield) + $3.50 (ElevenLabs) = $47.50 of the
$48.00 episode stop, so `qc_judges` gets the remaining $0.50 (stop $0.40) and the episode stop does
not move. `HFVG_QC_LINE_CAP_USD` raises it for bigger episodes, with a trade-off: above $0.50 the
default line caps add up to more than the $48 stop, which the ledger's design otherwise avoids. The
episode stop still bounds the total, so in an episode whose lines run close to their caps, judge
spend and generation compete for the last dollars under the stop: a late generation hold can be
refused there, and so can a judge call (NEEDS_REVIEW). Once the `qc_judges` line reaches its own
stop, every later check of that episode is NEEDS_REVIEW, never a pass.

**Per call.** "Held" is the reservation the code computes at the default settings (1,600 tokens per
image, the full 600-token answer), an upper bound. "Typical" is an estimate that assumes about 1,000
input tokens per 768-px image or composite, about 450 per 512-px reference, and a short answer. Both
use the default prices; the model's own token count and price sheet decide the real cost, which is
what the ledger commits.

| Call | Held | Typical (estimate) |
|---|---|---|
| Still review: the still and 4 references | $0.0045 | $0.0017 |
| Headcount at G2: the still and 3 cast sheets | $0.0037 | $0.0013 |
| Headcount in assembly QC: 3 frames and 3 cast sheets | $0.0050 | $0.0021 |
| Clip "fail if": 6 frames, every condition | $0.0051 | $0.0030 |
| Identity, finding the characters: 1 frame | $0.0018 | $0.0008 |
| Identity, judging one character: 1 composite | $0.0019 | $0.0012 |

**Per episode (estimate).** A shot with one locked character and `qc_fail_if` conditions, with one
still and one clip attempt, takes 12 calls (still review, 2 for the still's identity, G2 headcount,
clip check, 6 for the clip's identity, assembly headcount): about $0.016. A 20-shot episode is about
240 calls and about $0.32, inside the line's $0.40 stop. Each extra still attempt adds about $0.005,
each extra clip attempt about $0.009, and each further locked character one identity call per still
and three per clip (the finding call is shared).

**Paid once.** A verdict is cached by the content hashes of the media and the references plus every
text input, the model id and the judge version, so page loads, G2 approvals, retried activities and
edit rebuilds with the same clips do not pay again. A new still or clip, a changed reference or a
changed prompt is judged again.

**Credits.** The judges spend OpenAI dollars, not Higgsfield or ElevenLabs credits. A stricter QC
means more re-rolls, which stay bounded by `MAX_SHOT_RETRIES`, `MAX_PAID_RETRIES_PER_SHOT_PER_ROUND`
and the per-round caps.

## Code and tests

- `hfvg/qc/judges.py` (calls, cache, money, registration), `hfvg/qc/still_review.py`,
  `hfvg/qc/vision_counter.py`, `hfvg/qc/vision_constraints.py`, `hfvg/qc/identity.py`
  (`hfvg/qc/duck_identity.py` is the duck host's profile); wired in `hfvg/activities/qc.py`,
  `hfvg/workflows/shot.py`, `hfvg/qc/g2_headcount.py`, `hfvg/qc/animal_count.py`,
  `hfvg/worker.py` and `api/main.py`.
- `tests/test_qc_judges*.py`, `tests/test_qc_still_review.py`, `tests/test_qc_vision_counter.py`,
  `tests/test_qc_vision_constraints.py`, `tests/test_qc_identity.py` use a fake judge client (or a
  mocked OpenAI endpoint): no test makes a network call or pays. Red-green mutations M-730 to M-758
  cover the fail-closed rules.
