"""Automated QC judge (c), the clip "fail if" checker (hfvg/qc/vision_constraints.py,
docs/QC-JUDGES.md), behind ``ClipConstraintChecker``: frames from the whole clip, every declared
``qc_fail_if`` condition answered in one cached paid call, and ``evaluate_clip_constraints``'
fail-closed rules on top. A violated condition is a FAIL (the clip fails and only ``retry_clip``
makes another); an unsure, broken or refused judge is NEEDS_REVIEW (a person looks). Clips are
synthetic ffmpeg test patterns. FakeJudgeClient only: no network, no spend."""

from __future__ import annotations

import httpx
import pytest
import respx

from hfvg.activities.qc import review_clip
from hfvg.qc import clip_constraints as cc
from hfvg.qc.judges import FakeJudgeClient, JudgeService
from hfvg.qc.vision_constraints import SYSTEM, VisionClipConstraintChecker
from tests.qc_judge_helpers import live_episode, qc_line, reset_registries, verdict_rows
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg

pytestmark = needs_ffmpeg

EP = "ep58"
RULES = ["the tray ends in the water", "the tray vanishes", "the host touches a guest"]
PROMPT = "Pip carries a tray along the dock. Real-time, no slow motion."
URL = "https://cdn.example.com/m.mp4"


def _answer(violated=(), confidence=0.9, low=(), rows=None):
    """Every condition answered: those in ``violated`` seen, those in ``low`` with confidence 0.5."""
    rows = rows if rows is not None else range(1, len(RULES) + 1)
    return {"conditions": [{"n": n, "violated": n in violated, "confidence": 0.5 if n in low else confidence,
                            "detail": f"frame 3: condition {n} seen" if n in violated else "not seen"}
                           for n in rows]}


@pytest.fixture
async def db(tmp_path, monkeypatch):
    reset_registries()
    path = await live_episode(tmp_path, monkeypatch, EP)
    yield path
    reset_registries()


def _checker(*answers):
    client = FakeJudgeClient(*answers)
    return VisionClipConstraintChecker(JudgeService(client)), client


def _clip(tmp_path, name="c.mp4", duration=3.0):
    return make_clip(tmp_path / name, duration, MOVING)


def _ctx(rules=RULES, prompt=PROMPT):
    return {"episode_id": EP, "shot_id": "A02", "prompt": prompt, "constraints": list(rules)}


async def _evaluate(checker, clip, rules=RULES, prompt=PROMPT):
    return await cc.evaluate_clip_constraints(checker, str(clip), list(rules), _ctx(rules, prompt))


# ------------------------------------------------------------------------------------------ verdicts


async def test_a_violated_condition_fails_the_clip_with_what_the_judge_saw(db, tmp_path):
    checker, client = _checker(_answer(violated={1}))
    res = await _evaluate(checker, _clip(tmp_path))
    assert res["status"] == cc.FAIL, res
    assert [r["status"] for r in res["results"]] == [cc.FAIL, cc.PASS, cc.PASS]
    assert "the tray ends in the water: FAIL (frame 3: condition 1 seen)" in res["reason"]
    assert len(client.calls) == 1, "one call answers every declared condition"


async def test_a_clean_clip_passes(db, tmp_path):
    checker, client = _checker(_answer())
    res = await _evaluate(checker, _clip(tmp_path))
    assert res["status"] == cc.PASS and {r["status"] for r in res["results"]} == {cc.PASS}, res
    assert len(client.calls) == 1 and qc_line(db, EP)["spent"] == 560


async def test_an_unsure_answer_is_needs_review_even_when_it_says_violated(db, tmp_path):
    checker, _ = _checker(_answer(low={2}))
    res = await _evaluate(checker, _clip(tmp_path))
    assert res["status"] == cc.NEEDS_REVIEW, res
    assert [r["status"] for r in res["results"]] == [cc.PASS, cc.NEEDS_REVIEW, cc.PASS]
    assert "low confidence 0.50 < 0.7" in res["reason"]
    checker, _ = _checker(_answer(violated={3}, low={3}))
    res = await _evaluate(checker, _clip(tmp_path, "d.mp4", duration=3.5))
    assert res["status"] == cc.NEEDS_REVIEW and res["results"][2]["status"] == cc.NEEDS_REVIEW, res


@pytest.mark.parametrize("answer", [
    RuntimeError("upstream broke"),
    "{not json",
    {"verdict": "pass"},
    _answer(rows=[1, 2]),                                     # a condition left out
    _answer(rows=[1, 2, 2]),                                  # a condition answered twice
    _answer(rows=[1, 2, 3, 4]),                               # a condition nobody asked about
    {"conditions": [{"n": n, "violated": "no", "confidence": 0.9} for n in (1, 2, 3)]},
    {"conditions": [{"n": n, "violated": False, "confidence": 1.4} for n in (1, 2, 3)]},
    {"conditions": [{"n": n, "violated": False} for n in (1, 2, 3)]},
], ids=["upstream-error", "not-json", "no-conditions", "condition-left-out", "condition-twice", "unknown-condition",
        "violated-not-bool", "confidence-above-1", "no-confidence"])
async def test_a_judge_that_cannot_answer_is_needs_review_for_every_condition_and_asked_once(db, tmp_path, answer):
    checker, client = _checker(answer)
    res = await _evaluate(checker, _clip(tmp_path))
    assert res["status"] == cc.NEEDS_REVIEW, res
    assert all(r["status"] == cc.NEEDS_REVIEW and r["reason"].startswith("checker error: ")
               for r in res["results"]), res
    assert len(client.calls) == 1, "the failure is remembered: the other conditions do not pay again"


async def test_a_budget_refusal_is_needs_review_without_a_call(db, tmp_path):
    from hfvg.budget import BudgetLedger

    await BudgetLedger(db).ensure_line(EP, "qc_judges", 500_000, stop_usd_micros=1, provider="openai")
    checker, client = _checker(_answer())
    res = await _evaluate(checker, _clip(tmp_path))
    assert res["status"] == cc.NEEDS_REVIEW and "budget refused the judge call" in res["reason"], res
    assert "judge budget used up, a person checks this: " in res["reason"], res
    assert client.calls == [] and qc_line(db, EP)["reserved"] == 0


async def test_an_unreadable_clip_is_needs_review_without_a_call(db, tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    checker, client = _checker(_answer())
    res = await _evaluate(checker, junk)
    assert res["status"] == cc.NEEDS_REVIEW and "cannot read the duration of junk.mp4" in res["reason"], res
    assert client.calls == [] and qc_line(db, EP) is None


# ------------------------------------------------------------------------------------------ request and cache


async def test_the_judge_sees_frames_from_the_whole_clip_the_prompt_and_numbered_conditions(db, tmp_path):
    checker, client = _checker(_answer())
    await _evaluate(checker, _clip(tmp_path, duration=6.0))
    call = client.calls[0]
    assert call["system"] == SYSTEM and len(call["images"]) == 6
    assert "6 frames at 0.5s, 1.5s, 2.5s, 3.5s, 4.5s, 5.5s of a 6.0s clip." in call["text"], call["text"]
    assert f"The clip was generated from: {PROMPT}" in call["text"]
    assert "Fail if the clip shows:\n1. the tray ends in the water\n2. the tray vanishes\n" \
           "3. the host touches a guest" in call["text"]
    assert all(img.startswith(b"\xff\xd8") or img[:4] == b"\x89PNG" for img in call["images"])


async def test_a_retried_review_of_the_same_clip_does_not_pay_again(db, tmp_path):
    clip = _clip(tmp_path)
    checker, client = _checker(_answer(violated={2}))
    first = await _evaluate(checker, clip)
    again, client2 = _checker(_answer())
    second = await _evaluate(again, clip)
    assert second == first and client2.calls == [] and qc_line(db, EP)["spent"] == 560
    assert [row[0] for row in verdict_rows(db)] == ["clip_fail_if"]


async def test_a_new_clip_new_conditions_or_a_new_prompt_is_judged_again(db, tmp_path):
    clip = _clip(tmp_path)
    checker, client = _checker(_answer(), _answer(), _answer(rows=[1, 2]), _answer())
    await _evaluate(checker, clip)
    await _evaluate(checker, make_clip(tmp_path / "c.mp4", 3.5, MOVING))  # retry_clip: same name, new footage
    await _evaluate(checker, clip, RULES[:2])
    await _evaluate(checker, clip, prompt=PROMPT + " The guest waves.")
    assert len(client.calls) == 4


async def test_a_condition_checked_on_its_own_is_asked_on_its_own(db, tmp_path):
    checker, client = _checker({"conditions": [{"n": 1, "violated": True, "confidence": 0.95, "detail": "splash"}]})
    res = await checker.check(_clip(tmp_path), "the tray vanishes", {"episode_id": EP, "shot_id": "A02"})
    assert res == {"violated": True, "confidence": 0.95, "detail": "splash"}
    assert client.calls[0]["text"].endswith("Fail if the clip shows:\n1. the tray vanishes")


# ------------------------------------------------------------------------------------------ review_clip


async def _review(tmp_path, rubric_extra):
    from hfvg.qc.clip_expect import expected_clip_dims

    dims = expected_clip_dims({"model": "kling", "aspect_ratio": "9:16"})
    tmp_path.mkdir(parents=True, exist_ok=True)
    clip = make_clip(tmp_path / "m.mp4", 2.0, MOVING, size=dims["resolution"], rate=24)
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(200, content=clip.read_bytes()))
        return await HB().run(review_clip, URL, {"duration": 2.0, **dims, **rubric_extra}, EP, "A02")


async def test_review_clip_fails_a_violated_clip_and_escalates_an_unsure_one(db, tmp_path):
    checker, client = _checker(_answer(violated={1}))
    cc.set_constraint_checker(checker)
    out = await _review(tmp_path, {"qc_fail_if": RULES, "prompt": PROMPT})
    assert out["passed"] is False and out["escalate"] is False, out
    assert any(i.startswith("qc_fail_if violated: the tray ends in the water") for i in out["issues"]), out
    assert f"The clip was generated from: {PROMPT}" in client.calls[0]["text"]

    checker, _ = _checker(_answer(low={1}))
    cc.set_constraint_checker(checker)
    out = await _review(tmp_path / "unsure", {"qc_fail_if": RULES, "prompt": PROMPT + " Again."})
    assert out["passed"] is False and out["escalate"] is True, out
    assert out["constraints"]["status"] == cc.NEEDS_REVIEW


async def test_review_clip_escalates_when_the_judge_breaks(db, tmp_path):
    checker, _ = _checker(RuntimeError("upstream broke"))
    cc.set_constraint_checker(checker)
    out = await _review(tmp_path, {"qc_fail_if": RULES})
    assert out["passed"] is False and out["escalate"] is True, out
    assert "checker error: judge error (RuntimeError)" in out["constraints"]["reason"]


async def test_review_clip_passes_a_clean_clip_and_asks_nothing_without_conditions(db, tmp_path):
    checker, client = _checker(_answer())
    cc.set_constraint_checker(checker)
    out = await _review(tmp_path, {"qc_fail_if": RULES, "prompt": PROMPT, "shot_meta": {"count": 1}})
    assert out["passed"] is True and out["constraints"]["status"] == cc.PASS, out
    plain = await _review(tmp_path / "plain", {"prompt": PROMPT, "shot_meta": {"count": 1}})
    assert plain["passed"] is True and "constraints" not in plain and len(client.calls) == 1


async def test_with_the_flag_off_review_clip_is_unchanged_by_the_new_rubric_keys(db, tmp_path):
    out = await _review(tmp_path, {"qc_fail_if": RULES, "prompt": PROMPT,
                                   "shot_meta": {"count": 1, "characters": ["Pip"], "duck": False}})
    assert out["passed"] is False and out["escalate"] is True, out
    assert {r["reason"] for r in out["constraints"]["results"]} == {
        "no clip constraint checker configured: a human must verify"}
    plain = await _review(tmp_path / "plain", {"prompt": PROMPT, "shot_meta": {"count": 1}})
    assert plain["passed"] is True and "constraints" not in plain and "identity" not in plain, plain
    assert qc_line(db, EP) is None
