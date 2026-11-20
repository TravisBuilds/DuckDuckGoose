"""Clip "fail if" checker (QC judge c, docs/QC-JUDGES.md): ``ClipConstraintChecker`` with a vision model.

The judge looks at FRAMES_PER_CLIP frames spread across the whole clip (ffmpeg through hfvg.subproc)
and answers every ``qc_fail_if`` condition of the clip in one paid call: ``review_clip`` passes them
all as ``shot_context["constraints"]``, the first ``check`` pays and caches the answer (clip content
hash, frame times, conditions, prompt) and the other conditions read it. ``evaluate_clip_constraints``
keeps its fail-closed rules: an error raised here, or a confidence under 0.7, is NEEDS_REVIEW.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from hfvg.qc.clip_constraints import ClipConstraintChecker
from hfvg.qc.judges import (
    REF_PX,
    JudgeCall,
    JudgeService,
    JudgeUnavailable,
    bool_of,
    confidence_of,
    extract_frames,
    file_sha256,
    prepare_image,
    probe_duration,
    spread_times,
)

FRAMES_PER_CLIP = 6

SYSTEM = """You check generated video clips of an animated episode for declared failure conditions. \
The images are frames spread across the whole clip, in time order. For every numbered condition say \
whether the clip shows it in any frame.
Answer with a JSON object only: {"conditions": [{"n": number, "violated": true | false, \
"confidence": number 0-1, "detail": "a few words: what you saw, in which frame"}]}, one entry per \
condition. confidence is how sure you are of that answer."""


def _validator(conditions: list[str]):
    def validate(answer: dict[str, Any]) -> dict[str, Any]:
        rows = answer.get("conditions")
        if not isinstance(rows, list):
            raise ValueError("no conditions list")
        by_n: dict[int, dict] = {}
        for row in rows:
            n = row.get("n") if isinstance(row, dict) else None
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(conditions) or n in by_n:
                raise ValueError(f"condition number {n!r} is unknown or repeated")
            by_n[n] = {"violated": bool_of(row.get("violated"), f"condition {n} violated"),
                       "confidence": confidence_of(row.get("confidence")),
                       "detail": str(row.get("detail") or "")[:300]}
        missing = [n for n in range(1, len(conditions) + 1) if n not in by_n]
        if missing:
            raise ValueError(f"no answer for condition(s) {missing}")
        return {"results": {conditions[n - 1]: by_n[n] for n in sorted(by_n)}}
    return validate


class VisionClipConstraintChecker(ClipConstraintChecker):
    """One cached vision call per clip for all of its declared conditions."""

    def __init__(self, service: JudgeService, frames_per_clip: int = FRAMES_PER_CLIP):
        self.service = service
        self.frames_per_clip = frames_per_clip

    async def check(self, media_path, constraint, shot_context):
        ctx = dict(shot_context or {})
        conditions = [c for c in (ctx.get("constraints") or []) if isinstance(c, str)]
        if constraint not in conditions:
            conditions = [constraint]
        media = Path(media_path)
        duration = await probe_duration(media)
        times = spread_times(duration, self.frames_per_clip)
        with tempfile.TemporaryDirectory(prefix="hfvg-fail-if-") as tmp:
            frames = await extract_frames(media, times, tmp)
            images = [prepare_image(f, REF_PX) for f in frames]
        prompt = str(ctx.get("prompt") or "").strip()
        text = "\n".join(
            [f"Shot {ctx.get('shot_id') or ''}: {len(frames)} frames at "
             + ", ".join(f"{t:.1f}s" for t in times) + f" of a {duration:.1f}s clip."]
            + ([f"The clip was generated from: {prompt}"] if prompt else [])
            + ["Fail if the clip shows:"] + [f"{i}. {c}" for i, c in enumerate(conditions, 1)])
        answer = await self.service.ask(JudgeCall(
            kind="clip_fail_if", episode_id=ctx.get("episode_id"),
            subject=f"{ctx.get('shot_id') or ''} {media.name}".strip(), system=SYSTEM, text=text, images=images,
            key_parts={"clip": file_sha256(media), "times": times, "conditions": conditions, "prompt": prompt},
            validate=_validator(conditions)))
        row = answer["results"].get(constraint)
        if row is None:
            raise JudgeUnavailable(f"no answer for {constraint!r}")
        return {"violated": row["violated"], "confidence": row["confidence"], "detail": row["detail"]}
