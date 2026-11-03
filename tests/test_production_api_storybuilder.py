"""StoryBuilder's Production tab: UPLOAD_TOKEN reaches every route it needs to upload and check an episode's
four files and nothing else, validate checks those files together without saving them, /lint lints the prompts,
and the token never replaces the files of an episode that already has a workflow."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

ADMIN = "test_admin_secret_at_least_32_chars_long_here"
TOKEN = "test_upload_token_at_least_32_chars_long"
AS_TOKEN = {"Authorization": f"Bearer {TOKEN}"}
AS_ADMIN = {"Authorization": f"Bearer {ADMIN}"}
EP = "ep41"

LINT = {"count_template": "Ducks in frame: {n}.", "realtime_pattern": "real-time",
        "locked_camera_clause": "locked camera", "duck_clause": "The tuft stays three-part",
        "duck_characters": ["D"], "banned_decor": ["anime"], "forbidden_phrases": []}
KIT = {"style": "Soft light.", "characters": {"D": {"name": "Duck", "description": "A duck.", "ref": ""}},
       "lint": LINT}


def prompt(n=1):
    return f"A duck on a branch. The tuft stays three-part. Real-time, locked camera. Ducks in frame: {n}."


def shot(sid="SH01", **kw):
    return {"id": sid, "count": 1, "characters": ["D"], "wet": False, "water": False, "exterior": True,
            "duck_present": True, "model": "kling", "duration": 5, "end_frame": False, "aspect_ratio": "9:16",
            "ref_keys": [], "prompt": prompt(), "clip_prompt": "The leaves sway slowly. " + prompt(), **kw}


def beatmap(*rows):
    """rows: (shot id, model letter, action)"""
    lines = ["# ep41: BEATMAP", "",
             "| # | ID | Screen (gen) s | Room | TOD | Chars (codes) | Action | Purpose | In | Out | Model | N | Duck state |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {i} | {sid} | 5 (5) | Forest | Dusk | D | {action} | Beat | Cut | Cut | {model} | 1 | present |"
              for i, (sid, model, action) in enumerate(rows, 1)]
    return "\n".join(lines) + "\n"


def episode_files(shots=None, refs=None, kit=KIT, beat=None):
    shots = shots if shots is not None else [shot()]
    refs = refs if refs is not None else {"per_shot": {s["id"]: [] for s in shots}}
    beat = beat or beatmap(*[(s["id"], "S" if s["model"] == "seedance" else "K", "The duck looks up") for s in shots])
    return {"beatmap": beat.encode(), "shots": json.dumps({"episode_id": EP, "shots": shots}).encode(),
            "refs": json.dumps(refs).encode(), "kit": json.dumps(kit).encode()}


def temporal(status=None):
    """A Temporal client whose episode workflow has `status`, or that has none at all."""
    from temporalio.service import RPCError, RPCStatusCode

    handle = MagicMock()
    if status is None:
        handle.describe = AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    else:
        handle.describe = AsyncMock(return_value=MagicMock(status=status))
    client = MagicMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.setenv("UPLOAD_TOKEN", TOKEN)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    import api.main as api_main
    from hfvg.studio_db import init_studio_db

    db_path = str(tmp_path / "studio.db")
    asyncio.run(init_studio_db(db_path))
    monkeypatch.setattr(api_main, "DATABASE_PATH", db_path)
    monkeypatch.setattr(api_main, "temporal_client", temporal())
    client = TestClient(api_main.app)
    client.module = api_main
    client.episodes = tmp_path / "episodes"
    return client


def validate(client, files, headers=AS_TOKEN):
    return client.post("/api/episodes/upload/validate", headers=headers, data={"episode_id": EP}, files={
        "beatmap_file": ("BEATMAP.md", files["beatmap"]), "shots_file": ("shots.json", files["shots"]),
        "refs_file": ("refs.json", files["refs"]), "prompt_kit_file": ("prompt_kit.json", files["kit"])})


def send(client, files, headers=AS_TOKEN):
    """What the Production tab's Send does after validate: the three uploads, then lint and preflight."""
    return [
        client.post("/api/episodes/upload", headers=headers, data={"episode_id": EP},
                    files={"beatmap_file": ("BEATMAP.md", files["beatmap"])}),
        client.post(f"/api/episodes/{EP}/upload-prompt-kit", headers=headers,
                    files={"prompt_kit_file": ("prompt_kit.json", files["kit"])}),
        client.post(f"/api/episodes/{EP}/upload-shots", headers=headers,
                    files={"shots_file": ("shots.json", files["shots"]), "refs_file": ("refs.json", files["refs"])}),
        client.post(f"/api/episodes/{EP}/lint", headers=headers),
        client.post(f"/api/episodes/{EP}/preflight", headers=headers, json={}),
    ]


# ------------------------------------------------------------------ what the token reaches

def test_the_token_uploads_and_checks_an_episode(api):
    files = episode_files()
    checked = validate(api, files)
    assert checked.status_code == 200, checked.text
    assert checked.json()["valid"] is True, checked.json()["errors"]
    upload, kit, shots, lint, preflight = send(api, files)
    for response in (upload, kit, shots, lint, preflight):
        assert response.status_code == 200, response.text
    assert lint.json()["status"] == "pass", lint.json()
    assert preflight.json()["has_errors"] is False
    assert (api.episodes / EP / "shots.json").read_bytes() == files["shots"]
    assert (api.episodes / EP / "prompt_kit.json").read_bytes() == files["kit"]


@pytest.mark.parametrize("method,path", [
    ("post", "/api/episodes"),
    ("post", f"/api/episodes/{EP}/approve"),
    ("post", f"/api/episodes/{EP}/set-live"),
    ("get", f"/api/episodes/{EP}"),
    ("post", "/api/lint"),
])
def test_the_token_reaches_nothing_else(api, method, path):
    response = getattr(api, method)(path, headers=AS_TOKEN, **({"json": {}} if method == "post" else {}))
    assert response.status_code == 403, f"{path}: {response.status_code} {response.text}"


@pytest.mark.parametrize("path", [f"/api/episodes/{EP}/upload-prompt-kit", f"/api/episodes/{EP}/upload-shots",
                                  f"/api/episodes/{EP}/lint", f"/api/episodes/{EP}/preflight"])
def test_the_new_routes_still_refuse_a_wrong_or_missing_credential(api, path):
    assert api.post(path, headers={"Authorization": "Bearer " + "x" * 40}).status_code == 403
    assert api.post(path).status_code == 401


def test_an_unset_upload_token_matches_nothing(api, monkeypatch):
    monkeypatch.delenv("UPLOAD_TOKEN")
    assert api.post(f"/api/episodes/{EP}/lint", headers={"Authorization": "Bearer "}).status_code in (401, 403)
    assert api.post(f"/api/episodes/{EP}/lint", headers=AS_TOKEN).status_code == 403


# ------------------------------------------------------------------ existing workflows

def test_the_token_never_replaces_files_under_a_finished_workflow(api, monkeypatch):
    from temporalio.client import WorkflowExecutionStatus

    monkeypatch.setattr(api.module, "temporal_client", temporal(WorkflowExecutionStatus.COMPLETED))
    upload, kit, shots, _, _ = send(api, episode_files())
    for response in (upload, kit, shots):
        assert response.status_code == 409, response.text
        assert "already has a workflow (completed)" in response.json()["detail"]
    assert not (api.episodes / EP).exists()


def test_the_console_keeps_its_behaviour_under_a_finished_workflow(api, monkeypatch):
    from temporalio.client import WorkflowExecutionStatus

    monkeypatch.setattr(api.module, "temporal_client", temporal(WorkflowExecutionStatus.COMPLETED))
    upload, kit, shots, _, _ = send(api, episode_files(), headers=AS_ADMIN)
    assert [r.status_code for r in (upload, kit, shots)] == [200, 200, 200]


def test_nobody_uploads_while_a_workflow_runs(api, monkeypatch):
    from temporalio.client import WorkflowExecutionStatus

    monkeypatch.setattr(api.module, "temporal_client", temporal(WorkflowExecutionStatus.RUNNING))
    for headers in (AS_TOKEN, AS_ADMIN):
        upload = send(api, episode_files(), headers=headers)[0]
        assert upload.status_code == 409
        assert "currently running" in upload.json()["detail"]


# ------------------------------------------------------------------ validate

def test_validate_reports_per_shot_and_writes_nothing(api):
    body = validate(api, episode_files([shot("SH01"), shot("SH02", model="seedance")])).json()
    assert body["valid"] is True, body["errors"]
    assert set(body["shots"]) == {"SH01", "SH02"}
    assert body["files_validated"]["shots"] and body["files_validated"]["prompt_kit"]
    assert body["cost_estimate"]["total_usd_micros"] > 0
    assert not api.episodes.exists()


@pytest.mark.parametrize("case,files,expected", [
    ("wet on seedance", episode_files([shot(model="seedance", wet=True)]), "SH01: routing"),
    ("two animals on seedance", episode_files([shot(model="seedance", count=2, prompt=prompt(2),
                                                    clip_prompt="Leaves sway. " + prompt(2))]), "SH01: routing"),
    ("beatmap says swims", episode_files([shot(model="seedance")], beat=beatmap(("SH01", "S", "The duck swims"))),
     "SH01: routing"),
    ("count clause missing", episode_files([shot(prompt="A duck. The tuft stays three-part. Real-time.")]),
     "SH01: lint still: missing count clause"),
    ("camera clause missing", episode_files([shot(clip_prompt="Leaves sway. The tuft stays three-part. Real-time. "
                                                               "Ducks in frame: 1.")]),
     "SH01: lint clip: missing locked-camera clause"),
    ("refs out of order", episode_files([shot(ref_keys=["a", "b"])], refs={"per_shot": {"SH01": [
        {"key": "b", "path": "refs/b.png"}, {"key": "a", "path": "refs/a.png"}]}}), "SH01: refs.json lists"),
    ("different shots", episode_files([shot()], beat=beatmap(("SH09", "K", "x"))), "list different shots"),
    ("different model", episode_files([shot(model="seedance")], beat=beatmap(("SH01", "K", "x"))),
     "the beatmap says model K"),
    ("kit without lint", episode_files(kit={"style": "s", "characters": {}}), "no 'lint' section"),
    ("too long for kling", episode_files([shot(duration=20)]), "SH01: preflight"),
    ("broken shots.json", {**episode_files(), "shots": b"{nope"}, "shots.json"),
])
def test_validate_reports_what_an_upload_or_a_live_run_would_refuse(api, case, files, expected):
    body = validate(api, files).json()
    assert body["valid"] is False, case
    assert any(expected in e for e in body["errors"]), f"{case}: {body['errors']}"
    assert not api.episodes.exists()


def test_validate_injects_continuity_locks_as_the_submit_does(api):
    lock = {"id": "bill", "when": "duck", "kinds": ["still", "clip"], "text": "The bill stays flat."}
    body = validate(api, episode_files(kit={**KIT, "continuity_locks": [lock]})).json()
    assert body["valid"] is True, body["errors"]


def test_validate_with_the_beatmap_alone_is_unchanged(api):
    files = episode_files()
    body = api.post("/api/episodes/upload/validate", headers=AS_TOKEN, data={"episode_id": EP},
                    files={"beatmap_file": ("BEATMAP.md", files["beatmap"])}).json()
    assert body["valid"] is True and body["shots_count"] == 1
    assert "shots" not in body


# ------------------------------------------------------------------ /lint

def _write(api, files, kit=True):
    folder = api.episodes / EP
    folder.mkdir(parents=True)
    (folder / "BEATMAP.md").write_bytes(files["beatmap"])
    (folder / "shots.json").write_bytes(files["shots"])
    if kit:
        (folder / "prompt_kit.json").write_bytes(files["kit"])


def test_lint_route_lints_the_prompts(api):
    _write(api, episode_files([shot(prompt="A duck. The tuft stays three-part. Real-time, locked camera.")]))
    body = api.post(f"/api/episodes/{EP}/lint", headers=AS_TOKEN).json()
    assert body["status"] == "fail"
    assert any(i.startswith("SH01 still: missing count clause") for i in body["issues"]), body["issues"]


def test_lint_route_without_a_kit_warns_and_passes(api):
    _write(api, episode_files(), kit=False)
    body = api.post(f"/api/episodes/{EP}/lint", headers=AS_TOKEN).json()
    assert body["status"] == "pass", body
    assert any("prompt_kit.json missing" in w for w in body["warnings"])


def test_lint_report_injects_continuity_locks_first(tmp_path, monkeypatch):
    from hfvg.prompt_lint import kit_from_prompt_kit, lint_plans
    from hfvg.shot_data import parse_shots

    lock = {"id": "bill", "when": "duck", "kinds": ["still"], "text": "The bill stays flat."}
    plans = parse_shots({"shots": [shot()]})
    assert lint_plans(plans, kit_from_prompt_kit({**KIT, "continuity_locks": [lock]})) == {}
    assert lint_plans(plans, kit_from_prompt_kit({**KIT, "lint": {**LINT, "count_template": "Geese: {n}."}}))
