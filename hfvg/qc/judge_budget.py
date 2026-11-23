"""The size of an episode's ``qc_judges`` budget line when ``HFVG_QC_LINE_CAP_USD=auto``
(docs/QC-JUDGES.md, "What it costs").

One take of a shot (its still and clip, again for every re-roll) can ask the judges:

    still review         1 call         the still and its first references
    G2 headcount         1              the still and the cast sheets
    still identity       1 + N          one call finds the N locked characters, one judges each
    clip qc_fail_if      1              when the shot declares conditions
    clip identity        3 x (1 + N)    the first, middle and last frame
    assembly headcount   1              frames of the edit and the cast sheets

N being the locked cast-library characters the shot declares: 8 + 4N calls for a shot with
conditions and N >= 1, 4 for a shot with conditions and no locked character. The line's stop
(EPISODE_STOP_PERCENT of its cap) holds TAKES_PER_SHOT takes of every shot at each call's held
amount (its reservation, an upper bound of what the call costs). The line is never smaller than
the fixed default cap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hfvg.budget import get_budget_settings
from hfvg.cast_prompt import cast_entries
from hfvg.pricing import usd_to_micros
from hfvg.qc import still_review, vision_constraints, vision_counter
from hfvg.qc.identity import clip_frame_times, locked_entries
from hfvg.qc.judges import DEFAULT_LINE_CAP_USD, call_estimate_usd_micros, max_output_tokens

TAKES_PER_SHOT = 2
CALL_TEXT_CHARS = 6000  # instructions and request text of one call, an upper bound
IDENTITY_CLIP_FRAMES = len(clip_frame_times(1.0))


@dataclass(frozen=True)
class ShotJudging:
    """What one shot asks of the judges: its locked characters, its declared characters (a sheet
    each in the headcount) and whether it declares qc_fail_if conditions."""

    locked: int = 0
    cast: int = 0
    fail_if: bool = False


def shot_judging(plan: dict[str, Any]) -> ShotJudging:
    meta = plan.get("shot_meta") or {}
    return ShotJudging(locked=len(locked_entries(meta)), cast=len(cast_entries(meta)),
                       fail_if=bool(plan.get("qc_fail_if")))


def take_calls(shot: ShotJudging) -> list[tuple[str, int]]:
    """(what, images) of every judge call one take of the shot can make."""
    sheets = min(shot.cast, vision_counter.MAX_REFS)
    calls = [("still review", 1 + still_review.MAX_REFS),
             ("G2 headcount", 1 + sheets),
             ("assembly headcount", vision_counter.FRAMES_PER_CLIP + sheets)]
    if shot.fail_if:
        calls.append(("clip qc_fail_if", vision_constraints.FRAMES_PER_CLIP))
    if shot.locked:
        for _frame in range(1 + IDENTITY_CLIP_FRAMES):
            calls.append(("identity: find the locked characters", 1))
            calls += [("identity: judge one character", 1)] * shot.locked
    return calls


def take_usd_micros(shot: ShotJudging, model: str | None = None) -> int:
    out = max_output_tokens()
    return sum(call_estimate_usd_micros(images, CALL_TEXT_CHARS, out, model) for _what, images in take_calls(shot))


def line_cap_usd_micros(shots: list[ShotJudging], model: str | None = None) -> int:
    """The cap of a line whose stop holds TAKES_PER_SHOT takes of every shot (at least the default
    cap). Raises ValueError when the prices or the stop percent are invalid."""
    need = TAKES_PER_SHOT * sum(take_usd_micros(s, model) for s in shots)
    percent = get_budget_settings().stop_percent
    if not 0 < percent <= 100:
        raise ValueError(f"EPISODE_STOP_PERCENT must be 1-100, got {percent}")
    return max(-(-need * 100 // percent), usd_to_micros(DEFAULT_LINE_CAP_USD))


def episode_shots(episode_id: str) -> list[ShotJudging]:
    """The episode's shots as planned from its shots.json and cast library (no composite written)."""
    from hfvg.cast_plan import plan_cast
    from hfvg.shot_data import episode_profile, parse_shots, shots_json_path

    plans = parse_shots(json.loads(Path(shots_json_path(episode_id)).read_text(encoding="utf-8")),
                        episode_profile(episode_id))
    for plan in plans.values():
        plan.setdefault("refs", [])
        plan.setdefault("missing_refs", [])
    errors = plan_cast(episode_id, plans, live=False, materialize=False)["errors"]
    if errors:
        raise ValueError("; ".join(errors)[:300])
    return [shot_judging(p) for p in plans.values()]


def episode_line_cap_usd_micros(episode_id: str, model: str | None = None) -> int:
    return line_cap_usd_micros(episode_shots(episode_id), model)
