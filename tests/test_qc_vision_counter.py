"""Automated QC judge (b), the headcount counter (hfvg/qc/vision_counter.py, docs/QC-JUDGES.md),
behind ``CastCounter``: the G2 and recompose headcounts and the assembly headcount. One cached judge
call per still or clip window answers for every figure the shot declares (by its id) and for the
figures nobody declared. A counter that cannot answer (error, budget refusal, low confidence,
malformed answer) is NEEDS_REVIEW for that shot: never a pass, never a 500 or an activity retry. A
reviewer's recorded headcount still wins. The shots here are shots of the first series (main
characters without cast entries, a host declared by ``duck_present``), which keep working as data;
shows with a cast library are in tests/test_qc_any_show.py. FakeJudgeClient only: no network, no spend."""

from __future__ import annotations

import pytest

from hfvg.activities.qc import check_assembly_qc
from hfvg.compat.species_counter import check_shot_animals
from hfvg.qc.g2_headcount import (
    ReviewerHeadcountCounter,
    check_still_headcount,
    g2_headcount_report,
    record_headcount,
)
from hfvg.qc.headcount import set_cast_counter, shot_roster
from hfvg.qc.judges import DEFAULT_PRICE_IN_USD, DEFAULT_PRICE_OUT_USD, FakeJudgeClient, JudgeService, JudgeUnavailable
from hfvg.qc.vision_counter import VisionCastCounter
from tests.lint_helpers import lint_meta
from tests.qc_judge_helpers import image, live_episode, locked_character, qc_line, reset_registries
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg

EP = "ep42"
CAPY3 = {"count": 3, "characters": ["R", "H", "M"], "duck": False}
DUCK_HOST = {"count": 1, "characters": ["D", "R"], "duck": True}


def _answer(counts=None, unexpected=0, background=0, confidence=0.9, mismatched=(), **extra):
    """The judge's answer: {figure id: count}; ``mismatched``: the ids that do not look like their
    description."""
    figures = {fid: {"count": n, "matches": fid not in mismatched} for fid, n in (counts or {}).items()}
    return {"figures": figures, "unexpected": unexpected, "background": background, "confidence": confidence,
            "notes": "counted", **extra}


def _cast(n=3, **kw):
    """The answer for a shot whose main characters have no cast entry: one group, counted together."""
    return _answer({"_cast": n}, **kw)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    reset_registries()
    path = await live_episode(tmp_path, monkeypatch, EP)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    yield path
    reset_registries()


def _still(tmp_path, shot="V12", v=2, seed=0):
    return image(tmp_path / "episodes" / EP / "outputs" / shot / f"still_v{v}.png", seed=seed)


def _counter(*answers, **kw):
    client = FakeJudgeClient(*answers, **kw)
    return VisionCastCounter(JudgeService(client)), client


def _ctx(shot="V12"):
    return {"episode_id": EP, "shot_id": shot}


# ------------------------------------------------------------------------------------------ G2 headcount


async def test_one_call_answers_every_declared_figure_of_a_still(db, tmp_path):
    counter, client = _counter(_cast(3))
    res = await check_still_headcount(counter, _still(tmp_path), CAPY3, shot_context=_ctx())
    assert res["status"] == "PASS", res
    assert [(c["label"], c["counted"], c["method"]) for c in res["checks"]] == \
        [("cast", 3, "vision_judge"), ("unexpected figures", 0, "vision_judge")]
    assert res["expected"] == {"figures": [{"id": "_cast", "name": "cast", "expected": 3, "group": True,
                                            "locked": False}], "unexpected": 0}
    assert len(client.calls) == 1


async def test_a_fourth_cast_animal_or_a_missing_duck_fails(db, tmp_path):
    counter, _ = _counter(_cast(4))
    res = await check_still_headcount(counter, _still(tmp_path), CAPY3, shot_context=_ctx())
    assert res["status"] == "FAIL" and "cast: expected 3, counted 4" in res["reason"], res
    counter, _ = _counter(_answer({"_cast": 1, "_duck_host": 0}))
    res = await check_still_headcount(counter, _still(tmp_path, seed=1), DUCK_HOST, shot_context=_ctx())
    assert res["status"] == "FAIL" and "duck: expected 1, counted 0" in res["reason"], res


async def test_an_undeclared_figure_fails_and_a_background_figure_goes_to_a_person(db, tmp_path):
    counter, _ = _counter(_cast(3, unexpected=1))
    res = await check_still_headcount(counter, _still(tmp_path), CAPY3, shot_context=_ctx())
    assert res["status"] == "FAIL" and res["reason"] == "unexpected figures: expected 0, counted 1", res
    counter, _ = _counter(_cast(3, background=2))
    res = await check_still_headcount(counter, _still(tmp_path, seed=2), CAPY3, shot_context=_ctx())
    assert res["status"] == "NEEDS_REVIEW", res
    assert res["reason"] == "background figures nobody declared: 2 (a person decides)", res


async def test_low_confidence_is_needs_review(db, tmp_path):
    counter, _ = _counter(_cast(4, confidence=0.4))
    res = await check_still_headcount(counter, _still(tmp_path), CAPY3, shot_context=_ctx())
    assert res["status"] == "NEEDS_REVIEW" and "low confidence 0.40 < 0.7" in res["reason"], res


@pytest.mark.parametrize("answer", [
    RuntimeError("upstream broke"), "{not json", _cast(-1), _cast(1.5), _cast(confidence=2),
    {"figures": {"_cast": {"count": 3}}, "background": 0, "confidence": 0.9}, _answer({}),
], ids=["upstream-error", "not-json", "negative-count", "fractional-count", "confidence-above-1",
        "no-unexpected-count", "a-figure-not-counted"])
async def test_a_counter_that_cannot_answer_is_needs_review_and_asked_once(db, tmp_path, answer):
    counter, client = _counter(answer)
    still = _still(tmp_path)
    for _look in range(2):  # the G2 page, then a reload: the failure is remembered, not paid again
        res = await check_still_headcount(counter, still, CAPY3, shot_context=_ctx())
        assert res["status"] == "NEEDS_REVIEW", res
        assert [c["reason"] for c in res["checks"]] == [res["reason"]] and "counter error" in res["reason"], res
    assert len(client.calls) == 1


async def test_a_budget_refusal_is_needs_review_without_a_call(db, tmp_path):
    from hfvg.budget import BudgetLedger

    await BudgetLedger(db).ensure_line(EP, "qc_judges", 500_000, stop_usd_micros=1, provider="openai")
    counter, client = _counter(_cast())
    res = await check_still_headcount(counter, _still(tmp_path), CAPY3, shot_context=_ctx())
    assert res["status"] == "NEEDS_REVIEW" and "budget refused the judge call" in res["reason"], res
    assert res["reason"].startswith("judge budget used up, a person checks this: ") and res["judge_budget_used_up"]
    assert "the qc_judges line: $0.00 spent or held of its $0.000001 stop" in res["reason"], res
    assert client.calls == [] and qc_line(db, EP)["reserved"] == 0


async def test_a_reload_of_the_g2_page_does_not_pay_again(db, tmp_path):
    still = _still(tmp_path)
    counter, client = _counter(_cast())
    await check_still_headcount(counter, still, CAPY3, shot_context=_ctx())
    reload, again = _counter(_cast(4))
    res = await check_still_headcount(reload, still, CAPY3, shot_context=_ctx())
    assert res["status"] == "PASS" and again.calls == [] and qc_line(db, EP)["spent"] == 560
    changed = _still(tmp_path, seed=7)  # a re-rolled still with the same name: new content, new count
    res = await check_still_headcount(reload, changed, CAPY3, shot_context=_ctx())
    assert res["status"] == "FAIL" and len(again.calls) == 1


async def test_a_reviewer_count_wins_and_the_counter_is_asked_only_without_one(db, tmp_path):
    eps = tmp_path / "eps"
    v2, v3 = _still(tmp_path, v=2), _still(tmp_path, v=3, seed=3)
    record_headcount(EP, "V12", 2, {"cast": 3, "duck": 0}, by="reviewer", episodes_dir=eps)
    vision, client = _counter(_cast(4))
    from hfvg.qc.g2_headcount import load_headcounts
    counter = ReviewerHeadcountCounter(load_headcounts(EP, eps), vision)
    assert (await check_still_headcount(counter, v2, CAPY3, shot_context=_ctx()))["status"] == "PASS"
    assert client.calls == []
    assert (await check_still_headcount(counter, v3, CAPY3, shot_context=_ctx()))["status"] == "FAIL"
    assert len(client.calls) == 1


def _versions(**cur):
    return {s: {"current_version": v, "selection": None, "versions": []} for s, v in cur.items()}


async def test_the_g2_report_counts_each_kept_still_and_survives_a_broken_counter(db, tmp_path):
    _still(tmp_path, "V12", 2)
    _still(tmp_path, "V23", 2, seed=4)
    plans = {"V12": {"shot_meta": CAPY3}, "V23": {"shot_meta": CAPY3}}

    def by_shot(request):
        return _cast(3) if "Shot V12" in request["text"] else _cast(2)

    counter, client = _counter(by_shot, by_shot)
    rep = await g2_headcount_report(EP, _versions(V12=2, V23=2), plans, {}, counter=counter, data_dir=tmp_path,
                                    episodes_dir=tmp_path / "eps")
    assert rep["status"] == "FAIL" and rep["blocking"] == ["V23"], rep
    assert rep["shots"]["V12"]["status"] == "PASS" and len(client.calls) == 2

    _still(tmp_path, "V12", 3, seed=8)  # re-rolled stills: new content, so no cached count
    _still(tmp_path, "V23", 3, seed=9)
    broken, _ = _counter(RuntimeError("down"), RuntimeError("down"))
    rep = await g2_headcount_report(EP, _versions(V12=3, V23=3), plans, {}, counter=broken, data_dir=tmp_path,
                                    episodes_dir=tmp_path / "eps")
    assert rep["status"] == "NEEDS_REVIEW" and rep["blocking"] == [], rep
    assert rep["shots"]["V12"]["status"] == rep["shots"]["V23"]["status"] == "NEEDS_REVIEW"
    assert rep["judge_budget_used_up"] == []


async def test_the_g2_report_names_the_shots_a_person_counts_because_the_judge_budget_ran_out(db, tmp_path):
    from hfvg.budget import BudgetLedger

    _still(tmp_path, "V12", 2)
    _still(tmp_path, "V23", 2, seed=4)
    plans = {"V12": {"shot_meta": CAPY3}, "V23": {"shot_meta": CAPY3}}
    counter, client = _counter(_cast(3))
    rep = await g2_headcount_report(EP, _versions(V12=2), plans, {}, counter=counter, data_dir=tmp_path,
                                    episodes_dir=tmp_path / "eps")
    assert rep["status"] == "PASS" and rep["judge_budget_used_up"] == []
    ledger = BudgetLedger(db)
    line = await ledger.get_line_status(EP, "qc_judges")
    assert await ledger.reserve(EP, "qc_judges", line["available"], "held by another check", job_id="other")
    rep = await g2_headcount_report(EP, _versions(V12=2, V23=2), plans, {}, counter=counter, data_dir=tmp_path,
                                    episodes_dir=tmp_path / "eps")
    assert rep["shots"]["V12"]["status"] == "PASS"  # counted before the budget ran out: cached, free
    assert rep["status"] == "NEEDS_REVIEW" and rep["judge_budget_used_up"] == ["V23"], rep
    assert rep["shots"]["V23"]["reason"].startswith("judge budget used up, a person checks this"), rep
    assert len(client.calls) == 1


# ------------------------------------------------------------------------------------------ API


class _Handle:
    def __init__(self, versions=None, plans=None, recompose=None):
        self.versions, self.plans, self.recompose = versions or {}, plans or {}, recompose or {}

    async def query(self, name, args=None):
        if name == "still_versions":
            return self.versions
        if name == "still_version_plan":
            return self.plans.get(args[0])
        if name == "recompose_still_plans":
            return self.recompose
        return {"shot_decisions": {}}


@pytest.fixture
async def api(db, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import httpx

    from tests.test_round5_guards import ADMIN_SECRET, _login

    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    sys.modules.pop("api.main", None)
    import api.main as api_main

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://test")
    await _login(client)

    def wire(handle):
        def list_workflows(query, *a, **k):
            async def gen():
                yield SimpleNamespace(id=EP)
            return gen()
        temporal = MagicMock()
        temporal.list_workflows = list_workflows
        temporal.get_workflow_handle = MagicMock(return_value=handle)
        api_main.temporal_client = temporal

    yield client, wire
    await client.aclose()


async def test_the_g2_headcount_route_answers_needs_review_when_the_counter_breaks(api, tmp_path):
    client, wire = api
    _still(tmp_path, "V12", 2)
    wire(_Handle(_versions(V12=2), {"V12": {"shot_meta": CAPY3}}))
    counter, judge = _counter(ConnectionError("refused"))
    set_cast_counter(counter)
    r = await client.get(f"/api/episodes/{EP}/g2-headcount")
    assert r.status_code == 200, r.text
    shot = r.json()["shots"]["V12"]
    assert shot["status"] == "NEEDS_REVIEW" and "counter error" in shot["reason"], shot
    assert r.json()["blocking"] == [] and len(judge.calls) == 1


async def test_the_g2_headcount_route_with_a_working_counter_and_no_model_name(api, tmp_path, monkeypatch):
    client, wire = api
    monkeypatch.setenv("HFVG_QC_JUDGE_MODEL", "placeholder-vision-model")
    monkeypatch.setenv("HFVG_QC_JUDGE_USD_PER_MTOK_IN", DEFAULT_PRICE_IN_USD)
    monkeypatch.setenv("HFVG_QC_JUDGE_USD_PER_MTOK_OUT", DEFAULT_PRICE_OUT_USD)
    _still(tmp_path, "V12", 2)
    wire(_Handle(_versions(V12=2), {"V12": {"shot_meta": CAPY3}}))
    counter, judge = _counter(_cast(4))
    set_cast_counter(counter)
    r = await client.get(f"/api/episodes/{EP}/g2-headcount")
    assert r.status_code == 200 and r.json()["blocking"] == ["V12"], r.text
    assert "placeholder-vision-model" not in r.text
    r = await client.get(f"/api/episodes/{EP}/g2-headcount")  # a reload: the cached count
    assert r.json()["blocking"] == ["V12"] and len(judge.calls) == 1


async def test_the_recompose_headcount_route_answers_needs_review_when_the_counter_breaks(api, tmp_path):
    client, wire = api
    _still(tmp_path, "V12", 4)
    wire(_Handle(recompose={"V12": {"version": 4, "shot_meta": CAPY3, "decision": None, "still_ready": True}}))
    counter, _judge = _counter(RuntimeError("down"))
    set_cast_counter(counter)
    r = await client.get(f"/api/episodes/{EP}/recompose-headcount")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NEEDS_REVIEW" and r.json()["failing"] == [], r.text


# ------------------------------------------------------------------------------------------ requests


async def test_the_request_describes_the_cast_and_sends_its_sheet_images(db, tmp_path):
    pip = locked_character(EP)
    meta = {"count": 1, "characters": ["R"], "duck": False, "cast": [pip]}
    counter, client = _counter(_answer({pip["character"]: 1}))
    res = await check_still_headcount(counter, _still(tmp_path), meta, shot_context=_ctx())
    assert res["status"] == "PASS", res
    (request,) = client.calls
    assert f"- {pip['character']}: {pip['clause']}" in request["text"]
    assert "Reference images after the frames, in order: Pip." in request["text"]
    assert len(request["images"]) == 2


async def test_without_a_described_cast_the_judge_is_told_what_counts_as_cast(db, tmp_path):
    counter, client = _counter(_answer({"_cast": 1, "_duck_host": 1}))
    res = await check_still_headcount(counter, _still(tmp_path), DUCK_HOST, shot_context=_ctx())
    assert res["status"] == "PASS", res
    text = client.calls[0]["text"]
    assert "- _cast: cast, the shot's main characters that have no description here" in text
    assert "- _duck_host: the series' duck host, not one of the shot's main characters" in text


async def test_the_duck_host_is_left_out_of_the_assembly_animal_count(db, tmp_path):
    still = _still(tmp_path)
    counter, client = _counter(_answer({"_cast": 1, "_duck_host": 1}))
    res = await check_shot_animals(counter, still, DUCK_HOST, shot_context=_ctx())
    assert res["status"] == "PASS", res
    assert [(c["label"], c["expected"], c["counted"]) for c in res["checks"]] == \
        [("cast", 1, 1), ("duck", 1, 1), ("unexpected figures", 0, 0)]
    assert len(client.calls) == 1


async def test_a_count_without_declared_figures_is_refused(db, tmp_path):
    counter, client = _counter(_cast())
    with pytest.raises(JudgeUnavailable, match="no declared figures to count against"):
        await counter.count_cast(_still(tmp_path), _ctx())
    res = await check_still_headcount(counter, _still(tmp_path), {"characters": ["R"]}, shot_context=_ctx())
    assert res["status"] == "NEEDS_REVIEW" and res["reason"] == "shot declares no cast to verify against", res
    assert client.calls == []


async def test_a_clip_is_counted_on_frames_inside_its_trim_window(db, tmp_path, monkeypatch):
    from hfvg.qc import vision_counter

    seen = {}

    async def duration(video):
        return 10.0

    async def frames(video, times, out_dir):
        seen["times"] = times
        return [image(tmp_path / "frames" / f"f{i}.png", seed=i) for i in range(len(times))]

    monkeypatch.setattr(vision_counter, "probe_duration", duration)
    monkeypatch.setattr(vision_counter, "extract_frames", frames)
    clip = tmp_path / "A01.mp4"
    clip.write_bytes(b"clip bytes")
    counter, client = _counter(_cast(1), _cast(1))
    meta = lint_meta(1)
    ctx = {**_ctx("A01"), "shot_meta": meta, "roster": shot_roster(meta)}
    res = await counter.count_cast(clip, {**ctx, "window": (2.0, 6.0)})
    assert res["figures"] == {"_cast": {"count": 1, "matches": True}} and seen["times"] == [2.667, 4.0, 5.333]
    assert len(client.calls[0]["images"]) == 3 and "3 frame(s) to count" in client.calls[0]["text"]
    await counter.count_cast(clip, {**ctx, "window": (0.0, None)})
    assert seen["times"] == [1.667, 5.0, 8.333] and len(client.calls) == 2


# ------------------------------------------------------------------------------------------ assembly


@needs_ffmpeg
async def test_the_assembly_headcount_counts_each_shot_once(db, tmp_path):
    from hfvg.activities.media import assemble_mute_edit

    clips = {sid: str(make_clip(tmp_path / f"{sid}.mp4", 2.0, MOVING)) for sid in ("A01", "A02")}
    edit = await HB().run(assemble_mute_edit, EP, [{"shot_id": s, "duration": 2.0} for s in clips], clips,
                          str(tmp_path / "edit"))
    counter, client = _counter(_cast(1), _answer({"_cast": 1, "_duck_host": 1}))
    set_cast_counter(counter)
    qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], EP,
                        {"A01": lint_meta(1), "A02": lint_meta(1, duck=True)})
    assert qc["verdict"] == "PASS", qc
    assert qc["reason"] == "all shots passed motion and headcount checks"
    assert len(client.calls) == 2  # one per shot: the main characters and the host share it
    assert qc["headcount_results"] == qc["animal_results"] and len(qc["headcount_results"]) == 2


@needs_ffmpeg
async def test_a_counter_error_in_the_assembly_is_needs_review_not_a_retry(db, tmp_path):
    from hfvg.activities.media import assemble_mute_edit

    clips = {"A01": str(make_clip(tmp_path / "A01.mp4", 2.0, MOVING))}
    edit = await HB().run(assemble_mute_edit, EP, [{"shot_id": "A01", "duration": 2.0}], clips,
                          str(tmp_path / "edit"))
    counter, _client = _counter(RuntimeError("down"))
    set_cast_counter(counter)
    qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], EP, {"A01": lint_meta(1)})
    assert qc["verdict"] == "NEEDS_REVIEW" and qc["escalate"] is True, qc
    assert "counter error" in qc["reason"], qc


@needs_ffmpeg
async def test_the_assembly_names_the_shots_a_person_counts_because_the_judge_budget_ran_out(db, tmp_path):
    from hfvg.activities.media import assemble_mute_edit
    from hfvg.budget import BudgetLedger

    clips = {"A01": str(make_clip(tmp_path / "A01.mp4", 2.0, MOVING))}
    edit = await HB().run(assemble_mute_edit, EP, [{"shot_id": "A01", "duration": 2.0}], clips,
                          str(tmp_path / "edit"))
    await BudgetLedger(db).ensure_line(EP, "qc_judges", 500_000, stop_usd_micros=1, provider="openai")
    counter, client = _counter(_cast(1))
    set_cast_counter(counter)
    qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], EP, {"A01": lint_meta(1)})
    assert qc["verdict"] == "NEEDS_REVIEW" and qc["review_shots"] == ["A01"], qc
    assert qc["judge_budget_used_up"] == ["A01"] and client.calls == []
    assert qc["reason"].startswith("A01: judge budget used up, a person checks this: "), qc
