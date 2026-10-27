"""G2 headcount QC (GAP-27): count EVERY animal in each kept still against the shot's declared cast.

ep05v2 V12 v2 showed FOUR main-cast animals for a 3-animal shot and still reached the G2 package: nothing
compared the animals in a still with the cast. This check does, for the still version each shot
will actually animate (the latest re-roll or the version kept with select_still_version):

  cast == shot_meta.count          duck == 1 if shot_meta.duck else 0
  animal (every animal in frame, any species) == cast + duck   (an extra fox / 4th cast animal)

Any mismatch is FAIL and BLOCKS G2 approval (API, 409) unless the reviewer overrides that shot with
an audited reason. Counts come from, in order: a reviewer headcount recorded for that exact still
(POST /api/episodes/{ep}/shots/{shot}/headcount, stored in <EPISODES_DIR>/<ep>/g2_headcounts.json),
then the registered AnimalCounter. With neither the shot is NEEDS_REVIEW - never a silent PASS.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from hfvg.qc.animal_count import (
    DEFAULT_CONFIDENCE_THRESHOLD, FAIL, NEEDS_REVIEW, PASS, AnimalCounter, get_animal_counter,
)

SPECIES = ("cast", "duck", "animal")
_STILL_RE = re.compile(r"^still_v(\d+)(_end)?\.(?:jpe?g|png|webp)$", re.IGNORECASE)


def expected_cast(shot_meta: dict | None) -> dict[str, int] | None:
    """{"cast", "duck", "animal"} expected in frame, or None when the shot declares no count."""
    meta = shot_meta or {}
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    duck = 1 if meta.get("duck") else 0
    return {"cast": count, "duck": duck, "animal": count + duck}


def headcounts_path(episode_id: str, episodes_dir: str | os.PathLike | None = None) -> Path:
    root = Path(episodes_dir if episodes_dir is not None else os.getenv("EPISODES_DIR", "./data/episodes"))
    return root / episode_id / "g2_headcounts.json"


def load_headcounts(episode_id: str, episodes_dir=None) -> dict:
    p = headcounts_path(episode_id, episodes_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_headcount(episode_id: str, shot_id: str, version: int, counts: dict[str, int], by: str,
                     note: str = "", episodes_dir=None) -> dict:
    """Store a reviewer's count of one still version (atomic write). Returns the stored row."""
    for k in ("cast", "duck"):
        v = counts.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{k} must be a non-negative integer, got {v!r}")
    other = counts.get("other", 0)
    if isinstance(other, bool) or not isinstance(other, int) or other < 0:
        raise ValueError(f"other must be a non-negative integer, got {other!r}")
    row = {"cast": counts["cast"], "duck": counts["duck"], "other": other,
           "animal": counts["cast"] + counts["duck"] + other, "by": by, "note": note}
    data = load_headcounts(episode_id, episodes_dir)
    data.setdefault(shot_id, {})[str(int(version))] = row
    p = headcounts_path(episode_id, episodes_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)
    return row


class ReviewerHeadcountCounter(AnimalCounter):
    """Counts a human reviewer recorded for outputs/<shot>/still_v<N>.<ext>; else defers to `fallback`."""

    def __init__(self, headcounts: dict, fallback: AnimalCounter | None = None):
        self.headcounts = headcounts or {}
        self.fallback = fallback

    def lookup(self, media_path: str | Path) -> dict | None:
        p = Path(media_path)
        m = _STILL_RE.match(p.name)
        if not m or m.group(2):
            return None
        return (self.headcounts.get(p.parent.name) or {}).get(str(int(m.group(1))))

    async def count_animals(self, media_path, expected_type: str = "duck") -> dict[str, Any]:
        row = self.lookup(media_path)
        if row is not None and expected_type in row:
            return {"found": row[expected_type] > 0, "count": int(row[expected_type]), "confidence": 1.0,
                    "type_match": True, "method": f"reviewer_headcount ({row.get('by') or 'reviewer'})"}
        if self.fallback is not None:
            return await self.fallback.count_animals(media_path, expected_type)
        return {"found": False, "count": -1, "confidence": 0.0, "type_match": False, "method": "none"}

    def has_source(self, media_path) -> bool:
        return self.lookup(media_path) is not None or self.fallback is not None


async def check_still_headcount(counter: AnimalCounter | None, image_path: str | Path | None,
                                shot_meta: dict | None,
                                confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> dict[str, Any]:
    """Count main-cast animals, ducks and every animal in one still vs the declared cast. Worst status wins."""
    expected = expected_cast(shot_meta)
    if expected is None:
        return {"status": NEEDS_REVIEW, "checks": [], "expected": None,
                "reason": "shot declares no animal count to verify against"}
    if counter is None or (isinstance(counter, ReviewerHeadcountCounter) and image_path
                           and not counter.has_source(image_path)):
        return {"status": NEEDS_REVIEW, "checks": [], "expected": expected,
                "reason": "no animal counter configured and no reviewer headcount recorded for this still"}
    if not image_path or not Path(image_path).is_file():
        return {"status": NEEDS_REVIEW, "checks": [], "expected": expected,
                "reason": f"still not found on disk: {image_path}"}
    checks = []
    for species in SPECIES:
        res = await counter.count_animals(image_path, species)
        conf = float(res.get("confidence", 0.0))
        got = res.get("count")
        if conf < confidence_threshold:
            status, why = NEEDS_REVIEW, f"{species}: low confidence {conf:.2f} < {confidence_threshold}"
        elif not res.get("type_match", False):
            status, why = NEEDS_REVIEW, f"{species}: counter could not identify the species"
        elif int(got if got is not None else -1) != expected[species]:
            status, why = FAIL, f"{species}: expected {expected[species]}, counted {got}"
        else:
            status, why = PASS, f"{species}: {expected[species]} confirmed"
        checks.append({"species": species, "expected": expected[species], "counted": got, "status": status,
                       "reason": why, "method": res.get("method"), "confidence": conf})
    statuses = {c["status"] for c in checks}
    status = FAIL if FAIL in statuses else (NEEDS_REVIEW if NEEDS_REVIEW in statuses else PASS)
    reason = "; ".join(c["reason"] for c in checks if c["status"] != PASS) or "headcount matches the cast"
    return {"status": status, "checks": checks, "expected": expected, "reason": reason}


def _still_for(out_dir: Path, version: int | None) -> Path | None:
    if version is None:
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = out_dir / f"still_v{version}{ext}"
        if p.is_file():
            return p
    return None


async def g2_headcount_report(episode_id: str, versions: dict, plans: dict[str, dict | None],
                              decisions: dict | None = None, counter: AnimalCounter | None = None,
                              data_dir=None, episodes_dir=None,
                              overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Headcount of every non-rejected shot's kept still version.

    versions: EpisodeWorkflowV2.still_versions query; plans: shot -> still_version_plan of the kept
    version; decisions: shot_decisions (rejected shots are skipped); overrides: shot -> reason.
    Returns {"status", "blocking": [shots that FAIL and are not overridden], "shots": {...}}."""
    decisions = decisions or {}
    overrides = overrides or {}
    data_root = Path(data_dir if data_dir is not None else os.getenv("DATA_DIR", "./data"))
    reviewer = ReviewerHeadcountCounter(load_headcounts(episode_id, episodes_dir),
                                        counter if counter is not None else get_animal_counter())
    shots: dict[str, Any] = {}
    for shot_id in sorted(versions or {}):
        if decisions.get(shot_id) == "rejected":
            shots[shot_id] = {"status": "SKIPPED", "reason": "rejected at G2 (never animated)"}
            continue
        v = (versions[shot_id] or {}).get("current_version")
        plan = plans.get(shot_id) or {}
        path = _still_for(data_root / "episodes" / episode_id / "outputs" / shot_id, v)
        res = await check_still_headcount(reviewer, path, plan.get("shot_meta"))
        res.update({"version": v, "still": str(path) if path else None})
        if res["status"] == FAIL and shot_id in overrides:
            res["override"] = overrides[shot_id]
        shots[shot_id] = res
    blocking = sorted(s for s, r in shots.items() if r.get("status") == FAIL and s not in overrides)
    statuses = {r.get("status") for r in shots.values()}
    status = FAIL if blocking else (NEEDS_REVIEW if NEEDS_REVIEW in statuses or FAIL in statuses else PASS)
    return {"status": status, "blocking": blocking, "shots": shots}
