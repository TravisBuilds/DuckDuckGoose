"""A show with no animals runs on its own show data, no code change: tests/fixtures/shows/human_satire
is a satire with invented people (two candidates in suits, a moderator, an audience), one shot with
nobody in frame, one with a character alone and one with a prop. Its show.json declares its body
preset, sheet lead, identity signs, lint clauses and shot flag; it declares no flag figures and no
playbook rules, and real people stay refused. Every check drives the pipeline's own entry points
with that data. No network, no spend."""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

import pytest
from PIL import Image

from hfvg import show_profile
from hfvg.show_profile import ShowProfileError, evaluate_when, resolve_show_profile
from tests.test_retry_prompt_preflight import client  # noqa: F401  (fixture)

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


# ------------------------------------------------------------------------------- shot plan, lint, playbook


def _plans(folder: Path) -> dict:
    from hfvg.shot_data import parse_shots
    return parse_shots(json.loads((folder / "shots.json").read_text(encoding="utf-8")), resolve_show_profile(EP))


def test_the_shot_plan_carries_the_shows_flags_and_roles(satire):
    from hfvg.shot_data import ShotDataError, parse_shots

    plans = _plans(satire)
    metas = {sid: plan["shot_meta"] for sid, plan in plans.items()}
    assert metas["S02"]["flags"] == {"on_camera": True} and metas["S02"]["roles"] == ["candidate"]
    assert metas["S03"]["flags"] == {"on_camera": False} and metas["S03"]["roles"] == ["moderator"]
    assert metas["S01"]["flags"] == {} and "roles" not in metas["S01"]
    for meta in metas.values():
        assert not {"duck", "duck_declared", "antler"} & set(meta), meta
    assert not species_words(json.dumps(plans))

    raw = json.loads((satire / "shots.json").read_text(encoding="utf-8"))
    raw["shots"][1]["on_camera"] = "yes"
    with pytest.raises(ShotDataError, match=r"shot S02: on_camera must be a bool, got 'yes'"):
        parse_shots(raw, resolve_show_profile(EP))


def test_the_beatmap_reads_the_shows_character_codes(satire):
    from hfvg.episode_parser import build_prompt_from_shot, parse_beatmap

    shots = {s["shot_id"]: s for s in parse_beatmap(satire / "BEATMAP.md", resolve_show_profile(EP))}
    assert shots["S02"]["roles"] == ["candidate"] and shots["S03"]["roles"] == ["moderator"]
    assert "roles" not in shots["S01"] and not [s for s in shots.values() if "duck_role" in s]
    assert build_prompt_from_shot(shots["S02"]).startswith("candidate with C1, C2 MEDIUM.")
    assert build_prompt_from_shot(shots["S01"]).startswith("WIDE, locked.")
    assert not species_words(json.dumps([build_prompt_from_shot(s) for s in shots.values()]))
    # the same rows read without a profile keep the legacy layout
    assert {s["duck_role"] for s in parse_beatmap(satire / "BEATMAP.md")} == {"absent"}


def test_the_lint_runs_the_shows_rules_with_its_own_messages(satire):
    from hfvg.prompt_lint import (
        apply_continuity_locks,
        clip_lint_violations,
        lint_summary,
        lint_violations,
        load_lint_kit,
    )

    plans = _plans(satire)
    summary = lint_summary(EP, plans)
    assert summary["ok"] and summary["profile"] == "show" and summary["problem_count"] == 0, summary
    kit = load_lint_kit(EP)
    meta = plans["S02"]["shot_meta"]
    found = lint_violations("Medium shot: two candidates. A lookalike candidate waves with a logo.", meta, kit)
    assert found == [
        "missing count clause: 'Figures in frame: 2.'",
        "missing real-time / no-slow-motion clause",
        "invented people only: 'lookalike' next to 'candidate': 'A lookalike candidate waves with a logo'",
        "banned word: 'logo'",
        "missing the AI satire disclosure: 'AI satire disclosure: invented people only'",
        "missing continuity lock 'eyeline' (still)",
    ]
    clip = clip_lint_violations("Two candidates nod, nothing else moves.", meta, kit)
    assert "forbidden phrase: 'nothing else moves'" in clip, clip
    # the lock follows the shot flag; nobody in frame needs no disclosure
    sent, added = apply_continuity_locks(EP, plans["S02"]["prompt"], meta, True, kind="still")
    assert added == ["eyeline"] and sent.endswith("Whoever is on camera looks straight into the lens.")
    assert apply_continuity_locks(EP, plans["S03"]["prompt"], plans["S03"]["shot_meta"], True, kind="still")[1] == []
    assert lint_violations(plans["S01"]["prompt"], plans["S01"]["shot_meta"], kit) == []
    assert not species_words(json.dumps(found + clip))


def test_the_moderation_lint_runs_only_the_shows_rules(satire, monkeypatch):
    from hfvg import moderation_lint
    from hfvg.activities.g1_plan import _moderation_preview

    plans = _plans(satire)
    for sid, plan in plans.items():
        for kind, prompt in (("still", plan["prompt"]), ("clip", plan["clip_prompt"])):
            rec = moderation_lint.evaluate(sid, prompt, plan["shot_meta"], kind=kind, model="kling", live=True,
                                           episode_id=EP)
            assert list(rec["rules"]) == ["moderation_routing"] and rec["decision"] == "pass", rec
            assert not species_words(json.dumps(rec))
    # one deployment setting serves every show: the first series' rule names are accepted, not applied
    monkeypatch.setenv(moderation_lint.HARD_RULES_ENV, "duck_role, moderation_routing")
    assert moderation_lint.hard_rules(resolve_show_profile(EP)) == frozenset({"moderation_routing"})
    preview = _moderation_preview([{"shot_id": sid, **plan} for sid, plan in plans.items()], set(), EP)
    assert {sid: (list(row["still"]["rules"]), row["clip"]["decision"]) for sid, row in preview.items()} == {
        sid: (["moderation_routing"], "pass") for sid in plans}


def test_a_recompose_may_set_only_the_shows_declared_flags(satire):
    from hfvg.show_profile import recompose_flags_problem
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as W

    profile = resolve_show_profile(EP)
    assert W.recompose_meta_problem({"count": 1, "flags": {"on_camera": False}}) is None
    assert recompose_flags_problem(profile, {"on_camera": False}) is None
    assert recompose_flags_problem(profile, {"host_present": False}) == (
        "shot_meta.flags ['host_present'] are not shot flags of this show; it declares ['on_camera']")
    assert recompose_flags_problem(profile, {"on_camera": "yes"}) == "shot_meta.flags.on_camera must be a bool, got 'yes'"
    for bad in (["on_camera"], {"on_camera": None}, {"on_camera": 0.5}, {"": True}):
        assert W.recompose_meta_problem({"flags": bad}) == (
            "shot_meta.flags must be an object of boolean, string or integer values"), bad


class _Handle:
    def __init__(self, meta):
        self.meta, self.updates = meta, []

    async def query(self, name, *args, **kw):
        assert name == "retry_prompt_plan"
        return {"found": True, "still_prompt": "x", "clip_prompt": "x", "clip_model": "kling", "shot_meta": self.meta}

    async def execute_update(self, name, *a, **kw):
        self.updates.append((name, kw.get("args", a)))
        return {"status": "queued", "workflow_id": f"{EP}-shot-S02-recompose1", "recompose": 1, "version": 2}


class _Client:
    def __init__(self, handle):
        self.handle = handle

    def get_workflow_handle(self, wid):
        return self.handle


def test_the_recompose_route_checks_flags_against_the_show(satire, client, monkeypatch):  # noqa: F811
    api_main, c = client
    (satire / "refs").mkdir()
    (satire / "refs" / "podiums.png").write_bytes(b"\x89PNG")
    handle = _Handle(_plans(satire)["S02"]["shot_meta"])
    monkeypatch.setattr(api_main, "temporal_client", _Client(handle))
    prompt = ("Medium shot: the two candidates turn their backs to the camera. Figures in frame: 2. "
              "AI satire disclosure: invented people only. Real-time pace.")
    body = {"reason": "Studio: backs to camera", "prompt": prompt, "ref_keys": ["podiums"],
            "shot_meta": {"flags": {"host_present": False}}}
    r = c.post(f"/api/episodes/{EP}/shots/S02/recompose", json=body)
    assert r.status_code == 422 and handle.updates == [], r.text
    assert r.json()["detail"] == ("invalid shot_meta: shot_meta.flags ['host_present'] are not shot flags of "
                                  "this show; it declares ['on_camera']")
    # declared: the patched flag is what the lint reads (no eyeline lock off camera) and what is sent
    body["shot_meta"] = {"flags": {"on_camera": False}}
    r = c.post(f"/api/episodes/{EP}/shots/S02/recompose", json=body)
    assert r.status_code == 200, r.text
    (name, args), = handle.updates
    assert name == "recompose_shot" and args[2] == prompt and args[6] == {"flags": {"on_camera": False}}
    # on camera, the show's continuity lock is injected into what is sent
    body["shot_meta"] = {"flags": {"on_camera": True}}
    assert c.post(f"/api/episodes/{EP}/shots/S02/recompose", json=body).status_code == 200
    assert handle.updates[-1][1][2] == f"{prompt} Whoever is on camera looks straight into the lens."


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

    member = TestClient(api_main.app, raise_server_exceptions=False)
    login = member.post("/api/login", json={"email": MEMBER[0], "password": MEMBER[1]})
    assert login.status_code == 200, login.text
    member.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return member


async def test_members_read_the_show_profile_an_episode_gets(studio, satire):
    member = _member(studio)
    body = member.get(f"/api/episodes/{EP}/show-profile").json()
    profile = resolve_show_profile(EP)
    gate_policy = {"from": "neutral", "series": "Neutral default", "errors": []}
    assert body == {"episode_id": EP, "ok": True, **profile.summary(), "gate_policy": gate_policy, "errors": []}
    assert body["source"] == "episode" and body["digest"] == profile.digest
    assert body["body_presets"] == {"seated_desk": "Seated at a desk"}
    assert body["shot_flags"] == ["on_camera"] and body["identity_profiles"] == ["invented_person"]
    assert "lint" not in body and "AI satire disclosure" not in json.dumps(body)

    (satire / "show.json").write_text(json.dumps({"schema": "hfvg.show/1", "policy": {"real_people": "allow"}}))
    broken = member.get(f"/api/episodes/{EP}/show-profile").json()
    assert broken["ok"] is False and broken["source"] == "episode"
    assert broken["errors"] == ["policy.real_people must be one of ['refuse'] (docs/CONTENT-POLICY.md)"]

    (satire / "show.json").unlink()
    assert member.get(f"/api/episodes/{EP}/show-profile").json()["source"] == "legacy-adapter"
    for name in ("prompt_kit.json", "shots.json"):
        (satire / name).unlink()
    assert member.get(f"/api/episodes/{EP}/show-profile").json()["source"] == "default"


async def test_the_preflight_reports_the_profile_and_refuses_a_bad_one(studio, satire):
    from temporalio.testing import ActivityEnvironment

    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity

    member = _member(studio)
    report = member.post(f"/api/episodes/{EP}/preflight").json()
    assert report["show_profile"] == {"source": "episode", "digest": resolve_show_profile(EP).digest, "errors": []}

    (satire / "show.json").write_text(json.dumps({"schema": "hfvg.show/1", "cast": {"Bad Id": {}}}))
    report = member.post(f"/api/episodes/{EP}/preflight").json()
    assert report["has_errors"] is True and report["show_profile"]["errors"]
    with pytest.raises(PreflightError) as err:
        await ActivityEnvironment().run(preflight_validate_episode_activity, EP)
    assert any("show profile" in p and "cast.Bad Id" in p for p in err.value.problems), err.value.problems


async def test_the_cast_routes_offer_and_take_the_shows_body_preset(studio, satire, tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    member = _member(studio)
    presets = member.get(f"/api/cast/body-presets?episode_id={EP}")
    assert presets.status_code == 200, presets.text
    assert presets.json() == {"episode_id": EP, "default": "humanoid", "body_types": [
        {"value": "humanoid", "label": "Two legs, like a person", "source": "built-in"},
        {"value": "four_legged", "label": "Four legs", "source": "built-in"},
        {"value": "other", "label": "Another kind of body", "source": "built-in"},
        {"value": "seated_desk", "label": "Seated at a desk", "source": "show"}]}
    assert [b["value"] for b in member.get("/api/cast/body-presets").json()["body_types"]] == [
        "humanoid", "four_legged", "other"], "no episode: the deployment's (here the neutral) profile"
    admin = {"Authorization": f"Bearer {ADMIN}"}
    made = member.post(f"/api/episodes/{EP}/cast", headers=admin, json={
        "name": "The Moderator", "role": "moderator", "body_type": "seated_desk",
        "description": "an invented television moderator in a teal blazer with a neat grey bob"})
    assert made.status_code == 200 and made.json()["character"]["body_type"] == "seated_desk", made.text
    refused = member.post("/api/episodes/ep72other/cast", headers=admin,
                          json={"name": "Guest", "body_type": "seated_desk"})
    assert refused.status_code == 400 and refused.json()["detail"] == (
        "Body type must be one of: humanoid, four_legged, other"), "another show does not get this show's preset"


# ------------------------------------------------------------------------------------------ cast and sheets


@pytest.fixture
def cast(satire, tmp_path, monkeypatch):
    """The fixture's cast.json in the episode's own cast library (slug, name, role, look, body)."""
    from hfvg import cast_store

    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.setenv("DRY_RUN", "true")
    rows = json.loads((FIXTURE / "cast.json").read_text(encoding="utf-8"))["characters"]
    return {r["slug"]: cast_store.create_character(("episode", EP), r["name"], r["role"], r["description"],
                                                   slug=r["slug"], actor="tester", body_type=r["body_type"])
            for r in rows}


def _plan(character: dict, stage: str, **kw) -> list[dict]:
    from hfvg import cast_store
    from hfvg.cast_sheets import plan_jobs

    return plan_jobs(cast_store.get_character(character["character_id"]), 1, stage, episode_id=EP, live=False,
                     **kw)["jobs"]


def test_the_cast_keeps_the_shows_body_preset_and_another_show_does_not_get_it(cast):
    from hfvg import cast_store

    assert cast["moderator"]["body_type"] == "seated_desk" and cast["candidate-grey"]["body_type"] == "humanoid"
    assert cast_store.body_types(EP) == ("humanoid", "four_legged", "other", "seated_desk")
    with pytest.raises(cast_store.CastError, match="^Body type must be one of: humanoid, four_legged, other$"):
        cast_store.create_character(("episode", "ep72other"), "Guest", body_type="seated_desk", actor="tester")
    with pytest.raises(cast_store.CastError, match="seated_desk$"):
        cast_store.update_character(cast["moderator"]["character_id"], body_type="standing_desk")


def test_the_sheets_lead_with_the_show_and_frame_its_preset(cast):
    from hfvg.cast_sheets import PLACEHOLDER_HEAD, SHEET_BODIES, placeholder_image

    lead = "Satire cast sheet of an invented person."
    preset = resolve_show_profile(EP).body_preset("seated_desk")
    for slug, character in cast.items():
        options = _plan(character, "options", source="text")
        for job in options:
            assert job["prompt"].startswith(f"{lead} {character['name']}: "), job["prompt"]
            assert "original character" not in job["prompt"] and not species_words(job["prompt"]), job["prompt"]
        if slug == "moderator":
            assert all(preset.frame in j["prompt"] and j["aspect_ratio"] == "3:4" for j in options)
            assert all(j["placeholder"] == [832, 1024] for j in options)
            size = Image.open(io.BytesIO(placeholder_image(options[0]))).size
            assert size == (832, 1024)
        else:
            assert all(SHEET_BODIES["humanoid"]["frame"] in j["prompt"] and j["aspect_ratio"] == "2:3"
                       for j in options)
            assert all("placeholder" not in j for j in options), "a built-in body keeps the jobs it always had"

    from hfvg import cast_store
    moderator = cast["moderator"]
    cast_store.add_generated_image(moderator["character_id"], 1, _png(), role=cast_store.MASTER_ROLE,
                                   job_id=f"m-{moderator['character_id']}", placeholder=True, actor="t")
    views = {j["view"]: j for j in _plan(moderator, "turnaround")}
    assert all(preset.frame in views[v]["prompt"] for v in views if v != "head_sheet")
    assert preset.head in views["head_sheet"]["prompt"]
    assert Image.open(io.BytesIO(placeholder_image(views["head_sheet"]))).size == PLACEHOLDER_HEAD
    assert not any(species_words(j["prompt"]) for j in views.values())


def test_a_locked_look_with_a_show_preset_can_be_revised(cast):
    from hfvg import cast_store
    from tests.cast_helpers import complete_sheet

    cid = cast["moderator"]["character_id"]
    complete_sheet(cast["moderator"])
    cast_store.approve_version(cid, 1, "t")
    revised = cast_store.revise_locked_version(cid, 1, description="an invented moderator in a red blazer",
                                               reason="new season look", actor="admin-1", episode_id=EP)
    assert revised["locked_description"] == "The Moderator: an invented moderator in a red blazer."
    assert cast_store.verify_lock(cid, 1) == []
    assert cast_store.get_character(cid)["body_type"] == "seated_desk"


def _png() -> bytes:
    from tests.cast_helpers import image_bytes
    return image_bytes(seed=7)


# ------------------------------------------------------------------------------------------ routing, gates, plates


def test_routing_speaks_of_characters(satire):
    from hfvg.routing import RoutingError, plan_route

    shots = json.loads((satire / "shots.json").read_text(encoding="utf-8"))["shots"]
    for shot in shots:
        route = plan_route({**shot, "tier": "draft"})
        assert route.model == "kling"
        if shot["count"] > 1:
            with pytest.raises(RoutingError) as err:
                plan_route({**shot, "tier": "draft", "model": "seedance"})
            assert str(err.value).startswith(f"Multi-character shots (count={shot['count']}) must use Kling")
            assert not species_words(str(err.value))
    assert plan_route({**shots[0], "tier": "draft", "model": "seedance", "duration": 5}).model == "seedance", (
        "nobody in frame: Seedance stays eligible")


def test_the_show_gets_the_neutral_gate_policy_in_general_words(satire):
    profile = resolve_show_profile(EP)
    policy = show_profile.load_gate_policy(profile)
    assert policy.series == "Neutral default" and len(policy.gates) == 71
    words = {w for g in policy.gates for v in g.values() if isinstance(v, str) for w in species_words(v)}
    assert words == set(), words
    assert show_profile.gate_policy_summary(profile) == {"from": "neutral", "series": "Neutral default", "errors": []}


def test_a_plate_scales_the_shows_roles_against_its_scale_reference(tmp_path):
    from hfvg import plate_composite
    from tests.test_r9_plate import _spec

    spec = _spec(tmp_path)
    spec["scale_ref_height_frac"] = spec.pop("duck_height_frac")
    for layer, role in zip(spec["layers"], ("candidate", "moderator")):
        layer["height_frac_of_scale_ref"] = layer.pop("height_frac_of_duck")
        layer["role"] = role
    spec["rules"] = {"role_to_scale_ref": {"candidate": [1.8, 2.2], "moderator": [0.85, 1.15]},
                     "back_max_frac": {"candidate": 0.9}}
    manifest = plate_composite.compose(spec, tmp_path / "plate.png")
    assert manifest["passed"] is True and manifest["scale_ref_height_frac"] == 0.4
    for row in manifest["layers"]:
        assert row["ratio_to_scale_ref"] == row["ratio_to_duck"], "both spellings for one release"
        assert row["checks"]["ratio_to_scale_ref"] is True
    assert "back_below_ref_at_depth" in manifest["layers"][0]["checks"]
    assert "back_below_ref_at_depth" not in manifest["layers"][1]["checks"], "only the roles the show bounds"
    spec["layers"][1]["role"] = "audience"
    with pytest.raises(plate_composite.PlateSpecError, match=r"role must be one of \['candidate', 'moderator'\]"):
        plate_composite.validate_spec(spec)
    spec["layers"][1]["role"] = "moderator"
    spec["duck_height_frac"] = 0.4
    with pytest.raises(plate_composite.PlateSpecError, match="mixes"):
        plate_composite.validate_spec(spec)
