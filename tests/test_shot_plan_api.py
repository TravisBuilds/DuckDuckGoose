"""Shot plan API (api/shot_plan_routes.py): each shot's cast, props, place and prompts change before
the shot plan is approved. Only the episode's owner or an admin changes it (any member while it has
no owner), only whoever may see the cast sees it; bad picks change nothing; while a run waits at G1
the change goes through the workflow's replan_shots and a refusal puts the old shots.json back;
after approval the plan is locked. Every changed field lands in the edit history."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from hfvg import shot_plan_edits
from hfvg.paths import episode_dir
from tests.cast_helpers import image_bytes
from tests.lint_helpers import lint_ok
from tests.test_props_api import (EP, OTHER_EP, REFUSED, SHARED_EP, _added, _client, _running,  # noqa: F401
                                  studio)

BASE = f"/api/episodes/{EP}/shot-plan"
HIDDEN = "Only the people working on this episode (and admins) can see its shot plan"
PIER = lint_ok("A duck waits at the end of a pier")
NEW = lint_ok("A duck holds an umbrella at the end of a pier")


def _shot(sid: str, prompt: str) -> dict:
    return {"id": sid, "prompt": prompt, "count": 1, "characters": [], "wet": False, "exterior": False,
            "water": False, "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "ref_keys": []}


def _write_shots(ep: str = EP) -> str:
    folder = episode_dir(ep)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "refs.json").write_text(json.dumps({"episode_id": ep, "per_shot": {}}))
    text = json.dumps({"episode_id": ep, "shots": [_shot("A01", PIER), _shot("A02", lint_ok("A duck naps"))]})
    (folder / "shots.json").write_text(text)
    return text


def _shots_text(ep: str = EP) -> str:
    return (episode_dir(ep) / "shots.json").read_text()


def _shots_json(ep: str = EP) -> dict:
    return {s["id"]: s for s in json.loads(_shots_text(ep))["shots"]}


def _library(client, ep: str = EP) -> tuple[dict, dict]:
    cabin = _added(client, "Harbour cabin", "place", ep=ep, data=image_bytes(seed=1, color=(40, 60, 220)))
    umbrella = _added(client, "Red umbrella", "prop", ep=ep, data=image_bytes(seed=2))
    return cabin, umbrella


def _at_gate(api_main, state: dict):
    _running(api_main)
    snapshot = {"attrs": {"reopen_log": [], "_dry_run": True}}
    api_main.temporal_client.handle.query = AsyncMock(
        side_effect=lambda name, *a, **k: {"reopen_snapshot": snapshot, "get_state": state}[name])


G1 = {"current_gate": "G1_SHOT_PLAN", "passed_gates": [], "approvals": {"g1_shot_plan": False}, "replanning": False}
G2 = {"current_gate": "G2_SCENE_STILLS", "passed_gates": ["G1_SHOT_PLAN"], "approvals": {"g1_shot_plan": True}}


# ------------------------------------------------------------------------------------- who may do what


async def test_the_shot_plan_needs_a_login(studio):  # noqa: F811
    _write_shots()
    anon = _client(studio, None)
    assert anon.get(BASE).status_code == 401
    assert anon.post(BASE, json={"shots": {"A01": {"prompt": NEW}}}).status_code == 401
    assert _shots_json()["A01"]["prompt"] == PIER


async def test_another_member_can_neither_see_nor_change_the_shot_plan(studio):  # noqa: F811
    before = _write_shots()
    other = _client(studio, "other")
    res = other.get(BASE)
    assert res.status_code == 404 and res.json()["detail"] == HIDDEN
    res = other.post(BASE, json={"shots": {"A01": {"prompt": NEW}}})
    assert res.status_code == 403 and res.json()["detail"] == f"Episode {EP} {REFUSED}"
    assert _shots_text() == before and shot_plan_edits.list_edits(EP) == []
    studio.temporal_client.handle.execute_update.assert_not_called()


async def test_the_owner_picks_props_and_edits_a_prompt_before_a_run(studio):  # noqa: F811
    _write_shots()
    owner = _client(studio, "owner")
    cabin, umbrella = _library(owner)
    view = owner.get(BASE).json()
    assert view["can_edit"] is True and view["phase"]["state"] == "open" and view["may_change"] is True
    assert [p["slug"] for p in view["props"]] == ["harbour-cabin", "red-umbrella"]
    assert view["props"][0]["kind"] == "place" and view["props"][0]["thumb_url"].endswith("/thumb")
    assert [s["shot_id"] for s in view["shots"]] == ["A01", "A02"]
    assert view["shots"][0]["props"] == [] and view["shots"][0]["place"] is None

    res = owner.post(BASE, json={"shots": {"A01": {"prompt": NEW, "props": [umbrella["slug"]],
                                                   "place": cabin["slug"]}}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changed"] == [{"shot_id": "A01", "field": f} for f in ("prompt", "props", "place")]
    assert body["result"] is None
    saved = _shots_json()["A01"]
    assert saved["prompt"] == NEW and saved["props"] == ["red-umbrella"] and saved["place"] == "harbour-cabin"
    a01 = body["shots"][0]
    assert a01["props"] == ["red-umbrella"] and a01["place"] == "harbour-cabin"
    assert a01["refs"] == ["Harbour cabin photo", "Red umbrella photo"] and a01["errors"] == []
    history = [(h["field"], h["old"], h["new"], h["actor_name"], h["run_id"]) for h in a01["history"]]
    assert history == [("place", None, "harbour-cabin", "Owner", None), ("props", None, ["red-umbrella"], "Owner", None),
                       ("prompt", PIER, NEW, "Owner", None)]
    assert body["shots"][1]["history"] == []
    studio.temporal_client.handle.execute_update.assert_not_called()

    again = owner.post(BASE, json={"shots": {"A01": {"prompt": NEW, "place": cabin["slug"]}}}).json()
    assert again["changed"] == [] and len(shot_plan_edits.list_edits(EP)) == 3

    cleared = owner.post(BASE, json={"shots": {"A01": {"props": [], "place": ""}}, "note": "no umbrella here"})
    assert cleared.status_code == 200 and "props" not in _shots_json()["A01"] and "place" not in _shots_json()["A01"]
    assert [(h["field"], h["note"]) for h in shot_plan_edits.list_edits(EP)[:2]] == [
        ("place", "no umbrella here"), ("props", "no umbrella here")]


async def test_admins_change_any_shot_plan_and_an_episode_without_an_owner_any_member(studio):  # noqa: F811
    from hfvg.studio_db import create_episode
    _write_shots()
    admin = _client(studio, "admin")
    assert admin.get(BASE).json()["can_edit"] is True
    res = admin.post(BASE, json={"shots": {"A02": {"clip_prompt": lint_ok("The duck stretches")}}})
    assert res.status_code == 200, res.text
    assert shot_plan_edits.list_edits(EP)[0]["actor_name"] == "Admin"

    await create_episode(os.environ["DATABASE_PATH"], SHARED_EP)
    _write_shots(SHARED_EP)
    other = _client(studio, "other")
    assert other.get(f"/api/episodes/{SHARED_EP}/shot-plan").json()["can_edit"] is True
    res = other.post(f"/api/episodes/{SHARED_EP}/shot-plan", json={"shots": {"A01": {"prompt": NEW}}})
    assert res.status_code == 200 and _shots_json(SHARED_EP)["A01"]["prompt"] == NEW


@pytest.mark.parametrize("change, status, word", [
    ({"A09": {"prompt": NEW}}, 400, "Unknown shots"),
    ({"A01": {"props": ["blue-oar"]}}, 400, "is not in the props and places of this episode"),
    ({"A01": {"props": ["harbour-cabin"]}}, 400, "is a place, not a prop"),
    ({"A01": {"place": "red-umbrella"}}, 400, "is a prop, not a place"),
    ({"A01": {"cast": ["pip"]}}, 400, "is not in the cast of this episode"),
    ({"A01": {"props": ["red-umbrella", "red-umbrella"]}}, 400, "red-umbrella"),
    ({"A01": {"cast": [{"character": "pip", "version": "two"}]}}, 400, "version"),
    ({"A01": {"prompt": "   "}}, 400, "cannot be empty"),
    ({"A01": {"props": ["a", "b", "c", "d", "e"]}}, 422, ""),
    ({"A01": {"prompt": "x" * 6001}}, 422, ""),
])
async def test_bad_changes_are_refused_and_nothing_changes(studio, change, status, word):  # noqa: F811
    before = _write_shots()
    owner = _client(studio, "owner")
    _library(owner)
    res = owner.post(BASE, json={"shots": change})
    assert res.status_code == status, res.text
    assert word in json.dumps(res.json())
    assert _shots_text() == before and shot_plan_edits.list_edits(EP) == []


async def test_prompt_text_follows_the_content_policy(studio, monkeypatch):  # noqa: F811
    from hfvg import content_policy
    before = _write_shots()
    monkeypatch.setattr(content_policy, "precheck_banned_words", lambda text: ["moonbeam"] if "moonbeam" in text else [])
    res = _client(studio, "owner").post(BASE, json={"shots": {"A01": {"prompt": lint_ok("A moonbeam duck")}}})
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword")
    assert _shots_text() == before


# ------------------------------------------------------------------------------------- an open run


async def test_at_g1_the_change_goes_through_the_workflow(studio):  # noqa: F811
    _write_shots()
    owner = _client(studio, "owner")
    cabin, _ = _library(owner)
    _at_gate(studio, G1)
    replan = {"success": True, "action": "replan_shots", "changed": {"A01": ["prompt"]}, "preflight_errors": []}
    studio.temporal_client.handle.execute_update = AsyncMock(return_value=replan)
    view = owner.get(BASE).json()
    assert view["phase"]["state"] == "g1" and view["can_edit"] is True

    res = owner.post(BASE, json={"shots": {"A01": {"prompt": NEW, "place": cabin["slug"]}}})
    assert res.status_code == 200, res.text
    assert res.json()["result"] == replan
    call = studio.temporal_client.handle.execute_update.call_args
    assert call.args[0] == "replan_shots"
    assert call.kwargs["args"] == ["Shot plan edited by Owner: A01 prompt/place"]
    assert _shots_json()["A01"]["place"] == "harbour-cabin"
    assert {h["run_id"] for h in shot_plan_edits.list_edits(EP)} == {"run-open"}

    owner.post(BASE, json={"shots": {"A02": {"prompt": NEW}}, "note": "same pier as A01"})
    assert studio.temporal_client.handle.execute_update.call_args.kwargs["args"] == ["same pier as A01"]


async def test_a_refused_replan_puts_the_old_shot_plan_back(studio):  # noqa: F811
    before = _write_shots()
    owner = _client(studio, "owner")
    _at_gate(studio, G1)
    refusal = "Cannot change the shot plan: G1 already approved; the shot plan is locked"
    studio.temporal_client.handle.execute_update = AsyncMock(
        side_effect=WorkflowUpdateFailedError(ApplicationError(refusal, non_retryable=True)))
    res = owner.post(BASE, json={"shots": {"A01": {"prompt": NEW}}})
    assert res.status_code == 409 and res.json()["detail"] == refusal
    assert _shots_text() == before and shot_plan_edits.list_edits(EP) == []


async def test_after_approval_the_shot_plan_is_locked(studio):  # noqa: F811
    before = _write_shots()
    owner = _client(studio, "owner")
    _at_gate(studio, G2)
    view = owner.get(BASE).json()
    assert view["phase"]["state"] == "locked" and view["can_edit"] is False and view["may_change"] is True
    assert "approved" in view["phase"]["reason"]
    res = owner.post(BASE, json={"shots": {"A01": {"prompt": NEW}}})
    assert res.status_code == 409 and "cannot change now" in res.json()["detail"]
    assert _shots_text() == before and shot_plan_edits.list_edits(EP) == []
    studio.temporal_client.handle.execute_update.assert_not_called()


async def test_a_run_that_cannot_answer_locks_the_shot_plan(studio):  # noqa: F811
    before = _write_shots()
    owner = _client(studio, "owner")
    _running(studio)
    studio.temporal_client.handle.query = AsyncMock(side_effect=RuntimeError("no worker"))
    assert owner.get(BASE).json()["phase"]["state"] == "locked"
    assert owner.post(BASE, json={"shots": {"A01": {"prompt": NEW}}}).status_code == 409
    assert _shots_text() == before


async def test_no_shot_list_yet(studio):  # noqa: F811
    owner = _client(studio, "owner")
    view = owner.get(BASE).json()
    assert view["uploaded"] is False and view["shots"] == [] and view["can_edit"] is False
    res = owner.post(BASE, json={"shots": {"A01": {"prompt": NEW}}})
    assert res.status_code == 404 and "no shot list yet" in res.json()["detail"]


def test_other_episode_rows_stay_apart():
    shot_plan_edits.record("ep90hist", [{"shot_id": "A01", "field": "prompt", "old": "a", "new": "b"}],
                           actor="u1", actor_name="One")
    shot_plan_edits.record("ep91hist", [{"shot_id": "A01", "field": "props", "old": None, "new": ["oar"]}],
                           actor="u2", actor_name="Two", run_id="r2", note="why")
    assert [(e["field"], e["new"]) for e in shot_plan_edits.list_edits("ep91hist")] == [("props", ["oar"])]
    with pytest.raises(ValueError):
        shot_plan_edits.record("ep90hist", [{"shot_id": "A01", "field": "model", "old": "a", "new": "b"}],
                               actor=None, actor_name=None)
    assert len(shot_plan_edits.list_edits("ep90hist")) == 1
