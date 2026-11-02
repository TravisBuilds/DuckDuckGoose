"""GAP-66 (ep05v2 r12, 2026-10-28 17:30 CET): retry-clip sent the update without checking the clip prompt
it would SEND (plan prompt or override + the prompt kit's continuity locks). V22's launched clip prompt
2409 + the injected duck locks = 3149 > Kling's 2500 was refused inside the child (no spend), after the
workflow had already counted clip retry 3 of 3. The route now preflights length + clip lint on the
injected prompt (from the new retry_clip_plan query = the LAUNCHED plan) and answers 409 with nothing
sent. No network, no spend."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.lint_helpers import SYNTHETIC_LINT_KIT, lint_ok
from tests.test_continuity_locks import LOCKS
from tests.test_retry_prompt_preflight import client  # noqa: F401  (fixture)

DUCK_META = {"count": 0, "exterior": False, "water": False, "characters": ["Duck"]}
CLAUSE = SYNTHETIC_LINT_KIT["duck_clause"]


def _prompt(n):
    base = lint_ok("Scene", 0) + f" {CLAUSE}. "
    return base + "x" * (n - len(base))


class _Handle:
    def __init__(self, plan, fail=False):
        self.plan, self.fail, self.updates, self.queries = plan, fail, [], []

    async def query(self, name, *args, **kw):
        self.queries.append(name)
        if self.fail:
            raise RuntimeError("no such query")
        assert name == "retry_clip_plan", name
        return self.plan

    async def execute_update(self, name, *a, **kw):
        self.updates.append(name)
        return {"status": "queued", "workflow_id": "ep77-shot-A01-clipretry1", "retry": 1, "job_id": "j"}


class _Client:
    def __init__(self, h):
        self.h = h

    def get_workflow_handle(self, wid):
        return self.h


@pytest.fixture
def kit(tmp_path, monkeypatch):
    ep = tmp_path / "eps" / "ep77"
    ep.mkdir(parents=True)
    (ep / "prompt_kit.json").write_text(json.dumps({"lint": dict(SYNTHETIC_LINT_KIT, continuity_locks=LOCKS)}))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    # GAP-65's gates are not under test here
    import api.main as api_main

    async def _no_gate(*a, **k):
        return None
    monkeypatch.setattr(api_main, "_paid_gate_problem", _no_gate)


def _plan(prompt):
    return {"found": True, "clip_prompt": prompt, "clip_model": "kling", "shot_meta": DUCK_META}


def test_plan_prompt_over_the_limit_after_locks_is_refused_before_the_update(client, kit, monkeypatch):  # noqa: F811
    api_main, c = client
    monkeypatch.setenv("DRY_RUN", "false")
    h = _Handle(_plan(_prompt(2409)))  # fits alone; the injected duck locks push it past 2500
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t", "duration_s": 8})
    assert r.status_code == 409, r.text
    assert "GAP-66" in r.json()["detail"] and "accepts 2500" in r.json()["detail"]
    assert h.updates == []
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip",
               json={"reason": "t", "duration_s": 8, "prompt_override": _prompt(1500)})
    assert r.status_code == 200, r.text
    assert h.updates == ["retry_clip"]


def test_override_failing_the_clip_lint_is_refused(client, kit, monkeypatch):  # noqa: F811
    api_main, c = client
    monkeypatch.setenv("DRY_RUN", "false")
    h = _Handle(_plan(_prompt(800)))
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip",
               json={"reason": "t", "prompt_override": "A duck at a lantern. Real-time speed, no slow motion."})
    assert r.status_code == 409 and "clip prompt lint" in r.json()["detail"] and h.updates == [], r.text


def test_live_unreadable_plan_fails_closed_dry_run_does_not(client, kit, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(None, fail=True)
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    monkeypatch.setenv("DRY_RUN", "false")
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t"})
    assert r.status_code == 409 and "could not be read" in r.json()["detail"] and h.updates == []
    monkeypatch.setenv("DRY_RUN", "true")
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t"})
    assert r.status_code == 200 and h.updates == ["retry_clip"], r.text


def test_retry_clip_plan_query_reads_the_launched_plan_not_the_beatmap():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as W
    fake = SimpleNamespace(launched_shot_plans={
        "A01": {"prompt": "still p", "clip_prompt": "launched clip p", "params": {"model": "seedance"},
                "shot_meta": DUCK_META},
        "A02": {"prompt": "only still p"}},
        parsed_beatmap={"shots": [{"shot_id": "A01", "prompt": "beatmap p", "clip_prompt": "beatmap clip"}]})
    q = W.retry_clip_plan.__wrapped__ if hasattr(W.retry_clip_plan, "__wrapped__") else W.retry_clip_plan
    assert q(fake, "A01") == {"found": True, "clip_prompt": "launched clip p", "clip_model": "seedance",
                              "shot_meta": DUCK_META}
    assert q(fake, "A02")["clip_prompt"] == "only still p" and q(fake, "A02")["clip_model"] == "kling"
    assert q(fake, "Z9") == {"found": False}
