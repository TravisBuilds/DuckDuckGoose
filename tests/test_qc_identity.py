"""Automated QC judge (d), the identity gate for any locked character (hfvg/qc/identity.py,
docs/QC-JUDGES.md): criteria from the character's lock in the cast library, a composite of real crops
(the locked sheet next to the character cut out of the frame), a vision detector and judge, then the
profile's rules. Anything the gate cannot judge (no judge, the dry-run fakes on a live run, a changed
lock, an unsure or broken detector or judge, a budget refusal) escalates; drift is a FAIL. The first
series' duck host gate (hfvg.qc.duck_identity, a compatibility shim: its rules are one profile of the
same gate) has its live tests here, its dry-run tests in test_qc_duck_identity.py and
test_e2e_dryrun.py; shows of people, robots and props are in test_qc_any_show.py. FakeJudgeClient
only: no network, no spend."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx

from hfvg import cast_store
from hfvg.activities.qc import review_clip
from hfvg.qc.duck_identity import DuckIdentityGate, FakeDuckDetector, FakeVisionJudge
from hfvg.qc.identity import (
    COMPOSITE_PX,
    ESCALATE,
    FAIL,
    GENERIC_PROFILE,
    PASS,
    IdentityCriteria,
    IdentityGate,
    IdentityLockError,
    VisionCharacterDetector,
    VisionIdentityJudge,
    build_composite,
    clip_frame_times,
    criteria_from_lock,
    identity_review,
    locked_cast_criteria,
    set_identity_judge,
    sheet_panels,
)
from hfvg.qc.judges import JudgeService
from tests.cast_helpers import complete_sheet, new_character
from tests.qc_judge_helpers import (
    detect_answer,
    identity_answer,
    image,
    live_episode,
    locked_character,
    qc_line,
    reset_registries,
    routed_client,
)
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg

EP = "ep-identity"
DUCK_SIGNS = ("bill", "head", "neck_ring", "body", "too_small")
DUCK_DRESSED = {"apparel": {"tuque": {"present": True, "correct": True}, "vest": {"present": True, "correct": True}}}


@pytest.fixture
async def db(tmp_path, monkeypatch):
    reset_registries()
    path = await live_episode(tmp_path, monkeypatch, EP)
    yield path
    reset_registries()


def _frame(tmp_path, name="frame.png", seed=0):
    """``seed`` changes the figure's colour too, so the crops (and the identity composite) differ."""
    return image(tmp_path / "frames" / name, color=(90 + seed * 7, 140, 200 - seed * 5), seed=seed)


def _register(client):
    service = JudgeService(client)
    set_identity_judge(VisionCharacterDetector(service), VisionIdentityJudge(service))


def _meta(*cast):
    return {"count": len(cast), "cast": list(cast)}


async def _review(frame, *cast, kind="still"):
    return await identity_review(frame, kind=kind, episode_id=EP, shot_id="A01", shot_meta=_meta(*cast))


def _criteria(pip):
    character = cast_store.find_character(("episode", EP), pip["character"])
    return character, next(v for v in character["versions"] if v["version"] == 1)


# ------------------------------------------------------------------------------------------ criteria


async def test_the_criteria_come_from_the_characters_lock(db):
    pip = locked_character(EP)
    character, version = _criteria(pip)
    crit = criteria_from_lock(character, version, EP)
    assert (crit.slug, crit.name, crit.version, crit.episode_id) == (pip["character"], "Pip", 1, EP)
    assert crit.lock_hash == version["lock_hash"] and crit.lock_hash
    assert "teal shell" in crit.description and crit.profile is GENERIC_PROFILE
    roles = [role for role, _path in crit.sheet]
    assert roles[0] == cast_store.MASTER_ROLE and cast_store.HEAD_ROLE in roles, roles
    assert all(Path(path).is_file() for _role, path in crit.sheet)


async def test_an_unlocked_or_changed_character_cannot_be_judged(db):
    draft = new_character(("episode", EP), name="Bo", description="a tall green robot")
    complete_sheet(draft, 1, seed=300)
    draft = cast_store.get_character(draft["character_id"])
    with pytest.raises(IdentityLockError, match="is not locked"):
        criteria_from_lock(draft, draft["versions"][0], EP)

    pip = locked_character(EP)
    character, version = _criteria(pip)
    master = next(i for i in version["images"] if i["role"] == cast_store.MASTER_ROLE)
    cast_store.image_file(master["sha256"], master["ext"]).write_bytes(b"swapped")
    with pytest.raises(IdentityLockError):
        criteria_from_lock(character, version, EP)


async def test_the_shot_cast_lists_what_cannot_be_judged_and_skips_unlocked_entries(db):
    pip = locked_character(EP)
    ghost = {"character": "ghost", "version": 1, "name": "Ghost", "locked": True}  # no prompt clause: still judged
    old = {**pip, "version": 9}
    loose = {"character": "loose", "version": 1, "name": "Loose", "clause": "Loose, a robot", "locked": False}
    criteria, problems = locked_cast_criteria(EP, _meta(pip, ghost, old, loose))
    assert [c.slug for c in criteria] == [pip["character"]]
    assert criteria[0].roster == ((pip["character"], "Pip", criteria[0].description),)
    assert [(p["name"], p["reason"]) for p in problems] == [
        ("Ghost", "Ghost is not in the episode's cast library"), ("Pip", "Pip has no version 9")]
    assert locked_cast_criteria(EP, _meta(loose)) == ([], [])
    assert locked_cast_criteria(EP, {"count": 1, "cast": "Pip"}) == ([], [])


# ------------------------------------------------------------------------------------------ verdicts


async def test_a_frame_that_matches_the_sheet_passes(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"])], identity=[identity_answer()])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == PASS and res["reason"] == "every locked character matches its sheet", res
    assert res["characters"] == [{"character": pip["character"], "name": "Pip", "version": 1, "verdict": PASS,
                                  "reason": "Identity check passed"}]
    assert client.asked() == ["detect", "identity"]
    judge_call = client.calls[1]
    assert judge_call["text"].startswith("Character: Pip (version 1). Locked description: ")
    assert "panel 1: the locked sheet, full body; panel 2: the locked sheet, three-quarter view; " \
           "panel 3: the locked sheet, head; panel 4: the character in the frame, body; " \
           "panel 5: the character in the frame, head" in judge_call["system"]
    assert len(judge_call["images"]) == 1 and qc_line(db, EP)["spent"] == 2 * 560


@pytest.mark.parametrize("sign", list(GENERIC_PROFILE.signs))
async def test_drift_on_any_sign_fails(db, tmp_path, sign):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"])], identity=[identity_answer({sign: 2})])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == FAIL and res["reason"].startswith(f"Pip: Drift detected: {sign} (score 2)"), res


async def test_three_minor_differences_fail_and_two_pass(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"])] * 2,
                           identity=[identity_answer({"face": 1, "colours": 1, "outfit": 1}),
                                     identity_answer({"face": 1, "colours": 1})])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == FAIL and "Total minor score 3" in res["reason"], res
    res = await _review(_frame(tmp_path, "other.png", seed=1), pip)
    assert res["status"] == PASS, res


async def test_the_judges_own_fail_and_escalate_count(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"])] * 2,
                           identity=[identity_answer(verdict="FAIL", reason="a different character"),
                                     identity_answer(verdict="ESCALATE", reason="only a hand in frame")])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == FAIL and res["reason"] == "Pip: a different character", res
    res = await _review(_frame(tmp_path, "other.png", seed=1), pip)
    assert res["status"] == "NEEDS_REVIEW" and res["reason"] == "Pip: only a hand in frame", res


async def test_an_unsure_judge_or_detector_escalates(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"]), detect_answer(pip["character"], confidence=0.4)],
                           identity=[identity_answer({"face": 2}, confidence=0.5)])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW" and res["reason"] == "Pip: Low confidence 0.50 < 0.6", res
    res = await _review(_frame(tmp_path, "other.png", seed=1), pip)
    assert res["status"] == "NEEDS_REVIEW" and res["reason"] == "Pip: detector unsure (confidence 0.40 < 0.6)", res
    assert client.asked() == ["detect", "identity", "detect"]


async def test_a_declared_character_missing_from_the_frame_escalates(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"], found=False)])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW", res
    assert res["reason"] == "Pip expected (declared in this shot) but not found" and client.asked() == ["detect"]


BAD_DETECTIONS = [
    RuntimeError("upstream broke"),
    "{not json",
    {"characters": {}},
    {"characters": {"SLUG": {"found": "yes", "confidence": 0.9}}},
    {"characters": {"SLUG": {"found": True, "confidence": 0.9, "box": None}}},
    {"characters": {"SLUG": {"found": True, "confidence": 0.9, "box": {"x": 0.8, "y": 0.1, "w": 0.5, "h": 0.5}}}},
    {"characters": {"SLUG": {"found": True, "confidence": 0.9, "box": {"x": 0.1, "y": 0.1, "w": 0, "h": 0.5}}}},
]


@pytest.mark.parametrize("answer", BAD_DETECTIONS, ids=["upstream-error", "not-json", "no-character",
                                                        "found-not-bool", "no-box", "box-outside", "empty-box"])
async def test_a_broken_or_malformed_detection_escalates(db, tmp_path, answer):
    pip = locked_character(EP)
    if isinstance(answer, dict):
        answer = {"characters": {pip["character"] if k == "SLUG" else k: v for k, v in answer["characters"].items()}}
    client = routed_client(detect=[answer])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW" and res["reason"].startswith("Pip: detector failed: "), res
    assert client.asked() == ["detect"]


@pytest.mark.parametrize("answer", [
    RuntimeError("upstream broke"),
    "[]",
    {"verdict": "PASS", "confidence": 0.9},
    identity_answer({"face": 3}),
    identity_answer({"face": True}),
    identity_answer(verdict="MAYBE"),
    identity_answer(confidence=1.5),
], ids=["upstream-error", "not-an-object", "no-signs", "score-3", "score-bool", "unknown-verdict",
        "confidence-above-1"])
async def test_a_broken_or_malformed_identity_answer_escalates(db, tmp_path, answer):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"])], identity=[answer])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW" and res["reason"].startswith("Pip: identity judge failed: "), res


async def test_a_budget_refusal_escalates_without_a_call(db, tmp_path):
    from hfvg.budget import BudgetLedger

    pip = locked_character(EP)
    await BudgetLedger(db).ensure_line(EP, "qc_judges", 500_000, stop_usd_micros=1, provider="openai")
    client = routed_client(detect=[detect_answer(pip["character"])], identity=[identity_answer()])
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW" and "budget refused the judge call" in res["reason"], res
    assert "judge budget used up, a person checks this: " in res["reason"], res
    assert client.calls == [] and qc_line(db, EP)["reserved"] == 0


async def test_a_lock_that_changed_escalates_without_a_call(db, tmp_path):
    pip = locked_character(EP)
    _character, version = _criteria(pip)
    head = next(i for i in version["images"] if i["role"] == cast_store.HEAD_ROLE)
    cast_store.image_file(head["sha256"], head["ext"]).write_bytes(b"swapped")
    client = routed_client()
    _register(client)
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW" and res["characters"][0]["verdict"] == ESCALATE, res
    assert client.calls == []


# ------------------------------------------------------------------------------------------ no judge, fakes


async def test_without_an_identity_judge_or_a_locked_character_nothing_is_checked(db, tmp_path):
    pip = locked_character(EP)
    assert await _review(_frame(tmp_path), pip) is None  # the flag off: no identity judge registered
    client = routed_client()
    _register(client)
    unlocked = {**pip, "locked": False}
    assert await _review(_frame(tmp_path), unlocked) is None
    assert await identity_review(_frame(tmp_path), kind="still", episode_id=EP, shot_id="A01",
                                 shot_meta={"count": 1, "characters": ["R"]}) is None
    assert client.calls == []


async def test_a_gate_without_a_judge_or_a_sheet_escalates(db, tmp_path):
    frame = _frame(tmp_path)
    res = await IdentityGate().check_still(frame, {"shot_id": "A01"})
    assert res["verdict"] == ESCALATE and res["reason"] == "no identity judge configured"
    service = JudgeService(routed_client())
    gate = IdentityGate(VisionCharacterDetector(service), VisionIdentityJudge(service),
                        IdentityCriteria(slug="bo", name="Bo", episode_id=EP))
    res = await gate.check_still(frame, {"shot_id": "A01"})
    assert res["verdict"] == ESCALATE and res["reason"] == "Bo: no sheet image to compare against"


async def test_the_always_pass_fakes_escalate_live_and_pass_in_dry_run(db, tmp_path, monkeypatch):
    pip = locked_character(EP)
    character, version = _criteria(pip)
    gate = IdentityGate(FakeDuckDetector(), FakeVisionJudge(), criteria_from_lock(character, version, EP))
    res = await gate.check_still(_frame(tmp_path), {"shot_id": "A01"})
    assert res["verdict"] == ESCALATE and "needs a real judge" in res["reason"], res
    set_identity_judge(FakeDuckDetector(), FakeVisionJudge())
    res = await _review(_frame(tmp_path), pip)
    assert res["status"] == "NEEDS_REVIEW" and "needs a real judge" in res["reason"], res
    monkeypatch.setenv("DRY_RUN", "true")
    assert (await gate.check_still(_frame(tmp_path), {"shot_id": "A01"}))["verdict"] == PASS


# ------------------------------------------------------------------------------------------ composite


def _two_figure_frame(path):
    """A green block on the left, a red figure on the right."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 960), (235, 235, 235))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 300, 200, 800], fill=(40, 180, 60))
    draw.ellipse([400, 100, 560, 260], fill=(200, 50, 50))
    draw.rectangle([380, 280, 600, 880], fill=(200, 50, 50))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def _colour_counts(jpeg: bytes, box=None) -> dict[str, int]:
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as im:
        im = im.convert("RGB")
        if box:
            im = im.crop(box(im))
        counts = {"red": 0, "green": 0, "blue": 0}
        data = im.tobytes()
        for r, g, b in zip(data[0::3], data[1::3], data[2::3]):
            if r > 150 and g < 100 and b < 100:
                counts["red"] += 1
            elif g > 140 and r < 100 and b < 110:
                counts["green"] += 1
            elif b > 170 and r < 130 and 110 < g < 170:
                counts["blue"] += 1
        return counts


async def test_the_composite_is_the_locked_sheet_next_to_real_crops_of_the_frame(db, tmp_path):
    pip = locked_character(EP)
    character, version = _criteria(pip)
    crit = criteria_from_lock(character, version, EP)
    frame = _two_figure_frame(tmp_path / "frame.png")
    sheet = [path for _label, path in sheet_panels(crit.sheet)]
    jpeg = build_composite(frame, {"x": 380, "y": 100, "w": 220, "h": 780}, {"x": 400, "y": 100, "w": 160, "h": 160},
                           sheet)
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as im:
        assert im.format == "JPEG" and max(im.size) <= COMPOSITE_PX
    whole = _colour_counts(jpeg)
    assert whole["red"] > 2000 and whole["blue"] > 2000, whole
    assert whole["green"] < 50, "the crop is the character's box, not the whole frame"
    left = _colour_counts(jpeg, lambda im: (0, 0, im.width // 2, im.height))
    right = _colour_counts(jpeg, lambda im: (im.width * 3 // 4, 0, im.width, im.height))
    assert left["blue"] > left["red"] and right["red"] > right["blue"], (left, right)


def test_the_sheet_panels_follow_the_cast_roles_and_a_plain_folder_gives_two():
    sheet = (("turnaround_side", "s"), ("head_sheet", "h"), ("master", "m"), ("turnaround_three_quarter", "t"))
    assert sheet_panels(sheet) == [("the locked sheet, full body", "m"), ("the locked sheet, three-quarter view", "t"),
                                   ("the locked sheet, head", "h")]
    assert sheet_panels((("ref", "a"), ("ref", "b"), ("ref", "c"))) == [("reference sheet 1", "a"),
                                                                       ("reference sheet 2", "b")]


async def test_an_unreadable_frame_escalates(db, tmp_path):
    pip = locked_character(EP)
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image")
    client = routed_client()
    _register(client)
    res = await _review(junk, pip)
    assert res["status"] == "NEEDS_REVIEW" and "cannot read junk.png as an image" in res["reason"], res
    assert client.calls == []


# ------------------------------------------------------------------------------------------ cost


async def test_two_locked_characters_share_one_detection_and_a_second_look_is_free(db, tmp_path):
    pip = locked_character(EP)
    bo = locked_character(EP, name="Bo", seed=200, description="a tall green robot with square shoulders")
    client = routed_client(detect=[detect_answer(pip["character"], bo["character"])],
                           identity=[identity_answer(), identity_answer()])
    _register(client)
    frame = _frame(tmp_path)
    res = await _review(frame, pip, bo)
    assert res["status"] == PASS and [r["name"] for r in res["characters"]] == ["Pip", "Bo"], res
    assert client.asked() == ["detect", "identity", "identity"]
    assert "- " + pip["character"] + ": Pip." in client.calls[0]["text"]
    assert "- " + bo["character"] + ": Bo." in client.calls[0]["text"]
    again = await _review(frame, pip, bo)
    assert again == res and len(client.calls) == 3 and qc_line(db, EP)["spent"] == 3 * 560


# ------------------------------------------------------------------------------------------ clips


def test_clip_frames_are_the_first_middle_and_last_inside_the_clip():
    assert clip_frame_times(4.0) == [0.1, 2.0, 3.85]
    assert clip_frame_times(10.0) == [0.1, 5.0, 9.85]
    assert all(0 < t < 0.2 for t in clip_frame_times(0.2))


@needs_ffmpeg
async def test_a_clip_is_judged_on_three_frames_at_its_real_duration_and_the_worst_wins(db, tmp_path):
    pip = locked_character(EP)
    character, version = _criteria(pip)
    crit = criteria_from_lock(character, version, EP)
    clip = make_clip(tmp_path / "A01.mp4", 4.0, MOVING)
    client = routed_client(detect=[detect_answer(pip["character"])] * 3,
                           identity=[identity_answer(), identity_answer({"outfit": 2}), identity_answer()])
    service = JudgeService(client)
    gate = IdentityGate(VisionCharacterDetector(service), VisionIdentityJudge(service), crit)
    res = await gate.check_clip(clip, {"shot_id": "A01", "episode_id": EP})
    assert [f["frame_time"] for f in res["frames"]] == [0.1, 2.0, 3.85]
    assert [f["verdict"] for f in res["frames"]] == [PASS, FAIL, PASS]
    assert res["verdict"] == FAIL and res["reason"].startswith("Frame 1 failed: Drift detected: outfit"), res
    assert client.asked() == ["detect", "identity"] * 3


@needs_ffmpeg
async def test_an_unreadable_clip_escalates_without_a_call(db, tmp_path):
    pip = locked_character(EP)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    client = routed_client()
    _register(client)
    res = await _review(junk, pip, kind="clip")
    assert res["status"] == "NEEDS_REVIEW" and "cannot read the duration of junk.mp4" in res["reason"], res
    assert client.calls == []


async def _review_clip(tmp_path, shot_meta):
    from hfvg.qc.clip_expect import expected_clip_dims

    dims = expected_clip_dims({"model": "kling", "aspect_ratio": "9:16"})
    tmp_path.mkdir(parents=True, exist_ok=True)
    clip = make_clip(tmp_path / "m.mp4", 2.0, MOVING, size=dims["resolution"], rate=24)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://cdn.example.com/m.mp4").mock(return_value=httpx.Response(200, content=clip.read_bytes()))
        return await HB().run(review_clip, "https://cdn.example.com/m.mp4",
                              {"duration": 2.0, **dims, "shot_meta": shot_meta}, EP, "A01")


@needs_ffmpeg
async def test_review_clip_fails_on_drift_and_escalates_an_unsure_identity(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(detect=[detect_answer(pip["character"])] * 3,
                           identity=[identity_answer({"face": 2}), identity_answer(), identity_answer()])
    _register(client)
    out = await _review_clip(tmp_path, _meta(pip))
    assert out["passed"] is False and out["escalate"] is False, out
    assert out["issues"][-1].startswith("identity: Pip: Frame 0 failed: Drift detected: face"), out

    bo = locked_character(EP, name="Bo", seed=200, description="a tall green robot with square shoulders")
    client = routed_client(detect=[detect_answer(bo["character"], confidence=0.3)] * 3)
    _register(client)
    out = await _review_clip(tmp_path / "unsure", _meta(bo))
    assert out["passed"] is False and out["escalate"] is True, out
    assert out["issues"][-1].startswith("identity needs review: Bo: Frame 2 escalated: "), out
    assert client.asked() == ["detect"] * 3


@needs_ffmpeg
async def test_review_clip_without_an_identity_judge_is_unchanged(db, tmp_path):
    pip = locked_character(EP)
    out = await _review_clip(tmp_path, _meta(pip))
    assert out["passed"] is True and "identity" not in out, out
    assert qc_line(db, EP) is None


# ------------------------------------------------------------------------------------------ duck host, live


def _duck_gate(tmp_path, client):
    refs = tmp_path / "duck-refs"
    for i in range(2):
        image(refs / f"ref{i}.png", color=(230, 200, 120), seed=40 + i)
    service = JudgeService(client)
    return DuckIdentityGate(VisionCharacterDetector(service), VisionIdentityJudge(service), refs_path=refs,
                            episode_id=EP)


def _duck_answer(scores=None, tuft=True, **extra):
    fields = {**DUCK_DRESSED, "features": {"tuft": {"present": tuft, "correct": tuft}}, **extra}
    return identity_answer(scores, signs=DUCK_SIGNS, **fields)


HOST_B = {"shot_id": "A01", "duck_role": "host", "duck_state": "B"}


async def test_a_live_duck_host_gate_judges_with_the_duck_rules(db, tmp_path):
    client = routed_client(detect=[detect_answer("duck")], identity=[_duck_answer()])
    gate = _duck_gate(tmp_path, client)
    res = await gate.check_still(_frame(tmp_path), HOST_B)
    assert res["verdict"] == PASS, res
    system = client.calls[1]["system"]
    assert "- bill: bill shape and length" in system and "- tuque: cream tuque with a pom" in system
    assert "This is a B shot: report the tuft" in system
    assert "panel 1: reference sheet 1; panel 2: reference sheet 2; panel 3: the character in the frame" in system


@pytest.mark.parametrize("answer, reason", [
    (_duck_answer({"neck_ring": 1}), "Neck ring detected"),
    (_duck_answer(tuft=False), "B shot: tuft missing or incorrect"),
    (_duck_answer(apparel={"tuque": {"present": True, "correct": False},
                           "vest": {"present": True, "correct": True}}), "Tuque mismatch"),
    (_duck_answer({"bill": 2}), "Drift detected: bill"),
])
async def test_the_live_duck_rules_fail(db, tmp_path, answer, reason):
    gate = _duck_gate(tmp_path, routed_client(detect=[detect_answer("duck")], identity=[answer]))
    res = await gate.check_still(_frame(tmp_path), HOST_B)
    assert res["verdict"] == FAIL and res["reason"].startswith(reason), res


async def test_a_tiny_or_missing_duck_host_fails_or_escalates_before_the_identity_call(db, tmp_path):
    client = routed_client(detect=[detect_answer("duck", box={"x": 0.4, "y": 0.4, "w": 0.05, "h": 0.08})])
    res = await _duck_gate(tmp_path, client).check_still(_frame(tmp_path), HOST_B)
    assert res["verdict"] == FAIL and res["reason"] == "Duck too small: height fraction 0.080 < 0.12", res
    client = routed_client(detect=[detect_answer("duck", found=False)])
    res = await _duck_gate(tmp_path, client).check_still(_frame(tmp_path, seed=3), HOST_B)
    assert res["verdict"] == ESCALATE and res["reason"] == "Duck expected (host role) but not found", res
    assert client.asked() == ["detect"]


async def test_a_live_duck_gate_with_the_fakes_escalates(db, tmp_path):
    res = await DuckIdentityGate(episode_id=EP).check_still(_frame(tmp_path), HOST_B)
    assert res["verdict"] == ESCALATE and "needs a real judge" in res["reason"], res
