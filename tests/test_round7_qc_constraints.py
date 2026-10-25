"""Round 7 QC constraints.

- per-shot ``qc_fail_if`` (shots.json) is passed to the clip QC and checked on every clip through
  the ClipConstraintChecker interface; results are recorded per clip. Declared constraints with no
  checker (or no file / low confidence / checker error) are NEEDS_REVIEW: fail closed, never a pass.
- per-episode ``continuity_hard_checks`` selects which continuity checks block the assembly (hard)
  and which are advisory (reported, WATCH at worst).
"""

import aiosqlite
import httpx
import pytest
import respx

from hfvg.activities.qc import check_assembly_qc, review_clip
from hfvg.qc import clip_constraints as cc
from hfvg.qc import continuity as cont
from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
from hfvg.shot_data import ShotDataError, parse_episode_options, parse_shots
from tests.lint_helpers import lint_meta
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, no_counter  # noqa: F401

RULES = ["the tray ends in water", "the tray vanishes", "the host touches a guest"]


@pytest.fixture(autouse=True)
def _reset_checkers():
    cc.set_constraint_checker(None)
    cont.set_continuity_checker(None)
    yield
    cc.set_constraint_checker(None)
    cont.set_continuity_checker(None)


# ------------------------------------------------------------------ qc_fail_if: declaration
def test_qc_fail_if_parsed_and_validated():
    base = {"count": 1, "characters": ["D"], "wet": False, "model": "kling", "duration": 5,
            "end_frame": False, "prompt": "p", "ref_keys": []}
    plans = parse_shots({"shots": [{**base, "id": "A01", "qc_fail_if": [" the tray vanishes "]}, {**base, "id": "A02"}]})
    assert plans["A01"]["qc_fail_if"] == ["the tray vanishes"] and plans["A02"]["qc_fail_if"] == []
    for bad in ("tray vanishes", [""], ["a", "a"], [1], []):
        with pytest.raises(ShotDataError):
            parse_shots({"shots": [{**base, "id": "A01", "qc_fail_if": bad}]})


# ------------------------------------------------------------------ qc_fail_if: evaluation
@pytest.mark.asyncio
async def test_no_checker_is_needs_review_for_every_declared_constraint(tmp_path):
    media = tmp_path / "c.mp4"
    media.write_bytes(b"x")
    out = await cc.evaluate_clip_constraints(None, media, RULES)
    assert out["status"] == cc.NEEDS_REVIEW and [r["status"] for r in out["results"]] == [cc.NEEDS_REVIEW] * 3
    assert out["declared"] == RULES


@pytest.mark.asyncio
async def test_checker_outcomes(tmp_path):
    media = tmp_path / "c.mp4"
    media.write_bytes(b"x")
    ok = await cc.evaluate_clip_constraints(cc.FakeClipConstraintChecker(), media, RULES)
    assert ok["status"] == cc.PASS and len(ok["results"]) == 3
    bad = await cc.evaluate_clip_constraints(cc.FakeClipConstraintChecker({"the tray vanishes"}), media, RULES)
    assert bad["status"] == cc.FAIL and "the tray vanishes: FAIL" in bad["reason"]
    unsure = await cc.evaluate_clip_constraints(cc.FakeClipConstraintChecker(confidence=0.4), media, RULES)
    assert unsure["status"] == cc.NEEDS_REVIEW

    class Boom(cc.ClipConstraintChecker):
        async def check(self, media_path, constraint, shot_context):
            raise RuntimeError("judge down")
    err = await cc.evaluate_clip_constraints(Boom(), media, RULES)
    assert err["status"] == cc.NEEDS_REVIEW and "judge down" in err["reason"]
    gone = await cc.evaluate_clip_constraints(cc.FakeClipConstraintChecker(), tmp_path / "missing.mp4", RULES)
    assert gone["status"] == cc.NEEDS_REVIEW
    assert (await cc.evaluate_clip_constraints(None, media, []))["status"] == cc.NONE_DECLARED
    assert (await cc.evaluate_clip_constraints(None, None, RULES, dry_run=True))["status"] == cc.DRY_RUN


# ------------------------------------------------------------------ qc_fail_if in review_clip
async def _live_episode(tmp_path, monkeypatch, ep="ep58"):
    from hfvg.studio_db import create_episode, init_studio_db
    db_path = str(tmp_path / "qc.db")
    await init_studio_db(db_path)
    await create_episode(db_path, ep)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?", (ep,))
        await db.commit()
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")


async def _review(tmp_path, rubric_extra):
    from hfvg.qc.clip_expect import expected_clip_dims
    dims = expected_clip_dims({"model": "kling", "aspect_ratio": "9:16"})
    clip = make_clip(tmp_path / "m.mp4", 2.0, MOVING, size=dims["resolution"], rate=24)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://cdn.example.com/m.mp4").mock(return_value=httpx.Response(200, content=clip.read_bytes()))
        return await HB().run(review_clip, "https://cdn.example.com/m.mp4",
                              {"duration": 2.0, **dims, **rubric_extra}, "ep58", "A02")


@needs_ffmpeg
@pytest.mark.asyncio
async def test_live_clip_with_constraints_and_no_checker_escalates(tmp_path, monkeypatch):
    await _live_episode(tmp_path, monkeypatch)
    out = await _review(tmp_path, {"qc_fail_if": RULES})
    assert out["passed"] is False and out["escalate"] is True, out
    assert out["constraints"]["status"] == cc.NEEDS_REVIEW
    assert [r["constraint"] for r in out["constraints"]["results"]] == RULES


@needs_ffmpeg
@pytest.mark.asyncio
async def test_live_clip_violated_constraint_fails_and_clean_clip_passes(tmp_path, monkeypatch):
    await _live_episode(tmp_path, monkeypatch)
    fake = cc.FakeClipConstraintChecker({"the tray ends in water"})
    cc.set_constraint_checker(fake)
    out = await _review(tmp_path, {"qc_fail_if": RULES})
    assert out["passed"] is False and out["escalate"] is False and out["constraints"]["status"] == cc.FAIL, out
    assert any("qc_fail_if violated" in i for i in out["issues"])
    assert [c for _, c in fake.calls] == RULES, "every declared constraint is checked on the clip"
    cc.set_constraint_checker(cc.FakeClipConstraintChecker())
    good = await _review(tmp_path, {"qc_fail_if": RULES})
    assert good["passed"] is True and good["constraints"]["status"] == cc.PASS
    plain = await _review(tmp_path, {})
    assert plain["passed"] is True and "constraints" not in plain, "no declaration: QC unchanged"


@pytest.mark.asyncio
async def test_dry_clip_records_constraints_as_not_checked(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    out = await review_clip("https://x/c.mp4", {"qc_fail_if": RULES}, "ep58", "A02")
    assert out["passed"] is True and out["constraints"]["status"] == cc.DRY_RUN
    assert {r["status"] for r in out["constraints"]["results"]} == {cc.DRY_RUN}


# ------------------------------------------------------------------ continuity_hard_checks
def test_continuity_policy_and_episode_option():
    assert cont.continuity_policy(None) == {"hard": ["headcount"], "advisory": ["relative_size", "left_to_right_order"],
                                            "source": "default"}
    p = cont.continuity_policy(["relative_size", "headcount"])
    assert p["hard"] == ["headcount", "relative_size"] and p["advisory"] == ["left_to_right_order"]
    assert parse_episode_options({"shots": [], "continuity_hard_checks": ["headcount"]}) == {
        "continuity_hard_checks": ["headcount"]}
    for bad in (["size"], "headcount", ["headcount", "headcount"]):
        with pytest.raises((ShotDataError, cont.ContinuityError)):
            parse_episode_options({"shots": [], "continuity_hard_checks": bad})


@needs_ffmpeg
class TestContinuityHardVsAdvisory:
    async def _edit(self, tmp_path):
        from hfvg.activities.media import assemble_mute_edit
        clips = {sid: str(make_clip(tmp_path / f"{sid}.mp4", 2.0, MOVING)) for sid in ("A01", "A02")}
        shots = [{"shot_id": sid, "duration": 2.0} for sid in clips]
        return await HB().run(assemble_mute_edit, "ep41", shots, clips, str(tmp_path / "edit"))

    async def _qc(self, edit, hard):
        metas = {"A01": lint_meta(1), "A02": lint_meta(1)}
        args = [edit["assembly_path"], edit["manifest"], "ep41", metas] + ([hard] if hard is not None else [])
        return await HB().run(check_assembly_qc, *args)

    @pytest.mark.asyncio
    async def test_hard_size_fail_blocks_advisory_order_fail_only_watches(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        edit = await self._edit(tmp_path)
        cont.set_continuity_checker(cont.FakeContinuityChecker({("A02", "left_to_right_order"): False}))
        qc = await self._qc(edit, ["relative_size", "headcount"])
        assert qc["verdict"] == "WATCH" and qc["blocking"] is False, qc
        assert qc["watch_shots"] == ["A02"] and "(advisory)" in qc["reason"]
        cont.set_continuity_checker(cont.FakeContinuityChecker({("A01", "relative_size"): False}))
        qc = await self._qc(edit, ["relative_size", "headcount"])
        assert qc["verdict"] == "FAIL" and qc["blocking"] is True and qc["failed_shots"] == ["A01"], qc
        rows = {(r["shot_id"], r["check"]): r for r in qc["continuity_results"]}
        assert rows[("A01", "relative_size")]["hard"] is True
        assert rows[("A01", "left_to_right_order")]["hard"] is False

    @pytest.mark.asyncio
    async def test_hard_check_without_checker_needs_review(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        edit = await self._edit(tmp_path)
        qc = await self._qc(edit, ["relative_size", "headcount"])
        assert qc["verdict"] == "NEEDS_REVIEW" and qc["blocking"] is True, qc
        assert qc["review_shots"] == ["A01", "A02"]
        qc = await self._qc(edit, ["headcount"])  # size + order advisory: unverified is reported only
        assert qc["verdict"] == "WATCH" and qc["blocking"] is False, qc

    @pytest.mark.asyncio
    async def test_headcount_can_be_advisory(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 3}))  # wrong count everywhere
        cont.set_continuity_checker(cont.FakeContinuityChecker())
        edit = await self._edit(tmp_path)
        assert (await self._qc(edit, None))["verdict"] == "FAIL", "default: headcount is hard"
        qc = await self._qc(edit, ["relative_size"])
        assert qc["verdict"] == "WATCH" and qc["blocking"] is False, qc
        assert "advisory headcount" in qc["reason"]

    @pytest.mark.asyncio
    async def test_without_the_key_size_and_order_are_not_run(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        fake = cont.FakeContinuityChecker({"relative_size": False})
        cont.set_continuity_checker(fake)
        edit = await self._edit(tmp_path)
        qc = await self._qc(edit, None)
        assert qc["verdict"] == "PASS" and fake.calls == [] and qc["continuity_policy"]["source"] == "default"
