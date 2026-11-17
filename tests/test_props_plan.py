"""Props and places in shot planning and prompts: a shot's ``props`` and ``place`` (hfvg.prop_store)
become reference photos, ref keys and ``shot_meta["props"]``; the still submit injects their
descriptions (and the lint requires them), the clip gets one "keep" line naming them, the G1 plan,
lint preview and preflight see them, the live still sends their photos to the provider, and shots
that pick nothing plan exactly as before."""

from __future__ import annotations

import io
import json

import httpx
import pytest
import respx
from PIL import Image

from hfvg import cast_plan, preflight, prop_store
from hfvg.cast_prompt import (CAST_LEAD_IN, NO_TEXT_CLAUSE, PROPS_LEAD_IN, inject_cast_prompt, prop_clause,
                              props_clip_lock, with_cast_prompts)
from hfvg.paths import episode_dir
from hfvg.prompt_lint import (clip_lint_violations, inject_continuity_locks, lint_summary, lint_violations,
                              load_lint_kit)
from hfvg.shot_data import ShotDataError, load_shot_plans, merge_into_shots, parse_shots, resolve_refs
from tests.cast_helpers import new_character, new_place, new_prop, prop_photo, upload
from tests.lint_helpers import acapture, lint_ok

EP = "ep41cast"
LIB = ("episode", EP)
GROK = "xai/grok-imagine-image-2.0"
KEEP = "Keep these exactly as they look in the first frame: Harbour cabin, Red umbrella."
CABIN = "Harbour cabin (the setting): a small blue wooden cabin on stilts at the end of a pier."
UMBRELLA = "Red umbrella: a red umbrella with a curved wooden handle."


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON", "SHOTS_JSON_PATH", "REFS_JSON_PATH"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "private-cast"


def _shot(sid="A01", **kw) -> dict:
    shot = {"id": sid, "count": 1, "characters": [], "wet": False, "exterior": False, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "prompt": lint_ok(f"A quiet harbour at dawn, shot {sid}"), "ref_keys": []}
    shot.update(kw)
    return shot


def _write(shots: list[dict], refs: dict | None = None, episode: str = EP):
    folder = episode_dir(episode)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": episode, "shots": shots}))
    (folder / "refs.json").write_text(json.dumps({"episode_id": episode, "per_shot": refs or {}}))
    return folder


def _cap(monkeypatch, cap: int):
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({GROK: cap}))


def _cabin_and_umbrella(library=LIB, photos=True):
    cabin, umbrella = new_place(library), new_prop(library)
    if not photos:
        return cabin, umbrella, None, None
    return cabin, umbrella, prop_photo(cabin, seed=1, color=(40, 60, 220)), prop_photo(umbrella, seed=2)


def _parse_refusal(episode: str = EP) -> str:
    """Why the run's parse refuses the episode's shots.json ("" when it plans)."""
    try:
        load_shot_plans(episode, require=True)
    except ShotDataError as e:
        return str(e)
    return ""


def _meta(**kw) -> dict:
    meta = {"count": 1, "exterior": False, "water": False, "characters": [],
            "props": [{"slug": "harbour-cabin", "kind": "place", "name": "Harbour cabin", "clause": CABIN},
                      {"slug": "red-umbrella", "kind": "prop", "name": "Red umbrella", "clause": UMBRELLA}]}
    meta.update(kw)
    return meta


# ------------------------------------------------------------------------------------- shots.json


def test_props_and_place_are_read_from_shots_json():
    plans = parse_shots({"shots": [_shot("A01", props=["red-umbrella", " oar "], place="harbour-cabin"),
                                   _shot("A02", props=[])]})
    assert (plans["A01"]["props"], plans["A01"]["place"]) == (["red-umbrella", "oar"], "harbour-cabin")
    assert not {"props", "place"} & set(plans["A02"])


@pytest.mark.parametrize("fields,message", [
    ({"props": "red-umbrella"}, "props must be a list of prop ids"),
    ({"props": ["a", "b", "c", "d", "e"]}, "props lists 5 props; at most 4 per shot"),
    ({"props": ["Red Umbrella"]}, r"props\[0\] 'Red Umbrella' is not a prop id"),
    ({"props": [{"prop": "oar"}]}, "is not a prop id"),
    ({"props": ["oar", "oar"]}, "prop 'oar' is listed twice"),
    ({"place": "Harbour Cabin"}, "place 'Harbour Cabin' is not a place id"),
    ({"place": ["harbour-cabin"]}, "is not a place id"),
    ({"props": ["harbour-cabin"], "place": "harbour-cabin"}, "both the shot's place and one of its props"),
])
def test_invalid_props_and_places_are_refused(fields, message):
    with pytest.raises(ShotDataError, match=f"shots.json shot A01: .*{message}"):
        parse_shots({"shots": [_shot("A01", **fields)]})


# ------------------------------------------------------------------------------------- planning


def test_a_shot_with_a_place_and_a_prop_gets_their_photos_and_descriptions(tmp_path):
    _cabin, _umbrella, c1, u1 = _cabin_and_umbrella()
    ref = tmp_path / "harbor.png"
    ref.write_bytes(b"synthetic")
    _write([_shot("A01", ref_keys=["harbor"], props=["red-umbrella"], place="harbour-cabin")],
           {"A01": [{"key": "harbor", "path": str(ref)}]})
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == ["harbor", cast_plan.image_key(c1), cast_plan.image_key(u1)]
    assert plan["refs"] == [str(ref), str(prop_store.photo_path(c1)), str(prop_store.photo_path(u1))]
    assert plan["missing_refs"] == []
    assert plan["shot_meta"]["props"] == _meta()["props"]
    assert "cast" not in plan["shot_meta"] and "text_policy" not in plan["shot_meta"]
    merged = merge_into_shots(EP, [{"shot_id": "A01", "params": {}, "tags": []}], load_shot_plans(EP, require=True),
                              require=True)[0]
    assert merged["ref_keys"] == plan["ref_keys"] and merged["shot_meta"]["props"] == plan["shot_meta"]["props"]


def test_shots_that_pick_nothing_plan_exactly_as_before_and_never_read_the_props(monkeypatch):
    _cabin_and_umbrella()
    pip = new_character(LIB, name="Pip")
    image = upload(pip, seed=5)
    shots = [_shot("A01", cast=["pip"]), _shot("A02")]
    _write(shots, {"A01": [], "A02": []})

    def untouched(*_a, **_k):
        raise AssertionError("the props library must not be read when no shot picks from it")
    monkeypatch.setattr(prop_store, "list_items", untouched)
    plans = load_shot_plans(EP, require=True)
    assert plans["A01"]["ref_keys"] == [cast_plan.image_key(image)]
    assert set(plans["A01"]["shot_meta"]) - set(plans["A02"]["shot_meta"]) == {"cast", "text_policy"}
    expected = parse_shots({"shots": shots})["A02"]
    expected.update(resolve_refs(EP, {"A02": []})["A02"])
    assert plans["A02"] == expected
    still = with_cast_prompts([{"shot_id": "A01", **plans["A01"]}])[0]["prompt"]
    assert CAST_LEAD_IN in still and PROPS_LEAD_IN not in still
    assert props_clip_lock(plans["A01"]["shot_meta"]) is None


def test_each_place_and_prop_gets_its_main_photo_before_anyones_second_picture():
    pip = new_character(LIB, name="Pip")
    p1 = upload(pip, seed=1)
    cabin, _umbrella, c1, u1 = _cabin_and_umbrella()
    c2 = prop_photo(cabin, seed=3)
    shots = [_shot("A01", cast=["pip"], props=["red-umbrella"], place="harbour-cabin")]
    _write(shots, {"A01": []})
    # Grok Image 2.0 takes 5 refs, one slot is reserved: these four fit
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == [cast_plan.image_key(i) for i in (p1, c1, u1, c2)]
    assert [p["kind"] for p in plan["shot_meta"]["props"]] == ["place", "prop"]
    # a fifth: the first pictures stay, the second ones share a grid sheet
    p2 = upload(pip, seed=2)
    plan = load_shot_plans(EP, require=True)["A01"]
    keys = plan["ref_keys"]
    assert keys[:3] == [cast_plan.image_key(i) for i in (p1, c1, u1)] and keys[3].startswith(cast_plan.COMP_PREFIX)
    grid = json.loads(cast_plan.resolve_cast_key(keys[3]).with_suffix(".json").read_text())
    assert grid["kind"] == "grid" and grid["sources"] == [p2["sha256"], c2["sha256"]]
    report = cast_plan.preview_episode_cast(EP, {"shots": shots}, live=False)["shots"]["A01"]
    assert [r["label"] for r in report["refs"]] == [
        "Pip reference", "Harbour cabin photo", "Red umbrella photo",
        "grid sheet: Pip reference, Harbour cabin photo"]
    assert "2 reference images were merged into one grid sheet to stay within the still model's limit" in report["notes"]
    assert report["props"] == [
        {"slug": "harbour-cabin", "kind": "place", "name": "Harbour cabin", "images": [c1["image_id"], c2["image_id"]]},
        {"slug": "red-umbrella", "kind": "prop", "name": "Red umbrella", "images": [u1["image_id"]]}]
    assert (report["prop_ids"], report["place"]) == (["red-umbrella"], "harbour-cabin")


def test_with_one_slot_left_every_photo_goes_into_one_grid_sheet(monkeypatch, tmp_path):
    _cabin_and_umbrella()
    ref = tmp_path / "harbor.png"
    ref.write_bytes(b"synthetic")
    _write([_shot("A01", ref_keys=["harbor"], props=["red-umbrella"], place="harbour-cabin")],
           {"A01": [{"key": "harbor", "path": str(ref)}]})
    _cap(monkeypatch, 3)
    keys = load_shot_plans(EP, require=True)["A01"]["ref_keys"]
    assert keys[0] == "harbor" and len(keys) == 2 and keys[1].startswith(cast_plan.COMP_PREFIX)
    _cap(monkeypatch, 2)  # the shot's own ref fills the limit: descriptions only
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == ["harbor"] and [p["slug"] for p in plan["shot_meta"]["props"]] == [
        "harbour-cabin", "red-umbrella"]
    notes = cast_plan.preview_episode_cast(EP, {"shots": [_shot("A01", ref_keys=["harbor"], props=["red-umbrella"])]},
                                           live=False)["shots"]["A01"]["notes"]
    assert any(n.startswith("no room for reference images") for n in notes)


def test_a_prop_without_a_photo_still_gets_its_description():
    _cabin_and_umbrella(photos=False)
    shots = [_shot("A01", props=["red-umbrella"])]
    _write(shots, {"A01": []})
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == [] and plan["shot_meta"]["props"][0]["clause"] == UMBRELLA
    notes = cast_plan.preview_episode_cast(EP, {"shots": shots}, live=False)["shots"]["A01"]["notes"]
    assert "Red umbrella has no photo yet: only the description is used" in notes


def test_unknown_ids_and_the_wrong_kind_refuse_the_plan():
    _cabin_and_umbrella()
    cases = [
        (_shot("A01", props=["oar"]),
         "shot A01: prop 'oar' is not in the props and places of this episode (add it in Props & places)"),
        (_shot("A01", place="lighthouse"),
         "shot A01: place 'lighthouse' is not in the props and places of this episode (add it in Props & places)"),
        (_shot("A01", props=["harbour-cabin"]),
         "shot A01: Harbour cabin is a place, not a prop; pick it as the shot's place"),
        (_shot("A01", place="red-umbrella"),
         "shot A01: Red umbrella is a prop, not a place; pick it as one of the shot's props"),
    ]
    for shot, problem in cases:
        _write([shot], {"A01": []})
        assert cast_plan.cast_problems(EP, {"shots": [shot]}) == [problem]
        assert problem in _parse_refusal()


def test_a_series_episode_uses_the_series_props():
    from hfvg import cast_store
    series = cast_store.create_series("Harbour tales", None, "t")
    cast_store.link_episode("ep42cast", series["series_id"], "t")
    _cabin, _umbrella, c1, _u1 = _cabin_and_umbrella(("series", series["series_id"]))
    _write([_shot("A01", place="harbour-cabin")], {"A01": []}, episode="ep42cast")
    assert load_shot_plans("ep42cast", require=True)["A01"]["ref_keys"] == [cast_plan.image_key(c1)]
    _write([_shot("A01", place="harbour-cabin")], {"A01": []})  # ep41cast is not in the series
    with pytest.raises(ShotDataError, match="not in the props and places of this episode"):
        load_shot_plans(EP, require=True)


def test_prop_keys_resolve_at_the_recheck_and_a_deleted_photo_holds_the_shot():
    _cabin, _umbrella, c1, u1 = _cabin_and_umbrella()
    keys = [cast_plan.image_key(c1), cast_plan.image_key(u1)]
    _write([_shot("A01")], {"A01": []})
    assert resolve_refs(EP, {"A01": keys})["A01"] == {
        "refs": [str(prop_store.photo_path(c1)), str(prop_store.photo_path(u1))], "missing_refs": []}
    prop_store.delete_photo(u1["image_id"], "t")
    assert resolve_refs(EP, {"A01": keys})["A01"]["missing_refs"] == [keys[1]]


# ------------------------------------------------------------------------------------- prompts and lint


def test_the_still_prompt_gets_the_descriptions_after_the_cast_once():
    meta = _meta(cast=[{"character": "pip", "version": 1, "name": "Pip", "locked": False,
                        "clause": "Pip: a small round robot."}], text_policy="no_text")
    out, added = inject_cast_prompt("A quiet harbour at dawn.", meta)
    assert out == (f"A quiet harbour at dawn. {CAST_LEAD_IN} Pip: a small round robot. {PROPS_LEAD_IN} "
                   f"{CABIN} {UMBRELLA} {NO_TEXT_CLAUSE}")
    assert added == ["cast:pip", "place:harbour-cabin", "prop:red-umbrella", "no_text"]
    assert inject_cast_prompt(out, meta) == (out, [])
    only_props, added = inject_cast_prompt("A quiet harbour at dawn.", _meta())
    assert only_props == f"A quiet harbour at dawn. {PROPS_LEAD_IN} {CABIN} {UMBRELLA}"
    assert added == ["place:harbour-cabin", "prop:red-umbrella"]
    assert prop_clause(" Oar ", "a long oar.\n", "prop") == "Oar: a long oar."
    assert prop_clause("Pier", "", "place") == "Pier (the setting)."


def test_the_still_lint_requires_the_descriptions_and_the_clip_lint_the_keep_line():
    kit = load_lint_kit(EP)
    prompt = lint_ok("A quiet harbour at dawn")
    meta = _meta()
    assert lint_violations(prompt, meta, kit) == [
        "missing the description of place 'harbour-cabin' (the still submit injects it)",
        "missing the description of prop 'red-umbrella' (the still submit injects it)"]
    still, _ = inject_cast_prompt(prompt, meta)
    assert lint_violations(still, meta, kit) == []
    assert clip_lint_violations(prompt, meta, kit) == ["missing continuity lock 'shot-props' (clip)"]
    clip, added = inject_continuity_locks(prompt, meta, kit, "clip")
    assert clip == f"{prompt} {KEEP}" and added == ["shot-props"]
    assert clip_lint_violations(clip, meta, kit) == []
    assert inject_continuity_locks(prompt, meta, kit, "still") == (prompt, [])  # the still gets the descriptions
    assert props_clip_lock({"props": []}) is None and props_clip_lock(None) is None


def test_the_g1_lint_preview_injects_the_props_and_still_applies_the_kit():
    plans = {"A01": {"prompt": lint_ok("A quiet harbour at dawn"), "clip_prompt": None, "shot_meta": _meta()}}
    assert lint_summary(EP, plans)["ok"] is True
    banned = _meta(props=[{"slug": "paper-light", "kind": "prop", "name": "Paper light",
                           "clause": "Paper light: a paper lantern on a hook."}])
    summary = lint_summary(EP, {"A01": {**plans["A01"], "shot_meta": banned}})
    assert summary["ok"] is False and any("lantern" in p for p in summary["shots"]["A01"])


def test_the_g1_plan_shows_the_props_and_the_prompts_as_sent():
    from hfvg.activities.g1_plan import _cast_preview
    _cabin_and_umbrella()
    _write([_shot("A01", props=["red-umbrella"], place="harbour-cabin"), _shot("A02")], {"A01": [], "A02": []})
    merged = merge_into_shots(EP, [{"shot_id": s, "params": {}, "tags": []} for s in ("A01", "A02")],
                              load_shot_plans(EP, require=True), require=True)
    assert _cast_preview(merged) == {"A01": {
        "characters": [], "cast_refs": 2,
        "props": [{"slug": "harbour-cabin", "kind": "place", "name": "Harbour cabin"},
                  {"slug": "red-umbrella", "kind": "prop", "name": "Red umbrella"}]}}
    shown = with_cast_prompts(merged)
    assert shown[0]["prompt"].endswith(f"{PROPS_LEAD_IN} {CABIN} {UMBRELLA}")
    assert shown[1] is merged[1]


def test_the_preflight_measures_the_clip_prompt_with_the_keep_line():
    plan = {"shot_id": "A01", "prompt": "p", "clip_prompt": "c" * 2440, "shot_meta": _meta(),
            "params": {"model": "kling", "duration": 4, "aspect_ratio": "9:16"}}
    assert preflight.shot_inputs(plan)["clip_prompt"] == "c" * 2440 + " " + KEEP
    assert preflight.shot_inputs({**plan, "clip_prompt": f"c {KEEP}"})["clip_prompt"] == f"c {KEEP}"
    bare = {k: v for k, v in plan.items() if k != "shot_meta"}
    assert preflight.shot_inputs(bare)["clip_prompt"] == "c" * 2440
    assert preflight.preflight_plans([bare])["shots"]["A01"]["ok"] is True
    report = preflight.preflight_plans([plan])["shots"]["A01"]
    assert report["ok"] is False and any("2500" in e or "2,500" in e for e in report["errors"]), report


# ------------------------------------------------------------------------------------- submits


@pytest.mark.asyncio
async def test_the_dry_clip_submit_carries_the_keep_line(monkeypatch):
    from hfvg.activities import studio_generation as sg

    async def dry(_episode):
        return False, False
    seen = {}

    def capture(_episode, prompt, _meta, live, kind="still"):
        seen.update(prompt=prompt, live=live, kind=kind)
        raise RuntimeError("captured")
    monkeypatch.setattr(sg, "check_live_mode_and_g108", dry)
    monkeypatch.setattr(sg, "enforce_prompt_lint", capture)
    prompt = lint_ok("The umbrella opens")
    with pytest.raises(RuntimeError, match="captured"):
        await sg.submit_clip_job_enforced(EP, "A01", "/fake/still.jpg", prompt, 4, 1, None, _meta(), "kling", [])
    assert seen == {"prompt": f"{prompt} {KEEP}", "live": False, "kind": "clip"}


COLORS = {"place": (40, 60, 220), "prop": (200, 60, 60)}


def _colour_of(data: bytes) -> str:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    px = img.getpixel((img.width // 2, img.height // 2))
    best = min(COLORS, key=lambda k: sum((a - b) ** 2 for a, b in zip(COLORS[k], px)))
    assert sum((a - b) ** 2 for a, b in zip(COLORS[best], px)) < 900, f"unrecognized ref colour {px}"
    return best


@pytest.mark.asyncio
@respx.mock
async def test_a_live_still_sends_the_photos_of_its_place_and_props_and_their_descriptions(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from tests.test_round4_shot_data import _live_env

    await _live_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    cabin, umbrella = new_place(("episode", "ep99")), new_prop(("episode", "ep99"))
    prop_photo(cabin, seed=1, color=COLORS["place"])
    prop_photo(umbrella, seed=2, color=COLORS["prop"])
    _write([_shot("A01", props=["red-umbrella"], place="harbour-cabin")], {"A01": []}, episode="ep99")
    plan = load_shot_plans("ep99", require=True)["A01"]
    assert len(plan["refs"]) == 2 and plan["missing_refs"] == []

    puts, n = {}, {"i": 0}

    def upload_url(_req):
        n["i"] += 1
        return httpx.Response(200, json={"upload_url": f"http://mock.upload/put{n['i']}",
                                         "public_url": f"https://cdn.upload/ref{n['i']}.jpg",
                                         "upload_headers": {"Content-Type": "image/jpeg"}})
    respx.post("https://api.higgsfield.ai/files/generate-upload-url").mock(side_effect=upload_url)
    respx.put(url__regex=r"http://mock\.upload/put\d+").mock(
        side_effect=lambda r: (puts.__setitem__(str(r.url).rsplit("put", 1)[1], r.content), httpx.Response(200))[1])
    est = respx.post(f"https://api.higgsfield.ai/estimate/{GROK}").mock(
        return_value=httpx.Response(200, json={"credits": "0.96", "usd": "0.06"}))
    sub = respx.post(f"https://api.higgsfield.ai/{GROK}").mock(return_value=httpx.Response(200, json={"request_id": "r1"}))

    async def run():
        await submit_still_job_enforced("ep99", "A01", plan["prompt"], 1, plan["refs"], "1k", "medium", "9:16",
                                        None, plan["shot_meta"], plan["missing_refs"], 4, None)
    assert await acapture(ValueError, run()) is None
    assert est.call_count == 1 and sub.call_count == 1
    body = json.loads(sub.calls[0].request.content)
    sent = [_colour_of(puts[u.rsplit("ref", 1)[1].replace(".jpg", "")]) for u in body["image_urls"]]
    assert sent == ["place", "prop"]
    assert f"{PROPS_LEAD_IN} {CABIN} {UMBRELLA}" in json.dumps(body)
