"""GAP-38: the codec TP guard runs up to 16 limiter tries (several ffmpeg passes each) and used to
send no heartbeat, so mix_and_deliver (heartbeat_timeout 2 min) was cancelled mid-guard on ep05v2
g3r2. Every guard pass must heartbeat, and mix_and_deliver must hand its heartbeat to both guards."""
import ast
import inspect
import textwrap

import pytest

from hfvg import audio_mix

LOUD = {**audio_mix.DEFAULT_LOUDNESS, "target_lufs": -12.6, "tolerance_lu": 0.5, "tp_ceiling_dbtp": -2.0}


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    calls = []

    async def run_cmd(cmd, what="", *a, **k):
        calls.append(what)
        return "", ""

    async def measure(path, target=None):
        # encode always overshoots the ceiling: the guard walks every one of its tries
        return {"integrated_lufs": -12.6, "true_peak_dbtp": -0.5, "lra": 5.0, "threshold": -23.0}

    monkeypatch.setattr(audio_mix, "run_cmd", run_cmd)
    monkeypatch.setattr(audio_mix, "measure_loudness", measure)
    return calls


async def test_every_guard_try_heartbeats(fake_ffmpeg, tmp_path):
    beats = []
    pcm, info = await audio_mix.codec_safe_pcm("mix.wav", 192, LOUD, str(tmp_path), "master", heartbeat=beats.append)
    assert info["ok"] is False and len(info["tries"]) == 16
    assert len(beats) == 1 + len(info["tries"]), beats
    assert all(b["stage"] == "codec_tp_guard" and b["tag"] == "master" for b in beats)
    assert [b["try"] for b in beats] == list(range(17))


async def test_guard_without_heartbeat_still_works(fake_ffmpeg, tmp_path):
    pcm, info = await audio_mix.codec_safe_pcm("mix.wav", 64, LOUD, str(tmp_path), "phone")
    assert len(info["tries"]) == 16 and pcm.endswith(".wav")


def test_mix_and_deliver_passes_its_heartbeat_to_both_guards():
    tree = ast.parse(textwrap.dedent(inspect.getsource(audio_mix.mix_and_deliver)))
    guard_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and getattr(n.func, "id", None) == "codec_safe_pcm"]
    assert len(guard_calls) == 2
    for c in guard_calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert "heartbeat" in kw and getattr(kw["heartbeat"], "id", None) == "heartbeat", ast.dump(c)
