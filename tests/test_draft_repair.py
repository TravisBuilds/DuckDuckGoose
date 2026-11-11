"""Draft-from-idea: the drafted BEATMAP table is checked against shots.json and repaired up to twice
(each repair is sent the exact parser error); a response without some files is asked again for just
those files. The OpenAI client is mocked; no network."""

import json
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hfvg import draft_repair, genre_drafts
from hfvg.genre_presets import resolve_profile

ADMIN_SECRET = "draft-repair-admin-secret-0123456789abcdef"
API_KEY = "test-key-for-mocked-calls"
IDEA = "A lantern maker teaches a shy moth to follow the evening light across the river"
HEADER = draft_repair.TABLE_HEADER + "\n" + draft_repair.TABLE_SEPARATOR


def _row(n, sid, screen, model="K"):
    return f"| {n} | {sid} | {screen} | Workshop | Evening | A | Lanterns flicker on | Beat {n} | - | - | {model} |"


def _table(*rows):
    return "# Episode Beatmap\n\n" + HEADER + "\n" + "\n".join(rows) + "\n"


GOOD_TABLE = _table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5 (5)"), _row(3, "S03", "6 (6)"))


def _shot(sid, duration):
    return {"id": sid, "prompt": f"warm lantern light in a riverside workshop, shot {sid}", "count": 1,
            "characters": ["A"], "wet": False, "model": "kling", "duration": duration, "end_frame": False,
            "ref_keys": []}


def _files(beatmap=GOOD_TABLE):
    shots = [_shot("S01", 6), _shot("S02", 5), _shot("S03", 6)]
    return {
        "beatmap_md": beatmap,
        "shots_json": {"shots": shots},
        "refs_json": {"per_shot": {s["id"]: [] for s in shots}},
        "prompt_kit_json": {"style": "watercolor", "aspect_ratio": "9:16",
                            "characters": {"A": {"description": "an original lantern maker"}},
                            "sets": {"Workshop": "a riverside lantern workshop"}},
        "ip_steering_note": "",
    }


def _completion(payload):
    completion = MagicMock()
    content = payload if isinstance(payload, str) else json.dumps(payload)
    completion.choices = [MagicMock(message=MagicMock(content=content))]
    return completion


def _openai(*payloads):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[_completion(p) for p in payloads])
    return client


@pytest.fixture
def llm(monkeypatch):
    from hfvg.config import config

    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("DRAFT_LLM_FAKE", raising=False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", API_KEY)
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app)
    login = client.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert login.status_code == 200
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _draft(client, mock, archetype_id=None, target=30):
    with patch("openai.AsyncOpenAI", return_value=mock):
        return client.post("/api/episodes/draft-from-idea",
                           json={"idea": IDEA, "archetype_id": archetype_id, "target_duration": target})


def _prompt(mock, call):
    return mock.chat.completions.create.call_args_list[call].kwargs["messages"][-1]["content"]


# ------------------------------------------------------------------------------------- prompt wording

def test_draft_prompt_states_the_screen_cell_format(llm):
    mock = _openai(_files())
    assert _draft(llm, mock).status_code == 200
    system = mock.chat.completions.create.call_args_list[0].kwargs["messages"][0]["content"]
    assert "'<seconds> (<seconds>)' with no units, e.g. '6 (6)' or '6.0 (6.0)'" in system
    assert "Model: K (Kling) or S (Seedance)" in system
    assert "never put a '|' inside a cell" in system
    assert "| 1 | S01 | 6 (6) |" in system
    assert "whole seconds" not in system


@pytest.mark.parametrize("card", ["cozy-hangout", "creature-legend"])
def test_card_prompt_has_no_whole_seconds_wording(llm, card):
    profile = resolve_profile(card)
    target = profile["default_length_s"]
    files = genre_drafts.fake_draft(profile, IDEA, "ep42abc", target, "watercolor")
    mock = _openai({k: files[k] for k in draft_repair.REQUIRED_FILES})
    assert _draft(llm, mock, card, target).status_code == 200
    system = mock.chat.completions.create.call_args_list[0].kwargs["messages"][0]["content"]
    assert "whole seconds" not in system
    assert "'<seconds> (<seconds>)'" in system


# --------------------------------------------------------------------------------- beatmap repairs

def test_bad_first_table_is_repaired_with_the_exact_error(llm):
    bad = _table(_row(1, "S01", "6 seconds"), _row(2, "S02", "5 (5)"), _row(3, "S03", "6 (6)"))
    mock = _openai(_files(bad), {"beatmap_md": GOOD_TABLE})
    res = _draft(llm, mock)
    assert res.status_code == 200, res.text
    assert mock.chat.completions.create.call_count == 2
    assert res.json()["beatmap"] == GOOD_TABLE
    repair = _prompt(mock, 1)
    assert "BEATMAP shot S01: Screen (gen) cell '6 seconds' must read '<seconds> (<seconds>)'" in repair
    assert draft_repair.EXAMPLE_ROW in repair
    assert draft_repair.TABLE_HEADER in repair
    assert "S01 6 s, S02 5 s, S03 6 s" in repair
    assert bad in repair


def test_reported_screen_one_row_is_caught_by_the_duration_check(llm):
    """The reported failure (screen '1' on a K row) now reads as 1 s, which disagrees with shots.json."""
    bad = _table(_row(1, "S01", "1"), _row(2, "S02", "5 (5)"), _row(3, "S03", "6 (6)"))
    mock = _openai(_files(bad), {"beatmap_md": GOOD_TABLE})
    res = _draft(llm, mock)
    assert res.status_code == 200, res.text
    assert "BEATMAP shot S01: Screen (gen) says 1 s but shots_json gives the shot 6 s; write '6 (6)'" in _prompt(mock, 1)


def test_one_bad_row_is_not_dropped_when_the_others_parse(llm):
    bad = _table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5-6"), _row(3, "S03", "6 (6)"))
    mock = _openai(_files(bad), {"beatmap_md": GOOD_TABLE})
    res = _draft(llm, mock)
    assert res.status_code == 200, res.text
    assert "BEATMAP shot S02: Screen (gen) cell '5-6'" in _prompt(mock, 1)
    assert [s["id"] for s in res.json()["shots_json"]["shots"]] == ["S01", "S02", "S03"]


def test_repaired_table_must_keep_the_shot_lengths(llm):
    wrong_lengths = _table(_row(1, "S01", "6 (6)"), _row(2, "S02", "8 (8)"), _row(3, "S03", "6 (6)"))
    mock = _openai(_files(_table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5 (5)"))),
                   {"beatmap_md": wrong_lengths}, {"beatmap_md": GOOD_TABLE})
    res = _draft(llm, mock)
    assert res.status_code == 200, res.text
    assert mock.chat.completions.create.call_count == 3
    assert "lists shots ['S01', 'S02'] but shots_json has ['S01', 'S02', 'S03']" in _prompt(mock, 1)
    assert "BEATMAP shot S02: Screen (gen) says 8 s but shots_json gives the shot 5 s; write '5 (5)'" in _prompt(mock, 2)
    assert _row(2, "S02", "8 (8)") in _prompt(mock, 2)


def test_two_failed_repairs_return_an_error_that_explains_the_format(llm):
    first = _table(_row(1, "S01", "six"), _row(2, "S02", "5 (5)"), _row(3, "S03", "6 (6)"))
    second = _table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5 (5)", model="Veo"), _row(3, "S03", "6 (6)"))
    third = _table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5 (5)"), _row(3, "S03", "~6"))
    mock = _openai(_files(first), {"beatmap_md": second}, {"beatmap_md": third})
    res = _draft(llm, mock)
    assert res.status_code == 500
    assert mock.chat.completions.create.call_count == 3
    assert "Screen (gen) cell 'six'" in _prompt(mock, 1)
    assert "Model cell 'Veo' must be K (Kling) or S (Seedance)" in _prompt(mock, 2)
    detail = res.json()["detail"]
    assert detail.startswith("Draft generation failed: the shot table (BEATMAP) was still unreadable after 2 repairs")
    assert "BEATMAP shot S03: Screen (gen) cell '~6'" in detail
    assert "'<seconds> (<seconds>)' with no units, e.g. '6 (6)' or '6.0 (6.0)'" in detail
    assert "Model K (Kling) or S (Seedance)" in detail
    assert draft_repair.EXAMPLE_ROW in detail


def test_repair_reply_without_a_table_counts_as_a_failed_repair(llm):
    bad = _table(_row(1, "S01", "6 sec"), _row(2, "S02", "5 (5)"), _row(3, "S03", "6 (6)"))
    mock = _openai(_files(bad), "not json at all", {"something_else": 1})
    res = _draft(llm, mock)
    assert res.status_code == 500
    assert mock.chat.completions.create.call_count == 3
    assert "Screen (gen) cell '6 sec'" in _prompt(mock, 2)
    assert "Screen (gen) cell '6 sec'" in res.json()["detail"]


def test_failure_logs_card_length_and_row_but_not_the_idea(llm, caplog):
    profile = resolve_profile("cozy-hangout")
    files = genre_drafts.fake_draft(profile, IDEA, "ep42abc", 15, "watercolor")
    good = files["beatmap_md"]
    bad_line = next(line for line in good.splitlines() if "| S01 |" in line)
    bad = good.replace(bad_line, bad_line.replace("| K |", "| Kling 2.1 |"))
    assert IDEA in bad  # the drafted action cell quotes the idea
    payload = {k: files[k] for k in draft_repair.REQUIRED_FILES}
    mock = _openai({**payload, "beatmap_md": bad}, {"beatmap_md": bad}, {"beatmap_md": bad})
    with caplog.at_level(logging.WARNING, logger="hfvg.draft_repair"):
        res = _draft(llm, mock, "cozy-hangout", 15)
    assert res.status_code == 500
    records = [r.getMessage() for r in caplog.records if r.name == "hfvg.draft_repair"]
    assert len(records) == 3  # two repairs and the final failure
    action = bad_line.split("|")[7].strip()
    for message in records:
        assert "card=cozy-hangout" in message and "length=15s" in message
        assert "Model cell 'Kling 2.1'" in message
        assert f"row=| 1 | S01 | {files['shots_json']['shots'][0]['duration']:.1f} (" in message
        assert f"| <{len(action)} chars> |" in message and "| Kling 2.1 |" in message
        assert IDEA not in message and IDEA[:30] not in message and API_KEY not in message
    assert "attempt=1" in records[0] and "attempt=2" in records[1] and "beatmap unrepaired" in records[2]


# --------------------------------------------------------------------------------- missing files

def test_missing_files_are_asked_for_again(llm):
    first = {k: v for k, v in _files().items() if k != "prompt_kit_json"}
    mock = _openai(first, {"prompt_kit_json": _files()["prompt_kit_json"]})
    res = _draft(llm, mock)
    assert res.status_code == 200, res.text
    assert mock.chat.completions.create.call_count == 2
    retry = mock.chat.completions.create.call_args_list[1].kwargs["messages"]
    assert retry[-2] == {"role": "assistant", "content": json.dumps(first)}
    assert retry[-1]["content"].startswith('Your response did not include "prompt_kit_json".')
    assert res.json()["prompt_kit_json"]["sets"] == {"Workshop": "a riverside lantern workshop"}


def test_cut_off_response_is_asked_for_every_file(llm):
    cut_off = json.dumps(_files())[:400]
    mock = _openai(cut_off, _files())
    res = _draft(llm, mock)
    assert res.status_code == 200, res.text
    assert '"beatmap_md", "shots_json", "refs_json", "prompt_kit_json"' in _prompt(mock, 1)


def test_long_legend_without_refs_and_kit_is_completed(llm):
    """creature-legend at 180 s (the reported "missing required files" case): refs.json is rebuilt
    from the shots (drafts never attach references) and only prompt_kit_json is asked for again."""
    profile = resolve_profile("creature-legend")
    files = genre_drafts.fake_draft(profile, IDEA, "ep42abc", 180, "painterly")
    first = {"beatmap_md": files["beatmap_md"], "shots_json": files["shots_json"],
             "audio_script": files["audio_script"]}
    mock = _openai(first, {"prompt_kit_json": files["prompt_kit_json"]})
    res = _draft(llm, mock, "creature-legend", 180)
    assert res.status_code == 200, res.text
    assert mock.chat.completions.create.call_count == 2
    assert _prompt(mock, 1).startswith('Your response did not include "prompt_kit_json".')
    data = res.json()
    assert sorted(data["refs_json"]["per_shot"]) == sorted(s["id"] for s in data["shots_json"]["shots"])
    assert all(refs == [] for refs in data["refs_json"]["per_shot"].values())
    assert sum(s["duration"] for s in data["shots_json"]["shots"]) == 180


def test_files_still_missing_after_two_retries_fail_with_their_names(llm):
    first = {k: v for k, v in _files().items() if k != "prompt_kit_json"}
    mock = _openai(first, {"ip_steering_note": ""}, "{}")
    res = _draft(llm, mock)
    assert res.status_code == 500
    assert mock.chat.completions.create.call_count == 3
    assert res.json()["detail"] == ("Draft generation failed: LLM response missing required files "
                                    "(prompt_kit_json) after 2 retries")


# --------------------------------------------------------------------------------- check_beatmap

def test_check_beatmap_accepts_a_matching_table():
    parsed, problem, row = draft_repair.check_beatmap(GOOD_TABLE, _files()["shots_json"])
    assert problem is None and row == ""
    assert [(p["shot_id"], p["screen_time"], p["gen_time"]) for p in parsed] == [
        ("S01", 6.0, 6.0), ("S02", 5.0, 5.0), ("S03", 6.0, 6.0)]


@pytest.mark.parametrize("content,expected", [
    ("", "beatmap_md is empty"),
    ("# Beatmap\n\nNo table here", "BEATMAP.md has no shot table"),
    (_table(_row(1, "S01", "6 (6)"), _row(2, "S03", "5 (5)"), _row(3, "S02", "6 (6)")),
     "the BEATMAP table lists shots ['S01', 'S03', 'S02'] but shots_json has ['S01', 'S02', 'S03']"),
    (_table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5.5 (5.5)"), _row(3, "S03", "6 (6)")),
     "BEATMAP shot S02: Screen (gen) says 5.5 s but shots_json gives the shot 5 s; write '5 (5)'"),
])
def test_check_beatmap_problems(content, expected):
    parsed, problem, _ = draft_repair.check_beatmap(content, _files()["shots_json"])
    assert parsed is None
    assert problem.startswith(expected)


def test_check_beatmap_returns_the_offending_row():
    table = _table(_row(1, "S01", "6 (6)"), _row(2, "S02", "5 (5) | extra"), _row(3, "S03", "6 (6)"))
    _, problem, row = draft_repair.check_beatmap(table, _files()["shots_json"])
    assert "'|' inside a cell" in problem
    assert row == _row(2, "S02", "5 (5) | extra")


def test_loggable_row_hides_free_text_cells():
    row = "| 2 | S02 | 5-6 | Workshop | Evening | A | The lantern maker opens the window wide | Beat | - | - | K |"
    assert draft_repair.loggable_row(row) == "| 2 | S02 | 5-6 | Workshop | Evening | A | <39 chars> | Beat | - | - | K |"
    assert draft_repair.loggable_row("") == ""


def test_fill_refs_rebuilds_empty_refs_from_the_shots():
    result = {"shots_json": json.dumps({"shots": [_shot("S01", 6), _shot("S02", 5)]})}
    assert draft_repair.fill_refs(result) is True
    assert result["refs_json"] == {"per_shot": {"S01": [], "S02": []}}
    assert draft_repair.fill_refs(result) is False
    assert draft_repair.fill_refs({"shots_json": "{cut off"}) is False
    assert draft_repair.fill_refs({}) is False
