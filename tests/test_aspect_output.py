"""16:9 and 4:3 cards render at their own aspect, end to end.

The card's aspect (hfvg/data/genre_presets.yaml) is written into every drafted shot and the prompt
kit, so the stills are generated in it (Kling / Seedance clips follow the start frame); the mute
edit, its QC and the delivery render on that canvas (hfvg.aspect) instead of letterboxing into
1080x1920. 9:16 episodes keep every activity input, cache key and output they had.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from hfvg import aspect, audio_mix, genre_drafts
from hfvg.activities import audio as audio_act
from hfvg.activities.media import assemble_mute_edit, render_shot_preview
from hfvg.edit_plan import EditPlanError, validate_shot_ops
from hfvg.genre_presets import REGISTRY_PATH, VALID_ASPECTS, resolve_profile
from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
from hfvg.qc.clip_expect import expected_clip_dims
from hfvg.qc.motion import _run_stillgate_analysis
from tests.lint_helpers import capture
from tests.test_audio_delivery import FAST_ENCODE, episode
from tests.test_moderation_recovery import Harness, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (  # noqa: F401
    HB,
    MOVING,
    TASK_QUEUE,
    ClipFactory,
    make_clip,
    needs_ffmpeg,
    no_counter,
)

PRESET_IDS = [p["id"] for p in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]]
ADMIN_SECRET = "aspect-output-admin-secret-0123456789abcdef"
IDEA = "A lighthouse keeper befriends a lost paper kite on a windy evening by the harbor"
SHA = "a" * 64
CLIP_SIZE = {"9:16": "360x640", "16:9": "640x360", "4:3": "480x360"}


def _video(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,sample_aspect_ratio", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True, timeout=30).stdout
    return json.loads(out)["streams"][0]


def _dims(path) -> tuple[int, int]:
    v = _video(path)
    return int(v["width"]), int(v["height"])


def _picture_box(path) -> str:
    """cropdetect's box of the non-black picture: the full frame unless letterboxed / pillarboxed."""
    err = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vf", "cropdetect=limit=24:round=2:reset=0",
                          "-frames:v", "20", "-f", "null", "-"], capture_output=True, text=True, timeout=60).stderr
    boxes = [w.split("=", 1)[1] for w in err.split() if w.startswith("crop=")]
    assert boxes, err[-500:]
    return boxes[-1]


# ------------------------------------------------------------------ canvas table (pure)

def test_canvas_per_aspect():
    assert aspect.canvas("9:16") == aspect.canvas(None) == (1080, 1920)
    assert aspect.canvas("16:9") == (1920, 1080)
    assert aspect.canvas("4:3") == (1440, 1080)
    assert aspect.canvas("16:9_rotated_to_9:16") == (1080, 1920)
    err = capture(aspect.AspectError, aspect.canvas, "2:1")
    assert err is not None and "unsupported aspect ratio '2:1'" in str(err)


def test_every_card_aspect_has_a_canvas():
    assert {a: aspect.generation_aspect(a) for a in VALID_ASPECTS} == {
        "9:16": "9:16", "16:9": "16:9", "4:3": "4:3", "16:9_rotated_to_9:16": "9:16"}
    for pid in PRESET_IDS:
        w, h = aspect.canvas(resolve_profile(pid)["aspect"])
        assert w % 2 == 0 and h % 2 == 0, pid


def test_episode_canvas_is_the_aspect_most_shots_use():
    assert aspect.episode_aspect([]) == "9:16"
    assert aspect.episode_aspect([None, None]) == "9:16"
    assert aspect.episode_aspect(["16:9", "9:16", "16:9"]) == "16:9"
    assert aspect.episode_aspect(["4:3", "16:9"]) == "4:3"  # tie: the first shot's
    assert aspect.episode_aspect(["16:9_rotated_to_9:16"]) == "9:16"
    assert aspect.episode_aspect(["2:1", "4:3"]) == "4:3"


def test_aspect_of_a_frame_size():
    assert aspect.aspect_of_size(1920, 1080) == "16:9"
    assert aspect.aspect_of_size(1440, 1080) == "4:3"
    assert aspect.aspect_of_size(540, 960) == "9:16"
    assert aspect.aspect_of_size(720, 1276) == "9:16"  # provider rounding stays 9:16
    assert aspect.aspect_of_size(2000, 1000) is None
    assert aspect.aspect_of_size(0, 1080) is None


def test_clip_expectations_match_the_canvas():
    for a in ("9:16", "16:9", "4:3"):
        dims = expected_clip_dims({"model": "kling", "aspect_ratio": a})
        assert dims["resolution"] == "x".join(map(str, aspect.canvas(a))), a
        assert dims["aspect_ratio"] == a


def test_canvas_argument_only_for_other_aspects():
    """9:16 episodes schedule assemble_mute_edit / mix_and_deliver with exactly their old inputs."""
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

    def args(*aspects):
        shots = [{"shot_id": f"S{i}", "params": {"aspect_ratio": a} if a else {}} for i, a in enumerate(aspects)]
        return EpisodeWorkflowV2._canvas_args(SimpleNamespace(assembly_shots=shots))

    assert args("9:16", "9:16") == [] and args(None) == [] and args() == []
    assert args("16:9", "16:9") == ["16:9"]
    assert args("4:3") == ["4:3"]
    assert args("16:9_rotated_to_9:16") == []


# ------------------------------------------------------------------ subtitles per canvas (pure)

def test_subtitle_style_per_canvas():
    portrait = aspect.subtitle_style(1080, 1920)
    assert portrait == {"aspect": "9:16", "play_res": (1080, 1920), "fontsize": 64, "margin_v": 260,
                        "max_line_chars": 24}
    assert {k: portrait[k] for k in ("fontsize", "margin_v", "max_line_chars")} == {
        k: audio_mix.DEFAULT_SUBTITLES[k] for k in ("fontsize", "margin_v", "max_line_chars")}
    assert aspect.subtitle_style(540, 960)["play_res"] == (1080, 1920)
    assert aspect.subtitle_style(1920, 1080) == {"aspect": "16:9", "play_res": (1920, 1080), "fontsize": 54,
                                                 "margin_v": 64, "max_line_chars": 42}
    assert aspect.subtitle_style(720, 540) == {"aspect": "4:3", "play_res": (1440, 1080), "fontsize": 54,
                                               "margin_v": 70, "max_line_chars": 36}


def test_subtitle_defaults_follow_the_picture_and_plan_values_stay():
    subs = dict(audio_mix.DEFAULT_SUBTITLES)
    assert audio_mix.subtitle_settings(subs, 1080, 1920) == (subs, (1080, 1920))
    wide, res = audio_mix.subtitle_settings(subs, 1920, 1080)
    assert res == (1920, 1080)
    assert (wide["fontsize"], wide["margin_v"], wide["max_line_chars"]) == (54, 64, 42)
    assert wide["font"] == subs["font"] and wide["burn"] is True
    kept, _ = audio_mix.subtitle_settings({**subs, "fontsize": 80, "max_line_chars": 30}, 1440, 1080)
    assert (kept["fontsize"], kept["margin_v"], kept["max_line_chars"]) == (80, 70, 30)


def test_ass_script_canvas(tmp_path):
    cue = [{"start": 1.0, "end": 2.0, "lines": ["Hi"]}]
    audio_mix.write_ass(cue, str(tmp_path / "wide.ass"), audio_mix.DEFAULT_SUBTITLES, (1920, 1080))
    audio_mix.write_ass(cue, str(tmp_path / "tall.ass"), audio_mix.DEFAULT_SUBTITLES)
    assert "PlayResX: 1920\nPlayResY: 1080\n" in (tmp_path / "wide.ass").read_text()
    assert "PlayResX: 1080\nPlayResY: 1920\n" in (tmp_path / "tall.ass").read_text()


# ------------------------------------------------------------------ edit plan crops (pure)

def test_crop_must_have_the_canvas_shape():
    tall = {"sha256": SHA, "reason": "punch in", "crop": [0, 0, 540, 960]}
    wide = {"sha256": SHA, "reason": "punch in", "crop": [0, 0, 960, 540]}
    four_three = {"sha256": SHA, "reason": "punch in", "crop": [0, 0, 960, 720]}
    assert validate_shot_ops("S01", tall)["crop"] == [0, 0, 540, 960]
    assert validate_shot_ops("S01", wide, 1920, 1920 / 1080)["crop"] == [0, 0, 960, 540]
    assert validate_shot_ops("S01", four_three, 1440, 1440 / 1080)["crop"] == [0, 0, 960, 720]
    err = capture(EditPlanError, validate_shot_ops, "S01", wide)
    assert err is not None and "is not 9:16 (would letterbox)" in str(err)
    err = capture(EditPlanError, validate_shot_ops, "S01", tall, 1920, 1920 / 1080)
    assert err is not None and "is not the 1.778 canvas shape (would letterbox)" in str(err)
    err = capture(EditPlanError, validate_shot_ops, "S01", {**wide, "crop": [0, 0, 640, 360]}, 1920, 1920 / 1080)
    assert err is not None and "3.00x upscale" in str(err)


# ------------------------------------------------------------------ drafts

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient

    from api.main import app

    test_client = TestClient(app)
    login = test_client.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert login.status_code == 200
    test_client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return test_client


def _draft(client, card: str):
    res = client.post("/api/episodes/draft-from-idea",
                      json={"idea": IDEA, "archetype_id": card,
                            "target_duration": resolve_profile(card)["default_length_s"]})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize("card,expected", [("mascot-sketch", "16:9"), ("creature-legend", "4:3"),
                                           ("cute-greeting", "9:16"), ("fullscreen-action-anime", "9:16")])
def test_draft_shots_and_prompt_kit_carry_the_card_aspect(client, card, expected):
    data = _draft(client, card)
    assert data["prompt_kit_json"]["aspect_ratio"] == expected
    assert {s["aspect_ratio"] for s in data["shots_json"]["shots"]} == {expected}
    notes = [w for w in data["warnings"] if "framed for" in w]
    if card == "fullscreen-action-anime":
        assert notes == ["Full-screen action anime is designed for 16:9 rotated to 9:16; the studio renders it "
                         "9:16, so the shots are framed for 9:16."], data["warnings"]
    else:
        assert notes == [], data["warnings"]


def test_still_model_without_the_card_aspect_falls_back_to_9_16(client, monkeypatch):
    import hfvg.model_caps as caps

    class Registry:
        def resolve(self, model):
            return SimpleNamespace(limit=lambda name: SimpleNamespace(value=["9:16", "1:1"]))

    monkeypatch.setenv("MODEL_PATH_STILL", "example/portrait-only-still")
    monkeypatch.setattr(caps, "get_registry", lambda: Registry())
    data = _draft(client, "creature-legend")
    assert data["prompt_kit_json"]["aspect_ratio"] == "9:16"
    assert {s["aspect_ratio"] for s in data["shots_json"]["shots"]} == {"9:16"}
    assert ("Fantasy creature legend is designed for 4:3, but the still model example/portrait-only-still "
            "cannot make 4:3 images, so the shots are framed for 9:16.") in data["warnings"]


def test_unknown_still_model_keeps_the_card_aspect(client, monkeypatch):
    """Preflight refuses an unknown still model at run start; the draft does not guess."""
    monkeypatch.setenv("MODEL_PATH_STILL", "example/not-in-the-registry")
    data = _draft(client, "mascot-sketch")
    assert data["prompt_kit_json"]["aspect_ratio"] == "16:9"
    assert not [w for w in data["warnings"] if "framed for" in w]


def test_llm_prompt_and_files_use_the_card_aspect(client, monkeypatch):
    from hfvg.config import config

    monkeypatch.delenv("DRAFT_LLM_FAKE")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-for-mocked-calls")
    profile = resolve_profile("ambience-journey")
    target = profile["default_length_s"]
    files = genre_drafts.fake_draft(profile, IDEA, "ep42abc", target, "watercolor")
    files["prompt_kit_json"]["aspect_ratio"] = "9:16"  # the model ignored the card
    for shot in files["shots_json"]["shots"]:
        shot.pop("aspect_ratio")
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=json.dumps(
        {k: files[k] for k in ("beatmap_md", "shots_json", "refs_json", "prompt_kit_json")})))]
    llm = MagicMock()
    llm.chat.completions.create = AsyncMock(return_value=completion)
    with patch("openai.AsyncOpenAI", return_value=llm):
        res = client.post("/api/episodes/draft-from-idea",
                          json={"idea": IDEA, "archetype_id": "ambience-journey", "target_duration": target})
    assert res.status_code == 200, res.text
    system = llm.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert '"aspect_ratio": "4:3"' in system and '"aspect_ratio": "9:16"' not in system
    assert "Every shot is framed landscape (4:3)" in system
    data = res.json()
    assert data["prompt_kit_json"]["aspect_ratio"] == "4:3"
    assert {s["aspect_ratio"] for s in data["shots_json"]["shots"]} == {"4:3"}


# ------------------------------------------------------------------ edit, preview, QC (ffmpeg)

@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_mute_edit_renders_the_canvas_without_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    shots = [{"shot_id": "S01", "duration": 1.0}, {"shot_id": "S02", "duration": 1.0}]
    results = {}
    for name, (canvas_aspect, size) in {"tall": (None, "360x640"), "wide": ("16:9", "640x360"),
                                        "four_three": ("4:3", "480x360")}.items():
        clips = {s["shot_id"]: str(make_clip(tmp_path / f"{name}_{s['shot_id']}.mp4", 1.2, MOVING, size=size))
                 for s in shots}
        args = ["ep86", shots, clips, str(tmp_path / name)] + ([canvas_aspect] if canvas_aspect else [])
        res = await HB().run(assemble_mute_edit, *args)
        w, h = aspect.canvas(canvas_aspect)
        assert _dims(res["assembly_path"]) == (w, h), name
        assert _video(res["assembly_path"])["sample_aspect_ratio"] in ("1:1", "N/A"), name
        assert res["manifest"]["format"] == {"width": w, "height": h, "fps": 30, "pix_fmt": "yuv420p"}
        assert _picture_box(res["assembly_path"]) == f"{w}:{h}:0:0", name
        results[name] = res
    assert len({r["input_key"] for r in results.values()}) == 3

    # The canvas decides: a 9:16 clip in a 16:9 episode is pillarboxed, never stretched.
    clips = {s["shot_id"]: str(tmp_path / f"tall_{s['shot_id']}.mp4") for s in shots}
    mixed = await HB().run(assemble_mute_edit, "ep86", shots, clips, str(tmp_path / "mixed"), "16:9")
    assert _dims(mixed["assembly_path"]) == (1920, 1080)
    assert _picture_box(mixed["assembly_path"]) == "608:1080:656:0"

    explicit = await HB().run(assemble_mute_edit, "ep86", shots,
                              {s["shot_id"]: str(tmp_path / f"tall_{s['shot_id']}.mp4") for s in shots},
                              str(tmp_path / "tall"), "9:16")
    assert explicit["input_key"] == results["tall"]["input_key"] and explicit["reused"] is True

    err = None
    try:
        await HB().run(assemble_mute_edit, "ep86", shots, clips, str(tmp_path / "bad"), "2:1")
    except ApplicationError as e:
        err = e
    assert err is not None and err.non_retryable and "unsupported aspect ratio '2:1'" in str(err)


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shot_preview_renders_the_canvas(tmp_path):
    src = make_clip(tmp_path / "wide.mp4", 1.5, MOVING, size="1280x720")
    tall = await render_shot_preview(str(src), 1.0, str(tmp_path / "tall.mp4"), 24)
    wide = await render_shot_preview(str(src), 1.0, str(tmp_path / "wide_p.mp4"), 24, canvas_aspect="16:9")
    assert _dims(tall["path"]) == (1080, 1920)
    assert _dims(wide["path"]) == (1920, 1080)
    assert _picture_box(wide["path"]) == "1920:1080:0:0"
    crop = await render_shot_preview(str(src), 1.0, str(tmp_path / "crop.mp4"), 24,
                                     ops={"reason": "punch in", "crop": [160, 90, 960, 540]}, canvas_aspect="16:9")
    assert crop["ops"]["crop"] == [160, 90, 960, 540] and _dims(crop["path"]) == (1920, 1080)
    assert _picture_box(crop["path"]) == "1920:1080:0:0"


@needs_ffmpeg
def test_assembly_motion_check_reads_a_landscape_edit(tmp_path):
    edit = make_clip(tmp_path / "edit.mp4", 2.0, MOVING, size="640x360")
    rows = _run_stillgate_analysis(str(edit), {"format": {"width": 1920, "height": 1080},
                                               "clips": [{"id": "S01", "seg_s": 2.0}], "dissolves": {}})
    assert [(r["id"], r["verdict"]) for r in rows] == [("S01", "PASS")], rows


# ------------------------------------------------------------------ delivery

@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_dry_stand_in_picture_has_the_canvas_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    sizes = {}
    for a in (None, "16:9", "4:3"):
        path = await audio_act._stand_in_picture("ep87", 1.0, a)
        sizes[a] = (path.name, _dims(path))
    assert sizes == {None: ("mute_standin_1000ms.mp4", (540, 960)),
                     "16:9": ("mute_standin_1000ms_960x540.mp4", (960, 540)),
                     "4:3": ("mute_standin_1000ms_720x540.mp4", (720, 540))}


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_landscape_delivery_keeps_the_picture_size_and_checks_the_canvas(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    mute, plan = episode(tmp_path, "ep88", seconds=6.0, size="640x360")
    manifest = await audio_mix.mix_and_deliver("ep88", str(mute), plan, canvas_aspect="16:9")
    assert (manifest["picture_width"], manifest["picture_height"], manifest["canvas_aspect"]) == (640, 360, "16:9")
    assert manifest["subtitle_style"] == {"play_res": [1920, 1080], "fontsize": 54, "margin_v": 64,
                                          "max_line_chars": 42}
    assert "PlayResX: 1920\nPlayResY: 1080\n" in Path(manifest["paths"]["ass"]).read_text()
    for name in ("master_subs", "nosubs", "phone"):
        assert _dims(manifest["paths"][name]) == (640, 360), name
    qc = await audio_mix.delivery_qc(manifest, plan)
    checks = {c["name"]: c for c in qc["checks"]}
    assert qc["verdict"] == "PASS", qc["failed"]
    for name in ("master_subs_dims", "nosubs_dims", "phone_dims"):
        assert checks[name]["ok"] and checks[name]["value"] == "640x360", checks[name]
    assert checks["picture_aspect"]["ok"] and checks["picture_aspect"]["value"] == "16:9"

    # A 4:3 episode whose files are not 1440x1080: every picture file and the canvas shape fail.
    wrong = await audio_mix.delivery_qc({**manifest, "canvas_aspect": "4:3", "picture_width": 1440,
                                         "picture_height": 1080}, plan)
    assert wrong["verdict"] == "FAIL"
    assert sorted(wrong["failed"]) == ["master_subs_dims", "nosubs_dims", "phone_dims"], wrong["failed"]
    wrong = await audio_mix.delivery_qc({**manifest, "canvas_aspect": "9:16"}, plan)
    assert wrong["verdict"] == "FAIL" and wrong["failed"] == ["picture_aspect"], wrong["failed"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_phone_size_of_another_shape_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    mute, plan = episode(tmp_path, "ep89", seconds=4.0, size="640x360")
    plan = {**plan, "phone": {**plan["phone"], "width": 720, "height": 1280}}
    err = None
    try:
        await audio_mix.mix_and_deliver("ep89", str(mute), plan)
    except audio_mix.AudioInputError as e:
        err = e
    assert err is not None and "phone size 720x1280 is not the shape of the 640x360 picture" in str(err)
    assert not Path(audio_mix.delivery_paths("ep89")["phone"]).exists()


# ------------------------------------------------------------------ one card per template, end to end

E2E_CARDS = {  # template group -> card
    "cozy_micro": "cute-greeting",
    "gag_short": "satire-sketch",
    "dialogue_episode": "mascot-sketch",
    "continuous_pov": "3d-moment",
    "action_sequence": "live-action-fight",
    "music_montage": "creature-legend",
    "mood_loop": "ambience-journey",
}
E2E_SHOT_S = 3  # Kling's shortest shot: keeps seven real 1080p edits + deliveries quick


def test_the_end_to_end_cards_cover_every_template_and_aspect():
    templates = {resolve_profile(pid)["template"] for pid in PRESET_IDS}
    assert set(E2E_CARDS) == templates
    assert all(resolve_profile(card)["template"] == t for t, card in E2E_CARDS.items())
    assert {resolve_profile(card)["aspect"] for card in E2E_CARDS.values()} == {"9:16", "16:9", "4:3"}


class AspectClips(ClipFactory):
    """Real moving clips at each episode's shot aspect (what the provider returns for that frame)."""

    def __init__(self, tmp_path: Path, sizes: dict[str, str]):
        super().__init__(tmp_path, [MOVING])
        self.sizes = sizes

    def activity(self):
        from temporalio import activity

        from hfvg.activities.save_assets import save_asset_locally as real_save

        @activity.defn(name="save_asset_locally")
        async def save(episode_id, shot_id, asset_url, asset_type, attempt=1):
            if asset_type != "clip":
                return await real_save(episode_id, shot_id, asset_url, asset_type, attempt)
            path = self.dir / f"{episode_id}_{shot_id}_{attempt}.mp4"
            await asyncio.to_thread(make_clip, path, E2E_SHOT_S + 0.5, MOVING, self.sizes[episode_id])
            self.saved.append(str(path))
            return str(path)

        return save


def _write_one_shot_episode(root: Path, ep: str, card: str, data: dict) -> str:
    """The card's draft cut to its first shot (E2E_SHOT_S long) with the card's audio plan for it."""
    shot = {**data["shots_json"]["shots"][0], "duration": E2E_SHOT_S}
    header, separator, first = [ln for ln in data["beatmap"].splitlines() if ln.startswith("|")][:3]
    cells = first.split("|")
    cells[3] = f" {E2E_SHOT_S} ({E2E_SHOT_S}) "
    root.mkdir(parents=True)
    (root / "BEATMAP.md").write_text(f"# Episode {ep} Beatmap\n\n{header}\n{separator}\n{'|'.join(cells)}\n")
    (root / "shots.json").write_text(json.dumps({"shots": [shot]}))
    (root / "refs.json").write_text(json.dumps({"per_shot": {shot["id"]: []}}))
    (root / "prompt_kit.json").write_text(json.dumps(data["prompt_kit_json"]))
    script = [{"speaker": ln.get("speaker") or "Narrator", "text": ln["text"]}
              for ln in (data["audio_plan_json"].get("vo") or {}).get("lines") or []]
    plan, _ = genre_drafts.build_audio_plan(resolve_profile(card), [float(E2E_SHOT_S)], script or None)
    (root / "audio").mkdir()
    (root / "audio" / "audio_plan.json").write_text(json.dumps({**plan, **FAST_ENCODE}))
    return str(root / "BEATMAP.md")


def _concurrent_worker(env, h: Harness, clips: ClipFactory):
    """_worker with the account run lock (one running episode per studio account) stubbed out, so
    the cards run side by side; the lock is covered by tests/test_account_lock.py."""
    from temporalio import activity
    from temporalio.worker import Worker

    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    @activity.defn(name="acquire_run_lock")
    async def acquire(episode_id, workflow_id, account_identifier="default", stale_timeout_seconds=None):
        return {"acquired": True}

    @activity.defn(name="release_run_lock")
    async def release(episode_id):
        return None

    stubbed = {"acquire_run_lock", "release_run_lock", "save_asset_locally"}
    acts = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name not in stubbed]
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                  activities=[*acts, clips.activity(), acquire, release])


async def _poll(handle, pred, timeout: float, what: str) -> dict:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    state = None
    while loop.time() < end:
        state = await handle.query("get_state")
        if pred(state):
            return state
        await asyncio.sleep(0.5)
    raise AssertionError(f"{what} not reached; last state={state}")


async def _run_to_delivery(env, h: Harness, ep: str, beatmap: str) -> tuple:
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
    await _poll(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", 60, f"{ep} G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _poll(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", 60, f"{ep} G2")
    loop = asyncio.get_running_loop()
    end = loop.time() + 60
    while not [c for c in h.still_calls if c[0] == ep]:
        assert loop.time() < end, f"{ep}: no still submitted"
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    await handle.execute_update("approve_g2_scene_stills")
    state = await _poll(handle, lambda s: (s["delivery"].get("qc") or {}).get("verdict"), 200, f"{ep} delivery")
    return handle, state


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_one_card_per_template_renders_its_aspect_end_to_end(dry_env, tmp_path, monkeypatch, no_counter):  # noqa: F811
    """Draft (API) -> stills submitted in the card's aspect -> real clips of that shape -> real mute
    edit on the card's canvas -> assembly QC -> delivery (dry stand-in audio, real picture) whose
    every picture file ffprobes at the canvas size with square pixels and no bars."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient

    from api.main import app

    api = TestClient(app)
    login = api.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert login.status_code == 200
    api.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))

    episodes, sizes = {}, {}
    for i, (template, card) in enumerate(sorted(E2E_CARDS.items())):
        ep = f"ep{80 + i}{template.replace('_', '')[:10]}"
        data = _draft(api, card)
        card_aspect = data["prompt_kit_json"]["aspect_ratio"]
        assert card_aspect == aspect.generation_aspect(resolve_profile(card)["aspect"]), card
        sizes[ep] = CLIP_SIZE[card_aspect]
        episodes[ep] = (card, card_aspect, _write_one_shot_episode(tmp_path / "data" / "episodes" / ep, ep, card, data))

    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    h = Harness({})
    async with await WorkflowEnvironment.start_local() as env:
        async with _concurrent_worker(env, h, AspectClips(tmp_path, sizes)):
            runs = await asyncio.gather(*(_run_to_delivery(env, h, ep, beatmap)
                                          for ep, (_, _, beatmap) in episodes.items()))
            for (ep, (card, card_aspect, _)), (handle, state) in zip(episodes.items(), runs):
                w, h_px = aspect.canvas(card_aspect)
                stills = [c for c in h.still_calls if c[0] == ep]
                assert stills and {c[7] for c in stills} == {card_aspect}, (card, [c[7] for c in stills])
                asm = state["assembly"]
                assert asm["qc"]["verdict"] == "PASS", (card, asm["qc"])
                assert asm["manifest"]["format"]["width"] == w and asm["manifest"]["format"]["height"] == h_px, card
                assert _dims(asm["path"]) == (w, h_px), card
                assert _picture_box(asm["path"]) == f"{w}:{h_px}:0:0", card
                d = state["delivery"]
                assert d["qc"]["verdict"] == "DRY_RUN" and d["qc"]["failed"] == [], (card, d["qc"])
                assert d["manifest"]["canvas_aspect"] == (None if card_aspect == "9:16" else card_aspect), card
                checks = {c["name"] for c in d["qc"]["checks"]}
                assert {"nosubs_dims", "phone_dims"} <= checks, (card, checks)
                outputs = ["nosubs", "phone"] + (["master_subs"] if d["manifest"].get("subtitles_burned") else [])
                for name in outputs:
                    v = _video(d["paths"][name])
                    assert (int(v["width"]), int(v["height"])) == (w, h_px), (card, name)
                    assert v.get("sample_aspect_ratio", "1:1") in ("1:1", "N/A"), (card, name)
                    assert aspect.aspect_of_size(int(v["width"]), int(v["height"])) == card_aspect, (card, name)
                await handle.terminate("aspect e2e done")
