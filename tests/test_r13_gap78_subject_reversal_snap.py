"""GAP-78 (ep05v2 r13 v11): subject-crop out-and-back reversal + fast pose snap.
v10 misses of GAP-75: V11 head flick (turn right then back to camera, ~45.4-46.5 s) and V25 snap
(profile -> rear in ~0.4 s, 106.6 s). Signals here are synthetic in the shape measured on v10."""
from __future__ import annotations

import numpy as np

from hfvg import motion_qc as mq

FPS = 24


def _r(*parts):
    return np.concatenate([np.full(int(n), float(v)) for v, n in parts])


def test_flick_out_and_back_flags_and_reports_direction():
    dx = _r((0, 10), (0.8, 12), (0, 3), (-0.8, 12), (0, 10))           # +9.6 px then -9.6 px (V11 v10)
    r = mq.out_and_back(dx, FPS)
    assert len(r) == 1 and r[0]["first_dir"] == "right" and r[0]["back_s"] - r[0]["start_s"] < 1.5


def test_single_turn_that_holds_is_not_out_and_back():
    assert mq.out_and_back(_r((0, 10), (1.0, 12), (0, 30)), FPS) == []                 # turns, holds
    assert mq.out_and_back(_r((1.0, 12), (0, 2), (-0.3, 12)), FPS) == []               # partial return
    assert mq.out_and_back(_r((1.2, 72)), FPS) == []                                     # pan
    assert mq.out_and_back(_r((0.25, 12), (-0.25, 12)), FPS) == []                       # 3 px wobble
    assert len(mq.out_and_back(_r((0.25, 12), (-0.25, 12)), FPS, min_disp=2.0)) == 1


def test_out_and_back_window_and_cancel_configurable():
    dx = _r((1.0, 10), (0, 40), (-1.0, 10))                                             # back after ~1.7 s
    assert mq.out_and_back(dx, FPS, window_s=1.5) == []
    assert len(mq.out_and_back(dx, FPS, window_s=2.0)) == 1
    part = _r((1.0, 12), (0, 2), (-0.4, 12))                                            # 40 % return
    assert mq.out_and_back(part, FPS) == [] and len(mq.out_and_back(part, FPS, cancel=0.5, min_disp=4)) == 0
    assert len(mq.out_and_back(part, FPS, cancel=0.7)) == 1


def test_pose_snap_flags_fast_change_in_a_still_shot():
    c = _r((0.05, 40), (0.2, 2), (0.45, 4), (0.2, 2), (0.06, 40))
    r = mq.pose_snaps(c, FPS)
    assert len(r) == 1 and 40 <= r[0]["start_f"] <= 42 and r[0]["change"] == 0.45


def test_slow_turn_or_busy_shot_is_not_a_snap():
    assert mq.pose_snaps(np.linspace(0, 0.25, 96), FPS) == []                         # gradual, < 0.3
    walk = 0.3 + 0.1 * np.sin(np.arange(120) / 3.0)                                   # waddle: high base
    assert mq.pose_snaps(walk, FPS) == []
    assert mq.pose_snaps(np.zeros(3), FPS) == []


def test_appearance_change_sees_a_pose_flip_not_a_hold():
    rng = np.random.default_rng(5)
    a, b = rng.uniform(0, 255, (128, 128)), rng.uniform(0, 255, (128, 128))
    frames = [a] * 20 + [b] * 20
    c = mq.appearance_change(frames, lag=6)
    assert c[:20].max() < 1e-6 and c[20:26].min() > 0.5 and c[30:].max() < 1e-6
    assert mq.pose_snaps(c, FPS)[0]["start_f"] == 20


def test_subject_report_combines_and_skips_gait():
    dx = _r((0, 10), (0.8, 12), (0, 3), (-0.8, 12), (0, 30))
    c = _r((0.05, 30), (0.5, 4), (0.05, 33))
    still = mq.subject_qc_report("V11", dx, c, FPS, {"clip_prompt": "duck stands on the deck"})
    assert sorted(w["kind"] for w in still["warnings"]) == ["out_and_back", "pose_snap"]
    walk = mq.subject_qc_report("V20", dx, c, FPS, {"clip_prompt": "the duck walks along the deck"})
    assert walk["gait"] and walk["warnings"] == []


def test_subject_signals_numpy_fallback(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError()) if n == "cv2" else real(n, *a, **k))
    base = np.random.default_rng(0).uniform(0, 255, (64, 96))
    dx, ch = mq.subject_signals([np.roll(base, 3 * i, axis=1) for i in range(6)])
    assert np.allclose(dx[1:], 3.0) and len(ch) == 6
