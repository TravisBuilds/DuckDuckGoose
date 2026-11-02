"""GAP-59 (ep05v2 v9b SFX re-spot, 2026-10-28): GAP-23's remap moves an SFX by edit TIME only, so on a
shot retimed by shot_ops (V07 slowed to 0.558x, V22 to 0.505x, V11 with a cut window) the SFX stayed at
its old offset and landed ~1.2 s before the item touched the tray / the first bite. A point SFX may now
carry ``event`` = (shot, source clip sha256, src_s in that clip, hit_s = transient offset in the SFX
file); load_audio_plan maps it through trim + shot_ops segments + speed of the CURRENT edit and places
the SFX so its transient lands on the event frame."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from hfvg import audio_mix as am
from hfvg.audio_mix import AudioInputError

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _manifest():
    # A: plain 4 s; B: 0-4 s source slowed to 0.5x over 8 s; C: segments [0,1]+[3,4] at 0.5x over 4 s
    return {"clips": [
        {"id": "A", "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0, "source_sha256": SHA_A},
        {"id": "B", "seg_s": 8.0, "trim_in": 0.0, "trim_out": 8.0, "source_sha256": SHA_B,
         "shot_ops": {"segments": [[0.0, 4.0]], "speed": 0.5, "interp": "mci", "sha256": SHA_B}},
        {"id": "C", "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0, "source_sha256": SHA_C,
         "shot_ops": {"segments": [[0.0, 1.0], [3.0, 4.0]], "speed": 0.5, "sha256": SHA_C}},
        {"id": "D", "seg_s": 4.0, "trim_in": 1.5, "trim_out": 5.5, "source_sha256": "d" * 64},
    ], "dissolves": {}, "total_duration": 20.0}


def _sfx(cid, at, shot, sha, src, hit=0.0, **kw):
    return {"id": cid, "path": "s.wav", "at": at, "dur": 1.0,
            "event": {"shot": shot, "clip_sha256": sha, "src_s": src, "hit_s": hit}, **kw}


def _plan(tmp_path, sfx, vo_at=2.0):
    return am.normalize_plan({"vo": {"lines": [{"id": "L1", "path": "a.wav", "at": vo_at}]}, "sfx": sfx},
                             tmp_path)


def _resolve(tmp_path, sfx, manifest=None):
    cur = am.timeline_from_manifest(manifest or _manifest())
    return am.resolve_sfx_events(_plan(tmp_path, sfx), cur)


def test_timeline_carries_sha_and_ops():
    tl = am.timeline_from_manifest(_manifest())
    b = tl["clips"][1]
    assert b["sha256"] == SHA_B and b["ops"] == {"segments": [[0.0, 4.0]], "speed": 0.5}
    assert "ops" not in tl["clips"][0]
    assert am.validate_timeline(tl, "t")["clips"][2]["ops"]["segments"] == [[0.0, 1.0], [3.0, 4.0]]


def test_event_follows_speed(tmp_path):
    # source 1.5 s in B (slowed 0.5x, B starts at 4.0) plays at 4.0 + 3.0 = 7.0 s
    out = _resolve(tmp_path, [_sfx("P", 5.5, "B", SHA_B, 1.5, hit=0.25)])
    (c,) = out["sfx"]
    assert c["at"] == pytest.approx(7.0 - 0.25)
    (ev,) = out["timeline"]["events"]
    assert ev["event_t"] == pytest.approx(7.0) and ev["event_frame"] == 168 and ev["from"] == 5.5


def test_event_follows_segments(tmp_path):
    # C starts at 12.0; src 3.5 is 0.5 s into the 2nd segment after 1.0 s kept -> (1.0 + 0.5) / 0.5 = 3.0
    out = _resolve(tmp_path, [_sfx("P", 0.0, "C", SHA_C, 3.5)])
    assert out["sfx"][0]["at"] == pytest.approx(15.0)


def test_event_in_cut_window_is_removed(tmp_path):
    out = _resolve(tmp_path, [_sfx("P", 13.0, "C", SHA_C, 2.0), _sfx("Q", 1.0, "A", SHA_A, 1.0)])
    assert [c["id"] for c in out["sfx"]] == ["Q"]
    (r,) = out["timeline"]["removed_sfx"]
    assert r["id"] == "P" and "cut away" in r["reason"]


def test_event_in_dropped_shot_is_removed(tmp_path):
    m = _manifest()
    m["clips"] = [c for c in m["clips"] if c["id"] != "B"]
    out = _resolve(tmp_path, [_sfx("P", 5.0, "B", SHA_B, 1.0)], m)
    assert out["sfx"] == [] and "left the edit" in out["timeline"]["removed_sfx"][0]["reason"]


def test_non_ops_shot_uses_trim(tmp_path):
    # D starts at 16.0 with trim_in 1.5: src 2.0 -> 16.5; src 1.0 is trimmed away
    out = _resolve(tmp_path, [_sfx("P", 0.0, "D", "d" * 64, 2.0, hit=0.1),
                              _sfx("Q", 0.0, "D", "d" * 64, 1.0)])
    assert [(c["id"], c["at"]) for c in out["sfx"]] == [("P", pytest.approx(16.4))]


def test_sha_mismatch_fails_closed(tmp_path):
    with pytest.raises(AudioInputError, match="re-pick the event frame"):
        _resolve(tmp_path, [_sfx("P", 5.0, "B", SHA_A, 1.0)])
    m = _manifest()
    del m["clips"][1]["source_sha256"]
    with pytest.raises(AudioInputError, match="unknown clip"):
        _resolve(tmp_path, [_sfx("P", 5.0, "B", SHA_B, 1.0)], m)


def test_event_validation(tmp_path):
    with pytest.raises(AudioInputError, match="64 hex"):
        _plan(tmp_path, [_sfx("P", 1.0, "B", "abc", 1.0)])
    with pytest.raises(AudioInputError, match="loop bed"):
        _plan(tmp_path, [_sfx("P", 1.0, "B", SHA_B, 1.0, loop=True)])
    with pytest.raises(AudioInputError, match="before 0"):
        _resolve(tmp_path, [_sfx("P", 1.0, "A", SHA_A, 0.1, hit=0.5)])


def test_vo_and_unanchored_sfx_untouched(tmp_path):
    plan = _plan(tmp_path, [{"id": "X", "path": "s.wav", "at": 3.3, "dur": 1.0},
                            _sfx("P", 5.0, "B", SHA_B, 1.0)], vo_at=2.25)
    out = am.resolve_sfx_events(plan, am.timeline_from_manifest(_manifest()))
    assert out["vo"] == plan["vo"]
    assert {c["id"]: c["at"] for c in out["sfx"]}["X"] == 3.3
    plain = _plan(tmp_path, [{"id": "X", "path": "s.wav", "at": 3.3, "dur": 1.0}])
    assert am.resolve_sfx_events(plain, am.timeline_from_manifest(_manifest())) is plain


def test_gap23_remap_does_not_move_anchored_sfx(tmp_path):
    ref = copy.deepcopy(_manifest())
    for c in ref["clips"]:
        c.pop("shot_ops", None)
    ref["clips"][1]["seg_s"] = ref["clips"][1]["trim_out"] = 4.0   # B was 4 s unslowed in the ref edit
    raw = {"vo": {"lines": [{"id": "L1", "path": "a.wav", "at": 1.0}]},
           "sfx": [_sfx("P", 5.5, "B", SHA_B, 1.5, hit=0.25)],
           "timeline_ref": am.timeline_from_manifest(ref)}
    plan = am.normalize_plan(raw, tmp_path)
    cur = am.timeline_from_manifest(_manifest())
    out = am.resolve_sfx_events(am.remap_plan_to_edit(plan, cur), cur)
    assert out["sfx"][0]["at"] == pytest.approx(6.75)          # event frame, not the time remap (5.5)
    assert not any(mv["id"] == "P" for mv in out["timeline"]["moves"])


def test_load_audio_plan_resolves_events(tmp_path, monkeypatch):
    from hfvg.activities import audio as audio_act
    ep = tmp_path / "ep59"
    (ep / "audio").mkdir(parents=True)
    (ep / "edit").mkdir()
    raw = {"vo": {"lines": [{"id": "L1", "path": "a.wav", "at": 1.0}]},
           "sfx": [_sfx("P", 5.5, "B", SHA_B, 1.5, hit=0.25)]}       # no timeline_ref: events alone
    (ep / "audio" / "audio_plan.json").write_text(json.dumps(raw))
    (ep / "edit" / "assembly_manifest.json").write_text(json.dumps(_manifest()))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "false")
    plan = asyncio.run(audio_act.load_audio_plan("ep59", None))
    assert plan["sfx"][0]["at"] == pytest.approx(6.75)
    assert plan["timeline"]["events"][0]["event_frame"] == 168
    (ep / "edit" / "assembly_manifest.json").unlink()
    with pytest.raises(Exception, match="no edit manifest"):
        asyncio.run(audio_act.load_audio_plan("ep59", None))


def test_gap23_does_not_remove_anchored_sfx_whose_old_time_left_the_edit(tmp_path):
    # the SFX file starts 1.2 s before its transient, so its old start sat in shot Z (dropped since);
    # GAP-23 alone would remove it as "moment dropped" although its event frame (in B) is on screen
    ref = {"clips": [{"id": "A", "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0},
                     {"id": "Z", "seg_s": 2.0, "trim_in": 0.0, "trim_out": 2.0},
                     {"id": "B", "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0}], "total_duration": 10.0}
    cur_m = _manifest()
    raw = {"vo": {"lines": [{"id": "L1", "path": "a.wav", "at": 1.0}]},
           "sfx": [_sfx("P", 5.3, "B", SHA_B, 0.25, hit=1.2)],
           "timeline_ref": am.timeline_from_manifest(ref)}
    plan = am.normalize_plan(raw, tmp_path)
    cur = am.timeline_from_manifest(cur_m)
    remapped = am.remap_plan_to_edit(plan, cur)
    assert [c["id"] for c in remapped["sfx"]] == ["P"] and remapped["timeline"]["removed_sfx"] == []
    out = am.resolve_sfx_events(remapped, cur)
    assert out["sfx"][0]["at"] == pytest.approx(4.0 + 0.5 - 1.2)
