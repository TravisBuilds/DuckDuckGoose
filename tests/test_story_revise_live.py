"""Live story shaping revises a flagged outline and the run goes on.

A live run whose critique flagged issues crashed in revise_story_shape_activity: the prompt did
json.dumps(story.model_dump()) and StoryShape.created_at is a datetime ("Object of type datetime is not
JSON serializable"), so the workflow failed before the story gate. These tests take the LIVE path with
a mocked OpenAI client: no DRY_RUN short-circuit and no set_test_stub_story_shape (either one sends
revise down the deterministic branch that never builds the prompt).
"""

import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.activities.story_shaping import critique_story_shape_activity, revise_story_shape_activity
from hfvg.config import config
from hfvg.models import StoryShape
from hfvg.story_structures import build_layout

ADMIN_SECRET = "story-revise-admin-secret-0123456789abcdef"
IDEA = "A sleepy cat watches the rain slide down a cafe window"
LOGLINE = "A sleepy cafe cat and a rainy afternoon"
REVISED_LOGLINE = "A sleepy cafe cat waits out the rain"
HOOK = "A sleepy cat stares at the rain on the cafe window"
FLAGGED_PAYOFF = "Everything stops."  # no keyword shared with the hook: the critique flags it
REVISED_PAYOFF = "The sleepy cat dozes off as the rain on the window stops"
CREATED = datetime(2026, 11, 11, 9, 30, tzinfo=timezone.utc)
PAYOFF_ISSUE = "Payoff doesn't explicitly reference the hook (no shared keywords)"
BEATMAP = """# Episode {ep} Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Cafe | Day | A | A cat watches the rain | Open | - | - | K |
"""


def story(payoff: str, logline: str = LOGLINE) -> StoryShape:
    """A full outline (acts, beats, created_at) in the default structure."""
    layout = build_layout(None, IDEA, 30.0, config.STORY_SHAPING_MAX_BEAT_DURATION_S)
    return StoryShape(logline=logline, hook=HOOK, hook_deadline_s=4.0, acts=layout["acts"],
                      beats=layout["beats"], midpoint_beat_id=layout["midpoint_beat_id"], payoff=payoff,
                      total_duration_s=layout["total_duration_s"], created_at=CREATED)


def model_reply(payoff: str, logline: str = LOGLINE) -> dict:
    """The outline as the model answers it (the prompt's JSON format, nested acts[].beats)."""
    shape = story(payoff, logline).model_dump(mode="json")
    beats = {b["beat_id"]: b for b in shape["beats"]}
    return {
        "logline": logline, "hook": shape["hook"], "hook_deadline_s": shape["hook_deadline_s"],
        "acts": [{"act_number": a["act_number"], "description": a["description"],
                  "target_duration_s": a["target_duration_s"], "beats": [beats[i] for i in a["beat_ids"]]}
                 for a in shape["acts"]],
        "midpoint_beat_id": shape["midpoint_beat_id"], "payoff": payoff,
        "payoff_references_hook": False, "total_duration_s": shape["total_duration_s"],
    }


def fake_openai(monkeypatch, *replies: dict) -> list:
    """openai.AsyncOpenAI answering the chat completions with `replies` in order; returns the requests."""
    import openai

    sent: list = []

    async def create(**kwargs):
        sent.append(kwargs)
        assert len(sent) <= len(replies), f"unexpected model call #{len(sent)}"
        message = SimpleNamespace(content=json.dumps(replies[len(sent) - 1]))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeAsyncOpenAI:
        def __init__(self, api_key=None, **kwargs):
            assert api_key == "sk-story-test"
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-story-test")
    return sent


def revise_prompt_outline(request: dict) -> dict:
    """The outline JSON a revise request sent to the model."""
    prompt = request["messages"][-1]["content"]
    assert prompt.startswith("Revise this story structure based on critique.")
    return json.loads(prompt.split("Original:\n", 1)[1].split("\n\nCritique issues:", 1)[0])


def live_activities(*replace) -> list:
    """The production activities for a live run up to G1, without uploaded shot data or provider calls:
    the file check, the shot-data overlay, preflight and the provider estimates are stubbed (they are
    not what these tests are about); story shaping is real. `replace` swaps in more stubs."""
    from hfvg.activities.g1_plan import compute_g1_plan_activity
    from hfvg.episode_parser import parse_beatmap
    from hfvg.worker import PRODUCTION_ACTIVITIES

    @activity.defn(name="verify_episode_files_activity")
    async def files_present(episode_id):
        return {"passed": True, "missing_files": []}

    @activity.defn(name="parse_beatmap_activity")
    async def parse_without_shot_data(beatmap_path=None, episode_id=None, require_shot_data=False):
        return {"shots": parse_beatmap(beatmap_path)}

    @activity.defn(name="preflight_validate_episode_activity")
    async def preflight_passes(episode_id, shots_subset=None, is_live=False):
        return {"passed": True}

    @activity.defn(name="compute_g1_plan_activity")
    async def plan_without_estimates(episode_id, shots, dry_run):
        return await compute_g1_plan_activity(episode_id, shots, True)

    stubs = [files_present, parse_without_shot_data, preflight_passes, plan_without_estimates, *replace]
    names = {activity._Definition.from_callable(a).name for a in stubs}
    kept = [a for a in PRODUCTION_ACTIVITIES if activity._Definition.from_callable(a).name not in names]
    return kept + stubs


@asynccontextmanager
async def studio(monkeypatch, activities: list, live: bool = True, workflows: list | None = None):
    """The API (as an admin client) on a Temporal dev server with a production-queue worker."""
    from httpx import ASGITransport, AsyncClient

    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    from hfvg.temporal_converter import temporal_data_converter
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "false" if live else "true")
    await init_studio_db(os.environ["DATABASE_PATH"])
    await CreditLedger(os.environ["DATABASE_PATH"]).init_db()
    sys.modules.pop("api.main", None)
    import api.main as api_main

    async with await WorkflowEnvironment.start_local(data_converter=temporal_data_converter) as env:
        async with Worker(env.client, task_queue="hfvg-tasks", workflows=workflows or [EpisodeWorkflowV2],
                          activities=activities):
            monkeypatch.setattr(api_main, "temporal_client", env.client)
            async with AsyncClient(transport=ASGITransport(app=api_main.app), base_url="http://test",
                                   headers={"Authorization": f"Bearer {ADMIN_SECRET}"}) as api:
                yield env, api


async def start(api, episode_id: str, live: bool = True) -> dict:
    res = await api.post("/api/episodes", json={
        "episode_id": episode_id, "beatmap_content": BEATMAP.format(ep=episode_id), "dry_run": not live,
        "enable_story_shaping": True})
    assert res.status_code == 200, res.text
    return res.json()


async def wait_for(api, path: str, ready, what: str, seconds: float = 30.0) -> dict:
    """GET `path` until `ready(body)`."""
    import asyncio

    body = None
    for _ in range(int(seconds / 0.2)):
        res = await api.get(path)
        if res.status_code == 200:
            body = res.json()
            if ready(body):
                return body
        await asyncio.sleep(0.2)
    pytest.fail(f"{path}: never {what} (last: {res.status_code} {json.dumps(body)[:1500] if body else res.text})")


def revised_and_recritiqued(body: dict) -> bool:
    log = [entry["action"] for entry in body.get("log") or []]
    return log == ["generated", "revised"] and (body.get("critique") or {}).get("payoff_references_hook") is True


@pytest.mark.parametrize("as_sent", ["model", "json"], ids=["created_at-datetime", "created_at-iso-string"])
async def test_live_revise_prompt_carries_the_whole_outline(monkeypatch, as_sent):
    """Revise gets the outline as a dict (datetime created_at from an activity result, or the ISO string
    the workflow holds after Temporal's converter) and must send all of it to the model."""
    outline = story(FLAGGED_PAYOFF)
    shape = outline.model_dump() if as_sent == "model" else outline.model_dump(mode="json")
    critique = await critique_story_shape_activity(shape)
    assert critique["needs_revision"] is True
    assert PAYOFF_ISSUE in critique["critique"]["issues"]
    sent = fake_openai(monkeypatch, model_reply(REVISED_PAYOFF, REVISED_LOGLINE))

    result = await ActivityEnvironment().run(revise_story_shape_activity, shape, critique["critique"], False)

    assert result["dry_run"] is False
    assert result["changes_made"][0] == "Revised based on critique"
    assert (result["story_shape"]["logline"], result["story_shape"]["payoff"]) == (REVISED_LOGLINE, REVISED_PAYOFF)
    assert [a["description"] for a in result["story_shape"]["acts"]] == [a.description for a in outline.acts]
    [request] = sent
    assert request["model"] == config.STORY_SHAPING_MODEL
    sent_outline = revise_prompt_outline(request)
    assert sent_outline == outline.model_dump(mode="json")
    assert sent_outline["created_at"] == "2026-11-11T09:30:00Z"
    assert PAYOFF_ISSUE in request["messages"][-1]["content"]
    recritique = await critique_story_shape_activity(result["story_shape"])
    assert recritique["critique"]["payoff_references_hook"] is True


async def test_live_story_shaping_revises_saves_approves_and_reaches_g1(monkeypatch):
    """The incident's path end to end: live run, critique flags the outline, revise runs (full outline in
    the prompt, created_at included) and the run goes on: story gate, outline save, approval, G1."""
    ep = "ep84live"
    sent = fake_openai(monkeypatch, model_reply(FLAGGED_PAYOFF), model_reply(REVISED_PAYOFF, REVISED_LOGLINE))
    async with studio(monkeypatch, live_activities()) as (env, api):
        await start(api, ep)
        shaped = await wait_for(api, f"/api/episodes/{ep}/story-shape", revised_and_recritiqued,
                                "revised and re-critiqued")

        assert len(sent) == 2, "generate, then revise (both live)"
        generated = revise_prompt_outline(sent[1])
        assert (generated["logline"], generated["payoff"]) == (LOGLINE, FLAGGED_PAYOFF)
        assert datetime.fromisoformat(generated["created_at"]).tzinfo is not None
        assert [b["beat_id"] for b in generated["beats"]] == [b.beat_id for b in story(FLAGGED_PAYOFF).beats]
        assert PAYOFF_ISSUE in sent[1]["messages"][-1]["content"]
        assert shaped["revision"]["dry_run"] is False
        assert shaped["revision"]["changes_made"][0] == "Revised based on critique"
        assert (shaped["story_shape"]["logline"], shaped["story_shape"]["payoff"]) == (REVISED_LOGLINE, REVISED_PAYOFF)

        state = (await api.get(f"/api/episodes/{ep}")).json()
        assert (state["stage"], state["current_gate"], state["approvals"]["story_shape"]) == ("script", "", False)
        assert (state["run"]["status"], state["run"]["ended"]) == ("RUNNING", False)

        edited = dict(shaped["story_shape"], logline="A cafe cat, the rain and one long nap")
        res = await api.put(f"/api/episodes/{ep}/story-shape", json=edited)
        assert res.status_code == 200, res.text
        assert res.json()["story_shape"]["logline"] == "A cafe cat, the rain and one long nap"

        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
        assert res.status_code == 200, res.text

        state = await wait_for(api, f"/api/episodes/{ep}", lambda s: s["current_gate"] == "G1_SHOT_PLAN",
                               "at G1_SHOT_PLAN")
        assert state["approvals"]["story_shape"] is True
        assert state["run"]["ended"] is False
        final = (await api.get(f"/api/episodes/{ep}/story-shape")).json()
        assert final["story_shape"]["logline"] == "A cafe cat, the rain and one long nap"
        assert [e["action"] for e in final["log"]] == ["generated", "revised", "updated", "approved"]
        assert any("S01" in beat["shot_ids"] for beat in final["story_shape"]["beats"])
        assert len(sent) == 2
