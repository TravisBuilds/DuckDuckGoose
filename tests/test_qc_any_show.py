"""The QC judges on shows without a duck (docs/QC-JUDGES.md). "Night shift" stars two people and a
robot; "The lamp" a person with a brass lamp on the set and, in one shot, a kettle that talks. Each
character is an ordinary locked entry of the episode's cast library and each check reads the shot
plan the pipeline builds from shots.json: the headcount counts every declared character by its id
plus the figures nobody declared, a figure that may not be the declared character goes to a person,
the identity gate judges every locked character with the same rules, and the judges' budget line can
be sized from the shots and their locked characters. The first series' duck host is one more cast
entry here. FakeJudgeClient only: no network, no spend."""

from __future__ import annotations

import json
import logging
import re

import pytest

from hfvg.budget import compute_stop_usd_micros, get_budget_settings
from hfvg.paths import episode_dir
from hfvg.pricing import micros_to_usd_str, usd_to_micros
from hfvg.qc import identity, still_review, vision_constraints, vision_counter
from hfvg.qc.g2_headcount import check_still_headcount
from hfvg.qc.headcount import FakeCastCounter, check_headcount, shot_roster
from hfvg.qc.identity import (
    GENERIC_PROFILE,
    VisionCharacterDetector,
    VisionIdentityJudge,
    identity_review,
    judge_system,
    set_identity_judge,
)
from hfvg.qc.judge_budget import (
    ShotJudging,
    episode_line_cap_usd_micros,
    line_cap_usd_micros,
    take_calls,
    take_usd_micros,
)
from hfvg.qc.judges import DEFAULT_LINE_CAP_USD, FakeJudgeClient, JudgeCall, JudgeService
from hfvg.qc.vision_counter import VisionCastCounter
from hfvg.shot_data import load_shot_plans
from tests.cast_helpers import new_prop
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

EP = "ep-any-show"
# Words that would tie a judge's instructions to one kind of character or one look
ASSUMED = {"duck", "ducks", "animal", "animals", "species", "beak", "bill", "muzzle", "fur", "feathers", "cartoon",
           "animated", "plush", "3d", "cgi", "anime"}


@pytest.fixture
async def db(tmp_path, monkeypatch):
    reset_registries()
    path = await live_episode(tmp_path, monkeypatch, EP)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    for var in ("SHOTS_JSON_PATH", "REFS_JSON_PATH", "PROMPT_LINT_KIT_PATH", "MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON"):
        monkeypatch.delenv(var, raising=False)
    yield path
    reset_registries()


def _assumed(text: str) -> set[str]:
    return ASSUMED & set(re.findall(r"[a-z0-9]+", text.lower()))


def _night_shift() -> tuple[dict, dict, dict]:
    """Two people and a robot, locked in the episode's cast library (their shot_meta cast entries)."""
    ana = locked_character(EP, name="Ana", seed=110,
                           description="a woman in her thirties with short black hair, a yellow raincoat and green boots")
    ben = locked_character(EP, name="Ben", seed=120,
                           description="a tall man with a grey beard, round glasses and a blue knitted jumper")
    pip = locked_character(EP)  # a small round robot with a teal shell and one yellow antenna
    return ana, ben, pip


def _mara() -> dict:
    return locked_character(EP, name="Mara", seed=130,
                            description="a girl of about ten with red curly hair and a striped green scarf")


def _shot(sid: str, *cast: dict, **kw) -> dict:
    """One shots.json shot whose declared characters (``cast``) are all in frame."""
    shot = {"id": sid, "count": len(cast) or 1, "characters": [], "wet": False, "exterior": False, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "prompt": f"Shot {sid}: late at night in a small repair shop.", "ref_keys": []}
    if cast:
        shot["cast"] = [c["character"] for c in cast]
    shot.update(kw)
    return shot


def _plans(*shots: dict) -> dict:
    """The shot plans the pipeline builds from this shots.json (cast library entries resolved)."""
    folder = episode_dir(EP)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": EP, "shots": list(shots)}))
    return load_shot_plans(EP, require=False)


def _still(tmp_path, shot: str, seed: int = 0):
    """``seed`` changes the figure's colour too, so the crops (and the identity composites) differ."""
    return image(tmp_path / "episodes" / EP / "outputs" / shot / f"still_v{seed + 1}.png",
                 color=(90 + seed * 7, 140, 200 - seed * 5), seed=seed)


def _answer(counts: dict, unexpected: int = 0, background: int = 0, mismatched: tuple = (), confidence: float = 0.9):
    """The counter's answer: {cast id: count}; ``mismatched``: the ids that do not look like their sheet."""
    return {"figures": {fid: {"count": n, "matches": fid not in mismatched} for fid, n in counts.items()},
            "unexpected": unexpected, "background": background, "confidence": confidence, "notes": "counted"}


def _counter(*answers):
    client = FakeJudgeClient(*answers)
    return VisionCastCounter(JudgeService(client)), client


def _ctx(shot: str) -> dict:
    return {"episode_id": EP, "shot_id": shot}


def _rows(res: dict) -> list[tuple]:
    return [(c["label"], c.get("counted"), c["status"]) for c in res["checks"]]


# ------------------------------------------------------------------------------------------ judge instructions


def test_no_judge_instruction_names_a_species_or_a_look():
    labels = [label for label, _ in identity.SHEET_PANEL_ROLES]
    instructions = {"still review": still_review.SYSTEM, "headcount": vision_counter.SYSTEM,
                    "qc_fail_if": vision_constraints.SYSTEM, "find the characters": identity.DETECT_SYSTEM,
                    "identity": judge_system(GENERIC_PROFILE, labels)}
    assert {what: _assumed(text) for what, text in instructions.items()} == {what: set() for what in instructions}
    assert set(GENERIC_PROFILE.signs) == {"face", "surface", "colours", "outfit", "proportions"}


# ------------------------------------------------------------------------------------------ headcount


async def test_two_people_and_a_robot_are_counted_one_by_one(db, tmp_path):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N01", ana, ben, pip))["N01"]["shot_meta"]
    roster = shot_roster(meta)
    assert [(f.id, f.name, f.expected, f.locked, f.group) for f in roster.figures] == [
        (ana["character"], "Ana", 1, True, False), (ben["character"], "Ben", 1, True, False),
        (pip["character"], "Pip", 1, True, False)]
    assert roster.note is None

    counter, client = _counter(_answer({ana["character"]: 1, ben["character"]: 1, pip["character"]: 1}))
    res = await check_still_headcount(counter, _still(tmp_path, "N01"), meta, shot_context=_ctx("N01"))
    assert res["status"] == "PASS", res
    assert _rows(res) == [("Ana", 1, "PASS"), ("Ben", 1, "PASS"), ("Pip", 1, "PASS"),
                          ("unexpected figures", 0, "PASS")]
    (request,) = client.calls
    for entry in (ana, ben, pip):
        assert f"- {entry['character']} ({entry['name']}): {entry['clause']}" in request["text"]
    assert ("Reference images after the frames, in order: "
            f"{ana['character']} (Ana), {ben['character']} (Ben), {pip['character']} (Pip).") in request["text"]
    assert len(request["images"]) == 4  # the still and one sheet image per character
    assert "_cast" not in request["text"] and not _assumed(request["system"])


async def test_each_sheet_is_labelled_with_the_id_and_name_of_its_figure(db, tmp_path):
    guard = locked_character(EP, name="Marisol", seed=160, slug="guard-a",
                             description="a night guard in a navy uniform with a torch on her belt")
    robot = locked_character(EP, name="Unit 7", seed=170, slug="robot")
    meta = _plans(_shot("N05", guard, robot))["N05"]["shot_meta"]
    counter, client = _counter(_answer({"guard-a": 1, "robot": 1}))
    res = await check_still_headcount(counter, _still(tmp_path, "N05"), meta, shot_context=_ctx("N05"))
    assert res["status"] == "PASS", res
    (request,) = client.calls
    assert f"- guard-a (Marisol): {guard['clause']}" in request["text"]
    assert f"- robot (Unit 7): {robot['clause']}" in request["text"]
    assert "Reference images after the frames, in order: guard-a (Marisol), robot (Unit 7)." in request["text"]
    assert len(request["images"]) == 3


@pytest.mark.parametrize("who, counts, reason", [
    ("pip", {"pip": 2}, "Pip: expected 1, counted 2"),
    ("ben", {"ben": 0}, "Ben: expected 1, counted 0"),
    ("stranger", {"unexpected": 1}, "unexpected figures: expected 0, counted 1"),
], ids=["a-second-robot", "a-person-missing", "a-stranger-in-the-shot"])
async def test_a_second_robot_a_missing_person_or_a_stranger_fails(db, tmp_path, who, counts, reason):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N01", ana, ben, pip))["N01"]["shot_meta"]
    ids = {"ana": ana["character"], "ben": ben["character"], "pip": pip["character"]}
    figures = {ids[k]: counts.get(k, 1) for k in ids}
    counter, _client = _counter(_answer(figures, unexpected=counts.get("unexpected", 0)))
    res = await check_still_headcount(counter, _still(tmp_path, "N01"), meta, shot_context=_ctx("N01"))
    assert res["status"] == "FAIL" and res["reason"] == reason, res


async def test_a_figure_that_may_not_be_the_declared_character_goes_to_a_person(db, tmp_path):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N01", ana, ben, pip))["N01"]["shot_meta"]
    ids = [ana["character"], ben["character"], pip["character"]]
    counter, _client = _counter(_answer(dict.fromkeys(ids, 1), mismatched=(ben["character"],)))
    res = await check_still_headcount(counter, _still(tmp_path, "N01"), meta, shot_context=_ctx("N01"))
    assert res["status"] == "NEEDS_REVIEW", res
    assert res["reason"] == "Ben: counted, but may not be the declared character"
    assert [c.get("matches") for c in res["checks"][:3]] == [None, False, None]

    # the count decides first: a second figure under Ben's id fails, whatever it looks like
    counter, _client = _counter(_answer({**dict.fromkeys(ids, 1), ben["character"]: 2}, mismatched=(ben["character"],)))
    res = await check_still_headcount(counter, _still(tmp_path, "N01", seed=1), meta, shot_context=_ctx("N01"))
    assert res["status"] == "FAIL" and res["reason"] == "Ben: expected 1, counted 2", res

    # every counter answers in the same shape: the deterministic one and a clip too
    res = await check_headcount(FakeCastCounter(mismatched=(pip["character"],)), _still(tmp_path, "N01"), meta)
    assert res["status"] == "NEEDS_REVIEW" and res["reason"] == "Pip: counted, but may not be the declared character"


async def test_people_in_the_background_go_to_a_person(db, tmp_path):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N02", ana, ben, pip))["N02"]["shot_meta"]
    ids = [ana["character"], ben["character"], pip["character"]]
    counter, _client = _counter(_answer(dict.fromkeys(ids, 1), background=5))
    res = await check_still_headcount(counter, _still(tmp_path, "N02"), meta, shot_context=_ctx("N02"))
    assert res["status"] == "NEEDS_REVIEW" and res["reason"] == "background figures nobody declared: 5 (a person decides)"


async def test_a_person_with_a_lamp_is_counted_and_the_lamp_is_not_a_figure(db, tmp_path):
    mara = _mara()
    lamp = new_prop(("episode", EP), name="Brass lamp", description="a brass desk lamp with a green glass shade")
    meta = _plans(_shot("L01", mara, props=[lamp["slug"]]))["L01"]["shot_meta"]
    assert [p["slug"] for p in meta["props"]] == [lamp["slug"]]
    assert [f.id for f in shot_roster(meta).figures] == [mara["character"]]

    counter, client = _counter(_answer({mara["character"]: 1}))
    res = await check_still_headcount(counter, _still(tmp_path, "L01"), meta, shot_context=_ctx("L01"))
    assert res["status"] == "PASS" and _rows(res) == [("Mara", 1, "PASS"), ("unexpected figures", 0, "PASS")], res
    text = client.calls[0]["text"]
    assert "Not figures (props and the setting): Brass lamp: a brass desk lamp with a green glass shade." in text
    assert f"- {lamp['slug']}:" not in text and len(client.calls[0]["images"]) == 2


async def test_a_prop_that_acts_is_a_cast_entry_counted_and_judged_like_anyone(db, tmp_path):
    mara = _mara()
    kettle = locked_character(EP, name="Kettle", seed=150,
                              description="a dented copper kettle with two painted eyes and a spout that talks")
    meta = _plans(_shot("L02", mara, kettle))["L02"]["shot_meta"]
    assert [(f.name, f.locked) for f in shot_roster(meta).figures] == [("Mara", True), ("Kettle", True)]
    res = await check_headcount(FakeCastCounter({kettle["character"]: 0}), _still(tmp_path, "L02"), meta)
    assert res["status"] == "FAIL" and res["reason"] == "Kettle: expected 1, counted 0", res

    client = routed_client(detect=[detect_answer(mara["character"], kettle["character"])],
                           identity=[identity_answer(), identity_answer({"colours": 2})])
    _register_identity(client)
    res = await identity_review(_still(tmp_path, "L02"), kind="still", episode_id=EP, shot_id="L02", shot_meta=meta)
    assert res["status"] == "FAIL" and res["reason"].startswith("Kettle: Drift detected: colours"), res
    assert client.asked() == ["detect", "identity", "identity"]


async def test_a_shot_listing_more_characters_than_it_shows_goes_to_a_person(db, tmp_path):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N03", ana, ben, pip, count=2))["N03"]["shot_meta"]
    res = await check_headcount(FakeCastCounter(), _still(tmp_path, "N03"), meta)
    assert res["status"] == "NEEDS_REVIEW", res
    assert res["reason"] == ("the shot plan says 2 main character(s) in frame but its cast lists 3: "
                             "a person confirms who belongs in the shot")
    # more in frame than its cast: the others are one group, counted together
    meta = _plans(_shot("N04", ana, pip, count=4))["N04"]["shot_meta"]
    figures = shot_roster(meta).figures
    assert [(f.id, f.name, f.expected, f.group) for f in figures][-1] == ("_cast", "other cast", 2, True)
    res = await check_headcount(FakeCastCounter({"_cast": 3}), _still(tmp_path, "N04"), meta)
    assert res["status"] == "FAIL" and res["reason"] == "other cast: expected 2, counted 3", res


# ------------------------------------------------------------------------------------------ identity


def _register_identity(client) -> None:
    service = JudgeService(client)
    set_identity_judge(VisionCharacterDetector(service), VisionIdentityJudge(service))


async def test_two_people_and_a_robot_are_judged_with_one_set_of_rules(db, tmp_path):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N01", ana, ben, pip))["N01"]["shot_meta"]
    ids = [ana["character"], ben["character"], pip["character"]]
    client = routed_client(detect=[detect_answer(*ids)], identity=[identity_answer()] * 3)
    _register_identity(client)
    res = await identity_review(_still(tmp_path, "N01"), kind="still", episode_id=EP, shot_id="N01", shot_meta=meta)
    assert res["status"] == "PASS" and [r["name"] for r in res["characters"]] == ["Ana", "Ben", "Pip"], res
    assert client.asked() == ["detect", "identity", "identity", "identity"]
    for entry in (ana, ben, pip):
        assert f"- {entry['character']}: {entry['name']}." in client.calls[0]["text"]
    systems = {c["system"] for c in client.calls if c["judge"] == "identity"}
    assert len(systems) == 1  # the same signs for the people and the robot
    (system,) = systems
    assert all(f"- {sign}: " in system for sign in GENERIC_PROFILE.signs)
    assert not _assumed(system) and not _assumed(client.calls[0]["system"])


async def test_a_drifting_robot_fails_and_a_person_the_detector_is_unsure_of_goes_to_a_person(db, tmp_path):
    ana, ben, pip = _night_shift()
    meta = _plans(_shot("N01", ana, ben, pip))["N01"]["shot_meta"]
    ids = [ana["character"], ben["character"], pip["character"]]
    client = routed_client(detect=[detect_answer(*ids)],
                           identity=[identity_answer(), identity_answer(), identity_answer({"surface": 2})])
    _register_identity(client)
    res = await identity_review(_still(tmp_path, "N01"), kind="still", episode_id=EP, shot_id="N01", shot_meta=meta)
    assert res["status"] == "FAIL" and res["reason"].startswith("Pip: Drift detected: surface"), res
    assert [r["verdict"] for r in res["characters"]] == ["PASS", "PASS", "FAIL"]

    unsure = detect_answer(ana["character"], pip["character"])
    unsure["characters"].update(detect_answer(ben["character"], confidence=0.3)["characters"])
    client = routed_client(detect=[unsure], identity=[identity_answer(), identity_answer()])
    _register_identity(client)
    res = await identity_review(_still(tmp_path, "N01", seed=1), kind="still", episode_id=EP, shot_id="N01",
                                shot_meta=meta)
    assert res["status"] == "NEEDS_REVIEW", res
    assert res["reason"] == "Ben: detector unsure (confidence 0.30 < 0.6)"
    assert [r["verdict"] for r in res["characters"]] == ["PASS", "ESCALATE", "PASS"]


async def test_with_a_lamp_on_the_set_only_the_person_is_judged(db, tmp_path):
    mara = _mara()
    lamp = new_prop(("episode", EP), name="Brass lamp", description="a brass desk lamp with a green glass shade")
    meta = _plans(_shot("L01", mara, props=[lamp["slug"]]))["L01"]["shot_meta"]
    client = routed_client(detect=[detect_answer(mara["character"])], identity=[identity_answer()])
    _register_identity(client)
    res = await identity_review(_still(tmp_path, "L01"), kind="still", episode_id=EP, shot_id="L01", shot_meta=meta)
    assert res["status"] == "PASS" and [r["name"] for r in res["characters"]] == ["Mara"], res
    assert client.asked() == ["detect", "identity"] and lamp["slug"] not in client.calls[0]["text"]


async def test_the_first_series_duck_host_is_an_ordinary_locked_cast_entry(db, tmp_path):
    duck = locked_character(EP, name="Duck", seed=140,
                            description="a small duck in a cream tuque with a pom and a vest fading to olive")
    meta = _plans(_shot("D01", duck))["D01"]["shot_meta"]
    assert not meta.get("duck")  # declared by its cast entry, not by duck_present
    assert [f.id for f in shot_roster(meta).figures] == [duck["character"]]
    counter, _client = _counter(_answer({duck["character"]: 1}))
    res = await check_still_headcount(counter, _still(tmp_path, "D01"), meta, shot_context=_ctx("D01"))
    assert res["status"] == "PASS" and _rows(res)[0] == ("Duck", 1, "PASS"), res

    client = routed_client(detect=[detect_answer(duck["character"])], identity=[identity_answer()])
    _register_identity(client)
    res = await identity_review(_still(tmp_path, "D01"), kind="still", episode_id=EP, shot_id="D01", shot_meta=meta)
    assert res["status"] == "PASS", res
    system = client.calls[1]["system"]
    assert all(f"- {sign}: " in system for sign in GENERIC_PROFILE.signs) and "- bill:" not in system


# ------------------------------------------------------------------------------------------ budget


@pytest.mark.parametrize("locked", [0, 1, 2, 3, 4])
def test_a_take_with_n_locked_characters_can_ask_8_plus_4n_judge_calls(locked):
    calls = take_calls(ShotJudging(locked=locked, cast=locked, fail_if=True))
    assert len(calls) == (8 + 4 * locked if locked else 4), calls
    assert len(take_calls(ShotJudging(locked=locked, cast=locked))) == len(calls) - 1  # no qc_fail_if
    assert [what for what, _images in calls].count("identity: judge one character") == 4 * locked


def test_more_locked_characters_cost_more_and_the_line_grows_with_the_shots():
    takes = [take_usd_micros(ShotJudging(locked=n, cast=n, fail_if=True)) for n in range(5)]
    assert takes == sorted(set(takes))
    four = ShotJudging(locked=4, cast=4, fail_if=True)
    floor = usd_to_micros(DEFAULT_LINE_CAP_USD)
    assert line_cap_usd_micros([ShotJudging()]) == floor  # never below the fixed default
    caps = [line_cap_usd_micros([four] * shots) for shots in (12, 24, 48)]
    assert floor < caps[0] < caps[1] < caps[2]
    for shots, cap in zip((12, 24, 48), caps):
        stop = compute_stop_usd_micros(cap, get_budget_settings().stop_percent)
        assert stop >= 2 * shots * takes[4]  # the stop holds two takes of every shot


async def test_an_auto_line_is_sized_from_the_shots_and_their_locked_characters(db, monkeypatch):
    ana, ben, pip = _night_shift()
    mara = _mara()
    four = [ana, ben, pip, mara]
    shots = [_shot(f"N{i:02d}", *four, qc_fail_if=["a fifth figure walks in"]) for i in range(1, 13)]
    shots += [_shot("N13", pip), _shot("N14")]  # the robot alone; the shop empty but for one extra
    _plans(*shots)
    expected = line_cap_usd_micros([ShotJudging(locked=4, cast=4, fail_if=True)] * 12
                                   + [ShotJudging(locked=1, cast=1), ShotJudging()])
    assert episode_line_cap_usd_micros(EP) == expected > usd_to_micros(DEFAULT_LINE_CAP_USD)

    monkeypatch.setenv("HFVG_QC_LINE_CAP_USD", "auto")
    call = JudgeCall(kind="test_judge", episode_id=EP, subject="N01 still_v1.png", system="Answer with JSON.",
                     text="Is it blue?", images=[b"image"], key_parts={"media": "sha-1"})
    assert await JudgeService(FakeJudgeClient({"ok": True})).ask(call) == {"ok": True}
    line = qc_line(db, EP)
    assert (line["cap"], line["stop"]) == (expected, compute_stop_usd_micros(expected, 80))


async def test_an_auto_line_without_a_shot_plan_gets_the_default_cap_and_keeps_it(db, monkeypatch):
    monkeypatch.setenv("HFVG_QC_LINE_CAP_USD", "auto")

    def call(media):
        return JudgeCall(kind="test_judge", episode_id=EP, subject="N01 still_v1.png", system="Answer with JSON.",
                         text="Is it blue?", images=[b"image"], key_parts={"media": media})

    service = JudgeService(FakeJudgeClient({"ok": True}, {"ok": True}))
    await service.ask(call("sha-1"))
    assert qc_line(db, EP)["cap"] == usd_to_micros(DEFAULT_LINE_CAP_USD)
    pip = locked_character(EP)
    _plans(*[_shot(f"N{i:02d}", pip) for i in range(1, 60)])
    await service.ask(call("sha-2"))
    assert qc_line(db, EP)["cap"] == usd_to_micros(DEFAULT_LINE_CAP_USD)  # an existing line keeps its cap


def _line_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "hfvg.qc.judges" and "cap is below" in r.getMessage()]


async def test_a_fixed_line_smaller_than_the_shot_plan_needs_is_kept_and_logged(db, monkeypatch, caplog):
    ana, ben, pip = _night_shift()
    mara = _mara()
    _plans(*[_shot(f"N{i:02d}", ana, ben, pip, mara, qc_fail_if=["a fifth figure walks in"]) for i in range(1, 13)])
    sized = episode_line_cap_usd_micros(EP)
    monkeypatch.delenv("HFVG_QC_LINE_CAP_USD", raising=False)

    def call(media):
        return JudgeCall(kind="test_judge", episode_id=EP, subject="N01 still_v1.png", system="Answer with JSON.",
                         text="Is it blue?", images=[b"image"], key_parts={"media": media})

    service = JudgeService(FakeJudgeClient({"ok": True}, {"ok": True}))
    with caplog.at_level(logging.WARNING, logger="hfvg.qc.judges"):
        await service.ask(call("sha-1"))
        await service.ask(call("sha-2"))
    assert qc_line(db, EP)["cap"] == usd_to_micros(DEFAULT_LINE_CAP_USD)  # kept: no spend without a config change
    needs = micros_to_usd_str(-(-sized // 10_000) * 10_000)
    assert _line_warnings(caplog) == [  # once, when the line is created
        f"QC judge line of {EP}: its $0.50 cap is below the ${needs} its shot plan needs for 2 takes of every "
        "shot; checks past the line's stop go to a person (HFVG_QC_LINE_CAP_USD=auto sizes the line from the plan)"]


async def test_a_fixed_line_the_shot_plan_fits_in_logs_nothing(db, monkeypatch, caplog):
    _plans(_shot("N01", locked_character(EP)))
    monkeypatch.delenv("HFVG_QC_LINE_CAP_USD", raising=False)
    call = JudgeCall(kind="test_judge", episode_id=EP, subject="N01 still_v1.png", system="Answer with JSON.",
                     text="Is it blue?", images=[b"image"], key_parts={"media": "sha-1"})
    with caplog.at_level(logging.WARNING, logger="hfvg.qc.judges"):
        assert await JudgeService(FakeJudgeClient({"ok": True})).ask(call) == {"ok": True}
    assert qc_line(db, EP)["cap"] == usd_to_micros(DEFAULT_LINE_CAP_USD)
    assert _line_warnings(caplog) == []
