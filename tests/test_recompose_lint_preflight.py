"""GAP-24 (ep05full F06 recompose 1, 2026-10-27): the recompose route checked prompt LENGTH only, so a
prompt missing the kit's required clauses was queued and refused inside the child (at the paid-submit
lint gate; no spend) and still used up one of the shot's two recompose attempts. The route now runs the
same prompt + moderation lint as the paid submits (409 before anything is queued), and a child that
fails at a pre-spend gate does not count toward the max. No network, no spend."""
import json

import pytest

from tests.test_retry_prompt_preflight import SECRET, client  # noqa: F401  (fixture)

class _Handle:
    def __init__(self, meta):
        self.meta, self.updates = meta, []

    async def query(self, name, *args, **kw):
        assert name == "retry_prompt_plan"
        return {"found": True, "still_prompt": "x", "clip_prompt": "x", "clip_model": "kling",
                "shot_meta": self.meta}

    async def execute_update(self, name, *a, **kw):
        self.updates.append((name, kw.get("args", a)))
        return {"status": "queued", "workflow_id": "ep77-shot-A01-recompose1", "recompose": 1, "version": 2}


class _Client:
    def __init__(self, h):
        self.h = h

    def get_workflow_handle(self, wid):
        return self.h


@pytest.fixture
def episode(tmp_path, monkeypatch):
    from tests.lint_helpers import lint_ok  # the shared lint kit + a prompt that satisfies it
    import tests.lint_helpers as lh
    ep = tmp_path / "eps" / "ep77"
    (ep / "refs").mkdir(parents=True)
    (ep / "refs" / "char_r.png").write_bytes(b"\x89PNG")
    (ep / "prompt_kit.json").write_text(json.dumps({"lint": lh.SYNTHETIC_LINT_KIT}))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    return lint_ok


META = {"count": 1, "exterior": False, "water": False, "characters": []}


def test_recompose_prompt_missing_kit_clauses_is_refused_before_queueing(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(META)
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    body = {"reason": "Studio", "prompt": "Tighter wide shot. EXACTLY THREE animals.", "ref_keys": ["char_r"]}
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=body)
    assert r.status_code == 409 and "Prompt lint refused the recompose before any spend" in r.json()["detail"], r.text
    assert h.updates == []
    body["prompt"] = episode("Tighter wide shot of one animal on the boardwalk")
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=body)
    assert r.status_code == 200, r.text
    assert [u[0] for u in h.updates] == ["recompose_shot"]


def test_recompose_without_lint_metadata_fails_closed(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(None)
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep77/shots/A01/recompose",
               json={"reason": "S", "prompt": episode("One animal on the boardwalk"), "ref_keys": ["char_r"]})
    assert r.status_code == 409 and "no lint metadata" in r.json()["detail"] and h.updates == []


def test_pre_spend_refusals_do_not_count():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as W
    assert W.refused_before_spend({"status": "failed", "still_url": None,
                                   "error": "PromptLintError: Prompt lint failed: missing count clause"})
    assert W.refused_before_spend({"status": "failed", "error": "ModerationLintError: duck in water"})
    assert not W.refused_before_spend({"status": "failed", "error": "provider timeout"})       # may have spent
    assert not W.refused_before_spend({"status": "failed", "still_url": "s.jpg",
                                       "error": "PromptLintError on the clip"})                 # still was paid
    assert not W.refused_before_spend({"status": "rejected", "still_url": "s.jpg"})
