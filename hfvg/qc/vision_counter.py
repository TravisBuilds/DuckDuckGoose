"""Character and animal counter (QC judge b, docs/QC-JUDGES.md): ``AnimalCounter`` with a vision model.

One paid call per still or clip answers every count the callers ask for (``cast``, ``duck``,
``animal``), so the three counts of the G2 headcount share it; the answer is cached by the media's
content hash, the frame times and the shot's declared characters. ``cast`` counts the shot's main
characters of any kind (animals or not, the duck host excluded) using their locked descriptions and
one sheet image each from the cast library; ``animal`` counts every animal in frame, any species
(minus the duck host when the caller says ``exclude_duck_host``: the assembly check compares it with
``shot_meta.count``). A clip is counted on frames spread across its trim window: the largest number
seen in one frame.

Errors are raised (``JudgeUnavailable``): the callers turn them into NEEDS_REVIEW for that shot.
A reviewer's recorded headcount still wins (``ReviewerHeadcountCounter`` asks this counter only
when no reviewer count exists for the still).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from hfvg.qc.animal_count import AnimalCounter
from hfvg.qc.judges import (
    MEDIA_PX, REF_PX, JudgeCall, JudgeService, JudgeUnavailable, confidence_of, count_of, extract_frames,
    file_sha256, is_video, prepare_image, probe_duration, spread_times,
)

FRAMES_PER_CLIP = 3
MAX_REFS = 4
SPECIES = ("cast", "duck", "animal")

SYSTEM = """You count characters and animals in frames of an animated episode. \
The images before the reference images are the frames to count (one still, or frames from one clip). \
Count, in the frame that shows the most of each:
- cast: the shot's main characters described below (animals or not), each one that is visible; the \
duck host is not counted here;
- duck: every duck;
- animal: every animal of any species, main characters and ducks included, background animals too.
When a clip shows different numbers in different frames, give the largest number seen in one frame.
Answer with a JSON object only: {"cast": int, "duck": int, "animal": int, "confidence": number 0-1, \
"notes": "a few words"}. confidence is how sure you are of all three counts."""


def _validate(answer: dict[str, Any]) -> dict[str, Any]:
    return {**{k: count_of(answer.get(k)) for k in SPECIES}, "confidence": confidence_of(answer.get("confidence")),
            "notes": str(answer.get("notes") or "")[:200]}


def _cast_refs(episode_id: str | None, shot_meta: dict | None) -> list[tuple[str, str]]:
    """(name, first identity image) of each declared character found in the cast library."""
    from hfvg import cast_store
    from hfvg.cast_prompt import cast_entries

    entries = cast_entries(shot_meta)
    if not entries or not episode_id:
        return []
    out = []
    try:
        library = cast_store.library_for_episode(episode_id)
        for entry in entries:
            character = cast_store.find_character(library, entry.get("character"))
            version = next((v for v in (character or {}).get("versions") or []
                            if v["version"] == entry.get("version")), None)
            if version is None:
                continue
            images, _notes = cast_store.identity_images(character, version, live=True)
            if images:
                out.append((character["name"], str(cast_store.image_file(images[0]["sha256"], images[0]["ext"]))))
    except Exception:  # noqa: BLE001 - references help; the descriptions still go in the prompt
        return out
    return out[:MAX_REFS]


class VisionAnimalCounter(AnimalCounter):
    """Counts with one cached vision call per still or clip window."""

    def __init__(self, service: JudgeService, frames_per_clip: int = FRAMES_PER_CLIP):
        self.service = service
        self.frames_per_clip = frames_per_clip

    async def count_animals(self, media_path: str | Path, expected_type: str = "duck",
                            shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        if expected_type not in SPECIES:
            raise JudgeUnavailable(f"the counter counts {', '.join(SPECIES)}, not {expected_type!r}")
        ctx = dict(shot_context or {})
        answer = await self._headcount(Path(media_path), ctx)
        count = answer[expected_type]
        if expected_type == "animal" and ctx.get("exclude_duck_host"):
            count -= min(answer["duck"], 1)
        return {"found": count > 0, "count": count, "confidence": answer["confidence"], "type_match": True,
                "method": "vision_judge"}

    async def _headcount(self, media: Path, ctx: dict[str, Any]) -> dict[str, Any]:
        meta = ctx.get("shot_meta") or {}
        episode_id = ctx.get("episode_id")
        refs = await asyncio.to_thread(_cast_refs, episode_id, meta)
        described = [c.get("clause") or c.get("name") for c in meta.get("cast") or [] if isinstance(c, dict)]
        with tempfile.TemporaryDirectory(prefix="hfvg-count-") as tmp:
            if is_video(media):
                window = ctx.get("window") or (0.0, None)
                duration = await probe_duration(media)
                times = spread_times(duration, self.frames_per_clip, float(window[0] or 0.0),
                                     float(window[1]) if window[1] is not None else None)
                frames = await extract_frames(media, times, tmp)
            else:
                times, frames = None, [media]
            images = [prepare_image(f, MEDIA_PX) for f in frames] + [prepare_image(p, REF_PX) for _n, p in refs]
            key_parts = {"media": file_sha256(media), "times": times, "cast": described,
                         "duck_host": bool(meta.get("duck")), "refs": [file_sha256(p) for _n, p in refs]}
        lines = [f"Shot {ctx.get('shot_id') or ''}: {len(frames)} frame(s) to count."]
        lines.append("Main characters (cast):" if described else
                     "No main characters are described: count as cast every distinct character that is a "
                     "subject of the shot, the duck host excepted.")
        lines += [f"- {d}" for d in described]
        if meta.get("duck"):
            lines.append("The shot's host is a duck: count it under duck and animal, not under cast.")
        if refs:
            lines.append("Reference images after the frames, in order: " + ", ".join(n for n, _p in refs) + ".")
        return await self.service.ask(JudgeCall(
            kind="headcount", episode_id=episode_id, subject=f"{ctx.get('shot_id') or ''} {media.name}".strip(),
            system=SYSTEM, text="\n".join(lines), images=images, key_parts=key_parts, validate=_validate))
