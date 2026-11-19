"""First frames (hfvg/concept_frames.py, docs/CONCEPT-FRAMES.md): the studio writes each option's
prompt from the shot's story beat, words, characters, place, props and their reference pictures, laid
out by the prompt adapter of the server's still model (never shown to anyone); a round makes 1 to 4
options (3 by default); a change in plain words reaches every later round until cleared; a Universe
shot waits for its characters' locked looks; the pick becomes the shot's pre-approved start still in
shots.json (only from the shot as it is now, never over a start frame set by hand); and the new tables
change nothing an episode had. Pictures are flat shapes made here: no real people, no named IP."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from hfvg import cast_store, concept_frames, paths, world_store
from hfvg.cast_store import CastConflict, CastError, CastNotFound
from hfvg.concept_frames import FramePlanError
from tests.cast_helpers import (
    complete_sheet,
    new_character,
    new_place,
    new_prop,
    prop_photo,
    upload,
)

EP = "ep81frames"
SCENE = "Wren lifts the storm lamp at the top of the lighthouse stairs, wind rattling the glass."
BEATMAP = """# Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | Model |
|---|----|--------------|------|-----|-------|--------|---------|-------|
| 1 | A01 | 4 (4) | Lighthouse stairs | dusk | W | Wren climbs to the lamp room | She hears the harbour bell | K |
| 2 | A02 | 4 (4) | Harbour | night | W | Wren finds a brass key in the nets | The mystery starts | K |
"""
GROK = "xai/grok-imagine-image-2.0"


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("MODEL_PATH_STILL", "CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX", "CONCEPT_FRAME_OPTIONS",
                "SHOTS_JSON_PATH", "REFS_JSON_PATH"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "private-cast"


def _shot(sid: str, prompt: str = SCENE, **extra) -> dict:
    return {"id": sid, "count": 1, "characters": ["W"], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16", "prompt": prompt,
            "ref_keys": [], **extra}


def _write(shots: list[dict], *, beatmap: bool = True, ep: str = EP) -> None:
    folder = paths.episode_dir(ep)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": ep, "shots": shots}, indent=2), encoding="utf-8")
    if beatmap:
        (folder / "BEATMAP.md").write_text(BEATMAP, encoding="utf-8")


def _shots(ep: str = EP) -> dict:
    return {s["id"]: s for s in json.loads((paths.episode_dir(ep) / "shots.json").read_text())["shots"]}


def _library(library=("episode", EP)) -> dict:
    """Wren (one reference picture), the harbour cabin (a place) and a brass key (a prop), each with a photo."""
    wren = new_character(library, "Wren", description="a young lighthouse keeper with a red scarf and short dark hair")
    upload(wren, seed=3)
    cabin = new_place(library, "Harbour cabin")
    prop_photo(cabin, seed=4, color=(40, 60, 200))
    key = new_prop(library, "Brass key", description="a small brass key on a red string")
    prop_photo(key, seed=5, color=(200, 160, 40))
    return {"wren": wren, "cabin": cabin, "key": key}


def _cast_shots() -> None:
    _write([_shot("A01", cast=["wren"], place="harbour-cabin", props=["brass-key"]),
            _shot("A02", "Wren kneels by the nets on the quay, a lantern beside her.", cast=["wren"])])


def _round(shot: str = "A01", **kw) -> dict:
    plan = concept_frames.plan_round(EP, shot, live=False, **kw)
    return concept_frames.make_dry_set(plan, estimate_usd_micros=3 * 30_000, approved_total_usd_micros=3 * 30_000,
                                       requested_by="u-owner")


# ------------------------------------------------------------------------------------- how many, which adapter

@pytest.mark.parametrize("value,count", [(None, 3), ("2", 2), ("4", 4), ("9", 4), ("0", 1), ("-3", 1),
                                         ("three", 3), ("", 3)])
def test_a_round_makes_three_options_unless_configured_within_one_to_four(monkeypatch, value, count):
    if value is not None:
        monkeypatch.setenv("CONCEPT_FRAME_OPTIONS", value)
    assert concept_frames.options_per_round() == count


@pytest.mark.parametrize("model,adapter", [
    (GROK, "story"), ("grok", "story"), ("grok-imagine-image-2.0", "story"),
    ("higgsfield-ai/soul/v2/standard", "tags"), ("soul", "tags"),
    ("gpt_image_2", "sections"), ("gpt-image-2", "sections"),
    ("alibaba/qwen-image-3/edit", "edit"), ("qwen-image-3", "edit"),
    ("someone/new-still-model", "plain"),
])
def test_every_still_model_gets_its_own_prompt_adapter_and_any_other_the_plain_one(model, adapter):
    assert concept_frames.adapter_for(model).key == adapter


def _brief(**kw) -> concept_frames.Brief:
    fields = {"shot_id": "A01", "scene": SCENE, "beat": "Wren climbs to the lamp room (She hears the harbour bell)",
              "time_of_day": "dusk", "characters": [{"name": "Wren", "clause": "Wren: a young keeper with a red scarf."}],
              "place": {"name": "Harbour cabin", "clause": "Harbour cabin (the setting): a blue cabin on stilts."},
              "props": [{"name": "Brass key", "clause": "Brass key: a small brass key on a red string."}],
              "refs": [{"id": "cast:a", "path": "/refs/a.png", "label": "Wren reference"},
                       {"id": "cast:b", "path": "/refs/b.png", "label": "Harbour cabin photo"}],
              "aspect_ratio": "9:16", "no_text": True}
    return concept_frames.Brief(**{**fields, **kw})


@pytest.mark.parametrize("adapter", [*concept_frames.ADAPTERS, concept_frames.PLAIN], ids=lambda a: a.key)
def test_every_adapter_writes_the_scene_the_people_the_place_and_the_owners_changes(adapter):
    changes = ["make it night", "closer on her face"]
    text = concept_frames.write_prompt(_brief(), changes, 1, adapter, adapter.max_chars)
    for words in (SCENE.rstrip("."), "Wren: a young keeper with a red scarf", "a blue cabin on stilts",
                  "make it night (time of day: night, moonlight and practical lights)",
                  "closer on her face (framing: closer, a close-up)"):
        assert words in text, (adapter.key, words)
    assert "dusk" not in text  # the change names another time of day
    assert "tall, portrait, aspect ratio 9:16" in text and "no text" in text.lower()
    if adapter.key != "tags":  # the tags adapter is for a model that takes no reference images
        assert "Wren reference" in text and "Harbour cabin photo" in text
    for name in (GROK, "grok", "soul", "qwen", "gpt"):
        assert name not in text.lower()


@pytest.mark.parametrize("adapter", [*concept_frames.ADAPTERS, concept_frames.PLAIN], ids=lambda a: a.key)
def test_a_long_scene_is_clipped_so_the_changes_and_references_still_reach_the_model(adapter):
    long_scene = "Fog rolls over the quay and the lamps glow amber. " * 120
    limit = min(adapter.max_chars, 900)
    text = concept_frames.write_prompt(_brief(scene=long_scene), ["make it night"], 0, adapter, limit)
    assert len(text) <= limit
    assert "make it night" in text
    if adapter.key != "tags":
        assert "Wren reference" in text


def test_each_option_of_a_round_tries_its_own_camera_idea():
    adapter = concept_frames.adapter_for(GROK)
    prompts = [concept_frames.write_prompt(_brief(), [], k, adapter, adapter.max_chars) for k in range(4)]
    assert len(set(prompts)) == 4
    assert "different camera angle" in prompts[1] and "a breath later" in prompts[2]


def test_the_beatmaps_time_of_day_is_kept_without_a_change_that_names_one():
    adapter = concept_frames.adapter_for(GROK)
    assert "Time of day: dusk." in concept_frames.write_prompt(_brief(), ["warmer colours"], 0, adapter, 2000)


# ------------------------------------------------------------------------------------- the brief

def test_the_prompt_is_written_from_the_beat_the_words_the_cast_and_their_pictures():
    _library()
    _cast_shots()
    plan = concept_frames.plan_round(EP, "A01", live=False)
    assert plan["count"] == 3 and plan["round"] == 1 and plan["changes"] == []
    assert plan["adapter"] == "story" and plan["model_path"] == GROK and plan["aspect_ratio"] == "9:16"
    assert plan["summary"] == {"characters": ["Wren"], "place": "Harbour cabin", "props": ["Brass key"],
                               "references": 3}
    prompt = plan["prompts"][0]
    assert "The moment: Wren climbs to the lamp room (She hears the harbour bell)." in prompt
    assert f"What we see: {SCENE}" in prompt
    assert "Wren: a young lighthouse keeper with a red scarf and short dark hair." in prompt
    assert "Harbour cabin (the setting): a small blue wooden cabin on stilts at the end of a pier." in prompt
    assert "Brass key: a small brass key on a red string." in prompt
    assert "Time of day: dusk." in prompt and "No text" in prompt
    assert "image 1 is Wren reference; image 2 is Harbour cabin photo; image 3 is Brass key photo" in prompt
    for ref in plan["refs"]:
        with open(ref["path"], "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == ref["sha256"]
    jobs = concept_frames.plan_jobs(plan, set_id="cf_0123456789ab")
    assert [j["job_id"] for j in jobs] == [f"cframe-0123456789ab-{k}" for k in range(3)]
    assert {(j["resolution"], j["quality"], j["aspect_ratio"]) for j in jobs} == {("1k", "medium", "9:16")}
    assert jobs[1]["prompt"] == plan["prompts"][1] != plan["prompts"][0]


def test_a_shot_without_a_cast_or_beatmap_still_gets_a_prompt_from_its_words(monkeypatch):
    monkeypatch.setenv("CONCEPT_FRAME_OPTIONS", "2")
    _write([_shot("A01", "An empty quay at dawn, gulls over the water.")], beatmap=False)
    plan = concept_frames.plan_round(EP, "A01", live=False)
    assert plan["count"] == 2 and plan["refs"] == []
    assert plan["summary"] == {"characters": [], "place": None, "props": [], "references": 0}
    assert "What we see: An empty quay at dawn, gulls over the water." in plan["prompts"][0]
    assert "The moment" not in plan["prompts"][0]


def test_unknown_shots_and_episodes_without_a_shot_list_are_refused():
    with pytest.raises(CastNotFound):
        concept_frames.plan_round(EP, "A01", live=False)
    _write([_shot("A01")])
    with pytest.raises(CastNotFound):
        concept_frames.plan_round(EP, "Z99", live=False)
    with pytest.raises(CastNotFound):
        concept_frames.plan_round(EP, "../A01", live=False)


def test_a_cast_the_run_could_not_plan_is_refused_with_its_reason():
    _write([_shot("A01", cast=["nobody"])])
    with pytest.raises(FramePlanError, match="not in the cast"):
        concept_frames.plan_round(EP, "A01", live=False)
    view = concept_frames.frames_view(EP, live=False)
    assert view["shots"][0]["ready"] is False and "not in the cast" in view["shots"][0]["problems"][0]


def test_a_change_reaches_every_later_round_until_cleared():
    _library()
    _cast_shots()
    first = _round()
    assert first["round"] == 1 and first["changes"] == []
    night = concept_frames.plan_round(EP, "A01", change="  make it   night ", live=False)
    assert night["round"] == 2 and night["changes"] == ["make it night"]
    assert all("make it night (time of day: night" in p and "dusk" not in p for p in night["prompts"])
    concept_frames.make_dry_set(night, estimate_usd_micros=1, approved_total_usd_micros=1, requested_by="u-owner")
    closer = concept_frames.plan_round(EP, "A01", change="closer on her face", live=False)
    assert closer["changes"] == ["make it night", "closer on her face"]
    again = concept_frames.plan_round(EP, "A01", live=False)
    assert again["changes"] == ["make it night"]
    fresh = concept_frames.plan_round(EP, "A01", keep_changes=False, live=False)
    assert fresh["changes"] == [] and all("make it night" not in p for p in fresh["prompts"])
    with pytest.raises(CastError, match="under 200 characters"):
        concept_frames.plan_round(EP, "A01", change="x" * 201, live=False)


def test_only_the_last_few_changes_are_kept():
    _write([_shot("A01")])
    for k in range(concept_frames.CHANGES_KEPT + 2):
        plan = concept_frames.plan_round(EP, "A01", change=f"change {k}", live=False)
        concept_frames.make_dry_set(plan, estimate_usd_micros=1, approved_total_usd_micros=1, requested_by="u")
    assert plan["changes"] == [f"change {k}" for k in range(2, concept_frames.CHANGES_KEPT + 2)]


@pytest.mark.parametrize("change", ["add some gore", "make her topless"])
def test_words_the_studio_cannot_generate_are_refused_before_anything_is_made(change):
    _write([_shot("A01")])
    with pytest.raises(CastError, match="cannot be used for a picture"):
        concept_frames.plan_round(EP, "A01", change=change, live=False)
    assert concept_frames.frames_view(EP, live=False)["shots"][0]["rounds"] == []


def test_a_round_waits_for_the_one_being_made():
    _write([_shot("A01"), _shot("A02")])
    plan = concept_frames.plan_round(EP, "A01", live=False)
    jobs = [{**j, "estimated_usd_micros": 10, "approved_usd_micros": 10}
            for j in concept_frames.plan_jobs(plan, set_id="cf_00000000aaaa")]
    concept_frames.start_set(plan, jobs, set_id="cf_00000000aaaa", workflow_id="concept-frames-cf_00000000aaaa",
                             estimate_usd_micros=30, approved_total_usd_micros=30, requested_by="u-owner")
    with pytest.raises(CastConflict, match="being made"):
        concept_frames.plan_round(EP, "A01", live=False)
    with pytest.raises(CastConflict, match="being made"):
        concept_frames.make_dry_set(plan, estimate_usd_micros=1, approved_total_usd_micros=1, requested_by="u")
    assert concept_frames.plan_round(EP, "A02", live=False)["round"] == 1  # other shots are not held up
    view = concept_frames.frames_view(EP, live=False)
    assert view["shots"][0]["making"] is True and view["shots"][1]["making"] is False


def test_a_universe_shot_waits_for_its_characters_locked_looks():
    sid = world_store.create_universe("Lantern Bay", "u-owner", actor="u-owner",
                                      logline="A lighthouse keeper guards a harbour of secrets")["series"]["series_id"]
    cast_store.link_episode(EP, sid)
    wren = new_character(("series", sid), "Wren", description="a young keeper with a red scarf")
    _write([_shot("A01", cast=["wren"]), _shot("A02")])
    with pytest.raises(FramePlanError, match="Lock the look of Wren before first frames are made"):
        concept_frames.plan_round(EP, "A01", live=False)
    view = concept_frames.frames_view(EP, live=False)
    assert view["shots"][0]["ready"] is False and view["shots"][1]["ready"] is True
    assert concept_frames.plan_round(EP, "A02", live=False)["count"] == 3  # a shot without characters is not held up
    complete_sheet(wren)
    cast_store.approve_version(wren["character_id"], 1, "u-owner")
    plan = concept_frames.plan_round(EP, "A01", live=False)
    assert "The story this belongs to: A lighthouse keeper guards a harbour of secrets." in plan["prompts"][0]
    assert plan["summary"]["references"] >= 1


# ------------------------------------------------------------------------------------- rounds and picks

def test_a_dry_round_makes_placeholder_options_in_the_shots_shape():
    _library()
    _cast_shots()
    made = _round()
    assert (made["status"], made["dry_run"], made["made"], made["option_count"]) == ("ready", True, 3, 3)
    for k, option in enumerate(made["options"]):
        assert option["option_id"] == f"cframe-{made['set_id'][3:]}-{k}"
        assert (option["status"], option["placeholder"], option["width"], option["height"]) == (
            "completed", True, 576, 1024)
        assert option["image_url"] == f"/api/episodes/{EP}/concept-frames/images/{option['option_id']}"
        picture = concept_frames.option_picture(EP, option["option_id"])
        assert picture["path"].is_file() and cast_store.thumb_file(picture["sha256"]).is_file()
    with pytest.raises(CastNotFound):
        concept_frames.option_picture("ep99other", made["options"][0]["option_id"])
    with cast_store.connect() as conn:
        assert cast_store.file_in_use(conn, concept_frames.option(made["options"][0]["option_id"])["sha256"])


def test_picking_an_option_makes_it_the_shots_start_frame_for_the_run():
    from hfvg.pre_approved import pre_approved_still_map
    from hfvg.shot_data import parse_shots, pre_approved_still_paths
    _library()
    _cast_shots()
    made = _round()
    first, second = made["options"][0], made["options"][1]
    shot = concept_frames.pick(EP, "A01", first["option_id"], actor="u-owner", approved_by="Owner", live=False)
    row = concept_frames.option(first["option_id"])
    rel = f"concept_frames/A01-{row['sha256'][:12]}.png"
    assert _shots()["A01"]["pre_approved_still"] == {"path": rel, "sha256": row["sha256"], "approved_by": "Owner"}
    assert "pre_approved_still" not in _shots()["A02"]
    copied = paths.episode_dir(EP) / rel
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == row["sha256"]
    plans = parse_shots(json.loads((paths.episode_dir(EP) / "shots.json").read_text()))
    assert pre_approved_still_paths(EP, plans) == {"A01": str(copied)}
    run_shots = [{"shot_id": sid, **p} for sid, p in plans.items()]
    assert set(pre_approved_still_map(run_shots)) == {"A01"}
    assert shot["pick"]["source"] == "here" and shot["pick"]["option_id"] == first["option_id"]
    assert shot["pick"]["stale"] is False and shot["pick"]["picked_by"] == "u-owner"
    # a second pick replaces the first; the history keeps both
    concept_frames.pick(EP, "A01", second["option_id"], actor="u-owner", approved_by="Owner", live=False)
    assert _shots()["A01"]["pre_approved_still"]["sha256"] == concept_frames.option(second["option_id"])["sha256"]
    with cast_store.connect() as conn:
        picks = conn.execute("SELECT option_id, status FROM concept_frame_picks ORDER BY picked_at, rowid").fetchall()
        actions = [r[0] for r in conn.execute("SELECT action FROM cast_audit_log WHERE action LIKE 'concept_frame%' "
                                              "ORDER BY id")]
    assert [tuple(p) for p in picks] == [(first["option_id"], "replaced"), (second["option_id"], "current")]
    assert actions == ["concept_frames_request", "concept_frame_pick", "concept_frame_pick"]


def test_unpicking_takes_the_frame_off_the_shot_and_the_run_makes_its_still_again():
    _write([_shot("A01")])
    made = _round()
    concept_frames.pick(EP, "A01", made["options"][2]["option_id"], actor="u-owner", approved_by="Owner", live=False)
    shot = concept_frames.unpick(EP, "A01", actor="u-owner", live=False)
    assert "pre_approved_still" not in _shots()["A01"] and shot["pick"] is None
    with cast_store.connect() as conn:
        assert conn.execute("SELECT status FROM concept_frame_picks").fetchone()[0] == "removed"
    assert concept_frames.unpick(EP, "A01", actor="u-owner", live=False)["pick"] is None  # nothing to take off


def test_an_option_made_from_an_earlier_version_of_the_shot_cannot_be_picked():
    _write([_shot("A01"), _shot("A02")])
    made = _round()
    shots = json.loads((paths.episode_dir(EP) / "shots.json").read_text())
    shots["shots"][0]["prompt"] = "Wren rows out past the breakwater in the rain."
    (paths.episode_dir(EP) / "shots.json").write_text(json.dumps(shots))
    with pytest.raises(CastConflict, match="changed since these options were made"):
        concept_frames.pick(EP, "A01", made["options"][0]["option_id"], actor="u", approved_by="Owner", live=False)
    assert "pre_approved_still" not in _shots()["A01"]
    view = concept_frames.frames_view(EP, live=False)["shots"][0]
    assert view["changed"] is True and view["rounds"][0]["current"] is False


def test_a_start_frame_set_in_the_shot_list_by_hand_is_never_replaced():
    hand = {"path": "stills/a01_owner.png", "approved_by": "owner"}
    _write([_shot("A01", pre_approved_still=hand)])
    made = _round()
    with pytest.raises(CastConflict, match="set in its shot list"):
        concept_frames.pick(EP, "A01", made["options"][0]["option_id"], actor="u", approved_by="Owner", live=False)
    with pytest.raises(CastConflict, match="set in its shot list"):
        concept_frames.unpick(EP, "A01", actor="u", live=False)
    assert _shots()["A01"]["pre_approved_still"] == hand
    assert concept_frames.frames_view(EP, live=False)["shots"][0]["pick"]["source"] == "shot_list"


def test_a_live_studio_never_picks_a_dry_run_stand_in_or_an_option_of_another_shot():
    _write([_shot("A01"), _shot("A02")])
    made = _round()
    option_id = made["options"][0]["option_id"]
    with pytest.raises(CastConflict, match="dry-run stand-in"):
        concept_frames.pick(EP, "A01", option_id, actor="u", approved_by="Owner", live=True)
    with pytest.raises(CastNotFound):
        concept_frames.pick(EP, "A02", option_id, actor="u", approved_by="Owner", live=False)
    with pytest.raises(CastNotFound):
        concept_frames.pick(EP, "A01", "cframe-000000000000-0", actor="u", approved_by="Owner", live=False)
    assert all("pre_approved_still" not in s for s in _shots().values())


def test_a_live_round_ends_ready_partial_or_failed_and_skips_what_did_not_run():
    from tests.cast_helpers import image_bytes
    _write([_shot("A01"), _shot("A02"), _shot("A03")])

    def live_set(shot: str, set_id: str) -> list[dict]:
        plan = concept_frames.plan_round(EP, shot, live=False)
        jobs = [{**j, "estimated_usd_micros": 10, "approved_usd_micros": 12}
                for j in concept_frames.plan_jobs(plan, set_id=set_id)]
        concept_frames.start_set(plan, jobs, set_id=set_id, workflow_id=f"concept-frames-{set_id}",
                                 estimate_usd_micros=30, approved_total_usd_micros=36, requested_by="u-owner")
        return jobs

    full = live_set("A01", "cf_00000000000a")
    for k, job in enumerate(full):
        concept_frames.store_option_picture(job["job_id"], image_bytes(size=(576, 1024), seed=k), placeholder=False)
    assert concept_frames.finish_set("cf_00000000000a")["status"] == "ready"
    some = live_set("A02", "cf_00000000000b")
    concept_frames.store_option_picture(some[0]["job_id"], image_bytes(size=(576, 1024), seed=9), placeholder=False)
    concept_frames.set_option(some[1]["job_id"], "running")
    partial = concept_frames.finish_set("cf_00000000000b", "The price of a picture went up.")
    assert (partial["status"], partial["made"], partial["error"]) == ("partial", 1, "The price of a picture went up.")
    assert [o["status"] for o in partial["options"]] == ["completed", "skipped", "skipped"]
    none = live_set("A03", "cf_00000000000c")
    failed = concept_frames.finish_set("cf_00000000000c")
    assert (failed["status"], failed["made"], failed["error"]) == ("failed", 0, concept_frames.STOPPED)
    assert concept_frames.finish_set("cf_00000000000c")["status"] == "failed"  # idempotent
    assert concept_frames.making_sets(EP) == []
    assert none[0]["approved_usd_micros"] == 12


def test_the_view_never_names_a_model_or_a_prompt_adapter(monkeypatch):
    _library()
    _cast_shots()
    _round()
    for model in (GROK, "higgsfield-ai/soul/v2/standard"):
        monkeypatch.setenv("MODEL_PATH_STILL", model)
        text = json.dumps(concept_frames.frames_view(EP, live=False)).lower()
        for word in ("grok", "soul", "xai", "higgsfield", "model_path", "adapter", "story", "prompt"):
            assert word not in text, word


def test_the_shots_words_and_beat_are_left_out_for_who_may_not_read_its_prompts():
    _write([_shot("A01")])
    view = concept_frames.frames_view(EP, live=False, with_words=False)
    assert view["words_hidden"] is True
    assert view["shots"][0]["scene"] is None and view["shots"][0]["beat"] is None


# ------------------------------------------------------------------------------------- what was there

def test_an_episode_without_first_frames_reads_and_runs_as_before():
    shots = [_shot("A01"), _shot("A02", pre_approved_still="stills/a02.png")]
    _write(shots)
    before = (paths.episode_dir(EP) / "shots.json").read_text()
    view = concept_frames.frames_view(EP, live=False)
    assert [(s["shot_id"], s["rounds"], s["changes"]) for s in view["shots"]] == [("A01", [], []), ("A02", [], [])]
    assert view["shots"][0]["pick"] is None and view["shots"][1]["pick"]["source"] == "shot_list"
    assert (paths.episode_dir(EP) / "shots.json").read_text() == before
    assert concept_frames.frames_view("ep99none", live=False) == {
        "episode_id": "ep99none", "options_per_round": 3, "dry_run": True, "words_hidden": False, "uploaded": False,
        "shots": [], "error": None}


async def test_the_migration_adds_only_its_tables_and_changes_nothing_that_was_there(tmp_path):
    from hfvg.budget import BudgetLedger
    from hfvg.migrations import (
        CONCEPT_FRAMES_MIGRATION,
        apply_concept_frames_sync,
        migrate_cast_library,
        migrate_props_library,
        migrate_universe,
        migrate_universe_board,
        migrate_universe_story,
        run_all_migrations,
    )
    from hfvg.studio_db import create_episode, init_studio_db
    db = str(tmp_path / "existing.db")
    await init_studio_db(db)
    ledger = BudgetLedger(db)
    await ledger.init_db()
    for migrate in (migrate_cast_library, migrate_props_library, migrate_universe, migrate_universe_board,
                    migrate_universe_story):
        await migrate(db)
    await create_episode(db, "ep07")
    await ledger.init_episode_budget("ep07")

    def snapshot():
        with sqlite3.connect(db) as conn:
            schema = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN "
                                                       "('table', 'index') AND name NOT LIKE 'sqlite_%'")}
            data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("episodes", "budget_lines", "series", "schema_migrations")}
        return schema, data

    schema_before, data_before = snapshot()
    await run_all_migrations(db)
    await run_all_migrations(db)
    schema_after, data_after = snapshot()
    assert {k: v for k, v in schema_after.items() if k in schema_before} == schema_before
    # the later kept starts migration adds its own table (tests/test_kept_starts.py)
    assert {n for n in set(schema_after) - set(schema_before) if not n.startswith("idx_")} == {
        "concept_frame_sets", "concept_frame_options", "concept_frame_picks", "episode_kept_starts"}
    assert data_after["episodes"] == data_before["episodes"] and data_after["budget_lines"] == data_before["budget_lines"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                            (CONCEPT_FRAMES_MIGRATION,)).fetchone()[0] == 1
        assert apply_concept_frames_sync(conn) is False


def test_the_worker_side_gets_the_tables_on_a_db_without_them(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "fresh.db"))
    _write([_shot("A01")])
    assert concept_frames.frames_view(EP, live=False)["shots"][0]["rounds"] == []
    with sqlite3.connect(tmp_path / "fresh.db") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"concept_frame_sets", "concept_frame_options", "concept_frame_picks"} <= tables
