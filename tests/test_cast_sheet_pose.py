"""A character's body type and sheet pose (hfvg/cast_store.py, hfvg/cast_sheets.py, migration
2026-11-cast-sheet-pose-v1): sheet pictures are framed for the character's body, not always for a
person standing upright; a humanoid character without its own pose is planned exactly as before."""

from __future__ import annotations

import io
import sqlite3

import pytest
from PIL import Image

from hfvg import cast_store
from hfvg.cast_sheets import SHEET_BODIES, SheetPlanError, placeholder_image, plan_jobs
from tests.cast_helpers import image_bytes, new_character, upload
from tests.test_cast_api import _base, _client, _create, studio  # noqa: F401

LIB = ("episode", "ep88pose")
MARA = "a tall woman with short grey hair and a green coat"
WOOL = "a round fluffy white sheep with a black face and black legs"
UPRIGHT = ("Full body from head to feet, standing upright in a neutral relaxed pose, arms slightly away from "
           "the body, the whole figure centered in frame.")
BACKDROP_AND_NO_TEXT = ("Plain light grey seamless background, even soft studio lighting, no props, no other "
                        "characters. No text, no captions, no speech bubbles, no lettering.")
TURNAROUND_LEAD = ("Character turnaround reference. The reference image is this character's approved master: "
                   "keep the identity exactly (same face, proportions, colors and outfit).")


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("MODEL_PATH_STILL", raising=False)


def _options(character: dict, **kw) -> list[dict]:
    return plan_jobs(cast_store.get_character(character["character_id"]), 1, "options", episode_id=LIB[1],
                     live=False, **kw)["jobs"]


def _turnaround(character: dict) -> dict[str, dict]:
    cast_store.add_generated_image(character["character_id"], 1, image_bytes(seed=7), role=cast_store.MASTER_ROLE,
                                   job_id=f"m-{character['character_id']}", placeholder=True, actor="t")
    jobs = plan_jobs(cast_store.get_character(character["character_id"]), 1, "turnaround", episode_id=LIB[1],
                     live=False)["jobs"]
    return {j["view"]: j for j in jobs}


def _audit(character: dict, action: str) -> list[dict]:
    import json
    return [json.loads(r["details"]) for r in cast_store.list_audit(character_id=character["character_id"])
            if r["action"] == action]


# ------------------------------------------------------------------------------------- migration


async def test_the_migration_adds_only_its_table_and_changes_nothing_that_was_there(tmp_path):
    from hfvg.budget import BudgetLedger
    from hfvg.migrations import (
        CAST_SHEET_POSE_MIGRATION,
        apply_cast_sheet_pose_sync,
        migrate_cast_library,
        migrate_concept_frames,
        migrate_kept_starts,
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
                    migrate_universe_story, migrate_concept_frames, migrate_kept_starts):
        await migrate(db)
    await create_episode(db, "ep07", user_id="u-owner")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO cast_characters (character_id, library_kind, library_id, slug, name, role, "
                     "description, created_by, created_at, updated_at) VALUES ('ch_0123456789ab', 'episode', "
                     "'ep07', 'pip', 'Pip', '', 'a small round robot', 'u1', 'now', 'now')")

    def snapshot():
        with sqlite3.connect(db) as conn:
            schema = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN "
                                                       "('table', 'index') AND name NOT LIKE 'sqlite_%'")}
            data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("episodes", "cast_characters", "cast_versions", "cast_audit_log")}
        return schema, data

    schema_before, data_before = snapshot()
    await run_all_migrations(db)
    await run_all_migrations(db)
    schema_after, data_after = snapshot()
    assert {k: v for k, v in schema_after.items() if k in schema_before} == schema_before
    assert {n for n in set(schema_after) - set(schema_before) if not n.startswith("idx_")} == {"cast_character_bodies"}
    assert data_after == data_before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                            (CAST_SHEET_POSE_MIGRATION,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cast_character_bodies").fetchone()[0] == 0
        assert apply_cast_sheet_pose_sync(conn) is False


def test_the_worker_side_gets_the_table_and_a_character_from_before_reads_humanoid(tmp_path, monkeypatch):
    from hfvg.migrations import apply_cast_library_sync
    db = tmp_path / "older.db"
    conn = sqlite3.connect(db, isolation_level=None)
    apply_cast_library_sync(conn)
    conn.execute("INSERT INTO cast_characters (character_id, library_kind, library_id, slug, name, role, "
                 "description, created_by, created_at, updated_at) VALUES ('ch_0123456789ab', 'episode', "
                 "'ep07', 'pip', 'Pip', '', 'a small round robot', 'u1', 'now', 'now')")
    conn.execute("INSERT INTO cast_versions (character_id, version, label, outfit, created_by, created_at, "
                 "updated_at) VALUES ('ch_0123456789ab', 1, 'Default', '', 'u1', 'now', 'now')")
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    pip = cast_store.get_character("ch_0123456789ab")
    assert (pip["body_type"], pip["sheet_pose"]) == ("humanoid", "")
    with sqlite3.connect(db) as check:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "cast_character_bodies" in tables
    assert [j["body_type"] for j in _options(pip)] == ["humanoid"] * 3
    assert all(UPRIGHT in j["prompt"] and j["aspect_ratio"] == "2:3" for j in _options(pip))


# ------------------------------------------------------------------------------------- the store


def test_a_character_keeps_its_body_and_pose_and_each_change_is_audited():
    plain = new_character(LIB, "Mara", description=MARA)
    assert (plain["body_type"], plain["sheet_pose"]) == ("humanoid", "")
    with cast_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cast_character_bodies").fetchone()[0] == 0
    assert "body_type" not in _audit(plain, "character_create")[0]

    sheep = new_character(LIB, "Wool", description=WOOL, body_type=" Four_Legged ",
                          sheet_pose="  standing   side-on, head  turned to the camera ")
    assert (sheep["body_type"], sheep["sheet_pose"]) == ("four_legged", "standing side-on, head turned to the camera")
    assert _audit(sheep, "character_create")[0]["body_type"] == "four_legged"

    changed = cast_store.update_character(sheep["character_id"], body_type="other", sheet_pose="", actor="t")
    assert (changed["body_type"], changed["sheet_pose"]) == ("other", "")
    assert _audit(sheep, "character_update") == [{"body_type": "other", "sheet_pose": ""}]
    cast_store.update_character(sheep["character_id"], body_type="other", actor="t")
    assert len(_audit(sheep, "character_update")) == 1, "the same body type again is no change"
    renamed = cast_store.update_character(sheep["character_id"], name="Woolly", actor="t")
    assert (renamed["name"], renamed["body_type"]) == ("Woolly", "other")


@pytest.mark.parametrize("fields,why", [
    ({"body_type": "sheep"}, "Body type must be one of: humanoid, four_legged, other"),
    ({"body_type": ""}, "Body type must be one of"),
    ({"body_type": 4}, "Body type must be one of"),
    ({"sheet_pose": "x" * (cast_store.POSE_MAX + 1)}, f"keep it under {cast_store.POSE_MAX}"),
    ({"sheet_pose": ["sitting"]}, "Sheet pose must be text"),
])
def test_an_unknown_body_type_or_a_bad_pose_is_refused_and_nothing_is_kept(fields, why):
    with pytest.raises(cast_store.CastError, match=why):
        new_character(LIB, "Wool", description=WOOL, **fields)
    assert cast_store.list_characters(LIB) == []
    mara = new_character(LIB, "Mara", description=MARA)
    with pytest.raises(cast_store.CastError, match=why):
        cast_store.update_character(mara["character_id"], name="Mara Two", **fields)
    assert cast_store.get_character(mara["character_id"])["name"] == "Mara"
    assert _audit(mara, "character_update") == []


def test_a_character_with_a_picture_gets_its_body_too_and_deleting_it_removes_the_body():
    from tests.cast_helpers import attestation
    sheep = cast_store.create_character_with_upload(LIB, "Wool", "", WOOL, image_bytes(), filename="wool.png",
                                                    attestation=attestation(), actor="t", body_type="four_legged")
    assert sheep["body_type"] == "four_legged"
    cast_store.delete_character(sheep["character_id"], "t")
    with cast_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cast_character_bodies").fetchone()[0] == 0


# ------------------------------------------------------------------------------------- the sheet prompts


def test_every_body_type_has_a_sheet_frame_and_humanoid_is_the_frame_sheets_always_had():
    assert set(SHEET_BODIES) == set(cast_store.BODY_TYPES) and cast_store.DEFAULT_BODY_TYPE == "humanoid"
    assert SHEET_BODIES["humanoid"]["frame"] == UPRIGHT
    assert SHEET_BODIES["humanoid"]["aspects"] == ("2:3", "3:4", "9:16", "1:1")
    for kind, body in SHEET_BODIES.items():
        assert set(body) == {"frame", "aspects", "head", "placeholder"}, kind
        if kind != "humanoid":
            assert "upright" not in body["frame"] and "arms" not in body["frame"], kind


def test_a_humanoid_sheet_is_planned_exactly_as_before_body_types():
    mara = new_character(LIB, "Mara", description=MARA)
    options = _options(mara)
    assert [(j["prompt"], j["aspect_ratio"], j["body_type"]) for j in options] == [(
        f"Character design reference of an original character. Mara: {MARA}. {UPRIGHT} {BACKDROP_AND_NO_TEXT}",
        "2:3", "humanoid")] * 3
    upload(mara)
    assert _options(mara)[0]["prompt"] == (
        "Character design reference. The reference image shows this character: keep their identity exactly "
        f"(same face, proportions, colors and design details). Mara: {MARA}. {UPRIGHT} {BACKDROP_AND_NO_TEXT}")
    views = _turnaround(mara)
    assert views["turnaround_side"]["prompt"] == (
        f"{TURNAROUND_LEAD} Mara: {MARA}. Seen in full profile from the side. {UPRIGHT} {BACKDROP_AND_NO_TEXT}")
    assert views["head_sheet"]["prompt"] == (
        f"{TURNAROUND_LEAD} Mara: {MARA}. A head sheet: four head-and-shoulders views of the same character side "
        f"by side, evenly spaced: front, three-quarter, profile and back, neutral expression. {BACKDROP_AND_NO_TEXT}")
    assert {v: j["aspect_ratio"] for v, j in views.items()} == {
        "turnaround_front": "2:3", "turnaround_three_quarter": "2:3", "turnaround_side": "2:3",
        "turnaround_back": "2:3", "head_sheet": "3:2"}


def test_a_four_legged_sheet_stands_on_four_legs_in_landscape_pictures():
    sheep = new_character(LIB, "Wool", description=WOOL, body_type="four_legged")
    frame = SHEET_BODIES["four_legged"]["frame"]
    for job in _options(sheep):
        assert frame in job["prompt"] and job["aspect_ratio"] == "3:2" and job["body_type"] == "four_legged"
        assert "upright" not in job["prompt"] and "arms" not in job["prompt"] and "head to feet" not in job["prompt"]
    views = _turnaround(sheep)
    for view in ("turnaround_front", "turnaround_three_quarter", "turnaround_side", "turnaround_back"):
        assert frame in views[view]["prompt"] and views[view]["aspect_ratio"] == "3:2", view
        assert "upright" not in views[view]["prompt"], view
    assert SHEET_BODIES["four_legged"]["head"] in views["head_sheet"]["prompt"]
    assert "head-and-shoulders" not in views["head_sheet"]["prompt"]
    assert views["head_sheet"]["aspect_ratio"] == "3:2"


def test_another_kind_of_body_gets_a_neutral_resting_pose_in_square_pictures():
    lamp = new_character(LIB, "Glim", description="a floating paper lantern with two round eyes", body_type="other")
    job = _options(lamp)[0]
    assert SHEET_BODIES["other"]["frame"] in job["prompt"] and job["aspect_ratio"] == "1:1"
    assert "upright" not in job["prompt"] and "feet" not in job["prompt"]
    assert SHEET_BODIES["other"]["head"] in _turnaround(lamp)["head_sheet"]["prompt"]


def test_the_characters_own_pose_replaces_the_pose_of_its_body_type():
    mara = new_character(LIB, "Mara", description=MARA, sheet_pose="Sitting cross-legged on the floor, hands on knees.")
    job = _options(mara)[0]
    assert ("Full body, the whole figure centered in frame. Pose: Sitting cross-legged on the floor, hands on "
            "knees.") in job["prompt"]
    assert UPRIGHT not in job["prompt"] and "knees.." not in job["prompt"]
    assert job["aspect_ratio"] == "2:3", "the pose keeps the pictures of the body type"
    owl = new_character(LIB, "Hoot", description="a small brown owl", body_type="other",
                        sheet_pose="perched upright, wings folded")
    views = _turnaround(owl)
    assert "Pose: perched upright, wings folded." in views["turnaround_back"]["prompt"]
    assert SHEET_BODIES["other"]["frame"] not in views["turnaround_back"]["prompt"]
    assert views["turnaround_back"]["aspect_ratio"] == "1:1"


def test_a_pose_with_words_the_studio_cannot_generate_is_not_planned():
    from hfvg.content_policy import hard_stop_terms
    bad = next(t for t in ("gore", "nude", "blood") if hard_stop_terms(t))
    sheep = new_character(LIB, "Wool", description=WOOL, body_type="four_legged", sheet_pose=f"lying in {bad}")
    with pytest.raises(SheetPlanError, match="words the studio cannot generate"):
        _options(sheep)


def test_a_dry_run_stand_in_has_the_shape_of_the_body():
    def size(**job) -> tuple[int, int]:
        data = placeholder_image({"job_id": "castsheet-ch_0123456789ab-v1-master_option-a1", "view": "master_option",
                                  **job})
        return Image.open(io.BytesIO(data)).size

    assert size(body_type="humanoid") == size() == (768, 1152), "a job planned before body types is humanoid"
    assert placeholder_image({"job_id": "j1", "view": "master_option", "body_type": "humanoid"}) == \
        placeholder_image({"job_id": "j1", "view": "master_option"})
    assert size(body_type="four_legged") == (1152, 768)
    assert size(body_type="other") == (960, 960)
    assert size(body_type="unknown") == (768, 1152)
    assert size(body_type="four_legged", view="head_sheet") == (1152, 768)


# ------------------------------------------------------------------------------------- the API


async def test_the_cast_routes_take_and_show_the_body_and_pose(studio):  # noqa: F811
    owner = _client(studio, "owner")
    sheep = _create(owner, "Wool", body_type="four_legged", sheet_pose="standing side-on")
    assert (sheep["body_type"], sheep["sheet_pose"]) == ("four_legged", "standing side-on")
    plain = _create(owner, "Pip")
    assert (plain["body_type"], plain["sheet_pose"]) == ("humanoid", "")
    url = f"{_base()}/{sheep['character_id']}"
    res = owner.post(url, json={"body_type": "other", "sheet_pose": "curled up asleep"})
    assert res.status_code == 200, res.text
    assert owner.get(url).json()["character"]["body_type"] == "other"
    assert owner.get(_base()).json()["characters"][0]["sheet_pose"] == "curled up asleep"

    res = owner.post(url, json={"body_type": "sheep"})
    assert res.status_code == 400 and "Body type must be one of" in res.json()["detail"], res.text
    from hfvg.content_policy import hard_stop_terms
    bad = next(t for t in ("gore", "nude", "blood") if hard_stop_terms(t))
    res = owner.post(url, json={"sheet_pose": f"lying in {bad}"})
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword"), res.text
    res = owner.post(_base(), json={"name": "Moss", "sheet_pose": f"lying in {bad}"})
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword"), res.text
    res = owner.post(url, json={"sheet_pose": "x" * (cast_store.POSE_MAX + 1)})
    assert res.status_code == 422, res.text
    assert owner.get(url).json()["character"]["sheet_pose"] == "curled up asleep"

    body = io.BytesIO(image_bytes())
    res = owner.post(f"{_base()}/with-picture", files={"image_file": ("hoot.png", body, "image/png")},
                     data={"name": "Hoot", "description": "a small brown owl", "rights": "original",
                           "body_type": "other", "sheet_pose": "perched, wings folded"})
    assert res.status_code == 200, res.text
    assert (res.json()["character"]["body_type"], res.json()["character"]["sheet_pose"]) == (
        "other", "perched, wings folded")
    res = owner.post(f"{_base()}/with-picture", files={"image_file": ("pip.png", io.BytesIO(image_bytes()), "image/png")},
                     data={"name": "Pip Two", "rights": "original"})
    assert res.status_code == 200 and res.json()["character"]["body_type"] == "humanoid", res.text

    other = _client(studio, "other")
    assert other.post(url, json={"body_type": "humanoid"}).status_code == 403
    assert owner.get(url).json()["character"]["body_type"] == "other"


async def test_the_universe_cast_routes_take_the_body_and_pose_too(studio):  # noqa: F811
    owner = _client(studio, "owner")
    res = owner.post("/api/worlds", json={"name": "Hill Farm"})
    assert res.status_code == 200, res.text
    base = f"/api/worlds/{res.json()['world']['series_id']}/cast"
    res = owner.post(base, json={"name": "Wool", "description": WOOL, "body_type": "four_legged"})
    assert res.status_code == 200, res.text
    sheep = res.json()["character"]
    assert sheep["body_type"] == "four_legged"
    res = owner.post(f"{base}/{sheep['character_id']}", json={"sheet_pose": "grazing, head down"})
    assert res.status_code == 200 and res.json()["character"]["sheet_pose"] == "grazing, head down", res.text
    res = owner.post(f"{base}/with-picture", files={"image_file": ("dog.png", io.BytesIO(image_bytes()), "image/png")},
                     data={"name": "Rex", "rights": "original", "body_type": "four_legged"})
    assert res.status_code == 200 and res.json()["character"]["body_type"] == "four_legged", res.text
    res = owner.post(base, json={"name": "Moss", "body_type": "plant"})
    assert res.status_code == 400 and "Body type must be one of" in res.json()["detail"], res.text
