"""Headcount counter (QC judge b, docs/QC-JUDGES.md): ``CastCounter`` with a vision model.

One paid call per still or clip window answers for every figure the shot declares
(hfvg.qc.headcount.shot_roster): how many of each are in frame and whether each looks like the
declared character, plus the figures nobody declared (``unexpected`` as subjects of the shot,
``background`` only in the background). The request lists each figure by its id and name with its
locked description, sends one sheet image per declared character of the cast library, labelled with
the same id and name, and names the shot's props and place so they are not counted. A clip is
counted on frames spread across its trim window: the largest number seen in one frame. The answer
is cached by the media's content hash, the frame times, the roster, the props and the reference
images with their ids.

Errors are raised (``JudgeUnavailable``): ``check_headcount`` turns them into NEEDS_REVIEW for that
shot. A reviewer's recorded headcount still wins (``ReviewerHeadcountCounter`` asks this counter only
when no reviewer count exists for the still).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from hfvg.qc.headcount import CastCounter, Roster
from hfvg.qc.judges import (
    MEDIA_PX,
    REF_PX,
    JudgeCall,
    JudgeService,
    JudgeUnavailable,
    bool_of,
    confidence_of,
    count_of,
    extract_frames,
    file_sha256,
    is_video,
    prepare_image,
    probe_duration,
    spread_times,
)

FRAMES_PER_CLIP = 3
MAX_REFS = 4

SYSTEM = """You count the figures in frames of a video shot. \
The images before the reference images are the frames to count (one still, or frames from one clip). \
The request lists the figures the shot declares, each with an id: a character described by its look \
(a person, a creature, a robot, a puppet, an object that acts as a character, or anything else), or a \
group of the shot's main characters that have no description, counted together. Each reference image \
shows one described character; the request gives, in order, the id of the character each one shows.
Count, in the frame that shows the most of each:
- for every listed id: how many figures in the frame are that one (0 when it is not in frame; a group: \
how many figures belong to it), and matches: false when a figure counted under a described character's \
id does not look like its description or reference image, else true;
- unexpected: figures that are not listed and stand out as subjects of the shot (in the foreground, \
acting, or as prominent as a listed figure);
- background: figures that are not listed and are only part of the background (passers-by, a crowd).
The props and the setting named in the request are not figures: do not count them.
When a clip shows different numbers in different frames, give the largest number seen in one frame.
Answer with a JSON object only: {"figures": {"<id>": {"count": int, "matches": true | false}}, \
"unexpected": int, "background": int, "confidence": number 0-1, "notes": "a few words"}, with an entry \
for every listed id. confidence is how sure you are of every count."""


def _validator(roster: Roster):
    def validate(answer: dict[str, Any]) -> dict[str, Any]:
        raw = answer.get("figures")
        if not isinstance(raw, dict):
            raise ValueError("no figures object")
        figures = {}
        for fig in roster.figures:
            row = raw.get(fig.id)
            if not isinstance(row, dict):
                raise ValueError(f"no count for {fig.id}")
            figures[fig.id] = {"count": count_of(row.get("count")),
                               "matches": True if fig.group else bool_of(row.get("matches"), f"{fig.id} matches")}
        return {"figures": figures, "unexpected": count_of(answer.get("unexpected")),
                "background": count_of(answer.get("background")),
                "confidence": confidence_of(answer.get("confidence")), "notes": str(answer.get("notes") or "")[:200]}
    return validate


def _cast_refs(episode_id: str | None, shot_meta: dict | None) -> list[tuple[str, str]]:
    """(figure id, first identity image) of each declared character found in the cast library."""
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
                out.append((entry["character"], str(cast_store.image_file(images[0]["sha256"], images[0]["ext"]))))
    except Exception:  # noqa: BLE001 - references help; the descriptions still go in the prompt
        return out
    return out[:MAX_REFS]


def _label(roster: Roster, figure_id: str) -> str:
    """A figure as the request names it: its id, then its name when that says more."""
    fig = roster.figure(figure_id)
    name = fig.name if fig is not None else ""
    return figure_id if not name or name == figure_id else f"{figure_id} ({name})"


def _listing(roster: Roster) -> list[str]:
    lines = []
    for fig in roster.figures:
        if fig.group:
            lines.append(f"- {fig.id}: {fig.name}, the shot's main characters that have no description here "
                         "(every character that is a subject of the shot and none of the other listed ids)")
        else:
            label = _label(roster, fig.id)
            lines.append(f"- {label}: {fig.description}" if fig.description else f"- {label}")
    return lines


def _not_figures(shot_meta: dict | None) -> list[str]:
    from hfvg.cast_prompt import prop_entries

    return [p.get("clause") or p.get("name") or "" for p in prop_entries(shot_meta)]


class VisionCastCounter(CastCounter):
    """Counts with one cached vision call per still or clip window."""

    def __init__(self, service: JudgeService, frames_per_clip: int = FRAMES_PER_CLIP):
        self.service = service
        self.frames_per_clip = frames_per_clip

    async def count_cast(self, media_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        roster = (shot_context or {}).get("roster")
        if not isinstance(roster, Roster):
            raise JudgeUnavailable("no declared figures to count against")
        media = Path(media_path)
        meta = shot_context.get("shot_meta") or {}
        episode_id = shot_context.get("episode_id")
        refs = await asyncio.to_thread(_cast_refs, episode_id, meta)
        listed = [[f.id, f.name, f.description, f.group] for f in roster.figures]
        props = [p for p in _not_figures(meta) if p]
        with tempfile.TemporaryDirectory(prefix="hfvg-count-") as tmp:
            if is_video(media):
                window = shot_context.get("window") or (0.0, None)
                duration = await probe_duration(media)
                times = spread_times(duration, self.frames_per_clip, float(window[0] or 0.0),
                                     float(window[1]) if window[1] is not None else None)
                frames = await extract_frames(media, times, tmp)
            else:
                times, frames = None, [media]
            images = [prepare_image(f, MEDIA_PX) for f in frames] + [prepare_image(p, REF_PX) for _i, p in refs]
            key_parts = {"media": file_sha256(media), "times": times, "figures": listed,
                         "props": props, "refs": [[fid, file_sha256(p)] for fid, p in refs]}
        lines = [f"Shot {shot_context.get('shot_id') or ''}: {len(frames)} frame(s) to count."]
        lines += ["Figures:", *_listing(roster)] if roster.figures else \
            ["The shot declares no figures: every figure in frame is unexpected or background."]
        if props:
            lines.append("Not figures (props and the setting): " + " ".join(props))
        if refs:
            lines.append("Reference images after the frames, in order: "
                         + ", ".join(_label(roster, fid) for fid, _p in refs) + ".")
        answer = await self.service.ask(JudgeCall(
            kind="headcount", episode_id=episode_id, subject=f"{shot_context.get('shot_id') or ''} {media.name}".strip(),
            system=SYSTEM, text="\n".join(lines), images=images, key_parts=key_parts, validate=_validator(roster)))
        return {"figures": answer["figures"], "unexpected": answer["unexpected"], "background": answer["background"],
                "confidence": answer["confidence"],
                "method": "vision_judge"}


VisionAnimalCounter = VisionCastCounter  # the name before the general headcount
