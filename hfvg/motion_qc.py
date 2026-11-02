"""GAP-75..77 (ep05v2 r13, Reviewer v10 review Oct 31): motion-consistency detectors.

GAP-75 head/body yaw reversal: horizontal optical flow of the subject crop flips direction and flips
       back (turn away then back; look, look-away, look-back) within ``window_s`` -> QC warning.
GAP-76 slow-mo on an object-contact action: a shot tagged ``object_contact`` (explicit tag in shots.json,
       else inferred from its motion text: place/hand/put/tray/plate/pick ...) retimed below 0.85x is
       warned (or refused when the edit plan asks ``"slow_action": "refuse"``); suggest a 1.0x re-take
       or a trim instead. The GAP-63 0.7x warning is untouched and still fires.
GAP-77 gesture repeat: a motion-signature segment that repeats inside one shot (normalised
       autocorrelation peak at a lag >= ``min_lag_s``) -> QC warning.

Pure functions work on per-frame signals; ``flow_signals`` computes them from a clip (OpenCV Farneback
when available, numpy phase correlation otherwise), so tests need neither OpenCV nor real clips.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

import numpy as np

SLOW_ACTION_SPEED = 0.85
GAIT_RE = re.compile(r"\b(walk\w*|waddl\w*|paddl\w*|swim\w*|trot\w*|step\w*)\b")
OBJECT_CONTACT_WORDS = ("place", "places", "placing", "placed", "hand", "hands", "put", "puts", "putting",
                        "tray", "plate", "pick", "picks", "picking", "picked", "set down", "sets", "rolls",
                        "roll", "nudge", "nudges", "pour", "pours", "carry", "carrying", "carries", "hold",
                        "holds", "holding", "grab", "grabs", "take", "takes", "mouth", "bite", "bites",
                        "paw")
# a background/reference "plate" (lobby plate, cropped FAC05 plate) is not a dish
_PLATE_REF_RE = re.compile(r"\b(?:lobby|pool|lounge|aerial|background|deck|clean|\w*\d\w*)\s+plate\b")


def shot_motion_text(shot: dict[str, Any] | None) -> str:
    s = shot or {}
    return " ".join(str(s.get(k) or "") for k in ("motion", "action", "clip_prompt")).lower()


def infer_object_contact(text: str) -> list[str]:
    """Keywords in the motion text that imply a hand/wing/mouth-object action (empty = none)."""
    t = (text or "").lower().split("setting:")[0]          # the action, not the set description
    t = _PLATE_REF_RE.sub(" ", t)
    t = " " + re.sub(r"[^a-z ]+", " ", t) + " "
    return sorted({w for w in OBJECT_CONTACT_WORDS if f" {w} " in t})


def object_contact(shot: dict[str, Any] | None) -> tuple[bool, str]:
    """(tagged, source). An explicit ``object_contact`` bool in the shot wins over inference."""
    s = shot or {}
    if isinstance(s.get("object_contact"), bool):
        return s["object_contact"], "tag"
    words = infer_object_contact(shot_motion_text(s))
    return (bool(words), "inferred:" + ",".join(words)) if words else (False, "inferred:none")


def validate_object_contact_tag(shot: dict[str, Any]) -> str | None:
    """Shots-schema check for the optional tag (None = fine)."""
    if "object_contact" in shot and not isinstance(shot["object_contact"], bool):
        return f"shot {shot.get('id')}: object_contact must be true/false"
    return None


def slow_action_warnings(sid: str, speed: float, shot: dict[str, Any] | None,
                         floor: float = SLOW_ACTION_SPEED) -> list[dict[str, Any]]:
    tagged, src = object_contact(shot)
    if not tagged or speed >= floor - 1e-9:
        return []
    return [{"id": sid, "kind": "slow_action", "speed": round(speed, 4), "source": src,
             "msg": f"{sid}: object-contact shot at {speed:.3f}x (< {floor}x) reads as uncanny slow-mo; "
                    f"re-take at 1.0x or trim to a segment that plays at >= {floor}x"}]


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(x) < k:
        return np.asarray(x, float)
    return np.convolve(np.asarray(x, float), np.ones(k) / k, mode="same")


def _runs(sign: np.ndarray, min_len: int) -> list[tuple[int, int, int]]:
    runs: list[list[int]] = []
    for i, v in enumerate(sign):
        if v == 0:
            continue
        if runs and runs[-1][0] == v and runs[-1][2] >= i - 2:   # tolerate a 1-frame dropout
            runs[-1][2] = i
        else:
            runs.append([int(v), i, i])
    return [tuple(r) for r in runs if r[2] - r[1] + 1 >= min_len]   # type: ignore[misc]


def yaw_reversals(dx: Sequence[float], fps: float, window_s: float = 1.5, min_run_s: float = 0.125,
                  rel_thresh: float = 0.35, abs_thresh: float = 0.15, smooth: int = 5) -> list[dict[str, Any]]:
    """GAP-75: A -> B -> A direction flips of subject horizontal flow within window_s.

    dx = per-frame mean horizontal flow of the subject crop (px/frame, camera motion removed)."""
    x = _smooth(np.asarray(dx, float), smooth)
    if len(x) < 3:
        return []
    thr = max(abs_thresh, rel_thresh * float(np.percentile(np.abs(x), 90)))
    sign = np.where(x > thr, 1, np.where(x < -thr, -1, 0))
    runs = _runs(sign, max(1, int(round(min_run_s * fps))))
    out, win = [], window_s * fps
    for r0, r1, r2 in zip(runs, runs[1:], runs[2:]):
        if r0[0] == r2[0] != r1[0] and r2[1] - r0[2] <= win:
            out.append({"kind": "yaw_reversal", "start_f": r0[1], "turn_f": r1[1], "back_f": r2[1],
                        "start_s": round(r0[1] / fps, 3), "back_s": round(r2[1] / fps, 3),
                        "first_dir": "right" if r0[0] > 0 else "left"})
    return out


def gesture_repeats(signature: Sequence[Sequence[float]] | Sequence[float], fps: float, min_lag_s: float = 0.5,
                    max_lag_s: float = 3.0, min_corr: float = 0.8, min_energy: float = 0.05) -> list[dict[str, Any]]:
    """GAP-77: a motion segment that repeats inside a shot.

    signature = per-frame motion features (e.g. [dx, dy] of the subject crop, or a 1-D magnitude). The
    mean-removed signal's normalised autocorrelation is searched for a peak at lag in [min_lag, max_lag]
    that is a local maximum and >= min_corr. A still subject (energy below min_energy) never flags."""
    s = np.asarray(signature, float)
    if s.ndim == 1:
        s = s[:, None]
    s = s - s.mean(axis=0)
    n = len(s)
    if n < 4 or float(np.sqrt((s ** 2).sum(axis=1).mean())) < min_energy:
        return []
    lo, hi = max(1, int(round(min_lag_s * fps))), min(n - 2, int(round(max_lag_s * fps)))
    if hi <= lo:
        return []
    def ac(lag: int) -> float:
        a, b = s[:-lag], s[lag:]
        den = float(np.sqrt((a ** 2).sum() * (b ** 2).sum()))
        return float((a * b).sum() / den) if den > 1e-12 else 0.0
    vals = {lag: ac(lag) for lag in range(lo - 1, hi + 2) if 0 < lag < n - 1}
    best = None
    for lag in range(lo, hi + 1):
        v = vals.get(lag, 0.0)
        if v >= min_corr and v >= vals.get(lag - 1, -9) and v >= vals.get(lag + 1, -9) and n - lag >= lo:
            if best is None or v > best[1]:
                best = (lag, v)
    if best is None:
        return []
    return [{"kind": "gesture_repeat", "lag_f": best[0], "lag_s": round(best[0] / fps, 3),
             "corr": round(best[1], 3)}]


def flow_signals(frames: Iterable[np.ndarray], crop: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """Per-frame [subject dx, subject dy, subject magnitude] from grayscale frames (first row zeros).
    Subject = top-decile residual flow after removing the median (camera) motion. crop = (x, y, w, h)."""
    try:
        import cv2  # type: ignore
    except Exception:  # pragma: no cover - exercised where OpenCV is absent
        cv2 = None
    rows, prev = [], None
    for f in frames:
        g = np.asarray(f, np.float32)
        if g.ndim == 3:
            g = g.mean(axis=2)
        if crop:
            x, y, w, h = crop
            g = g[y:y + h, x:x + w]
        if prev is None:
            rows.append((0.0, 0.0, 0.0)); prev = g; continue
        if cv2 is not None:
            fl = cv2.calcOpticalFlowFarneback(prev.astype(np.uint8), g.astype(np.uint8), None,
                                              0.5, 3, 15, 3, 5, 1.2, 0)
            dx, dy = fl[..., 0] - np.median(fl[..., 0]), fl[..., 1] - np.median(fl[..., 1])
            m = np.hypot(dx, dy); sel = m >= np.percentile(m, 90)
            rows.append((float(dx[sel].mean()), float(dy[sel].mean()), float(m[sel].mean())))
        else:
            F = np.fft.fft2(prev) * np.conj(np.fft.fft2(g))
            r = np.abs(np.fft.ifft2(F / (np.abs(F) + 1e-9)))
            py, px = np.unravel_index(int(np.argmax(r)), r.shape)
            px = px - r.shape[1] if px > r.shape[1] // 2 else px
            py = py - r.shape[0] if py > r.shape[0] // 2 else py
            rows.append((float(-px), float(-py), float(np.hypot(px, py))))
        prev = g
    return np.asarray(rows, float).reshape(-1, 3)


def motion_qc_report(sid: str, signals: np.ndarray, fps: float, speed: float = 1.0,
                     shot: dict[str, Any] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """QC warnings for one shot (signals from flow_signals). cfg keys: window_s, min_corr, min_lag_s."""
    cfg = cfg or {}
    sig = np.asarray(signals, float).reshape(-1, 3)
    warns: list[dict[str, Any]] = []
    yaw = yaw_reversals(sig[:, 0], fps, window_s=float(cfg.get("window_s", 1.5)))
    reps = gesture_repeats(sig[:, :2], fps, min_lag_s=float(cfg.get("min_lag_s", 0.5)),
                           min_corr=float(cfg.get("min_corr", 0.8)))
    gait = bool(GAIT_RE.search(shot_motion_text(shot).split("setting:")[0]))
    if gait and (len(yaw) >= 3 or reps):
        # a walk/paddle cycle is periodic by design: regular flips + a repeat = gait, not a double take
        return {"id": sid, "frames": int(len(sig)), "gait": True,
                "warnings": slow_action_warnings(sid, speed, shot)}
    for r in yaw:
        warns.append({"id": sid, **r, "msg": f"{sid}: subject turns {r['first_dir']} and back "
                                             f"{r['start_s']}-{r['back_s']} s (double take)"})
    for r in reps:
        warns.append({"id": sid, **r, "msg": f"{sid}: motion repeats every {r['lag_s']} s (corr {r['corr']})"})
    warns.extend(slow_action_warnings(sid, speed, shot))
    return {"id": sid, "frames": int(len(sig)), "warnings": warns}


def cut_shot_spans(assembly_manifest: dict[str, Any]) -> list[tuple[str, int, int, dict[str, Any]]]:
    """(shot id, first cut frame, end frame, clip entry) from an assembly manifest; a dissolve into a shot
    starts that shot ``dissolve_s * fps`` frames early (the overlap)."""
    fps = int(((assembly_manifest.get("edit_plan") or {}).get("fps")) or assembly_manifest.get("fps") or 24)
    diss = assembly_manifest.get("dissolve_pairs") or {}
    into = {k.split("->")[1]: float(v) for k, v in diss.items() if "->" in k}
    out, f = [], 0
    for c in assembly_manifest.get("clips") or []:
        a = f - int(round(into.get(c["id"], 0.0) * fps))
        out.append((c["id"], a, a + int(c["frames"]), c))
        f = a + int(c["frames"])
    return out


def motion_qc_cut(assembly_manifest: dict[str, Any], signals: np.ndarray, fps: float,
                  shots: dict[str, dict[str, Any]] | None = None,
                  cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-shot motion QC over a whole cut. signals = flow_signals of the cut (one row per cut frame).
    The first 2 frames of each shot are skipped (cut transient). Returns {"shots": [...], "warnings": [...]}."""
    sig = np.asarray(signals, float).reshape(-1, 3)
    reps, warns = [], []
    for sid, a, b, c in cut_shot_spans(assembly_manifest):
        a2, b2 = max(0, a + 2), min(len(sig), b)
        if b2 - a2 < 4:
            continue
        speed = float((c.get("shot_ops") or {}).get("speed", 1.0))
        r = motion_qc_report(sid, sig[a2:b2], fps, speed, (shots or {}).get(sid), cfg)
        for w in r["warnings"]:
            for k in ("start_f", "turn_f", "back_f"):
                if k in w:
                    w["cut_" + k] = a2 + w[k]
            if "start_f" in w:
                w["cut_s"] = round((a2 + w["start_f"]) / fps, 3)
        reps.append(r)
        warns.extend(r["warnings"])
    return {"shots": reps, "warnings": warns}


# ---- GAP-78 (r13 v11): duck-crop out-and-back reversal + fast pose snap --------------------------------
# Learned on v10: a head turn seen as optical flow is ONE run (+ while turning away); turning back is the
# OPPOSITE run. So a double take on a subject crop = two adjacent opposite runs whose displacements
# cancel (the head ends where it started). GAP-75's A-B-A rule fits whole-body sway, and missed V11's
# head flick. A pose snap (V25 profile -> rear in ~0.4 s, V11's splice jump) is a large appearance change
# over ~0.25 s in a shot whose subject otherwise holds still.

def _run_sums(x: np.ndarray, thr: float, min_len: int) -> list[tuple[int, int, int, float]]:
    sign = np.where(x > thr, 1, np.where(x < -thr, -1, 0))
    return [(s, a, b, float(x[a:b + 1].sum())) for s, a, b in _runs(sign, min_len)]


def out_and_back(dx: Sequence[float], fps: float, window_s: float = 1.5, min_disp: float = 4.0,
                 cancel: float = 0.5, abs_thresh: float = 0.15, min_run_s: float = 0.125,
                 smooth: int = 3) -> list[dict[str, Any]]:
    """GAP-78: opposite adjacent flow runs on the subject crop that cancel (turn away, turn back).

    dx = per-frame MEAN horizontal flow of a tight subject crop (px/frame). A pair flags when both runs
    move >= min_disp px, the second starts within window_s of the first's end... and the net displacement
    |s1 + s2| <= cancel * max(|s1|, |s2|)."""
    x = _smooth(np.asarray(dx, float), smooth)
    runs = _run_sums(x, abs_thresh, max(1, int(round(min_run_s * fps))))
    out = []
    for (s1, a1, b1, d1), (s2, a2, b2, d2) in zip(runs, runs[1:]):
        if min(abs(d1), abs(d2)) < min_disp or (a2 - b1) > window_s * fps:
            continue
        if abs(d1 + d2) <= cancel * max(abs(d1), abs(d2)):
            out.append({"kind": "out_and_back", "start_f": a1, "turn_f": b1, "back_f": b2,
                        "start_s": round(a1 / fps, 3), "back_s": round(b2 / fps, 3),
                        "first_dir": "right" if s1 > 0 else "left",
                        "disp": [round(d1, 2), round(d2, 2)]})
    return out


def _small(g: np.ndarray, n: int = 64) -> np.ndarray:
    g = np.asarray(g, float)
    if g.ndim == 3:
        g = g.mean(axis=2)
    h, w = g.shape
    ys = (np.arange(n + 1) * h // n); xs = (np.arange(n + 1) * w // n)
    if h < n or w < n:
        return g
    return np.array([[g[ys[i]:ys[i + 1], xs[j]:xs[j + 1]].mean() for j in range(n)] for i in range(n)])


def appearance_change(frames: Iterable[np.ndarray], lag: int = 6,
                      crop: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """1 - normalised correlation between frame t and t-lag of the (cropped, 64x64) subject. 0 = same."""
    G = []
    for f in frames:
        g = np.asarray(f, float)
        if crop:
            x, y, w, h = crop
            g = g[y:y + h, x:x + w]
        G.append(_small(g).ravel())
    out = []
    for i in range(len(G)):
        a = G[max(0, i - lag)] - G[max(0, i - lag)].mean(); b = G[i] - G[i].mean()
        den = float(np.sqrt((a @ a) * (b @ b)))
        out.append(0.0 if den < 1e-9 else float(1 - (a @ b) / den))
    return np.asarray(out)


def pose_snaps(change: Sequence[float], fps: float, min_change: float = 0.3,
               max_base: float = 0.18) -> list[dict[str, Any]]:
    """GAP-78: fast pose snap = appearance change over ~0.25 s >= min_change while the shot's median
    change stays <= max_base (the subject otherwise holds; a walk/waddle has a high median). One flag
    per contiguous span above half the peak."""
    c = np.asarray(change, float)
    if len(c) < 4:
        return []
    base = float(np.median(c))
    if base > max_base:
        return []
    out, i = [], 0
    while i < len(c):
        if c[i] >= min_change:
            j = i
            while j + 1 < len(c) and c[j + 1] >= 0.5 * min_change:
                j += 1
            pk = i + int(np.argmax(c[i:j + 1]))
            a = pk
            while a > 0 and c[a - 1] >= 0.5 * c[pk]:
                a -= 1
            out.append({"kind": "pose_snap", "start_f": a, "peak_f": pk, "start_s": round(a / fps, 3),
                        "peak_s": round(pk / fps, 3), "change": round(float(c[pk]), 3),
                        "base": round(base, 3)})
            i = j + 1
        else:
            i += 1
    return out


def subject_qc_report(sid: str, dx: Sequence[float], change: Sequence[float], fps: float,
                      shot: dict[str, Any] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """GAP-78 per-shot report from a SUBJECT crop (mean flow dx + appearance change). Gait shots
    (walk/paddle in the motion text) skip both checks (periodic by design)."""
    cfg = cfg or {}
    gait = bool(GAIT_RE.search(shot_motion_text(shot).split("setting:")[0]))
    warns: list[dict[str, Any]] = []
    if not gait:
        for r in out_and_back(dx, fps, window_s=float(cfg.get("window_s", 1.5)),
                              min_disp=float(cfg.get("min_disp", 4.0)), cancel=float(cfg.get("cancel", 0.5))):
            warns.append({"id": sid, **r, "msg": f"{sid}: subject turns {r['first_dir']} and back "
                                                 f"{r['start_s']}-{r['back_s']} s (double take)"})
        for r in pose_snaps(change, fps, min_change=float(cfg.get("min_change", 0.3)),
                            max_base=float(cfg.get("max_base", 0.18))):
            warns.append({"id": sid, **r, "msg": f"{sid}: pose snaps at {r['start_s']}-{r['peak_s']} s "
                                                 f"(change {r['change']} vs base {r['base']})"})
    return {"id": sid, "gait": gait, "warnings": warns}


def subject_signals(frames: Sequence[np.ndarray], crop: tuple[int, int, int, int] | None = None,
                    lag: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """(mean horizontal flow dx, appearance change) of a subject crop, for subject_qc_report."""
    G = []
    for f in frames:
        g = np.asarray(f, np.float32)
        if g.ndim == 3:
            g = g.mean(axis=2)
        if crop:
            x, y, w, h = crop
            g = g[y:y + h, x:x + w]
        G.append(g)
    try:
        import cv2  # type: ignore
        dx = [0.0] + [float(cv2.calcOpticalFlowFarneback(G[i - 1].astype(np.uint8), G[i].astype(np.uint8), None,
                                                         0.5, 3, 15, 3, 5, 1.2, 0)[..., 0].mean())
                      for i in range(1, len(G))]
    except Exception:
        dx = list(flow_signals(G)[:, 0])
    return np.asarray(dx, float), appearance_change(G, lag)
