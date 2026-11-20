"""Still reviewer (QC judge a, docs/QC-JUDGES.md): a live still against its still prompt and references.

``review_still`` (hfvg/activities/qc.py) calls the registered reviewer after its fail-closed episode
checks; with none registered (HFVG_QC_JUDGES_ENABLED off) it escalates every live still as before.
The workflow passes the rubric (behind ``workflow.patched("qc-still-rubric")``):
``{"prompt": still prompt in force, "refs": planned reference paths, "shot_meta": ..., "still_path": local still}``.

Verdicts (``review_still``'s dict): PASS -> ``passed`` (recorded ``still_complete``; the shot still
waits for G2); FAIL -> ``escalate: False`` with the judge's reasons (the shot ends ``failed`` and is
re-rolled only through ``retry_shot``); anything the judge could not decide -> ``escalate: True``
(NEEDS_REVIEW). The identity gate (hfvg.qc.identity) then checks every locked character of a still
the reviewer passed; worst wins.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from hfvg.cast_prompt import inject_cast_prompt
from hfvg.qc.judges import (
    FAIL, MEDIA_PX, NEEDS_REVIEW, PASS, REF_PX, JudgeCall, JudgeInputError, JudgeService, JudgeUnavailable,
    confidence_of, file_sha256, prepare_image, review_deadline_s, text_list,
)

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
MAX_REFS = 6  # the plan's first six references (refs.json refs, then the cast sheets)

SYSTEM = """You are the quality-control reviewer of a short animated episode. You judge ONE generated \
still image (the first image) against the prompt it was generated from and the shot's reference images \
(the images after it: characters, props and the place, in the plan's order).
Answer with a JSON object only: {"verdict": "pass" | "fail" | "unsure", "confidence": number 0-1, \
"issues": [short reasons]}.
- "fail": the still clearly contradicts the prompt (a main subject missing or extra, the wrong action, \
setting or framing), a character, prop or place clearly differs from its reference image or description, \
or it has a visible defect (malformed, missing or extra limbs, fused bodies, a garbled face, unwanted \
text, letters or watermarks). Name every problem in issues.
- "pass": none of those is visible. issues is empty.
- "unsure": you cannot tell (too small, hidden, ambiguous).
confidence is how sure you are of the verdict."""


class StillReviewer(ABC):
    """Judges one still against its rubric."""

    @abstractmethod
    async def review(self, still_path: str | Path, rubric: dict[str, Any], episode_id: str | None,
                     shot_id: str | None) -> dict[str, Any]:
        """{"status": PASS|FAIL|NEEDS_REVIEW, "issues": [...], "reason": str, "confidence": float|None}."""


def _needs_review(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": NEEDS_REVIEW, "issues": [], "reason": reason, "confidence": None, **extra}


def _validate(answer: dict[str, Any]) -> dict[str, Any]:
    verdict = answer.get("verdict")
    if verdict not in ("pass", "fail", "unsure"):
        raise ValueError(f"verdict must be pass, fail or unsure, got {verdict!r}")
    issues = text_list(answer.get("issues"))
    if verdict == "fail" and not issues:
        raise ValueError("a fail verdict names no issue")
    return {"verdict": verdict, "confidence": confidence_of(answer.get("confidence")), "issues": issues}


def verdict_from(answer: dict[str, Any], threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> dict[str, Any]:
    """The reviewer's status for a validated answer. Below the threshold nothing passes or fails."""
    conf = answer["confidence"]
    if conf < threshold:
        return _needs_review(f"the still reviewer is unsure (confidence {conf:.2f} < {threshold})",
                             confidence=conf, issues=answer["issues"])
    if answer["verdict"] == "unsure":
        return _needs_review("the still reviewer could not decide", confidence=conf, issues=answer["issues"])
    if answer["verdict"] == "fail":
        return {"status": FAIL, "issues": answer["issues"], "reason": "; ".join(answer["issues"]),
                "confidence": conf}
    return {"status": PASS, "issues": [], "reason": "matches the prompt and references", "confidence": conf}


class VisionStillReviewer(StillReviewer):
    """One paid vision call per still (cached by still and reference content hashes plus the prompt)."""

    def __init__(self, service: JudgeService, confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD):
        self.service = service
        self.confidence_threshold = confidence_threshold

    async def review(self, still_path, rubric, episode_id, shot_id):
        rubric = rubric or {}
        prompt = str(rubric.get("prompt") or "").strip()
        if not prompt:
            return _needs_review("the review got no still prompt (rubric without prompt)")
        refs = [str(r) for r in (rubric.get("refs") or []) if r][:MAX_REFS]
        try:
            ref_data = [await _ref_bytes(r) for r in refs]
            images = [prepare_image(still_path, MEDIA_PX)] + [prepare_image(b, REF_PX) for b in ref_data]
            key_parts = {"still": file_sha256(still_path), "refs": [hashlib.sha256(b).hexdigest() for b in ref_data],
                         "prompt": inject_cast_prompt(prompt, rubric.get("shot_meta"))[0]}
        except JudgeInputError as e:
            return _needs_review(f"cannot review: {e}")
        text = (f"Shot {shot_id}. The still (image 1) was generated from this prompt:\n<<<\n"
                f"{key_parts['prompt']}\n>>>\n"
                + (f"Images 2-{len(refs) + 1} are the shot's reference images." if refs else
                   "The shot has no reference images: judge against the prompt."))
        try:
            answer = await self.service.ask(JudgeCall(
                kind="still_review", episode_id=episode_id, subject=f"{shot_id} {Path(still_path).name}",
                system=SYSTEM, text=text, images=images, key_parts=key_parts, validate=_validate))
        except JudgeUnavailable as e:
            return _needs_review(str(e))
        return verdict_from(answer, self.confidence_threshold)


_REVIEWER: StillReviewer | None = None


def set_still_reviewer(reviewer: StillReviewer | None) -> None:
    global _REVIEWER
    _REVIEWER = reviewer


def get_still_reviewer() -> StillReviewer | None:
    return _REVIEWER


async def _download(url: str, what: str) -> bytes:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except Exception as e:  # noqa: BLE001 - nothing to look at, no verdict
        raise JudgeInputError(f"{what} download failed ({type(e).__name__})") from None
    return response.content


async def _ref_bytes(ref: str) -> bytes:
    """A planned reference (a local path, or an http(s) URL from refs.json)."""
    if ref.startswith(("http://", "https://")):
        return await _download(ref, f"reference {Path(ref.split('?')[0]).name}")
    try:
        return Path(ref).read_bytes()
    except OSError:
        raise JudgeInputError(f"reference not readable: {Path(ref).name}") from None


async def _still_file(asset_url: str, rubric: dict[str, Any], tmp: Path) -> Path:
    local = rubric.get("still_path")
    if local and Path(local).is_file():
        return Path(local)
    data = await _download(asset_url, "still not found locally and its")
    path = tmp / ("still" + (Path(str(asset_url).split("?")[0]).suffix or ".png"))
    path.write_bytes(data)
    return path


def _result(status: str, issues: list[str], judges: dict[str, Any]) -> dict[str, Any]:
    if status == PASS:
        return {"passed": True, "issues": [], "escalate": False, "qc_judges": judges}
    return {"passed": False, "issues": issues, "escalate": status != FAIL, "qc_judges": judges}


async def judged_still_review(reviewer: StillReviewer, asset_url: str, rubric: dict[str, Any] | None,
                              episode_id: str | None, shot_id: str | None) -> dict[str, Any]:
    """``review_still``'s live verdict when the judges are registered: the rubric review, then the
    identity of every locked character of a still that passed. Worst wins; within
    HFVG_QC_REVIEW_DEADLINE_S, else NEEDS_REVIEW."""
    from hfvg.qc import identity

    rubric = dict(rubric or {})
    with tempfile.TemporaryDirectory(prefix="hfvg-still-qc-") as tmp:
        try:
            still = await _still_file(asset_url, rubric, Path(tmp))
        except JudgeInputError as e:
            judges = {"status": NEEDS_REVIEW, "still": _needs_review(f"cannot review: {e}")}
            return _result(NEEDS_REVIEW, [f"QC judge: cannot review: {e}"], judges)

        async def run() -> dict[str, Any]:
            verdict = await reviewer.review(still, rubric, episode_id, shot_id)
            judges: dict[str, Any] = {"status": verdict["status"], "still": verdict}
            if verdict["status"] == FAIL:
                return _result(FAIL, [f"QC judge: {i}" for i in verdict["issues"]], judges)
            if verdict["status"] != PASS:
                return _result(NEEDS_REVIEW, [f"QC judge needs review: {verdict['reason']}"], judges)
            check = await identity.identity_review(still, kind="still", episode_id=episode_id, shot_id=shot_id,
                                                   shot_meta=rubric.get("shot_meta"))
            if check is None:
                return _result(PASS, [], judges)
            judges.update(status=check["status"], identity=check)
            if check["status"] == FAIL:
                return _result(FAIL, [f"identity: {check['reason']}"], judges)
            if check["status"] != PASS:
                return _result(NEEDS_REVIEW, [f"identity needs review: {check['reason']}"], judges)
            return _result(PASS, [], judges)

        try:
            return await asyncio.wait_for(run(), review_deadline_s())
        except asyncio.TimeoutError:
            reason = f"the QC judges took longer than {review_deadline_s():g} s"
        except Exception as e:  # noqa: BLE001 - a broken judge is a person's call, not an activity retry
            reason = f"the QC judges failed ({type(e).__name__}: {str(e)[:120]})"
        return _result(NEEDS_REVIEW, [f"QC judge needs review: {reason}"],
                       {"status": NEEDS_REVIEW, "still": _needs_review(reason)})
