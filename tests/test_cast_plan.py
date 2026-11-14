"""Cast planning (hfvg/cast_plan.py via hfvg/shot_data.load_shot_plans): a shot's ``cast`` becomes
identity refs, ref keys and prompt facts; lineups for two or more characters, the still model's max
refs with a grid sheet when they do not fit, locked sheets, the text policy, and shots without a cast
exactly as before."""

from __future__ import annotations

import copy
import json

import pytest

from hfvg import cast_plan, cast_store
from hfvg.cast_prompt import NO_TEXT_CLAUSE
from hfvg.paths import episode_dir
from hfvg.shot_data import ShotDataError, load_shot_plans, merge_into_shots, parse_shots, resolve_refs
from tests.cast_helpers import complete_sheet, new_character, upload

EP = "ep41cast"
STILL_MODEL = "xai/grok-imagine-image-2.0"


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.delenv("MODEL_PATH_STILL", raising=False)
    monkeypatch.delenv("MODEL_MAX_REFS_JSON", raising=False)
    for var in ("SHOTS_JSON_PATH", "REFS_JSON_PATH", "PROMPT_LINT_KIT_PATH"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "private-cast"


def _shot(sid="A01", **kw) -> dict:
    shot = {"id": sid, "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "prompt": f"A quiet harbor at dawn, shot {sid}.", "ref_keys": []}
    shot.update(kw)
    return shot


def _write(shots: list[dict], refs: dict | None = None, kit: dict | None = None, episode: str = EP):
    folder = episode_dir(episode)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": episode, "shots": shots}))
    if refs is not None:
        (folder / "refs.json").write_text(json.dumps({"episode_id": episode, "per_shot": refs}))
    if kit is not None:
        (folder / "prompt_kit.json").write_text(json.dumps(kit))
    return folder


def _cap(monkeypatch, cap: int):
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({STILL_MODEL: cap}))


def _cast_keys(plan: dict) -> list[str]:
    return [k for k in plan["ref_keys"] if k.startswith("cast:")]


# ------------------------------------------------------------------------------------- legacy shots


def test_shots_without_a_cast_plan_exactly_as_before(tmp_path, monkeypatch):
    ref = tmp_path / "harbor.png"
    ref.write_bytes(b"synthetic")
    shots = [_shot("A01", ref_keys=["harbor"]), _shot("A02")]
    refs = {"A01": [{"key": "harbor", "path": str(ref)}], "A02": []}
    _write(shots, refs, kit={"lint": "default", "characters": {"A": "a heron"}})

    def untouched(*_a, **_k):
        raise AssertionError("the cast library must not be read for shots without a cast")
    monkeypatch.setattr(cast_store, "library_for_episode", untouched)
    plans = load_shot_plans(EP, require=True)
    expected = parse_shots({"shots": shots})
    for sid, plan in expected.items():
        plan.update(resolve_refs(EP, {sid: plan["ref_keys"]})[sid])
    assert plans == expected
    assert plans["A01"]["refs"] == [str(ref)] and plans["A01"]["ref_keys"] == ["harbor"]
    for plan in plans.values():
        assert "cast" not in plan["shot_meta"] and "text_policy" not in plan["shot_meta"]
    assert cast_plan.cast_problems(EP, {"shots": shots}) == []


def test_a_dry_episode_without_refs_json_still_plans_its_cast():
    pip = new_character(("episode", EP), name="Pip")
    image = upload(pip, seed=1)
    _write([_shot("A01", cast=["pip"])])
    plans = load_shot_plans(EP, require=False)
    assert plans["A01"]["ref_keys"] == [cast_plan.image_key(image)]
    assert plans["A01"]["refs"] == [str(cast_store.image_path(image))]


# ------------------------------------------------------------------------------------- one character


def test_one_character_becomes_ref_keys_refs_and_its_description(tmp_path):
    pip = new_character(("episode", EP), name="Pip", description="a small round robot with one green eye")
    first, second = upload(pip, seed=1), upload(pip, seed=2)
    ref = tmp_path / "harbor.png"
    ref.write_bytes(b"synthetic")
    _write([_shot("A01", ref_keys=["harbor"], cast=["pip"])], {"A01": [{"key": "harbor", "path": str(ref)}]})
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == ["harbor", cast_plan.image_key(first), cast_plan.image_key(second)]
    assert plan["refs"] == [str(ref), str(cast_store.image_path(first)), str(cast_store.image_path(second))]
    assert plan["missing_refs"] == []
    assert plan["shot_meta"]["cast"] == [{
        "character": "pip", "version": 1, "name": "Pip", "locked": False,
        "clause": "Pip: a small round robot with one green eye."}]
    assert plan["shot_meta"]["text_policy"] == "no_text"  # shots with a cast get it by default
    merged = merge_into_shots(EP, [{"shot_id": "A01", "params": {}, "tags": []}],
                              load_shot_plans(EP, require=True), require=True)[0]
    assert merged["ref_keys"] == plan["ref_keys"] and merged["shot_meta"]["cast"] == plan["shot_meta"]["cast"]


def test_the_primary_upload_comes_first():
    pip = new_character(("episode", EP), name="Pip")
    first, second = upload(pip, seed=1), upload(pip, seed=2)
    cast_store.set_primary(pip["character_id"], 1, second["image_id"], "t")
    _write([_shot("A01", cast=["pip"])], {"A01": []})
    assert _cast_keys(load_shot_plans(EP, require=True)["A01"]) == [
        cast_plan.image_key(second), cast_plan.image_key(first)]


def test_a_character_without_images_still_gets_its_description():
    new_character(("episode", EP), name="Pip", description="a small round robot")
    _write([_shot("A01", cast=["pip"])], {"A01": []})
    report = cast_plan.preview_episode_cast(EP, {"shots": [_shot("A01", cast=["pip"])]}, live=False)
    assert any("no reference images yet" in n for n in report["shots"]["A01"]["notes"])
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == [] and plan["shot_meta"]["cast"][0]["clause"] == "Pip: a small round robot."


# ------------------------------------------------------------------------------------- two characters, max refs


def test_two_characters_get_a_lineup_first(cast_dir):
    pip, moss = new_character(("episode", EP), name="Pip"), new_character(("episode", EP), name="Moss")
    p1, m1 = upload(pip, seed=1), upload(moss, seed=2)
    _write([_shot("A01", cast=["pip", "moss"])], {"A01": []})
    plan = load_shot_plans(EP, require=True)["A01"]
    lineup_key = plan["ref_keys"][0]
    assert lineup_key.startswith(cast_plan.COMP_PREFIX)
    assert plan["ref_keys"][1:] == [cast_plan.image_key(p1), cast_plan.image_key(m1)]
    lineup = cast_plan.resolve_cast_key(lineup_key)
    assert lineup.is_file() and lineup.is_relative_to(cast_dir)
    from PIL import Image
    with Image.open(lineup) as img:
        assert img.width > img.height  # side by side
    assert [c["character"] for c in plan["shot_meta"]["cast"]] == ["pip", "moss"]
    # content-addressed: planning again gives the same key and reuses the file
    mtime = lineup.stat().st_mtime_ns
    again = load_shot_plans(EP, require=True)["A01"]
    assert again["ref_keys"] == plan["ref_keys"] and lineup.stat().st_mtime_ns == mtime


def test_refs_over_the_models_limit_merge_into_a_grid_sheet(monkeypatch):
    _cap(monkeypatch, 3)  # one slot reserved for the end frame / anchor: room for 2 cast refs
    pip, moss = new_character(("episode", EP), name="Pip"), new_character(("episode", EP), name="Moss")
    for seed in (1, 2, 3):
        upload(pip, seed=seed)
        upload(moss, seed=10 + seed)
    _write([_shot("A01", cast=["pip", "moss"])], {"A01": []})
    plan = load_shot_plans(EP, require=True)["A01"]
    keys = plan["ref_keys"]
    assert len(keys) == 2 == 3 - cast_plan.RESERVED_SLOTS
    assert all(k.startswith(cast_plan.COMP_PREFIX) for k in keys)  # lineup + grid of the other six
    meta = json.loads(cast_plan.resolve_cast_key(keys[1]).with_suffix(".json").read_text())
    assert meta["kind"] == "grid" and len(meta["sources"]) == 6
    report = cast_plan.preview_episode_cast(EP, {"shots": [_shot("A01", cast=["pip", "moss"])]}, live=False)
    assert any("merged into one grid sheet" in n for n in report["shots"]["A01"]["notes"])
    assert len(plan["refs"]) == len(keys)


def test_the_shots_own_refs_count_against_the_limit(tmp_path, monkeypatch):
    _cap(monkeypatch, 3)
    ref = tmp_path / "harbor.png"
    ref.write_bytes(b"synthetic")
    pip = new_character(("episode", EP), name="Pip")
    upload(pip, seed=1)
    upload(pip, seed=2)
    _write([_shot("A01", ref_keys=["harbor"], cast=["pip"])], {"A01": [{"key": "harbor", "path": str(ref)}]})
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"][0] == "harbor" and len(plan["ref_keys"]) == 2
    assert plan["ref_keys"][1].startswith(cast_plan.COMP_PREFIX)  # both uploads in one grid sheet
    _cap(monkeypatch, 2)
    plan = load_shot_plans(EP, require=True)["A01"]
    assert plan["ref_keys"] == ["harbor"] and plan["shot_meta"]["cast"][0]["name"] == "Pip"


def test_the_preview_writes_no_composite_files(cast_dir):
    pip, moss = new_character(("episode", EP), name="Pip"), new_character(("episode", EP), name="Moss")
    upload(pip, seed=1)
    upload(moss, seed=2)
    report = cast_plan.preview_episode_cast(EP, {"shots": [_shot("A01", cast=["pip", "moss"])]}, live=False)
    assert report["shots"]["A01"]["refs"][0]["label"] == "lineup: Pip + Moss"
    assert not (cast_dir / "composites").exists()


# ------------------------------------------------------------------------------------- errors


def test_unknown_characters_and_versions_refuse_the_plan():
    new_character(("episode", EP), name="Pip")
    _write([_shot("A01", cast=["zed"])], {"A01": []})
    with pytest.raises(ShotDataError, match="'zed' is not in the cast of this episode"):
        load_shot_plans(EP, require=True)
    _write([_shot("A01", cast=[{"character": "pip", "version": 4}])], {"A01": []})
    with pytest.raises(ShotDataError, match="Pip has no version 4"):
        load_shot_plans(EP, require=False)
    assert cast_plan.cast_problems(EP, {"shots": [_shot("A01", cast=["zed"])]}) == [
        "shot A01: cast character 'zed' is not in the cast of this episode (add it in the Cast panel)"]
    for bad in (["Pip"], "pip", ["pip", "pip"], [{"character": "pip", "outfit": 2}], ["a"] * 7):
        with pytest.raises(ShotDataError):
            parse_shots({"shots": [_shot("A01", cast=bad)]})
    with pytest.raises(ShotDataError, match="allow_text must be true or false"):
        parse_shots({"shots": [_shot("A01", allow_text="yes")]})


# ------------------------------------------------------------------------------------- locked sheets


def test_a_locked_sheet_is_the_identity_and_its_description_is_locked():
    pip = new_character(("episode", EP), name="Pip", description="a small round robot")
    upload(pip, seed=1)
    sheet = complete_sheet(pip)
    cast_store.approve_version(pip["character_id"], 1, "t")
    cast_store.update_character(pip["character_id"], description="a tall square robot")
    _write([_shot("A01", cast=["pip"])], {"A01": []})
    plan = load_shot_plans(EP, require=True)["A01"]
    keys = _cast_keys(plan)
    # cap 5, one slot reserved: master, head sheet, 3/4 view, then side, back and front in one grid
    assert keys[:3] == [cast_plan.image_key(sheet[r]) for r in ("master", "head_sheet", "turnaround_three_quarter")]
    assert len(keys) == 4 and keys[3].startswith(cast_plan.COMP_PREFIX)
    grid = json.loads(cast_plan.resolve_cast_key(keys[3]).with_suffix(".json").read_text())
    assert grid["sources"] == [sheet[r]["sha256"] for r in ("turnaround_side", "turnaround_back", "turnaround_front")]
    entry = plan["shot_meta"]["cast"][0]
    assert entry["locked"] is True and entry["clause"].startswith("Pip: a small round robot")


def test_a_tampered_locked_sheet_refuses_the_plan():
    pip = new_character(("episode", EP), name="Pip")
    sheet = complete_sheet(pip)
    cast_store.approve_version(pip["character_id"], 1, "t")
    cast_store.image_path(sheet["master"]).write_bytes(b"swapped")
    _write([_shot("A01", cast=["pip"])], {"A01": []})
    with pytest.raises(ShotDataError, match="changed on disk"):
        load_shot_plans(EP, require=True)


def test_a_locked_outfit_is_used_only_where_a_shot_picks_it():
    pip = new_character(("episode", EP), name="Pip", description="a small round robot")
    main = complete_sheet(pip)
    cast_store.approve_version(pip["character_id"], 1, "t")
    cast_store.create_version(pip["character_id"], label="Raincoat", outfit="a yellow raincoat",
                              derive_from_version=1, actor="t")
    coat = complete_sheet(cast_store.get_character(pip["character_id"]), 2, seed=300)
    cast_store.approve_version(pip["character_id"], 2, "t")
    _write([_shot("A01", cast=["pip"]), _shot("A02", cast=[{"character": "pip", "version": 2}])],
           {"A01": [], "A02": []})
    plans = load_shot_plans(EP, require=True)
    usual, picked = plans["A01"]["shot_meta"]["cast"][0], plans["A02"]["shot_meta"]["cast"][0]
    assert (usual["version"], picked["version"]) == (1, 2)
    assert _cast_keys(plans["A01"])[0] == cast_plan.image_key(main["master"])
    assert _cast_keys(plans["A02"])[0] == cast_plan.image_key(coat["master"])
    assert "raincoat" not in usual["clause"] and "outfit: a yellow raincoat" in picked["clause"]


def test_live_plans_never_use_placeholder_images():
    pip = new_character(("episode", EP), name="Pip")
    complete_sheet(pip, placeholder=True)
    cast_store.approve_version(pip["character_id"], 1, "t", allow_placeholder=True)
    _write([_shot("A01", cast=["pip"])], {"A01": []})
    assert _cast_keys(load_shot_plans(EP, require=False)["A01"])  # dry: the placeholders stand in
    live = load_shot_plans(EP, require=True)["A01"]
    assert _cast_keys(live) == [] and live["shot_meta"]["cast"][0]["name"] == "Pip"


def test_an_unapproved_sheet_is_not_used():
    pip = new_character(("episode", EP), name="Pip")
    complete_sheet(pip)
    shots = [_shot("A01", cast=["pip"])]
    report = cast_plan.preview_episode_cast(EP, {"shots": shots}, live=False)
    assert report["shots"]["A01"]["refs"] == []
    assert any("not approved yet" in n for n in report["shots"]["A01"]["notes"])


def test_a_series_episode_uses_the_series_cast():
    series = cast_store.create_series("Harbor Tales", None, "t")
    cast_store.link_episode(EP, series["series_id"], "t")
    cast_store.link_episode("ep42cast", series["series_id"], "t")
    pip = new_character(("series", series["series_id"]), name="Pip")
    image = upload(pip, seed=1)
    _write([_shot("A01", cast=["pip"])], {"A01": []}, episode="ep42cast")
    assert load_shot_plans("ep42cast", require=True)["A01"]["ref_keys"] == [cast_plan.image_key(image)]
    _write([_shot("A01", cast=["zed"])], {"A01": []}, episode="ep42cast")
    with pytest.raises(ShotDataError, match=f"not in the cast of series {series['series_id']}"):
        load_shot_plans("ep42cast", require=True)


# ------------------------------------------------------------------------------------- refs.json, resolve


def test_cast_keys_resolve_without_a_refs_json_entry(tmp_path):
    pip = new_character(("episode", EP), name="Pip")
    image = upload(pip, seed=1)
    ref = tmp_path / "harbor.png"
    ref.write_bytes(b"synthetic")
    _write([_shot("A01")], {"A01": [{"key": "harbor", "path": str(ref)}]})
    key = cast_plan.image_key(image)
    resolved = resolve_refs(EP, {"A01": ["harbor", key]})["A01"]
    assert resolved == {"refs": [str(ref), str(cast_store.image_path(image))], "missing_refs": []}
    cast_store.image_path(image).unlink()
    assert resolve_refs(EP, {"A01": ["harbor", key]})["A01"]["missing_refs"] == [key]
    with pytest.raises(ShotDataError, match="differ from shots.json ref_keys"):
        resolve_refs(EP, {"A01": [key]})  # the refs.json keys still have to match
    assert cast_plan.resolve_cast_key("cast:img:../../etc/passwd.png") is None
    assert cast_plan.resolve_cast_key("cast:comp:" + "z" * 64) is None


# ------------------------------------------------------------------------------------- text policy


@pytest.mark.parametrize("kit_policy,shot,expected", [
    (None, {"cast": ["pip"]}, "no_text"),
    (None, {}, None),
    ("no_text", {}, "no_text"),
    ("allow_text", {"cast": ["pip"]}, None),
    ("allow_text", {"cast": ["pip"], "allow_text": False}, "no_text"),
    ("no_text", {"cast": ["pip"], "allow_text": True}, None),
])
def test_text_policy(kit_policy, shot, expected):
    new_character(("episode", EP), name="Pip")
    kit = {"lint": "default"}
    if kit_policy:
        kit["text_policy"] = kit_policy
    _write([_shot("A01", **shot)], {"A01": []}, kit=kit)
    meta = load_shot_plans(EP, require=True)["A01"]["shot_meta"]
    assert meta.get("text_policy") == expected
    assert ("text_policy" in meta) == (expected is not None)


def test_an_invalid_kit_text_policy_is_an_error():
    _write([_shot("A01")], {"A01": []}, kit={"lint": "default", "text_policy": "sometimes"})
    with pytest.raises(ShotDataError, match="text_policy must be one of"):
        load_shot_plans(EP, require=True)


def test_planning_does_not_change_the_parsed_input():
    pip = new_character(("episode", EP), name="Pip")
    upload(pip, seed=1)
    raw = {"shots": [_shot("A01", cast=["pip"])]}
    before = copy.deepcopy(raw)
    cast_plan.preview_episode_cast(EP, raw, live=False)
    assert raw == before
    assert NO_TEXT_CLAUSE.startswith("No text")
