"""Preflight checks the line caps the episode will actually get (policy, else defaults).

It used to sum the bare DEFAULT_LINE_CAPS_USD_MICROS ($44), so any EPISODE_CAP_USD below $55
was refused even when the gate policy sized the lines for that cap (ep05full: cap $40).
"""

import json

import pytest


def _write_policy(tmp_path, lines_usd: dict[str, float], episode: str = "ep05full"):
    # a valid full policy (the public example) plus this episode's USD line caps
    from pathlib import Path

    example = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*/HARNESS-GATES.json"))[0]
    policy = json.loads(example.read_text())
    budgets = policy.setdefault("budgets", {})
    budgets[episode] = {"higgsfield": {"lines": {k: 1 for k in lines_usd}, "stop_fraction": 0.8}}
    budgets.setdefault("usd", {})[f"{episode}_lines_app_usd"] = lines_usd
    path = tmp_path / "HARNESS-GATES.json"
    path.write_text(json.dumps(policy))
    return path


EP05FULL_LINES = {"L1_refs": 0.5, "L2_drafts": 4.0, "L3_final_stills": 0.5, "L4_video": 26.0}


def _budget_problems(problems):
    return [p for p in problems if "line caps total" in p]


async def _run_preflight(tmp_path, monkeypatch, episode_id):
    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity

    base = tmp_path / "data" / "episodes" / episode_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "BEATMAP.md").write_text("A01 (2.5s)")
    (base / "shots.json").write_text(json.dumps({
        "A01": {"prompt": "Test shot", "params": {"model": "kling", "duration": 3, "aspect_ratio": "9:16"}}
    }))
    (base / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    (base / "prompt_kit.json").write_text("{}")
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    await init_studio_db(db_path)
    original = temporalio.activity.info
    temporalio.activity.info = lambda: type("obj", (), {"workflow_id": "test-wf"})()
    try:
        try:
            await preflight_validate_episode_activity(episode_id, None, False)
            return []
        except PreflightError as e:
            return list(e.problems)
    finally:
        temporalio.activity.info = original


def test_effective_caps_use_policy_usd_lines(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger

    monkeypatch.setenv("HFVG_GATE_POLICY", str(_write_policy(tmp_path, EP05FULL_LINES)))
    caps = BudgetLedger(str(tmp_path / "x.db")).effective_line_caps_usd_micros("ep05full")
    assert caps == {"L1_refs": 500_000, "L2_drafts": 4_000_000, "L3_final_stills": 500_000, "L4_video": 26_000_000}
    assert sum(caps.values()) == 31_000_000  # under the $32 stop of a $40 cap


def test_effective_caps_fall_back_to_defaults(tmp_path, monkeypatch):
    from hfvg.budget import DEFAULT_LINE_CAPS_USD_MICROS, BudgetLedger

    monkeypatch.setenv("HFVG_GATE_POLICY", str(_write_policy(tmp_path, EP05FULL_LINES)))
    caps = BudgetLedger(str(tmp_path / "x.db")).effective_line_caps_usd_micros("ep77")
    assert caps == DEFAULT_LINE_CAPS_USD_MICROS


@pytest.mark.asyncio
async def test_init_episode_budget_creates_the_checked_caps(tmp_path, monkeypatch):
    import aiosqlite
    from hfvg.budget import BudgetLedger

    monkeypatch.setenv("HFVG_GATE_POLICY", str(_write_policy(tmp_path, EP05FULL_LINES)))
    monkeypatch.setenv("EPISODE_CAP_USD", "40")
    ledger = BudgetLedger(str(tmp_path / "x.db"))
    await ledger.init_episode_budget("ep05full")
    async with aiosqlite.connect(str(tmp_path / "x.db")) as db:
        rows = dict(await (await db.execute(
            "SELECT line_name, cap_usd_micros FROM budget_lines WHERE episode_id='ep05full' AND provider='higgsfield'"
        )).fetchall())
    for line, cap in ledger.effective_line_caps_usd_micros("ep05full").items():
        assert rows[line] == cap


@pytest.mark.asyncio
async def test_preflight_accepts_policy_lines_under_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HFVG_GATE_POLICY", str(_write_policy(tmp_path, EP05FULL_LINES)))
    monkeypatch.setenv("EPISODE_CAP_USD", "40")
    problems = await _run_preflight(tmp_path, monkeypatch, "ep05full")
    assert _budget_problems(problems) == []


@pytest.mark.asyncio
async def test_preflight_still_refuses_defaults_over_cap(tmp_path, monkeypatch):
    # no policy lines for this episode: the $44 defaults exceed a $40 cap and its $32 stop
    monkeypatch.setenv("HFVG_GATE_POLICY", str(_write_policy(tmp_path, EP05FULL_LINES, episode="ep04")))
    monkeypatch.setenv("EPISODE_CAP_USD", "40")
    problems = _budget_problems(await _run_preflight(tmp_path, monkeypatch, "ep05full"))
    assert len(problems) == 2 and "44.00" in problems[0]


@pytest.mark.asyncio
async def test_preflight_refuses_policy_lines_over_stop(tmp_path, monkeypatch):
    too_big = dict(EP05FULL_LINES, L4_video=30.0)  # $35 total > $32 stop (< $40 cap)
    monkeypatch.setenv("HFVG_GATE_POLICY", str(_write_policy(tmp_path, too_big)))
    monkeypatch.setenv("EPISODE_CAP_USD", "40")
    problems = _budget_problems(await _run_preflight(tmp_path, monkeypatch, "ep05full"))
    assert len(problems) == 1 and "episode stop" in problems[0] and "35.00" in problems[0]
