"""GAP-75..77 (ep05v2 r13, Reviewer v10 review): head-yaw reversal, slow-mo on object contact, gesture repeat.
v10 cases: V15 duck turns to the fire, to the window, back (0.678x); V07 wing rolls a fruit onto the tray at
0.585x; V11 sets the tray down at 0.525x; V22 pup takes the fruit in its mouth at 0.505x."""
from __future__ import annotations

import numpy as np
import pytest

from hfvg import motion_qc as mq
from hfvg.edit_plan import SHOT_OPS_WARN_SPEED, speed_warnings

FPS = 24


def _ramp(*parts):
    return np.concatenate([np.full(int(n), float(v)) for v, n in parts])


# ---- GAP-75 yaw reversal -------------------------------------------------------------------------
def test_turn_away_and_back_within_window_flags():
    dx = _ramp((0, 6), (2.0, 8), (0, 2), (-2.0, 8), (0, 2), (2.0, 8), (0, 6))
    r = mq.yaw_reversals(dx, FPS)
    assert len(r) == 1 and r[0]["first_dir"] == "right" and r[0]["back_s"] - r[0]["start_s"] <= 1.5


def test_single_turn_or_steady_walk_does_not_flag():
    assert mq.yaw_reversals(_ramp((0, 10), (2.0, 20), (0, 20)), FPS) == []          # one turn, holds
    assert mq.yaw_reversals(_ramp((1.5, 72)), FPS) == []                             # steady pan/walk
    assert mq.yaw_reversals(np.zeros(48), FPS) == []                                 # still
    rng = np.random.default_rng(1)
    assert mq.yaw_reversals(rng.normal(0, 0.03, 96), FPS) == []                      # sub-threshold noise


def test_reversal_slower_than_window_is_configurable():
    dx = _ramp((2.0, 8), (0, 2), (-2.0, 40), (0, 2), (2.0, 8))                       # back after ~1.8 s
    assert mq.yaw_reversals(dx, FPS, window_s=1.5) == []
    assert len(mq.yaw_reversals(dx, FPS, window_s=2.5)) == 1


def test_one_frame_blip_is_not_a_turn():
    dx = _ramp((2.0, 12), (-2.0, 1), (2.0, 12))
    assert mq.yaw_reversals(dx, FPS, smooth=1) == []


# ---- GAP-76 slow-mo on object contact -------------------------------------------------------------
@pytest.mark.parametrize("text", ["The duck sets the small wooden tray down", "rolls one whole fruit onto the tray",
                                  "Character2 takes the fresh fruit gently in his mouth", "puts the cup on a plate",
                                  "she picks up a spoon", "hand reaches out"])
def test_inference_from_motion_text(text):
    assert mq.object_contact({"clip_prompt": text})[0]


def test_no_contact_text_and_explicit_tag_wins():
    assert mq.object_contact({"clip_prompt": "wide shot of the peaks, snow blows sideways"}) == (False, "inferred:none")
    assert mq.object_contact({"clip_prompt": "family walks up. Setting: the lobby (as lobby plate LB)"})[0] is False
    assert mq.object_contact({"clip_prompt": "duck stands at the window (match the cropped FAC06 lounge plate)"})[0] is False
    assert mq.object_contact({"clip_prompt": "walks along the deck, wings empty"})[0] is False
    assert mq.object_contact({"clip_prompt": "puts the tray down", "object_contact": False}) == (False, "tag")
    assert mq.object_contact({"clip_prompt": "a lake", "object_contact": True}) == (True, "tag")
    assert mq.validate_object_contact_tag({"id": "V1", "object_contact": "yes"})
    assert mq.validate_object_contact_tag({"id": "V1", "object_contact": True}) is None


def test_slow_action_below_085_warns_and_07_warning_stays():
    ops = {"segments": [[0.0, 4.0]], "interp": "rife"}
    shot = {"clip_prompt": "the duck's nub wing rolls one whole fruit onto the tray"}
    w = speed_warnings("V07", ops, 0.585, shot)
    kinds = [x["kind"] for x in w]
    assert "slow" in kinds and "slow_action" in kinds                    # GAP-63 still fires
    assert "re-take at 1.0x" in [x for x in w if x["kind"] == "slow_action"][0]["msg"]
    w2 = speed_warnings("V07", ops, 0.8, shot)
    assert [x["kind"] for x in w2] == ["slow_action"]                    # 0.7 <= 0.8 < 0.85
    assert speed_warnings("V07", ops, 0.85, shot) == []
    assert speed_warnings("V13", ops, 0.8, {"clip_prompt": "snow squall over the lake"}) == []


def test_speed_warnings_without_shot_unchanged():
    ops = {"segments": [[0.0, 4.0]], "interp": "rife"}
    assert [x["kind"] for x in speed_warnings("V07", ops, 0.6)] == ["slow"]
    assert speed_warnings("V07", ops, 0.8) == []
    assert SHOT_OPS_WARN_SPEED == 0.7


# ---- GAP-77 gesture repeat ------------------------------------------------------------------------
def test_repeated_gesture_flags_with_lag():
    t = np.arange(96)
    nod = np.where((t % 24) < 10, np.sin(np.pi * (t % 24) / 10), 0.0)               # same nod every 1 s
    r = mq.gesture_repeats(np.c_[nod, 0.5 * nod], FPS)
    assert len(r) == 1 and abs(r[0]["lag_s"] - 1.0) < 0.1 and r[0]["corr"] >= 0.8


def test_single_gesture_still_or_noise_does_not_flag():
    t = np.arange(96)
    one = np.where((t > 20) & (t < 40), np.sin(np.pi * (t - 20) / 20), 0.0)
    assert mq.gesture_repeats(one, FPS) == []
    assert mq.gesture_repeats(np.zeros(96), FPS) == []
    assert mq.gesture_repeats(np.random.default_rng(3).normal(0, 1, (96, 2)), FPS) == []


# ---- reports / cut wiring ------------------------------------------------------------------------
def test_cut_spans_apply_dissolve_overlap_and_report_cut_frames():
    man = {"fps": 24, "dissolve_pairs": {"B->C": 1.0},
           "clips": [{"id": "A", "frames": 48}, {"id": "B", "frames": 48, "shot_ops": {"speed": 0.6}},
                     {"id": "C", "frames": 48}]}
    assert [(s, a, b) for s, a, b, _ in mq.cut_shot_spans(man)] == [("A", 0, 48), ("B", 48, 96), ("C", 72, 120)]
    sig = np.zeros((120, 3))
    sig[52:60, 0], sig[62:70, 0], sig[72:80, 0] = 2.0, -2.0, 2.0
    rep = mq.motion_qc_cut(man, sig, 24, {"B": {"clip_prompt": "puts the tray down"}})
    kinds = sorted(w["kind"] for w in rep["warnings"] if w["id"] == "B")
    assert kinds == ["slow_action", "yaw_reversal"]
    yr = [w for w in rep["warnings"] if w["kind"] == "yaw_reversal"][0]
    assert 50 <= yr["cut_start_f"] <= 60 and yr["cut_s"] == round(yr["cut_start_f"] / 24, 3)


def test_flow_signals_numpy_path_sees_horizontal_shift(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError()) if n == "cv2" else real(n, *a, **k))
    rng = np.random.default_rng(0)
    base = rng.uniform(0, 255, (40, 60))
    frames = [np.roll(base, 2 * i, axis=1) for i in range(5)]
    sig = mq.flow_signals(frames)
    assert sig.shape == (5, 3) and np.allclose(sig[1:, 0], 2.0)


def test_walk_cycle_is_gait_not_double_take():
    t = np.arange(120)
    sway = 2.0 * np.sign(np.sin(2 * np.pi * t / 24))                             # waddle, 1 s period
    sig = np.c_[sway, 0.3 * sway, np.abs(sway)]
    walk = mq.motion_qc_report("V20", sig, FPS, 1.0, {"clip_prompt": "the duck walks steadily along the deck"})
    assert walk["gait"] and walk["warnings"] == []
    still = mq.motion_qc_report("V15", sig, FPS, 1.0, {"clip_prompt": "duck stands at the window"})
    assert {w["kind"] for w in still["warnings"]} >= {"yaw_reversal", "gesture_repeat"}


def test_turn_then_hold_with_a_pause_is_not_a_reversal():
    dx = _ramp((2.0, 8), (0, 3), (-2.0, 8), (0, 6), (-2.0, 8))                    # right, left, pause, still left
    assert mq.yaw_reversals(dx, FPS, smooth=1) == []
