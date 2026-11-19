"""First frames API (api/concept_frame_routes.py): only the episode's owner or an admin prices, makes
and picks a shot's first frame (any member while it has no owner); only whoever may see the cast sees
the options; the price comes first and a round is refused above the approved total; a change in plain
words reaches the next round; the pick becomes the shot's pre-approved start still; nothing changes
while a run is going. Live: the episode must be in live mode, a worker must poll the first frames
queue, the provider must price every option (503 otherwise), and the budget line, the recorded
balance and the balance gate are checked before the round is recorded and its workflow started. No
call reaches a real provider: prices come from a stand-in and the workflow start is mocked."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from hfvg import concept_frames
from hfvg.paths import episode_dir
from tests.cast_helpers import image_bytes, two_prices
from tests.test_props_api import EP, REFUSED, SHARED_EP, _added, _client, _running, studio  # noqa: F401

BASE = f"/api/episodes/{EP}/concept-frames"
HIDDEN = "Only the people working on this episode (and admins) can see its first frames"
HF = "https://api.higgsfield.ai"
PRICE = 30_000


def _shot(sid: str, prompt: str, **extra) -> dict:
    return {"id": sid, "prompt": prompt, "count": 1, "characters": [], "wet": False, "exterior": True,
            "water": False, "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "ref_keys": [], **extra}


def _write_shots(ep: str = EP, **a01) -> None:
    folder = episode_dir(ep)
    folder.mkdir(parents=True, exist_ok=True)
    shots = [_shot("A01", "A brass key glints on the cabin table by a storm lamp.", **a01),
             _shot("A02", "The cabin door swings open onto the foggy quay.")]
    (folder / "shots.json").write_text(json.dumps({"episode_id": ep, "shots": shots}), encoding="utf-8")


def _shots(ep: str = EP) -> dict:
    return {s["id"]: s for s in json.loads((episode_dir(ep) / "shots.json").read_text())["shots"]}


def _dry_total(count: int = 3) -> int:
    from hfvg.estimates import still_call_usd_micros
    return still_call_usd_micros() * count


# ------------------------------------------------------------------------------------- who may do what

async def test_first_frames_need_a_login_and_another_member_neither_sees_nor_makes_them(studio):  # noqa: F811
    _write_shots()
    owner, other, anon = _client(studio, "owner"), _client(studio, "other"), _client(studio, None)
    made = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": _dry_total()}).json()["round"]
    option_id = made["options"][0]["option_id"]
    assert anon.get(BASE).status_code == 401
    assert anon.post(f"{BASE}/A01/estimate", json={}).status_code == 401
    res = other.get(BASE)
    assert res.status_code == 404 and res.json()["detail"] == HIDDEN
    res = other.get(f"{BASE}/images/{option_id}")
    assert res.status_code == 404 and res.json()["detail"] == HIDDEN
    for path, body in ((f"{BASE}/A01/estimate", {}), (f"{BASE}/A01", {"approved_total_usd_micros": 10 ** 9}),
                       (f"{BASE}/A01/pick", {"option_id": option_id}), (f"{BASE}/A01/unpick", {})):
        res = other.post(path, json=body)
        assert res.status_code == 403 and res.json()["detail"] == f"Episode {EP} {REFUSED}", (path, res.text)
    assert "pre_approved_still" not in _shots()["A01"]
    assert len(concept_frames.frames_view(EP, live=False)["shots"][0]["rounds"]) == 1


async def test_admins_and_any_member_of_an_episode_without_an_owner_make_first_frames(studio):  # noqa: F811
    from hfvg.studio_db import create_episode
    _write_shots()
    admin = _client(studio, "admin")
    res = admin.post(f"{BASE}/A02", json={"approved_total_usd_micros": _dry_total()})
    assert res.status_code == 200, res.text
    await create_episode(os.environ["DATABASE_PATH"], SHARED_EP)
    _write_shots(SHARED_EP)
    other = _client(studio, "other")
    assert other.get(f"/api/episodes/{SHARED_EP}/concept-frames").json()["can_change"] is True
    res = other.post(f"/api/episodes/{SHARED_EP}/concept-frames/A01", json={"approved_total_usd_micros": _dry_total()})
    assert res.status_code == 200, res.text
    option_id = res.json()["round"]["options"][1]["option_id"]
    res = other.post(f"/api/episodes/{SHARED_EP}/concept-frames/A01/pick", json={"option_id": option_id})
    assert res.status_code == 200 and _shots(SHARED_EP)["A01"]["pre_approved_still"]["approved_by"] == "Other"


# ------------------------------------------------------------------------------------- dry run

async def test_the_owner_prices_makes_tweaks_and_picks_a_first_frame(studio):  # noqa: F811
    owner = _client(studio, "owner")
    _added(owner, "Harbour cabin", "place", data=image_bytes(seed=1, color=(40, 60, 220)))
    _write_shots(place="harbour-cabin")
    view = owner.get(BASE).json()
    assert (view["can_change"], view["phase"], view["options_per_round"], view["dry_run"]) == (True, "open", 3, True)
    assert [s["shot_id"] for s in view["shots"]] == ["A01", "A02"]
    a01 = view["shots"][0]
    assert (a01["place"], a01["characters"], a01["references"], a01["ready"]) == ("Harbour cabin", [], 1, True)
    assert a01["description"] == "Harbour cabin" and a01["rounds"] == []
    assert "scene" not in a01 and "brass key glints" not in json.dumps(view).lower()  # the prompt stays here

    estimate = owner.post(f"{BASE}/A01/estimate", json={}).json()
    total = _dry_total()
    assert (estimate["count"], estimate["total_usd_micros"], estimate["dry_run"], estimate["round"]) == (
        3, total, True, 1)
    assert estimate["summary"] == "3 placeholder pictures, free in dry-run mode"
    assert estimate["place"] == "Harbour cabin" and estimate["references"] == 1

    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": total - 1})
    assert res.status_code == 409 and "review it and approve again" in res.json()["detail"]
    now, then = two_prices(res.json()["detail"])
    assert now != then
    assert owner.get(BASE).json()["shots"][0]["rounds"] == []

    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": total})
    assert res.status_code == 200, res.text
    made = res.json()
    assert (made["round"]["status"], made["round"]["made"], made["round"]["round"]) == ("ready", 3, 1)
    assert made["shot"]["rounds"][0]["current"] is True and made["estimate"]["count"] == 3
    option = made["round"]["options"][0]
    picture = owner.get(option["image_url"])
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
    assert "private" in picture.headers["cache-control"]
    thumb = owner.get(option["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"

    tweak = owner.post(f"{BASE}/A01/estimate", json={"change": "make it night"}).json()
    assert tweak["changes"] == ["make it night"] and tweak["round"] == 2
    res = owner.post(f"{BASE}/A01", json={"change": "make it night", "approved_total_usd_micros": total})
    assert res.status_code == 200 and res.json()["round"]["changes"] == ["make it night"]
    assert [r["round"] for r in res.json()["shot"]["rounds"]] == [2, 1]
    assert res.json()["shot"]["changes"] == ["make it night"]

    night = res.json()["round"]["options"][2]
    res = owner.post(f"{BASE}/A01/pick", json={"option_id": night["option_id"]})
    assert res.status_code == 200, res.text
    assert res.json()["shot"]["pick"]["option_id"] == night["option_id"]
    still = _shots()["A01"]["pre_approved_still"]
    assert still["approved_by"] == "Owner" and still["path"].startswith("concept_frames/A01-")
    assert (episode_dir(EP) / still["path"]).is_file()
    assert owner.get(BASE).json()["shots"][0]["pick"]["source"] == "here"

    res = owner.post(f"{BASE}/A01/unpick", json={})
    assert res.status_code == 200 and res.json()["shot"]["pick"] is None
    assert "pre_approved_still" not in _shots()["A01"]
    for text in (json.dumps(view), json.dumps(made), json.dumps(estimate)):
        assert "grok" not in text.lower() and "model_path" not in text and "adapter" not in text


async def test_a_pick_from_an_earlier_version_of_the_shot_or_over_a_hand_set_frame_is_refused(studio):  # noqa: F811
    owner = _client(studio, "owner")
    _write_shots()
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": _dry_total()})
    assert res.status_code == 200, res.text
    option_id = res.json()["round"]["options"][0]["option_id"]
    shots = json.loads((episode_dir(EP) / "shots.json").read_text())
    shots["shots"][0]["prompt"] = "The brass key lies on the cabin floor under a broken lamp."
    (episode_dir(EP) / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    res = owner.post(f"{BASE}/A01/pick", json={"option_id": option_id})
    assert res.status_code == 409 and "changed since these options were made" in res.json()["detail"], res.text
    assert "pre_approved_still" not in _shots()["A01"]

    hand = {"path": "stills/a01_owner.png", "approved_by": "owner"}
    _write_shots(pre_approved_still=hand)
    res = owner.post(f"{BASE}/A01/pick", json={"option_id": option_id})
    assert res.status_code == 409 and "set in its shot list" in res.json()["detail"], res.text
    assert _shots()["A01"]["pre_approved_still"] == hand


async def test_bad_requests_change_nothing(studio):  # noqa: F811
    _write_shots()
    owner = _client(studio, "owner")
    res = owner.post(f"{BASE}/A01/estimate", json={"change": "add some gore"})
    assert res.status_code == 400 and "Please reword" in res.json()["detail"]
    res = owner.post(f"{BASE}/Z99/estimate", json={})
    assert res.status_code == 404
    res = owner.post(f"{BASE}/A01", json={})
    assert res.status_code == 422
    res = owner.post(f"{BASE}/A01/pick", json={"option_id": "cframe-000000000000-0"})
    assert res.status_code == 404
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": _dry_total(), "change": "x" * 230})
    assert res.status_code in (400, 422)
    assert owner.get(f"{BASE}/images/cframe-000000000000-0").status_code == 404
    assert owner.get(BASE).json()["shots"][0]["rounds"] == [] and "pre_approved_still" not in _shots()["A01"]


async def test_first_frames_wait_while_a_run_is_going(studio):  # noqa: F811
    _write_shots()
    owner = _client(studio, "owner")
    made = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": _dry_total()}).json()["round"]
    _running(studio)
    view = owner.get(BASE).json()
    assert (view["can_change"], view["may_change"], view["phase"]) == (False, True, "locked")
    assert "has a run going" in view["locked_reason"]
    for path, body in ((f"{BASE}/A01/estimate", {}), (f"{BASE}/A01", {"approved_total_usd_micros": 10 ** 9}),
                       (f"{BASE}/A01/pick", {"option_id": made["options"][0]["option_id"]}),
                       (f"{BASE}/A01/unpick", {})):
        res = owner.post(path, json=body)
        assert res.status_code == 409 and "has a run going" in res.json()["detail"], (path, res.text)
    assert "pre_approved_still" not in _shots()["A01"]


async def test_the_shots_words_are_left_out_for_who_may_not_read_its_prompts(studio):  # noqa: F811
    """The owner of another episode of the series sees the series cast, not this episode's prompts."""
    from tests.test_props_api import OTHER_EP
    _write_shots()
    owner, other, admin = _client(studio, "owner"), _client(studio, "other"), _client(studio, "admin")
    sid = owner.post("/api/series", json={"name": "Harbour tales"}).json()["series"]["series_id"]
    assert owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid}).status_code == 200
    assert admin.post(f"/api/episodes/{OTHER_EP}/series", json={"series_id": sid}).status_code == 200
    view = other.get(BASE)
    assert view.status_code == 200, view.text
    body = view.json()
    assert (body["words_hidden"], body["may_change"], body["can_change"]) == (True, False, False)
    assert "scene" not in body["shots"][0] and body["shots"][0]["beat"] is None
    assert "brass key" not in json.dumps(body).lower()


# ------------------------------------------------------------------------------------- live

class Pricer:
    """The provider's estimate, as HiggsfieldStillProvider gives it (no other call is made here)."""

    price: object = PRICE
    estimates = 0

    def __init__(self, model_path=None, **_kw):
        self.model_path = model_path

    async def estimate_usd_and_credits(self, **params):
        Pricer.estimates += 1
        if isinstance(self.price, Exception):
            raise self.price
        return self.price, 0.5

    async def close(self):
        pass


@pytest.fixture
async def live(studio, monkeypatch):  # noqa: F811
    """Live mode with a recorded balance, a worker on the first frames queue and a provider price."""
    from hfvg.providers import higgsfield_still
    from hfvg.studio_db import set_live_mode
    from hfvg.workflows import concept_frame as frames_workflow
    from tests.ledger_helpers import set_balance
    db = os.environ["DATABASE_PATH"]
    for key, value in {"DRY_RUN": "false", "HIGGSFIELD_API_KEY": "test_id:test_secret",
                       "HIGGSFIELD_BASE_URL": HF}.items():
        monkeypatch.setenv(key, value)
    Pricer.price, Pricer.estimates = PRICE, 0
    monkeypatch.setattr(higgsfield_still, "HiggsfieldStillProvider", Pricer)
    polls = AsyncMock(return_value=True)
    monkeypatch.setattr(frames_workflow, "worker_polls", polls)
    studio.temporal_client.start_workflow = AsyncMock(return_value=None)
    studio.polls = polls
    await set_balance(db, usd="100.00", episode_id="ep-unrelated")
    await set_live_mode(db, EP, True)
    _write_shots()
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{HF}/models/usage").mock(return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
        yield studio


def _no_round(api_main) -> None:
    api_main.temporal_client.start_workflow.assert_not_called()
    assert concept_frames.frames_view(EP, live=True)["shots"][0]["rounds"] == []


async def test_live_first_frames_are_priced_by_the_provider_and_started_on_their_own_queue(live):
    from hfvg.workflows.concept_frame import ConceptFrameWorkflow
    owner = _client(live, "owner")
    estimate = owner.post(f"{BASE}/A01/estimate", json={}).json()
    assert (estimate["dry_run"], estimate["per_image_usd_micros"], estimate["total_usd_micros"]) == (
        False, [PRICE] * 3, 3 * PRICE)
    assert estimate["budget"]["available_usd_micros"] > 3 * PRICE
    assert estimate["summary"].startswith("3 pictures, about $0.09, charged to this episode's budget for draft pictures")
    approved = 3 * PRICE + 3_000
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": approved})
    assert res.status_code == 200, res.text
    made = res.json()["round"]
    assert (made["status"], made["dry_run"], made["made"]) == ("making", False, 0)
    assert [o["status"] for o in made["options"]] == ["queued"] * 3
    assert res.json()["shot"]["making"] is True
    call = live.temporal_client.start_workflow.call_args
    assert call.args[0] == ConceptFrameWorkflow.run
    request = call.args[1]
    assert (request["set_id"], request["episode_id"], request["shot_id"]) == (made["set_id"], EP, "A01")
    assert call.kwargs["task_queue"] == "hfvg-tasks-frames" and call.kwargs["id"] == f"concept-frames-{made['set_id']}"
    assert [j["approved_usd_micros"] for j in request["jobs"]] == [PRICE + 1_000] * 3
    assert [j["estimated_usd_micros"] for j in request["jobs"]] == [PRICE] * 3
    # another round waits for this one
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": approved})
    assert res.status_code == 409 and "being made" in res.json()["detail"]
    assert live.temporal_client.start_workflow.call_count == 1


async def test_live_first_frames_need_live_mode(live):
    from hfvg.studio_db import set_live_mode
    await set_live_mode(os.environ["DATABASE_PATH"], EP, False)
    owner = _client(live, "owner")
    for path in (f"{BASE}/A01/estimate", f"{BASE}/A01"):
        res = owner.post(path, json={"approved_total_usd_micros": 10 ** 9})
        assert res.status_code == 409 and "not in live mode" in res.json()["detail"], res.text
    assert Pricer.estimates == 0
    _no_round(live)


async def test_live_first_frames_need_a_worker_that_makes_them(live):
    owner = _client(live, "owner")
    live.polls.return_value = False
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9})
    assert res.status_code == 409 and "cannot make them yet" in res.json()["detail"]
    live.polls.side_effect = RuntimeError("temporal down")
    res = owner.post(f"{BASE}/A01/estimate", json={})
    assert res.status_code == 503 and "cannot be reached" in res.json()["detail"]
    assert Pricer.estimates == 0
    _no_round(live)


@pytest.mark.parametrize("price", [ValueError("no usd"), 0, None])
async def test_live_first_frames_without_a_provider_price_make_nothing(live, price):
    Pricer.price = price
    owner = _client(live, "owner")
    for path in (f"{BASE}/A01/estimate", f"{BASE}/A01"):
        res = owner.post(path, json={"approved_total_usd_micros": 10 ** 9})
        assert res.status_code == 503 and "did not give a price" in res.json()["detail"], res.text
    _no_round(live)


async def test_live_first_frames_above_the_approved_total_make_nothing(live):
    res = _client(live, "owner").post(f"{BASE}/A01", json={"approved_total_usd_micros": 3 * PRICE - 1})
    assert res.status_code == 409 and "review it and approve again" in res.json()["detail"]
    assert two_prices(res.json()["detail"]) == ("$0.090000", "$0.089999")
    _no_round(live)


async def test_a_new_price_and_the_approved_one_read_differently(live):
    Pricer.price = 61_000  # three options: $0.183, where $0.180 was approved
    res = _client(live, "owner").post(f"{BASE}/A01", json={"approved_total_usd_micros": 180_000})
    assert res.status_code == 409
    assert res.json()["detail"] == "The estimate is now $0.183 (you approved $0.180); review it and approve again"
    _no_round(live)


async def test_live_first_frames_stop_at_the_draft_budget_line(live):
    from hfvg.activities.concept_frame import _ensure_budget_lines
    from hfvg.budget import BudgetLedger
    db = os.environ["DATABASE_PATH"]
    await _ensure_budget_lines(db, EP)
    ledger = BudgetLedger(db)
    line = await ledger.get_line_status(EP, concept_frames.LINE)
    assert await ledger.reserve(EP, concept_frames.LINE, line["available"] - PRICE, "earlier drafts",
                                job_id="draft-earlier")
    res = _client(live, "owner").post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9})
    assert res.status_code == 409 and "budget for draft pictures" in res.json()["detail"], res.text
    _no_round(live)


async def test_live_first_frames_need_a_fresh_balance_and_pass_the_balance_gate(live, monkeypatch):
    import hfvg.balance_monitor  # noqa: F401 - it reads HFVG_BALANCE_MAX_AGE_HOURS as a whole number once, on import
    from hfvg.budget import BudgetLedger
    from tests.ledger_helpers import set_balance
    db = os.environ["DATABASE_PATH"]
    owner = _client(live, "owner")
    monkeypatch.setenv("HFVG_BALANCE_MAX_AGE_HOURS", "0.0000001")  # the balance was recorded before this
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9})
    assert res.status_code == 409 and "Nothing was charged: GC.01" in res.json()["detail"], res.text
    monkeypatch.delenv("HFVG_BALANCE_MAX_AGE_HOURS")
    await set_balance(db, usd="0.01", episode_id="ep-unrelated")
    assert (await BudgetLedger(db).get_balance_status())["manual_balance_usd_micros"] == 10_000
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9})
    assert res.status_code == 409 and "Balance check failed" in res.json()["detail"], res.text
    _no_round(live)


async def test_a_round_whose_workflow_cannot_start_ends_without_charge(live):
    live.temporal_client.start_workflow = AsyncMock(side_effect=RuntimeError("no temporal"))
    owner = _client(live, "owner")
    res = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9})
    assert res.status_code == 503 and "nothing was made or charged" in res.json()["detail"]
    rounds = concept_frames.frames_view(EP, live=True)["shots"][0]["rounds"]
    assert [(r["status"], r["made"]) for r in rounds] == [("failed", 0)]
    assert [o["status"] for o in rounds[0]["options"]] == ["skipped"] * 3
    # the next try is not held up by it
    live.temporal_client.start_workflow = AsyncMock(return_value=None)
    assert owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9}).status_code == 200


async def test_a_round_whose_workflow_ended_without_finishing_it_is_finished_by_the_page(live):
    from unittest.mock import MagicMock

    from temporalio.client import WorkflowExecutionStatus
    owner = _client(live, "owner")
    made = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": 10 ** 9}).json()["round"]
    live.temporal_client.handle.describe = AsyncMock(return_value=MagicMock(status=WorkflowExecutionStatus.FAILED))
    view = owner.get(BASE).json()
    assert view["shots"][0]["making"] is False
    assert concept_frames.get_set(made["set_id"])["status"] == "failed"


async def test_live_never_picks_a_dry_run_stand_in(live, monkeypatch):
    owner = _client(live, "owner")
    monkeypatch.setenv("DRY_RUN", "true")
    made = owner.post(f"{BASE}/A01", json={"approved_total_usd_micros": _dry_total()}).json()["round"]
    monkeypatch.setenv("DRY_RUN", "false")
    res = owner.post(f"{BASE}/A01/pick", json={"option_id": made["options"][0]["option_id"]})
    assert res.status_code == 409 and "dry-run stand-in" in res.json()["detail"]
    assert "pre_approved_still" not in _shots()["A01"]
