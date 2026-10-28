"""
GAP-15: a G2 re-roll with prompt_append is preflighted on the AMENDED prompts (still + the clip it
becomes) BEFORE the paid still; ep05full F06's amendment pushed its clip prompt to 2675 > Kling's 2500
and was only refused after G2, with the re-rolled still already paid for.
GAP-16: GET /api/episodes/{ep} returned 500 once failed_shots held the workflow's dict rows.
No network, no spend.
"""
import pytest

SECRET = "test-secret-retry-0123456789abcdefghijklmn"


def test_prompt_limit_violations_checks_the_clip_model():
    from hfvg import preflight
    ok = preflight.prompt_limit_violations("F06", still_prompt="a" * 100, clip_prompt="a" * 2500,
                                           clip_model_alias="kling")
    assert ok == []
    bad = preflight.prompt_limit_violations("F06", still_prompt="a" * 100, clip_prompt="a" * 2675,
                                            clip_model_alias="kling")
    assert len(bad) == 1 and bad[0].frame == "clip" and "Shorten it by 175" in bad[0].message


def test_amend_prompt_matches_retry_shot():
    from hfvg.workflows.episode_v2 import amend_prompt
    assert amend_prompt("Base prompt.  ", "  Three\n char_cs. ") == ("Base prompt. Three char_cs.",
                                                                     "Three char_cs.")


class _Handle:
    def __init__(self, base):
        self.base, self.updates = base, []

    async def query(self, name, *args, **kw):
        if name == "retry_prompt_plan":
            shot_id, append = kw["args"]
            from hfvg.workflows.episode_v2 import amend_prompt
            p = amend_prompt(self.base, append)[0]
            return {"found": True, "still_prompt": p, "clip_prompt": p, "clip_model": "kling"}
        return {"episode_id": "ep05full", "failed_shots": [
            {"shot_id": "F06", "status": "failed", "error": "PreflightError: clip prompt too long"}]}

    async def execute_update(self, name, *a, **kw):
        self.updates.append((name, kw.get("args", a)))
        return {"status": "queued", "workflow_id": "ep05full-shot-F06-retry1"}


class _Client:
    def __init__(self, h):
        self.h = h

    def get_workflow_handle(self, wid):
        return self.h


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import asyncio
    import api.main as api_main
    from hfvg.studio_db import init_studio_db
    db = str(tmp_path / "studio.db")
    asyncio.run(init_studio_db(db))
    monkeypatch.setattr(api_main, "DATABASE_PATH", db)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    return api_main, TestClient(api_main.app, headers={"Authorization": f"Bearer {SECRET}"})


def test_amended_reroll_over_the_clip_limit_is_refused_before_spend(client, monkeypatch):
    api_main, c = client
    h = _Handle("x" * 2400)
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep05full/shots/F06/retry", json={"prompt_append": "y " * 100, "reason": "t"})
    assert r.status_code == 409 and "before any spend" in r.json()["detail"], r.text
    assert h.updates == []  # nothing launched, nothing paid
    r = c.post("/api/episodes/ep05full/shots/F06/retry", json={"prompt_append": "three char_cs", "reason": "t"})
    assert r.status_code == 200 and h.updates == [("retry_shot", ["F06", "three char_cs"])], r.text


def test_state_with_dict_failed_shots_is_readable(client, monkeypatch):
    api_main, c = client
    monkeypatch.setattr(api_main, "temporal_client", _Client(_Handle("x")))
    r = c.get("/api/episodes/ep05full")
    assert r.status_code == 200, r.text
    assert r.json()["failed_shots"][0]["shot_id"] == "F06"


class _MetaHandle(_Handle):
    async def query(self, name, *args, **kw):
        out = await super().query(name, *args, **kw)
        if name == "retry_prompt_plan":
            out["shot_meta"] = {"count": 0}
        return out


def test_amended_reroll_failing_prompt_lint_is_refused_before_spend(client, monkeypatch, tmp_path):
    """ep05v2 V24: an amendment saying "no lanterns" tripped the banned-decor lint inside the child,
    after the re-roll had consumed an attempt. The API now lints the amended still prompt first."""
    import json
    api_main, c = client
    kit = tmp_path / "prompt_kit.json"
    kit.write_text(json.dumps({"lint": {"count_template": "Exactly {n} animals in frame",
                                         "realtime_pattern": "real-time speed.{0,40}no slow motion",
                                         "locked_camera_clause": "no camera drift",
                                         "banned_decor": ["lantern", "lanterns"]}}))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(kit))
    h = _MetaHandle("Exactly 0 animals in frame. A calm pool at blue hour. Real-time speed, no slow motion.")
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep05v2/shots/V24/retry", json={"prompt_append": "No lanterns outside.", "reason": "t"})
    assert r.status_code == 409 and "Prompt lint refused" in r.json()["detail"] and "lantern" in r.json()["detail"], r.text
    assert h.updates == []
    r = c.post("/api/episodes/ep05v2/shots/V24/retry", json={"prompt_append": "No lamps outside.", "reason": "t"})
    assert r.status_code == 200 and h.updates == [("retry_shot", ["V24", "No lamps outside."])], r.text
