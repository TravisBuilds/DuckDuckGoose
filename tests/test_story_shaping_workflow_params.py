"""The create wizard's brief reaches story shaping through the workflow input.

API start -> EpisodeWorkflowV2 on a real Temporal test server -> the real generate_story_shape_activity
gets [episode_id, idea, the card's profile dict, the clamped length, dry_run], converted by the
project's data converter from the activity's own type hints. The workflow reads no preset file: the
API resolved the card and sent it as the optional 9th run input.
"""

import json
from types import SimpleNamespace

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor, Worker

from hfvg.config import config
from hfvg.genre_presets import resolve_profile
from hfvg.story_structures import build_layout, prompt_guidance

ADMIN_SECRET = "story-params-admin-secret-0123456789abcdef"
IDEA = "A sleepy cat watches the rain slide down a cafe window"
BEATMAP = """# Episode {ep} Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Cafe | Day | A | A cat watches the rain | Open | - | - | K |
"""


class _RecordStoryShapeArgs(Interceptor):
    """Records the story shaping arguments as the real activity receives them (after conversion)."""

    def __init__(self, captured: list):
        self.captured = captured

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        captured = self.captured

        class _Record(ActivityInboundInterceptor):
            async def execute_activity(self, input: ExecuteActivityInput):
                if activity.info().activity_type == "generate_story_shape_activity":
                    captured.append(list(input.args))
                return await super().execute_activity(input)

        return _Record(next)


def _activities(live: bool) -> list:
    from hfvg.worker import PRODUCTION_ACTIVITIES

    if not live:
        return list(PRODUCTION_ACTIVITIES)

    @activity.defn(name="revise_story_shape_activity")
    async def keep_story_shape(story_shape, critique, dry_run):
        return {"story_shape": story_shape, "changes_made": [], "dry_run": dry_run}

    @activity.defn(name="verify_episode_files_activity")
    async def files_present(episode_id):
        return {"ok": True}

    replaced = {"revise_story_shape_activity", "verify_episode_files_activity"}
    kept = [a for a in PRODUCTION_ACTIVITIES if activity._Definition.from_callable(a).name not in replaced]
    return kept + [keep_story_shape, files_present]


def _fake_openai(monkeypatch, reply: dict) -> list:
    """openai.AsyncOpenAI answering every chat completion with `reply`; returns the sent requests."""
    import openai

    sent: list = []

    async def create(**kwargs):
        sent.append(kwargs)
        message = SimpleNamespace(content=json.dumps(reply))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeAsyncOpenAI:
        def __init__(self, api_key=None, **kwargs):
            assert api_key == "sk-story-test"
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-story-test")
    return sent


def _model_reply(layout: dict) -> dict:
    """A model answer in the prompt's JSON format (nested acts[].beats) for a layout."""
    beats = {b["beat_id"]: b for b in layout["beats"]}
    return {
        "logline": layout["logline"], "hook": layout["hook"], "hook_deadline_s": 5.0,
        "acts": [{"act_number": a["act_number"], "description": a["description"],
                  "target_duration_s": a["target_duration_s"], "beats": [beats[i] for i in a["beat_ids"]]}
                 for a in layout["acts"]],
        "midpoint_beat_id": layout["midpoint_beat_id"], "payoff": layout["payoff"],
        "payoff_references_hook": True, "total_duration_s": layout["total_duration_s"],
    }


async def _start_and_wait(monkeypatch, episode_id: str, body: dict, live: bool = False):
    import os
    import sys

    from fastapi.testclient import TestClient

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

    captured: list = []
    async with await WorkflowEnvironment.start_time_skipping(data_converter=temporal_data_converter) as env:
        async with Worker(env.client, task_queue="hfvg-tasks", workflows=[EpisodeWorkflowV2],
                          activities=_activities(live), interceptors=[_RecordStoryShapeArgs(captured)]):
            api_main.temporal_client = env.client
            res = TestClient(api_main.app).post(
                "/api/episodes", headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
                json={"episode_id": episode_id, "beatmap_content": BEATMAP.format(ep=episode_id),
                      "dry_run": not live, "enable_story_shaping": True, **body})
            assert res.status_code == 200, res.text
            handle = env.client.get_workflow_handle(episode_id)
            for _ in range(60):
                state = await handle.query("get_state")
                if state.get("story_shape"):
                    return captured, state
                await env.sleep(1)
    pytest.fail(f"{episode_id}: no story shape (captured {captured})")


async def test_workflow_story_shaping_receives_idea_and_preset(monkeypatch):
    profile = resolve_profile("cozy-hangout")
    captured, state = await _start_and_wait(monkeypatch, "ep42loop", {
        "archetype_id": "cozy-hangout", "idea": IDEA, "target_duration": 12})
    assert captured == [["ep42loop", IDEA, profile, 12.0, True]]
    assert (state["archetype_id"], state["target_duration_s"]) == ("cozy-hangout", 12)
    story = state["story_shape"]
    expected = build_layout(profile, IDEA, 12.0, config.STORY_SHAPING_MAX_BEAT_DURATION_S)
    assert [a["description"] for a in story["acts"]] == [a["description"] for a in expected["acts"]]
    assert {a["description"] for a in story["acts"]}.isdisjoint({"Setup", "Confrontation", "Resolution"})
    assert story["total_duration_s"] == pytest.approx(12.0)
    assert sum(b["target_duration_s"] for b in story["beats"]) == pytest.approx(12.0, abs=0.5)


async def test_workflow_story_shaping_without_a_brief_keeps_the_defaults(monkeypatch):
    captured, state = await _start_and_wait(monkeypatch, "ep42plain", {})
    assert captured == [["ep42plain", "Episode ep42plain", None, 120.0, True]]
    assert (state["archetype_id"], state["target_duration_s"]) == (None, None)
    assert [a["description"] for a in state["story_shape"]["acts"]] == ["Setup", "Confrontation", "Resolution"]


async def test_workflow_story_shaping_live_prompt_follows_the_card(monkeypatch):
    card = "time-travel-vlog"
    profile = resolve_profile(card)
    max_beat = config.STORY_SHAPING_MAX_BEAT_DURATION_S
    layout = build_layout(profile, IDEA, 50.0, max_beat)
    sent = _fake_openai(monkeypatch, _model_reply(layout))
    captured, state = await _start_and_wait(monkeypatch, "ep42live", {
        "archetype_id": card, "idea": IDEA, "target_duration": 50}, live=True)
    assert captured == [["ep42live", IDEA, profile, 50.0, False]]
    assert len(sent) == 1 and sent[0]["model"] == config.STORY_SHAPING_MODEL
    prompt = sent[0]["messages"][-1]["content"]
    assert prompt_guidance(profile, 50.0, max_beat) in prompt
    assert f"Format: {profile['name']} ({profile['template']})" in prompt
    assert "3-act" not in prompt
    assert [a["description"] for a in state["story_shape"]["acts"]] == [a["description"] for a in layout["acts"]]


def test_workflow_code_never_reads_the_preset_registry():
    """The card reaches the workflow as plain data; reading the registry file stays in the API."""
    from pathlib import Path

    from hfvg.workflows import episode_v2

    source = Path(episode_v2.__file__).read_text(encoding="utf-8")
    for name in ("genre_presets", "get_preset", "resolve_profile", "REGISTRY_PATH", "yaml", "open("):
        assert name not in source, f"hfvg/workflows/episode_v2.py references {name!r}"
