"""Preflight rules beyond refs/prompt/duration: aspect/resolution/quality, frames, model status, audio."""
import pytest
from PIL import Image

from hfvg import model_caps, preflight

GROK = "xai/grok-imagine-image-2.0"
KLING = "kling-video/v3.0/pro/image-to-video"
SEEDANCE = "bytedance/seedance-2.5/image-to-video"


def _still(**kw):
    shot = {"id": "A01", "start_prompt": "Test", "ref_keys": [], "resolution": "1k", "quality": "medium",
            "aspect_ratio": "9:16"}
    shot.update(kw)
    return preflight.preflight_shot(shot, {"still_model": GROK}, model_caps.get_registry(), phase="start")


def _by(violations, field, severity=None):
    return [v for v in violations if v.field == field and (severity is None or v.severity == severity)]


def test_aspect_resolution_quality():
    """T11: unsupported aspect/resolution refused; omitted values mean the provider default; harness
    quality 'high' is a budget tier the provider sends as 'medium' (warning, not an error)."""
    assert len(_by(_still(aspect_ratio="21:9"), "aspect_ratio", "error")) == 1
    assert "Supported:" in _by(_still(aspect_ratio="21:9"), "aspect_ratio")[0].message
    assert len(_by(_still(resolution="4k"), "resolution", "error")) == 1
    assert len(_by(_still(resolution="720p"), "resolution", "error")) == 1
    # every documented Grok aspect passes, including 'auto'
    for ar in ("auto", "1:1", "16:9", "9:16", "2:3"):
        assert not _by(_still(aspect_ratio=ar), "aspect_ratio"), ar
    # aspect None / quality None = field omitted, provider default: never a violation
    assert not [v for v in _still(aspect_ratio=None, quality=None) if v.severity == "error"]
    # 'high' -> sent as 'medium': one warning that names what is sent, no error
    high = _by(_still(quality="high"), "quality")
    assert [v.severity for v in high] == ["warning"], high
    assert "sent to" in high[0].message and "'medium'" in high[0].message
    assert not _by(_still(quality="low"), "quality") and not _by(_still(quality="medium"), "quality")


def _clip(files=None, **kw):
    shot = {"id": "A01", "clip_prompt": "Test", "duration": 5, "start_frame": "s", "end_frame": None}
    shot.update(kw)
    return preflight.preflight_shot(shot, {"clip_model": kw.pop("model", KLING)}, model_caps.get_registry(),
                                    phase="clip", files=files)


def test_frames_rules(tmp_path):
    """T12: start frame required; end_frame False/None is 'no end frame'; last-frame-only refused;
    frame files: decoded format (not the extension), bytes, min side, aspect range."""
    assert len(_by(_clip(start_frame=None), "start_frame", "error")) == 1
    assert not [v for v in _clip(end_frame=False) if v.severity == "error"], "end_frame False is not an end frame"
    assert not [v for v in _clip(end_frame="e") if v.severity == "error"], "Kling supports start+end"
    lfo = _by(_clip(start_frame=None, end_frame="e"), "frames", "error")
    assert len(lfo) == 1 and "last-frame-only" in lfo[0].message

    ok = tmp_path / "ok.png"
    Image.new("RGB", (720, 1280), "white").save(ok, "PNG")
    assert not [v for v in _clip(files={"s": str(ok)}) if v.severity == "error"]

    bmp_named_png = tmp_path / "frame.png"   # a BMP with a .png name: the extension must not fool it
    Image.new("RGB", (720, 1280), "white").save(bmp_named_png, "BMP")
    fmt = _by(_clip(files={"s": str(bmp_named_png)}), "frame_format", "error")
    assert len(fmt) == 1 and "image/bmp" in fmt[0].message, fmt
    webp = tmp_path / "frame.webp"
    Image.new("RGB", (720, 1280), "white").save(webp, "WEBP")
    assert len(_by(_clip(files={"s": str(webp)}), "frame_format", "error")) == 1, "Kling: jpeg/png only"
    assert not _by(_clip(files={"s": str(webp)}, model=SEEDANCE, duration=5), "frame_format"), "Seedance takes webp"

    small = tmp_path / "small.png"
    Image.new("RGB", (299, 600), "white").save(small, "PNG")
    px = _by(_clip(files={"s": str(small)}), "frame_min_px", "error")
    assert len(px) == 1 and "299 px" in px[0].message and "300 px" in px[0].message

    wide = tmp_path / "wide.png"
    Image.new("RGB", (1300, 500), "white").save(wide, "PNG")   # 2.6 > 2.5
    assert len(_by(_clip(files={"s": str(wide)}), "frame_aspect_range", "error")) == 1
    edge = tmp_path / "edge.png"
    Image.new("RGB", (1250, 500), "white").save(edge, "PNG")   # exactly 2.5 is allowed
    assert not _by(_clip(files={"s": str(edge)}), "frame_aspect_range")

    big = tmp_path / "big.png"
    big.write_bytes(ok.read_bytes() + b"\0" * (10 * 1024 * 1024))   # valid PNG, > 10 MiB
    size = _by(_clip(files={"s": str(big)}), "frame_max_bytes", "error")
    assert len(size) == 1 and "10485760" in size[0].message, size

    missing = _by(_clip(files={"s": str(tmp_path / "nope.png")}), "frame_file", "error")
    assert len(missing) == 1


def test_model_status_rules():
    """T13: MCP-only refused; deprecated / not-routed warn; eleven_music warns that music_v1 is sent."""
    gpt = _by(preflight.preflight_shot({"id": "A01", "start_prompt": "T", "ref_keys": []},
                                       {"still_model": "gpt_image_2"}, model_caps.get_registry(), phase="start"),
              "status")
    assert [v.severity for v in gpt] == ["error"] and "MCP" in gpt[0].message
    qwen = _by(preflight.preflight_shot({"id": "A01", "start_prompt": "T", "ref_keys": ["r"]},
                                        {"still_model": "alibaba/qwen-image-3/edit"}, model_caps.get_registry(),
                                        phase="start"), "status")
    assert [v.severity for v in qwen] == ["warning"] and "not routed" in qwen[0].message
    reg = model_caps.get_registry()
    turbo = _by(preflight.preflight_shot({"id": "V1", "text": "hi"}, {"audio_model": "eleven_turbo_v2_5"}, reg,
                                         phase="audio"), "status")
    assert [v.severity for v in turbo] == ["warning"] and "deprecated" in turbo[0].message
    music = _by(preflight.preflight_shot({"id": "M1"}, {"audio_model": "eleven_music"}, reg, phase="audio"), "status")
    assert [v.severity for v in music] == ["warning"] and "music_v1" in music[0].message
    unknown = _by(preflight.preflight_shot({"id": "A01", "clip_prompt": "T", "duration": 5, "start_frame": "s"},
                                           {"clip_model": "made/up"}, reg, phase="clip"), "model")
    assert [v.severity for v in unknown] == ["error"]


def test_audio_limits():
    """T14: TTS text length, SFX seconds, music milliseconds are enforced (errors), boundaries pass."""
    reg = model_caps.get_registry()

    def audio(model, **shot):
        return [v for v in preflight.preflight_shot({"id": "V1", **shot}, {"audio_model": model}, reg, phase="audio")
                if v.severity == "error"]

    assert not audio("eleven_multilingual_v2", text="a" * 10000)
    tts = audio("eleven_multilingual_v2", text="a" * 10001)
    assert len(tts) == 1 and tts[0].field == "text_max_chars" and "Shorten it by 1" in tts[0].message
    assert not audio("sound_generation", duration_s=0.5) and not audio("sound_generation", duration_s=30)
    assert [v.field for v in audio("sound_generation", duration_s=0.4)] == ["duration_s"]
    assert [v.field for v in audio("sound_generation", duration_s=30.5)] == ["duration_s"]
    assert not audio("eleven_music", music_length_ms=3000) and not audio("eleven_music", music_length_ms=300000)
    assert [v.field for v in audio("eleven_music", music_length_ms=2999)] == ["duration_ms"]
    assert [v.field for v in audio("eleven_music", music_length_ms=300001)] == ["duration_ms"]


def test_plan_normalizer_reads_plans_and_shots_json():
    """shot_inputs(): G1 plans and shots.json entries give the same preflight inputs."""
    plan = {"shot_id": "A02", "prompt": "P", "end_prompt": "E", "refs": ["a", "b"],
            "params": {"duration": 4, "model": "seedance", "resolution": "1k", "quality": "high",
                       "aspect_ratio": None, "end_frame": True}}
    entry = {"id": "A02", "prompt": "P", "end_prompt": "E", "ref_keys": ["a", "b"], "model": "seedance",
             "duration": 4, "end_frame": True}
    a, b = preflight.shot_inputs(plan), preflight.shot_inputs(entry)
    for k in ("id", "start_prompt", "end_prompt", "ref_keys", "duration", "clip_model_alias", "start_frame",
              "end_frame"):
        assert a[k] == b[k], k
    assert a["end_frame"] == "planned" and a["start_frame"] == "planned"
    assert preflight.shot_inputs(dict(entry, end_frame=False))["end_frame"] is None
    assert preflight.route_for(a)["clip_model"] == SEEDANCE
    assert preflight.route_for(preflight.shot_inputs({"id": "X", "prompt": "p"}))["clip_model"] == KLING
