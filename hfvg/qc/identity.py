"""Identity gate for any locked character (QC judge d, docs/QC-JUDGES.md).

``IdentityGate`` judges whether a character in a still or clip is the locked character of the cast
library. Its criteria come from the character's lock (``criteria_from_lock``): the locked
description, the sheet images (master, head sheet, turnaround) and the lock hash, after
``cast_store.verify_lock``. Its rules are an ``IdentityProfile``: the signs the judge scores
(0 on model, 1 minor, 2 drift), apparel that must match when present, per-shot features and a size
floor. Every locked character uses ``GENERIC_PROFILE``; the duck host of hfvg.qc.duck_identity is
one configured profile of the same gate.

Per frame: find the character (``CharacterDetector``), build a composite of real crops (the locked
sheet next to the character cut out of the frame), judge it (``IdentityJudge``), then the rules:
confidence below 0.6 -> ESCALATE; a sign at 2 -> FAIL; a hint sign (the duck's neck ring) at 1 with
confidence >= 0.5 -> FAIL; apparel present but wrong -> FAIL; a required feature missing -> FAIL;
minor scores adding up to 3 -> FAIL; else the judge's own FAIL / ESCALATE, else PASS. A clip is
checked on its first, middle and last frame (real duration, ffprobe), worst frame wins.

A gate without a detector and judge, or a live gate with only the always-pass fakes, escalates.
``identity_review`` runs the gate for every locked character a shot declares, inside the still and
clip review, when the QC judges are registered.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import math
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hfvg.qc import judges
from hfvg.qc.judges import (
    MEDIA_PX,
    NEEDS_REVIEW,
    JudgeCall,
    JudgeInputError,
    JudgeService,
    confidence_of,
    file_sha256,
    prepare_image,
)

PASS = "PASS"
FAIL = "FAIL"
ESCALATE = "ESCALATE"
LOW_CONFIDENCE = 0.6
HINT_CONFIDENCE = 0.5
MINOR_SCORE_LIMIT = 3
PANEL_PX = 384
GAP_PX = 12
COMPOSITE_PX = 1536
# Locked sheet images in the composite, left to right (cast_store roles)
SHEET_PANEL_ROLES = (
    ("master", "the locked sheet, full body"),
    ("turnaround_three_quarter", "the locked sheet, three-quarter view"),
    ("head_sheet", "the locked sheet, head"),
)
REF_PANELS = 2  # a sheet without cast roles (the duck host's reference folder): its first two images


@dataclass(frozen=True)
class IdentityProfile:
    """The identity rules of one kind of character."""
    name: str
    label: str = "Character"
    signs: dict[str, str] = field(default_factory=dict)  # sign -> what the judge compares
    apparel: dict[str, str] = field(default_factory=dict)  # item -> what correct is (present but wrong: FAIL)
    hint_signs: tuple[str, ...] = ()  # FAIL already at score >= 1 with confidence >= 0.5
    state_key: str | None = None  # shot_context key of the character's state
    state_features: dict[str, tuple[str, str]] = field(default_factory=dict)  # state -> (feature, correct is)
    min_height_fraction: float | None = None  # a required character smaller than this in frame: FAIL
    presence: tuple[str, Any] | None = None  # (shot_context key, value) making it required; None: always
    presence_note: str = "declared in this shot"


GENERIC_PROFILE = IdentityProfile(
    name="locked_character",
    signs={
        "face": "face shape and features (eyes, nose, muzzle or beak, mouth)",
        "hair_or_fur": "hair, fur or feathers: colour, length and style",
        "colours": "the colours and markings of the body",
        "outfit": "the clothes and accessories of the locked outfit",
        "proportions": "body shape, build and proportions (head to body, limbs)",
    },
)


@dataclass(frozen=True)
class IdentityCriteria:
    """What one character is judged against (from its lock in the cast library)."""
    slug: str
    name: str
    version: int | None = None
    description: str = ""
    lock_hash: str | None = None
    sheet: tuple[tuple[str, str], ...] = ()  # (role, image path) in cast_store.IDENTITY_ORDER
    episode_id: str | None = None
    profile: IdentityProfile = GENERIC_PROFILE
    roster: tuple[tuple[str, str, str], ...] = ()  # (slug, name, description) of each character looked for

    def key(self) -> dict[str, Any]:
        return {"slug": self.slug, "version": self.version, "lock_hash": self.lock_hash,
                "description": self.description, "profile": self.profile.name}


class IdentityLockError(Exception):
    """The character's lock cannot be used (not locked, changed since it was locked, files missing)."""


def criteria_from_lock(character: dict, version: dict, episode_id: str | None = None) -> IdentityCriteria:
    """The criteria of one locked character version (hfvg.cast_store dicts). Raises IdentityLockError."""
    from hfvg import cast_store

    label = f"{character.get('name') or character.get('slug')} v{version.get('version')}"
    if not version.get("locked"):
        raise IdentityLockError(f"{label} is not locked")
    problems = cast_store.verify_lock(character["character_id"], int(version["version"]))
    if problems:
        raise IdentityLockError("; ".join(problems))
    images, _notes = cast_store.identity_images(character, version, live=True)
    sheet = tuple((img["role"], str(cast_store.image_file(img["sha256"], img["ext"]))) for img in images)
    if not sheet:
        raise IdentityLockError(f"{label}: no locked sheet image on disk")
    return IdentityCriteria(
        slug=character["slug"], name=character["name"], version=int(version["version"]),
        description=version.get("locked_description") or version.get("clause") or "",
        lock_hash=version.get("lock_hash"), sheet=sheet, episode_id=episode_id)


def locked_entries(shot_meta: dict | None) -> list[dict]:
    """The shot's locked cast entries (shot_meta["cast"]), with or without a prompt clause."""
    cast = (shot_meta or {}).get("cast") if isinstance(shot_meta, dict) else None
    return [c for c in cast if isinstance(c, dict) and c.get("locked")] if isinstance(cast, list) else []


def locked_cast_criteria(episode_id: str | None, shot_meta: dict | None) -> tuple[list[IdentityCriteria], list[dict]]:
    """(criteria of every locked character the shot declares, [{character, name, reason}] that cannot be
    judged). Blocking (sqlite, file hashes): call it in a thread from async code."""
    from hfvg import cast_store

    entries = locked_entries(shot_meta)
    if not entries:
        return [], []
    criteria: list[IdentityCriteria] = []
    problems: list[dict] = []
    try:
        library = cast_store.library_for_episode(episode_id) if episode_id else None
    except Exception as e:  # noqa: BLE001 - no library, nothing to judge against
        library, error = None, f"cast library unavailable ({type(e).__name__})"
    else:
        error = None if library else "no episode to find the cast library"
    for entry in entries:
        slug = entry.get("character")
        try:
            if library is None:
                raise IdentityLockError(error)
            character = cast_store.find_character(library, slug)
            if character is None:
                raise IdentityLockError(f"{entry.get('name') or slug} is not in the episode's cast library")
            version = next((v for v in character["versions"] if v["version"] == entry.get("version")), None)
            if version is None:
                raise IdentityLockError(f"{character['name']} has no version {entry.get('version')}")
            criteria.append(criteria_from_lock(character, version, episode_id))
        except IdentityLockError as e:
            problems.append({"character": slug, "name": entry.get("name"), "reason": str(e)})
        except Exception as e:  # noqa: BLE001 - a cast DB error is never a pass
            problems.append({"character": slug, "name": entry.get("name"),
                             "reason": f"cast library error ({type(e).__name__})"})
    roster = tuple((c.slug, c.name, c.description) for c in criteria)
    return [replace(c, roster=roster) for c in criteria], problems


class CharacterDetector(ABC):
    """Finds one character in an image."""

    @abstractmethod
    async def detect(self, image_path: str | Path, criteria: IdentityCriteria) -> dict[str, Any]:
        """{"found": bool, "bbox": {"x", "y", "w", "h"} in pixels, "head_bbox": same or None,
        "confidence": 0-1, "height_fraction": box height / image height}."""


class IdentityJudge(ABC):
    """Scores a composite (locked sheet crops next to frame crops) against the character's criteria."""

    @abstractmethod
    async def judge_identity(self, composite_path: str | Path, shot_context: dict[str, Any],
                             criteria: IdentityCriteria) -> dict[str, Any]:
        """{"signs": {sign: {"score": 0|1|2, "confidence", "evidence"}}, "apparel": {item: {"present",
        "correct"}}, <feature>: {"present", "correct"}, "total_minor_score", "verdict", "reason",
        "confidence"}."""


def _takes(fn: Any, n: int) -> bool:
    """True when ``fn`` takes ``n`` positional arguments (detectors and judges written for the duck
    gate take one argument fewer: no criteria)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    return len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= n


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _checked_box(box: Any, what: str) -> dict[str, float]:
    if not isinstance(box, dict) or any(_num(box.get(k)) is None for k in ("x", "y", "w", "h")):
        raise ValueError(f"{what} must have numeric x, y, w, h")
    out = {k: float(box[k]) for k in ("x", "y", "w", "h")}
    if out["w"] <= 0 or out["h"] <= 0 or out["x"] < 0 or out["y"] < 0:
        raise ValueError(f"{what} is empty or outside the image")
    return out


def _checked_detection(detection: Any) -> dict[str, Any]:
    if not isinstance(detection, dict) or not isinstance(detection.get("found"), bool):
        raise ValueError("the detector gave no found true/false")
    if detection["found"]:
        detection = {**detection, "bbox": _checked_box(detection.get("bbox"), "bbox")}
        if detection.get("head_bbox") is not None:
            detection["head_bbox"] = _checked_box(detection["head_bbox"], "head_bbox")
    if detection.get("confidence") is not None and _num(detection["confidence"]) is None:
        raise ValueError("the detector's confidence is not a number")
    return detection


def _open_rgb(path: str | Path):
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError) as e:
        raise JudgeInputError(f"cannot read {Path(path).name} as an image ({type(e).__name__})") from None


def _trim(im):
    """The sheet image without its plain background (the colour of the top-left corner)."""
    from PIL import Image, ImageChops

    background = Image.new("RGB", im.size, im.getpixel((0, 0)))
    mask = ImageChops.difference(im, background).convert("L").point(lambda v: 255 if v > 24 else 0)
    box = mask.getbbox()
    return im.crop(box) if box else im


def _crop(im, box: dict[str, float], margin: float):
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    left, top = max(0, int(x - w * margin)), max(0, int(y - h * margin))
    right, bottom = min(im.width, int(math.ceil(x + w * (1 + margin)))), min(im.height, int(math.ceil(y + h * (1 + margin))))
    if right - left < 4 or bottom - top < 4:
        raise JudgeInputError("the character's box in the frame is empty")
    return im.crop((left, top, right, bottom))


def _fit(im, height: int = PANEL_PX):
    width = max(1, round(im.width * height / im.height))
    if width > 2 * height:
        width, height = 2 * height, max(1, round(im.height * 2 * height / im.width))
    return im.resize((width, height))


def sheet_panels(sheet: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """(label, image path) of the sheet panels of the composite, left to right."""
    by_role = {role: path for role, path in sheet}
    picked = [(label, by_role[role]) for role, label in SHEET_PANEL_ROLES if role in by_role]
    return picked or [(f"reference sheet {i + 1}", path) for i, (_role, path) in enumerate(sheet[:REF_PANELS])]


def build_composite(frame_path: str | Path, bbox: dict, head_bbox: dict | None,
                    sheet_paths: list[str]) -> bytes:
    """JPEG strip, left to right: the sheet images (background trimmed), then the character cut out
    of the frame (body, head). Real crops, no placeholders."""
    from PIL import Image

    panels = [_fit(_trim(_open_rgb(p))) for p in sheet_paths]
    frame = _open_rgb(frame_path)
    head = head_bbox or {**bbox, "h": bbox["h"] * 0.35}
    panels += [_fit(_crop(frame, bbox, 0.08)), _fit(_crop(frame, head, 0.15))]
    strip = Image.new("RGB", (sum(p.width for p in panels) + GAP_PX * (len(panels) - 1),
                              max(p.height for p in panels)), "white")
    x = 0
    for p in panels:
        strip.paste(p, (x, 0))
        x += p.width + GAP_PX
    strip.thumbnail((COMPOSITE_PX, COMPOSITE_PX))
    buf = io.BytesIO()
    strip.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def clip_frame_times(duration: float) -> list[float]:
    """First, middle and last frame of a clip (inside it, whatever its real duration)."""
    first = min(0.1, duration / 4)
    last = max(first, duration - 0.15)
    return [round(first, 3), round(duration / 2, 3), round(last, 3)]


def _status(verdicts: set[str]) -> str:
    if FAIL in verdicts:
        return FAIL
    return NEEDS_REVIEW if verdicts - {PASS} else PASS


class IdentityGate:
    """The identity gate of one character (criteria) with a detector and a judge."""

    def __init__(self, detector: Any = None, judge: Any = None, criteria: IdentityCriteria | None = None):
        self.detector = detector
        self.judge = judge
        self.criteria = criteria or IdentityCriteria(slug="character", name="Character")
        self.profile = self.criteria.profile

    def _uses_fakes(self) -> bool:
        return bool(getattr(self.detector, "dry_run_only", False) or getattr(self.judge, "dry_run_only", False))

    def _unjudged(self) -> str | None:
        """Why this gate cannot judge, else None."""
        if self.detector is None or self.judge is None:
            return "no identity judge configured"
        if self._uses_fakes():
            if not judges.dry_run():
                return "a live identity check needs a real judge (the always-pass fakes are for dry run only)"
        elif not self.criteria.sheet:
            return f"{self.criteria.name or self.profile.label}: no sheet image to compare against"
        return None

    def _required(self, shot_context: dict[str, Any]) -> bool:
        if self.profile.presence is None:
            return True
        key, value = self.profile.presence
        return shot_context.get(key) == value

    async def _detect(self, image_path: str | Path) -> dict[str, Any]:
        if _takes(self.detector.detect, 2):
            return await self.detector.detect(image_path, self.criteria)
        return await self.detector.detect(image_path)

    async def _judge(self, composite_path: Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        if _takes(self.judge.judge_identity, 3):
            return await self.judge.judge_identity(composite_path, shot_context, self.criteria)
        return await self.judge.judge_identity(composite_path, shot_context)

    async def extract_frames(self, video_path: str | Path, frame_times: list[float],
                             out_dir: str | Path | None = None) -> list[Path]:
        """Frames at the given times (ffmpeg through hfvg.subproc; next to the video by default)."""
        video_path = Path(video_path)
        return await judges.extract_frames(video_path, frame_times,
                                           out_dir or video_path.parent / f"{video_path.stem}_frames")

    async def create_composite(self, frame_path: str | Path, bbox: dict[str, float], shot_context: dict[str, Any],
                               output_path: str | Path, head_bbox: dict[str, float] | None = None) -> Path:
        """The composite the judge scores (build_composite); with the dry-run fakes, which never look
        at it, an unreadable frame is copied instead."""
        sheet = [p for _label, p in sheet_panels(self.criteria.sheet)]
        try:
            data = await asyncio.to_thread(build_composite, frame_path, bbox, head_bbox, sheet)
        except JudgeInputError:
            if not self._uses_fakes():
                raise
            shutil.copy(frame_path, output_path)
            return Path(output_path)
        Path(output_path).write_bytes(data)
        return Path(output_path)

    async def check_frame(self, frame_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        """Detect, size floor, composite, judge, rules. Every failure to judge is ESCALATE."""
        ctx = dict(shot_context or {})
        out: dict[str, Any] = {"frame": str(frame_path), "character": self.criteria.slug, "detection": None}
        why = self._unjudged()
        if why:
            return {**out, "verdict": ESCALATE, "reason": why}
        label = self.criteria.name or self.profile.label
        try:
            detection = _checked_detection(await self._detect(frame_path))
        except Exception as e:  # noqa: BLE001 - a detector failure is never a pass
            return {**out, "verdict": ESCALATE, "reason": f"{label}: detector failed: {e}"}
        out["detection"] = detection
        if not detection["found"]:
            if self._required(ctx):
                return {**out, "verdict": ESCALATE,
                        "reason": f"{label} expected ({self.profile.presence_note}) but not found"}
            return {**out, "verdict": PASS, "reason": f"{label} absent (expected)"}
        found_conf = _num(detection.get("confidence"))
        if found_conf is not None and found_conf < LOW_CONFIDENCE:
            return {**out, "verdict": ESCALATE,
                    "reason": f"{label}: detector unsure (confidence {found_conf:.2f} < {LOW_CONFIDENCE})"}
        height = _num(detection.get("height_fraction")) or 0.0
        floor = self.profile.min_height_fraction
        if floor is not None and height < floor and self._required(ctx):
            return {**out, "verdict": FAIL, "reason": f"{label} too small: height fraction {height:.3f} < {floor}"}
        with tempfile.TemporaryDirectory(prefix="hfvg-identity-") as tmp:
            composite = Path(tmp) / f"{Path(frame_path).stem}_composite.jpg"
            try:
                await self.create_composite(frame_path, detection["bbox"], ctx, composite,
                                            head_bbox=detection.get("head_bbox"))
                judge_result = await self._judge(composite, ctx)
            except Exception as e:  # noqa: BLE001 - no verdict is never a pass
                return {**out, "verdict": ESCALATE, "reason": f"{label}: identity judge failed: {e}"}
        if not isinstance(judge_result, dict):
            return {**out, "verdict": ESCALATE, "reason": f"{label}: the identity judge gave no result"}
        verdict, reason = self._apply_auto_fail_logic(judge_result, ctx)
        return {**out, "judge": judge_result, "verdict": verdict, "reason": reason}

    def _apply_auto_fail_logic(self, judge_result: dict[str, Any], shot_context: dict[str, Any]) -> tuple[str, str]:
        """(verdict, reason) from the judge's scores and the profile's rules (module docstring)."""
        profile = self.profile
        signs = judge_result.get("signs") or {}
        apparel = judge_result.get("apparel") or {}
        confidence = _num(judge_result.get("confidence", 0))
        if confidence is None or confidence < LOW_CONFIDENCE:
            shown = f"{confidence:.2f}" if confidence is not None else repr(judge_result.get("confidence"))
            return (ESCALATE, f"Low confidence {shown} < {LOW_CONFIDENCE}")
        for sign_name, sign_data in signs.items():
            if (sign_data or {}).get("score") == 2:
                return (FAIL, f"Drift detected: {sign_name} (score 2) - {sign_data.get('evidence', '')}")
        for hint in profile.hint_signs:
            data = signs.get(hint) or {}
            if (_num(data.get("score")) or 0) >= 1 and (_num(data.get("confidence")) or 0) >= HINT_CONFIDENCE:
                return (FAIL, f"{hint.replace('_', ' ').capitalize()} detected (confidence ≥ {HINT_CONFIDENCE})")
        for item in profile.apparel:
            data = apparel.get(item) or {}
            if data.get("present") and not data.get("correct"):
                return (FAIL, f"{item.capitalize()} mismatch (present but incorrect)")
        state = shot_context.get(profile.state_key) if profile.state_key else None
        if state in profile.state_features:
            feature = profile.state_features[state][0]
            data = judge_result.get(feature) or {}
            if not data.get("present") or not data.get("correct"):
                return (FAIL, f"{state} shot: {feature} missing or incorrect")
        total_minor = _num(judge_result.get("total_minor_score", 0)) or 0
        if total_minor >= MINOR_SCORE_LIMIT:
            return (FAIL, f"Total minor score {judge_result.get('total_minor_score')} ≥ {MINOR_SCORE_LIMIT}")
        if judge_result.get("verdict") in (FAIL, ESCALATE):
            return (judge_result["verdict"], judge_result.get("reason", "Judge decision"))
        return (PASS, "Identity check passed")

    async def check_still(self, image_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        return await self.check_frame(image_path, shot_context)

    async def check_clip(self, video_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        """First, middle and last frame at the clip's real duration; FAIL if any frame fails."""
        out: dict[str, Any] = {"video": str(video_path), "frames": []}
        why = self._unjudged()
        if why:
            return {**out, "verdict": ESCALATE, "reason": why}
        try:
            frame_times = clip_frame_times(await judges.probe_duration(video_path))
        except judges.JudgeUnavailable as e:
            return {**out, "verdict": ESCALATE, "reason": str(e)}
        frame_results = []
        with tempfile.TemporaryDirectory(prefix="hfvg-identity-frames-") as tmp:
            try:
                frame_paths = await self.extract_frames(video_path, frame_times, out_dir=tmp)
            except judges.JudgeUnavailable as e:
                return {**out, "verdict": ESCALATE, "reason": str(e)}
            for i, frame_path in enumerate(frame_paths):
                result = await self.check_frame(frame_path, shot_context)
                result.update(frame_index=i, frame_time=frame_times[i], frame=f"{frame_times[i]:.2f}s")
                frame_results.append(result)
        verdict, reason = PASS, "All frames passed"
        for result in frame_results:
            if result["verdict"] == FAIL:
                verdict, reason = FAIL, f"Frame {result['frame_index']} failed: {result['reason']}"
                break
            if result["verdict"] == ESCALATE:
                verdict, reason = ESCALATE, f"Frame {result['frame_index']} escalated: {result['reason']}"
        return {**out, "frames": frame_results, "verdict": verdict, "reason": reason}


# ------------------------------------------------------------------------------------------ vision judges

DETECT_SYSTEM = """You locate characters in one frame of an animated episode. For every character id \
listed, say whether that character is visible and, when it is, give its bounding box and the box of its \
head as fractions of the image width and height (x, y is the top-left corner).
Answer with a JSON object only: {"characters": {"<id>": {"found": true | false, "confidence": number 0-1, \
"box": {"x": n, "y": n, "w": n, "h": n} | null, "head": {"x": n, "y": n, "w": n, "h": n} | null}}}.
confidence is how sure you are that the character is (or is not) in the frame. Answer for every id."""


def _detect_validator(ids: list[str]):
    def validate(answer: dict[str, Any]) -> dict[str, Any]:
        chars = answer.get("characters")
        if not isinstance(chars, dict):
            raise ValueError("no characters object")
        out = {}
        for slug in ids:
            entry = chars.get(slug)
            if not isinstance(entry, dict) or not isinstance(entry.get("found"), bool):
                raise ValueError(f"no found true/false for {slug}")
            row = {"found": entry["found"], "confidence": confidence_of(entry.get("confidence")), "box": None,
                   "head": None}
            if entry["found"]:
                for key in ("box", "head"):
                    box = entry.get(key)
                    if box is None and key == "head":
                        continue
                    box = _checked_box(box, f"{slug} {key}")
                    if box["x"] + box["w"] > 1.001 or box["y"] + box["h"] > 1.001:
                        raise ValueError(f"{slug} {key} is outside the image")
                    row[key] = box
            out[slug] = row
        return {"characters": out}
    return validate


def _pixels(box: dict[str, float], width: int, height: int) -> dict[str, float]:
    return {"x": round(box["x"] * width, 1), "y": round(box["y"] * height, 1),
            "w": round(box["w"] * width, 1), "h": round(box["h"] * height, 1)}


class VisionCharacterDetector(CharacterDetector):
    """One paid call per frame for all the shot's locked characters (the answer is cached, so the
    second character's lookup in the same frame costs nothing)."""

    def __init__(self, service: JudgeService):
        self.service = service

    async def detect(self, image_path, criteria):
        roster = criteria.roster or ((criteria.slug, criteria.name, criteria.description),)
        ids = [slug for slug, _name, _desc in roster]
        frame = _open_rgb(image_path)
        listing = "\n".join(f"- {slug}: {name}. {desc}".strip() for slug, name, desc in roster)
        answer = await self.service.ask(JudgeCall(
            kind="identity_detect", episode_id=criteria.episode_id, subject=Path(image_path).name,
            system=DETECT_SYSTEM, text=f"Characters to find:\n{listing}",
            images=[prepare_image(image_path, MEDIA_PX)],
            key_parts={"image": file_sha256(image_path), "roster": [list(r) for r in roster]},
            validate=_detect_validator(ids)))
        entry = answer["characters"][criteria.slug]
        if not entry["found"]:
            return {"found": False, "bbox": None, "head_bbox": None, "confidence": entry["confidence"],
                    "height_fraction": 0.0}
        return {"found": True, "bbox": _pixels(entry["box"], frame.width, frame.height),
                "head_bbox": _pixels(entry["head"], frame.width, frame.height) if entry["head"] else None,
                "confidence": entry["confidence"], "height_fraction": round(entry["box"]["h"], 4)}


def _panels(sheet_labels: list[str]) -> str:
    names = [*sheet_labels, "the character in the frame, body", "the character in the frame, head"]
    return "; ".join(f"panel {i + 1}: {n}" for i, n in enumerate(names))


def judge_system(profile: IdentityProfile, sheet_labels: list[str], state: Any = None) -> str:
    lines = [
        "You check that a character in a frame of an animated episode is the locked character of its "
        f"character sheet. The image is a strip of panels, left to right ({_panels(sheet_labels)}). Compare "
        "the frame panels with the sheet panels and the locked description.",
        "Score each sign 0 (on model), 1 (minor difference) or 2 (clear drift, a different look):",
        *[f"- {sign}: {what}" for sign, what in profile.signs.items()],
    ]
    if profile.apparel:
        lines.append("Apparel (present: is it worn; correct: does it match the sheet):")
        lines += [f"- {item}: {what}" for item, what in profile.apparel.items()]
    feature = profile.state_features.get(state) if profile.state_key else None
    if feature:
        lines.append(f"This is a {state} shot: report the {feature[0]} ({feature[1]}) as present and correct.")
    lines.append(
        'Answer with a JSON object only: {"signs": {"<sign>": {"score": 0 | 1 | 2, "confidence": number 0-1, '
        '"evidence": "a few words"}}, "apparel": {"<item>": {"present": bool, "correct": bool}}, '
        '"features": {"<feature>": {"present": bool, "correct": bool}}, "verdict": "PASS" | "FAIL" | '
        '"ESCALATE", "reason": "a few words", "confidence": number 0-1}. Use ESCALATE when the frame does '
        "not show enough of the character to judge.")
    return "\n".join(lines)


def _judge_validator(profile: IdentityProfile, feature: str | None):
    def flag_pair(value: Any, what: str) -> dict[str, bool]:
        if not isinstance(value, dict) or not isinstance(value.get("present"), bool) \
                or not isinstance(value.get("correct"), bool):
            raise ValueError(f"{what} needs present and correct true/false")
        return {"present": value["present"], "correct": value["correct"]}

    def validate(answer: dict[str, Any]) -> dict[str, Any]:
        conf = confidence_of(answer.get("confidence"))
        raw_signs = answer.get("signs")
        if not isinstance(raw_signs, dict):
            raise ValueError("no signs object")
        signs = {}
        for sign in profile.signs:
            data = raw_signs.get(sign)
            score = data.get("score") if isinstance(data, dict) else None
            if isinstance(score, bool) or score not in (0, 1, 2):
                raise ValueError(f"sign {sign} needs a score 0, 1 or 2")
            sign_conf = data.get("confidence")
            signs[sign] = {"score": score, "confidence": conf if sign_conf is None else confidence_of(sign_conf),
                           "evidence": str(data.get("evidence") or "")[:200]}
        apparel = {item: flag_pair((answer.get("apparel") or {}).get(item), f"apparel {item}")
                   for item in profile.apparel}
        verdict = answer.get("verdict")
        if verdict is not None and verdict not in (PASS, FAIL, ESCALATE):
            raise ValueError(f"verdict must be PASS, FAIL or ESCALATE, got {verdict!r}")
        out = {"signs": signs, "apparel": apparel,
               "total_minor_score": sum(1 for s in signs.values() if s["score"] == 1),
               "verdict": verdict, "reason": str(answer.get("reason") or "")[:300], "confidence": conf}
        if feature:
            out[feature] = flag_pair((answer.get("features") or {}).get(feature), f"feature {feature}")
        return out
    return validate


class VisionIdentityJudge(IdentityJudge):
    """One paid call per composite (cached by the composite's content hash and the criteria)."""

    def __init__(self, service: JudgeService):
        self.service = service

    async def judge_identity(self, composite_path, shot_context, criteria):
        profile = criteria.profile
        state = (shot_context or {}).get(profile.state_key) if profile.state_key else None
        feature = profile.state_features.get(state, (None,))[0] if profile.state_key else None
        labels = [label for label, _path in sheet_panels(criteria.sheet)]
        text = (f"Character: {criteria.name}" + (f" (version {criteria.version})" if criteria.version else "")
                + f". Locked description: {criteria.description or 'none'}")
        return await self.service.ask(JudgeCall(
            kind="identity", episode_id=criteria.episode_id,
            subject=f"{(shot_context or {}).get('shot_id') or ''} {criteria.slug}".strip(),
            system=judge_system(profile, labels, state), text=text,
            images=[prepare_image(composite_path, COMPOSITE_PX)],
            key_parts={"composite": file_sha256(composite_path), "criteria": criteria.key(), "state": state,
                       "panels": labels},
            validate=_judge_validator(profile, feature)))


# ------------------------------------------------------------------------------------------ registry + review

_COMPONENTS: tuple[CharacterDetector, IdentityJudge] | None = None


def set_identity_judge(detector: CharacterDetector | None, judge: IdentityJudge | None = None) -> None:
    global _COMPONENTS
    _COMPONENTS = (detector, judge) if detector is not None and judge is not None else None


def get_identity_judge() -> tuple[CharacterDetector, IdentityJudge] | None:
    return _COMPONENTS


async def identity_review(media_path: str | Path, *, kind: str, episode_id: str | None, shot_id: str | None,
                          shot_meta: dict | None) -> dict[str, Any] | None:
    """The identity of every locked character the shot declares in one still or clip, or None when
    no identity judge is registered or the shot has no locked character.
    {"status": PASS|FAIL|NEEDS_REVIEW, "characters": [{character, name, version, verdict, reason}], "reason"}."""
    components = get_identity_judge()
    if components is None or not locked_entries(shot_meta):
        return None
    criteria, problems = await asyncio.to_thread(locked_cast_criteria, episode_id, shot_meta)
    rows = [{"character": p["character"], "name": p["name"], "version": None, "verdict": ESCALATE,
             "reason": p["reason"]} for p in problems]
    detector, judge = components
    ctx = {"shot_id": shot_id, "episode_id": episode_id}
    for crit in criteria:
        gate = IdentityGate(detector, judge, crit)
        result = await (gate.check_clip(media_path, ctx) if kind == "clip" else gate.check_still(media_path, ctx))
        rows.append({"character": crit.slug, "name": crit.name, "version": crit.version,
                     "verdict": result["verdict"], "reason": result["reason"]})
    status = _status({r["verdict"] for r in rows})
    reason = "; ".join(_named(r["name"] or r["character"], r["reason"]) for r in rows if r["verdict"] != PASS) \
        or "every locked character matches its sheet"
    return {"status": status, "characters": rows, "reason": reason}


def _named(name: str, reason: str) -> str:
    return reason if reason.startswith(name) else f"{name}: {reason}"
