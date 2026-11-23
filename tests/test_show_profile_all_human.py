"""A show with no animals runs on its own show data, no code change: tests/fixtures/shows/human_satire
is a satire with invented people (two candidates in suits, a moderator, an audience), one shot with
nobody in frame, one with a character alone and one with a prop. Its show.json declares its body
preset, sheet lead, identity signs, lint clauses and shot flag; it declares no flag figures and no
playbook rules, and real people stay refused. Every check drives the pipeline's own entry points
with that data. No network, no spend."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from hfvg import show_profile
from hfvg.show_profile import ShowProfileError, evaluate_when, resolve_show_profile

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shows" / "human_satire"
EP = "ep71satire"
ADMIN = "show-profile-admin-secret-0123456789abcdef"
MEMBER = ("member@satire.test", "member-pass-123")
# Words that would tie the engine's output to the first series or to animals
SPECIES = {"duck", "ducks", "animal", "animals", "species", "beak", "bill", "muzzle", "fur", "feathers", "plush",
           "antler", "antlers", "moose", "guest-animal", "quadrupedal", "tuque"}


def species_words(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(text).lower()))
    return SPECIES & (words | {p for w in words for p in w.split("-")})


@pytest.fixture
def satire(tmp_path, monkeypatch):
    """The fixture show as an episode package under EPISODES_DIR (show.json, prompt_kit.json,
    shots.json, BEATMAP.md); no override env vars."""
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    for var in ("SHOTS_JSON_PATH", "REFS_JSON_PATH", "PROMPT_LINT_KIT_PATH", "HFVG_SHOW_PROFILE_PATH",
                "HFVG_PLAYBOOK_HARD_RULES", "HFVG_PLAYBOOK_WORLD_REFS", "MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON"):
        monkeypatch.delenv(var, raising=False)
    folder = tmp_path / "eps" / EP
    folder.mkdir(parents=True)
    for name in ("show.json", "prompt_kit.json", "shots.json"):
        shutil.copy(FIXTURE / name, folder / name)
    shutil.copy(FIXTURE / "beatmap.md", folder / "BEATMAP.md")
    return folder


# ------------------------------------------------------------------------------------------ loader


def test_the_profile_resolves_from_the_episode_folder(satire):
    profile = resolve_show_profile(EP)
    assert profile.source == "episode" and profile.path == str(satire / "show.json")
    assert profile == show_profile.load_show_profile(FIXTURE / "show.json")
    assert profile.digest == resolve_show_profile(EP).digest == show_profile.profile_digest(profile)
    assert re.fullmatch(r"[0-9a-f]{64}", profile.digest)
    assert profile.real_people == "refuse"
    assert profile.flag_figures == () and profile.playbook.rules == ()
    assert [f.meta for f in profile.shot_flags] == ["on_camera"]
    assert profile.sheet_lead == "Satire cast sheet of an invented person."
    assert sorted(profile.body_presets) == ["seated_desk"]
    assert sorted(profile.identity_profiles["invented_person"].signs) == ["accessories", "face", "hair", "suit"]
    assert profile.summary()["source"] == "episode"
    # the canonical form loads back to the same profile, and no word in it is about animals
    canonical = show_profile.profile_dict(profile)
    assert show_profile.parse_profile(json.loads(json.dumps(canonical))) == profile
    assert not species_words(json.dumps(canonical))


def test_the_digest_follows_the_rules_not_the_file_layout(satire):
    before = resolve_show_profile(EP).digest
    data = json.loads((satire / "show.json").read_text(encoding="utf-8"))
    (satire / "show.json").write_text(json.dumps(data, indent=4, sort_keys=True))
    assert resolve_show_profile(EP).digest == before
    data["lint"]["banned_words"] = ["logo", "slogan"]
    (satire / "show.json").write_text(json.dumps(data))
    assert resolve_show_profile(EP).digest != before


def test_a_bad_show_json_reports_every_problem_in_one_list(satire):
    data = json.loads((satire / "show.json").read_text(encoding="utf-8"))
    data["schema"] = "hfvg.show/0"
    data["body_presets"]["Desk"] = data["body_presets"]["seated_desk"]
    data["cast"]["moderator"]["body"] = "standing_desk"
    data["lint"]["count_clause"]["template"] = "Figures in frame."
    data["lint"]["required_clauses"][0]["when"] = {"sometimes": True}
    data["policy"]["real_people"] = "allow"
    data["unexpected"] = 1
    (satire / "show.json").write_text(json.dumps(data))
    with pytest.raises(ShowProfileError) as err:
        resolve_show_profile(EP)
    problems = err.value.problems
    assert len(problems) == 7, problems
    for expected in ("unknown keys ['unexpected']", "schema must be 'hfvg.show/1'",
                     "body_presets.Desk: preset id must match", "cast.moderator.body 'standing_desk'",
                     "lint.count_clause.template must contain '{n}'", "lint.required_clauses[0].when",
                     "policy.real_people must be one of ['refuse']"):
        assert any(expected in p for p in problems), (expected, problems)
    assert show_profile.show_profile_problems(EP) == [f"show profile {satire / 'show.json'}: {p}" for p in problems]


def test_real_people_stay_refused(satire):
    data = json.loads((satire / "show.json").read_text(encoding="utf-8"))
    data["policy"] = {}
    (satire / "show.json").write_text(json.dumps(data))
    assert resolve_show_profile(EP).real_people == "refuse"
    for stance in ("allow", "consented", True):
        data["policy"] = {"real_people": stance}
        (satire / "show.json").write_text(json.dumps(data))
        with pytest.raises(ShowProfileError, match="real_people must be one of"):
            resolve_show_profile(EP)


def test_a_deployment_profile_applies_to_episodes_without_their_own(satire, tmp_path, monkeypatch):
    monkeypatch.setenv("HFVG_SHOW_PROFILE_PATH", str(FIXTURE / "show.json"))
    (tmp_path / "eps" / "ep-other").mkdir()
    other = resolve_show_profile("ep-other")
    assert other.source == "env" and other == resolve_show_profile(EP)
    assert resolve_show_profile(EP).source == "episode"
    monkeypatch.setenv("HFVG_SHOW_PROFILE_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(ShowProfileError, match="cannot read the profile"):
        resolve_show_profile("ep-other")


@pytest.mark.parametrize("pred, meta, tags, expected", [
    ({"always": True}, {}, [], True),
    ({"flag": "on_camera"}, {"flags": {"on_camera": True}}, [], True),
    ({"flag": "on_camera"}, {"flags": {"on_camera": False}}, [], False),
    ({"flag": "on_camera"}, {}, [], False),
    ({"flag": "on_camera", "default": True}, {}, [], True),
    ({"flag": "on_camera", "default": {"count_gt": 1}}, {"count": 2}, [], True),
    ({"characters_any": ["c1"]}, {"characters": ["C1", "M"]}, [], True),
    ({"characters_any": ["C3"]}, {"characters": ["C1"]}, [], False),
    ({"count_gt": 0}, {"count": 0}, [], False),
    ({"count_gt": 0}, {"count": True}, [], False),
    ({"tag": "crowd"}, {}, ["crowd"], True),
    ({"role": "candidate"}, {"roles": ["candidate"]}, [], True),
    ({"role": "moderator"}, {"cast": [{"character": "m", "role": "Moderator"}]}, [], True),
    ({"all": [{"count_gt": 1}, {"not": {"flag": "on_camera"}}]}, {"count": 2}, [], True),
    ({"any": [{"tag": "x"}, {"count_gt": 5}]}, {"count": 2}, [], False),
])
def test_predicates(pred, meta, tags, expected):
    problems: list[str] = []
    show_profile.validate_when(pred, "when", problems)
    assert problems == []
    assert evaluate_when(pred, meta, tags) is expected


# ------------------------------------------------------------------------------------------ API and preflight


@pytest.fixture
async def studio(satire, monkeypatch):
    import os
    import sys

    import bcrypt

    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *_a, **_k: gensalt(rounds=4))
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    users = UserManager(db)
    await users.init_db()
    await users.create_user(name="Member", email=MEMBER[0], role="member", password=MEMBER[1])
    await create_episode(db, EP)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    return api_main


def _member(api_main):
    from fastapi.testclient import TestClient

    client = TestClient(api_main.app, raise_server_exceptions=False)
    login = client.post("/api/login", json={"email": MEMBER[0], "password": MEMBER[1]})
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


async def test_members_read_the_show_profile_an_episode_gets(studio, satire):
    client = _member(studio)
    body = client.get(f"/api/episodes/{EP}/show-profile").json()
    profile = resolve_show_profile(EP)
    assert body == {"episode_id": EP, "ok": True, **profile.summary(), "errors": []}
    assert body["source"] == "episode" and body["digest"] == profile.digest
    assert body["body_presets"] == {"seated_desk": "Seated at a desk"}
    assert body["shot_flags"] == ["on_camera"] and body["identity_profiles"] == ["invented_person"]
    assert "lint" not in body and "AI satire disclosure" not in json.dumps(body)

    (satire / "show.json").write_text(json.dumps({"schema": "hfvg.show/1", "policy": {"real_people": "allow"}}))
    broken = client.get(f"/api/episodes/{EP}/show-profile").json()
    assert broken["ok"] is False and broken["source"] == "episode"
    assert broken["errors"] == ["policy.real_people must be one of ['refuse'] (docs/CONTENT-POLICY.md)"]

    (satire / "show.json").unlink()
    assert client.get(f"/api/episodes/{EP}/show-profile").json()["source"] == "legacy-adapter"
    for name in ("prompt_kit.json", "shots.json"):
        (satire / name).unlink()
    assert client.get(f"/api/episodes/{EP}/show-profile").json()["source"] == "default"


async def test_the_preflight_reports_the_profile_and_refuses_a_bad_one(studio, satire):
    from temporalio.testing import ActivityEnvironment

    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity

    client = _member(studio)
    report = client.post(f"/api/episodes/{EP}/preflight").json()
    assert report["show_profile"] == {"source": "episode", "digest": resolve_show_profile(EP).digest, "errors": []}

    (satire / "show.json").write_text(json.dumps({"schema": "hfvg.show/1", "cast": {"Bad Id": {}}}))
    report = client.post(f"/api/episodes/{EP}/preflight").json()
    assert report["has_errors"] is True and report["show_profile"]["errors"]
    with pytest.raises(PreflightError) as err:
        await ActivityEnvironment().run(preflight_validate_episode_activity, EP)
    assert any("show profile" in p and "cast.Bad Id" in p for p in err.value.problems), err.value.problems
