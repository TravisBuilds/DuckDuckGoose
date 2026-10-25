"""Audio mix + delivery engine (G-AUD-1b).

Ports the proven ffmpeg chain technique: a VO bus built from per-line
``aformat -> volume -> alimiter -> adelay`` inputs summed by ``amix normalize=0``,
VO-activity ducking of the music bed and SFX beds, pickup RMS matching, a master
true-peak limiter, EBU R128 two-pass ``loudnorm`` with the measured JSON parsed from
ffmpeg's stderr, ASS/SRT subtitles burned with libx264, and a size-capped two-pass
phone encode made from the picture + the PCM mix (never AAC-from-AAC).

Everything here is local: no provider calls, no spend. The Temporal activities in
``hfvg.activities.audio`` and the ``hfvg mix`` CLI both call into this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np

SR = 48000
DELIVERY_VERSION = 1
MIN_VO_GAP_RANGE = (0.3, 0.4)
DEFAULT_MIN_VO_GAP_S = 0.4
TAIL_GUARD_S = 0.3            # last VO line must end this far before the picture ends
ACTIVITY_FLOOR_DB = -45.0     # VO activity threshold (20 ms moving average of |x|)
PICKUP_MAX_GAIN_DB = 6.0
DURATION_TOLERANCE_S = 0.1    # delivered file vs locked picture
CUE_TOLERANCE_S = 0.05        # subtitle cue vs VO line placement
VO_BUS_DB = -2.5              # VO bus trim into the final mix
MIX_MAKEUP_DB = 4.7           # pre-limiter makeup (reference chain)
MASTER_LIMIT = 0.86           # master alimiter ceiling (linear)
LINE_LIMIT = 0.79             # per-line limiter when a line is boosted
AUDIO_PLAN_NAME = "audio_plan.json"

DEFAULT_LOUDNESS = {"target_lufs": -12.5, "tolerance_lu": 1.0, "tp_ceiling_dbtp": -1.0,
                    "tp_margin_db": 0.5, "lra": 11.0}
DEFAULT_PHONE = {"max_mb": 50.0, "video_kbps": None, "max_video_kbps": 3000, "min_video_kbps": 300,
                 "audio_kbps": 192, "preset": "medium"}
DEFAULT_MASTER = {"crf": 18, "preset": "fast", "audio_kbps": 192}
DEFAULT_SUBTITLES = {"burn": True, "font": "Arial", "fontsize": 64, "fonts_dir": None,
                     "max_line_chars": 24, "margin_v": 260}

Heartbeat = Callable[[dict], None]


class AudioInputError(Exception):
    """A required audio input (plan, VO line, library file, picture) is missing or invalid.

    Never retryable: retrying cannot make a missing file appear."""


class PlacementError(AudioInputError):
    """VO placement violates the minimum gap / protected windows / tail guard."""


class LoudnormParseError(RuntimeError):
    """ffmpeg loudnorm did not print a parseable, finite JSON measurement."""


# ---------------------------------------------------------------------------- paths


def episodes_root() -> Path:
    return Path(os.getenv("EPISODES_DIR") or "./data/episodes")


def episode_dir(episode_id: str) -> Path:
    return episodes_root() / episode_id


def delivery_dir(episode_id: str, root: Path | None = None) -> Path:
    return (root if root is not None else episode_dir(episode_id)) / "delivery"


def audio_plan_path(episode_id: str, root: Path | None = None) -> Path:
    return (root if root is not None else episode_dir(episode_id)) / "audio" / AUDIO_PLAN_NAME


def delivery_paths(episode_id: str, root: Path | None = None) -> dict[str, str]:
    """Deterministic output paths under ./data/episodes/{id}/delivery/."""
    d = delivery_dir(episode_id, root)
    return {
        "dir": str(d),
        "manifest": str(d / "delivery_manifest.json"),
        "mix_wav": str(d / "mix.wav"),
        "vo_stem": str(d / "stems" / "vo.wav"),
        "music_stem": str(d / "stems" / "music.wav"),
        "sfx_stem": str(d / "stems" / "sfx.wav"),
        "nosubs": str(d / f"{episode_id}_nosubs.mp4"),
        "master_subs": str(d / f"{episode_id}_subs.mp4"),
        "phone": str(d / f"{episode_id}_phone.mp4"),
        "ass": str(d / f"{episode_id}.ass"),
        "srt": str(d / f"{episode_id}.srt"),
    }


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------- subprocess


async def run_cmd(cmd: list[str], what: str, cwd: str | None = None,
                  stdin: bytes | None = None) -> tuple[bytes, str]:
    """Run a subprocess without blocking the event loop. Returns (stdout bytes, stderr text)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(stdin)
    err_s = err.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {proc.returncode}): {err_s[-800:]}")
    return out, err_s


async def probe(path: str | Path) -> dict:
    out, _ = await run_cmd(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                            str(path)], f"ffprobe {Path(path).name}")
    return json.loads(out.decode() or "{}")


async def probe_duration(path: str | Path) -> float:
    info = await probe(path)
    dur = info.get("format", {}).get("duration")
    if dur in (None, "N/A"):
        raise AudioInputError(f"cannot read duration of {path}")
    return float(dur)


# ---------------------------------------------------------------------------- plan


def _num(v: Any, name: str, lo: float | None = None, hi: float | None = None) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise AudioInputError(f"{name} must be a number, got {v!r}") from None
    if not math.isfinite(f) or (lo is not None and f < lo) or (hi is not None and f > hi):
        raise AudioInputError(f"{name}={f} out of range [{lo}, {hi}]")
    return f


def _resolve(p: str | None, base: Path) -> str | None:
    if not p:
        return None
    pp = Path(p)
    return str(pp if pp.is_absolute() else (base / pp))


def validate_min_gap(value: Any) -> float:
    """The VO minimum gap is a validated setting: 0.3-0.4 s."""
    lo, hi = MIN_VO_GAP_RANGE
    return _num(value, "min_vo_gap_s", lo, hi)


def normalize_plan(raw: dict, base_dir: str | Path) -> dict:
    """Validate an audio plan and fill defaults. Relative paths resolve against base_dir."""
    if not isinstance(raw, dict):
        raise AudioInputError("audio plan must be a JSON object")
    base = Path(base_dir)
    vo_raw = raw.get("vo") or {}
    lines = []
    seen: set[str] = set()
    for i, ln in enumerate(vo_raw.get("lines") or []):
        lid = str(ln.get("id") or f"L{i + 1:02d}")
        if lid in seen:
            raise AudioInputError(f"duplicate VO line id {lid}")
        seen.add(lid)
        path, text = _resolve(ln.get("path"), base), ln.get("text")
        if not path and not text:
            raise AudioInputError(f"VO line {lid} needs a locked file path or text to generate")
        lines.append({
            "id": lid, "at": _num(ln.get("at"), f"vo.lines[{lid}].at", 0.0),
            "path": path, "text": text,
            "subtitle": ln.get("subtitle", text),
            "gain_db": None if ln.get("gain_db") is None else _num(ln["gain_db"], f"{lid}.gain_db", -24, 12),
        })
    music = None
    if raw.get("music"):
        m = raw["music"]
        if not m.get("path") and not m.get("prompt"):
            raise AudioInputError("music needs a library path or a prompt")
        music = {
            "id": str(m.get("id") or "music"), "path": _resolve(m.get("path"), base), "prompt": m.get("prompt"),
            "model": m.get("model", "music_v1"),
            "offset_s": _num(m.get("offset_s", 0.0), "music.offset_s", 0.0),
            "src_start_s": _num(m.get("src_start_s", 0.0), "music.src_start_s", 0.0),
            "level_db": _num(m.get("level_db", -30.2), "music.level_db", -60, 0),
            "duck_db": _num(m.get("duck_db", -4.0), "music.duck_db", -30, 0),
            "fade_in_s": _num(m.get("fade_in_s", 2.0), "music.fade_in_s", 0.0),
            "fade_out_s": _num(m.get("fade_out_s", 1.0), "music.fade_out_s", 0.0),
            "loop": bool(m.get("loop", True)),
        }
    sfx = []
    for i, c in enumerate(raw.get("sfx") or []):
        cid = str(c.get("id") or f"S{i + 1:02d}")
        if not c.get("path") and not c.get("prompt"):
            raise AudioInputError(f"sfx {cid} needs a library path or a prompt")
        norm = c.get("norm", "rms")
        if norm not in ("rms", "peak"):
            raise AudioInputError(f"sfx {cid}.norm must be rms or peak")
        sfx.append({
            "id": cid, "path": _resolve(c.get("path"), base), "prompt": c.get("prompt"),
            "at": _num(c.get("at"), f"sfx[{cid}].at", 0.0),
            "dur": _num(c.get("dur"), f"sfx[{cid}].dur", 0.05),
            "src_start_s": _num(c.get("src_start_s", 0.0), f"sfx[{cid}].src_start_s", 0.0),
            "loop": bool(c.get("loop", False)), "norm": norm,
            "level_db": _num(c.get("level_db", -24.0), f"sfx[{cid}].level_db", -60, 0),
            "duck_db": _num(c.get("duck_db", 0.0), f"sfx[{cid}].duck_db", -30, 0),
            "fade_in_s": _num(c.get("fade_in_s", 0.01), f"sfx[{cid}].fade_in_s", 0.0),
            "fade_out_s": _num(c.get("fade_out_s", 0.05), f"sfx[{cid}].fade_out_s", 0.0),
        })
    protected = []
    for w in raw.get("protected") or []:
        a, b = _num(w[0], "protected.start", 0.0), _num(w[1], "protected.end", 0.0)
        if b <= a:
            raise AudioInputError(f"protected window {w} is empty")
        protected.append([a, b])
    loud = {**DEFAULT_LOUDNESS, **(raw.get("loudness") or {})}
    loud = {
        "target_lufs": _num(loud["target_lufs"], "loudness.target_lufs", -30, -5),
        "tolerance_lu": _num(loud["tolerance_lu"], "loudness.tolerance_lu", 0.1, 3),
        "tp_ceiling_dbtp": _num(loud["tp_ceiling_dbtp"], "loudness.tp_ceiling_dbtp", -9, 0),
        "tp_margin_db": _num(loud["tp_margin_db"], "loudness.tp_margin_db", 0.0, 3),
        "lra": _num(loud["lra"], "loudness.lra", 1, 20),
    }
    phone = {**DEFAULT_PHONE, **(raw.get("phone") or {})}
    phone["max_mb"] = _num(phone["max_mb"], "phone.max_mb", 0.1)
    subs = {**DEFAULT_SUBTITLES, **(raw.get("subtitles") or {})}
    if subs.get("fonts_dir"):
        subs["fonts_dir"] = _resolve(subs["fonts_dir"], base)
    return {
        "version": DELIVERY_VERSION,
        "min_vo_gap_s": validate_min_gap(raw.get("min_vo_gap_s", DEFAULT_MIN_VO_GAP_S)),
        "pickup_match": bool(vo_raw.get("pickup_match", True)),
        "vo": {"voice_id": vo_raw.get("voice_id"), "model": vo_raw.get("model", "eleven_v3"), "lines": lines},
        "music": music, "sfx": sfx, "protected": protected, "loudness": loud,
        "phone": phone, "master": {**DEFAULT_MASTER, **(raw.get("master") or {})}, "subtitles": subs,
    }


def load_plan_file(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise AudioInputError(f"audio plan not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise AudioInputError(f"audio plan {p} is not valid JSON: {e}") from None
    # Paths in the plan are relative to the episode dir (the parent of audio/).
    return normalize_plan(raw, p.parent.parent)


# ---------------------------------------------------------------------------- placement


def validate_placement(lines: list[dict], total_s: float, min_gap_s: float,
                       protected: list[list[float]] | None = None) -> dict:
    """Check VO line placement: minimum gap, no overlap with protected windows, tail guard.

    ``lines`` items need ``id``, ``at`` and ``dur``. Returns {"gaps": [...], "min_gap_s": x};
    raises PlacementError listing every violation."""
    min_gap_s = validate_min_gap(min_gap_s)
    problems: list[str] = []
    ordered = sorted(lines, key=lambda x: x["at"])
    gaps = []
    for a, b in zip(ordered, ordered[1:]):
        gap = round(b["at"] - (a["at"] + a["dur"]), 4)
        gaps.append({"after": a["id"], "before": b["id"], "gap_s": gap})
        if gap < min_gap_s - 1e-6:
            problems.append(f"gap {a['id']}->{b['id']} is {gap:.3f}s < min {min_gap_s:.2f}s")
    for ln in ordered:
        end = ln["at"] + ln["dur"]
        if end > total_s - TAIL_GUARD_S + 1e-6:
            problems.append(f"line {ln['id']} ends at {end:.3f}s, past picture end {total_s:.3f}s "
                            f"minus {TAIL_GUARD_S}s tail guard")
        for s, e in protected or []:
            if ln["at"] < e and end > s:
                problems.append(f"line {ln['id']} [{ln['at']:.2f}, {end:.2f}] overlaps protected window "
                                f"[{s:.2f}, {e:.2f}]")
    if problems:
        raise PlacementError("; ".join(problems))
    return {"gaps": gaps, "min_gap_s": min(g["gap_s"] for g in gaps) if gaps else None}


# ---------------------------------------------------------------------------- dsp helpers


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(float(np.mean(np.square(x, dtype=np.float64)))) + 1e-12)


def peak_db(x: np.ndarray) -> float:
    return 20.0 * math.log10(float(np.max(np.abs(x))) + 1e-12) if x.size else -120.0


def _moving_avg(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    c = np.cumsum(np.concatenate([[0.0], x]))
    out = (c[n:] - c[:-n]) / n
    pad = n - 1
    return np.concatenate([np.full(pad // 2, out[0]), out, np.full(pad - pad // 2, out[-1])])


def activity_mask(x: np.ndarray, floor_db: float = ACTIVITY_FLOOR_DB) -> np.ndarray:
    """Boolean mask of samples where the 20 ms moving average of |x| is above floor_db."""
    if x.size == 0:
        return np.zeros(0, dtype=bool)
    mono = np.max(np.abs(x), axis=1) if x.ndim == 2 else np.abs(x)
    env = _moving_avg(mono.astype(np.float64), max(1, int(0.02 * SR)))
    return env > db_to_lin(floor_db)


def activity_envelope(x: np.ndarray, pre_s: float = 0.08, post_s: float = 0.15,
                      smooth_s: float = 0.04) -> np.ndarray:
    """0..1 VO activity signal: thresholded, dilated (pre/post) and smoothed."""
    m = activity_mask(x).astype(np.float64)
    if m.size == 0:
        return m
    idx = np.flatnonzero(np.diff(np.concatenate([[0.0], m, [0.0]])))
    out = np.zeros_like(m)
    for s, e in zip(idx[::2], idx[1::2]):
        out[max(0, s - int(pre_s * SR)):min(m.size, e + int(post_s * SR))] = 1.0
    return np.clip(_moving_avg(out, max(1, int(smooth_s * SR))), 0.0, 1.0)


def active_rms_db(x: np.ndarray) -> float:
    m = activity_mask(x)
    return rms_db(x[m]) if m.any() else -120.0


def pickup_gains(levels: dict[str, float], max_gain_db: float = PICKUP_MAX_GAIN_DB) -> dict[str, float]:
    """Gain per line to match each line's active RMS to the median of all lines (pickup match)."""
    usable = {k: v for k, v in levels.items() if v > -100}
    if len(usable) < 2:
        return {k: 0.0 for k in levels}
    target = statistics.median(usable.values())
    gains = {}
    for k, v in levels.items():
        g = 0.0 if k not in usable else max(-max_gain_db, min(max_gain_db, target - v))
        gains[k] = round(g, 2) if abs(g) >= 0.25 else 0.0
    return gains


def duck_envelope(n: int, windows: list[tuple[float, float]], duck_db: float,
                  pre_s: float = 0.15, attack_s: float = 0.3, post_s: float = 0.25,
                  release_s: float = 0.5) -> np.ndarray:
    """Linear gain curve for the music bed: dips by duck_db around each VO line."""
    if n == 0:
        return np.zeros(0)
    t = np.arange(n) / SR
    act = np.zeros(n)
    for start, end in windows:
        a, b = start - pre_s, end + post_s
        up = (t - (a - attack_s)) / attack_s
        down = ((b + release_s) - t) / release_s
        act = np.maximum(act, np.clip(np.minimum(up, down), 0.0, 1.0))
    return np.power(10.0, (duck_db * act) / 20.0)


def fade(x: np.ndarray, fade_in_s: float, fade_out_s: float) -> np.ndarray:
    n = x.shape[0]
    g = np.ones(n)
    fi, fo = min(n, int(fade_in_s * SR)), min(n, int(fade_out_s * SR))
    if fi > 0:
        g[:fi] = np.linspace(0.0, 1.0, fi, endpoint=False)
    if fo > 0:
        g[n - fo:] = np.minimum(g[n - fo:], np.linspace(1.0, 0.0, fo))
    return x * g[:, None]


async def decode(path: str, n: int | None = None, start_s: float = 0.0, loop: bool = False) -> np.ndarray:
    """Decode to float32 stereo at SR; pad/trim to n samples when given."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    if start_s:
        cmd += ["-ss", f"{start_s:.4f}"]
    cmd += ["-i", path]
    if n is not None:
        cmd += ["-t", f"{n / SR:.6f}"]
    cmd += ["-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    out, _ = await run_cmd(cmd, f"decode {Path(path).name}")
    x = np.frombuffer(out, dtype="<f4").reshape(-1, 2).astype(np.float32)
    if n is not None:
        if x.shape[0] < n:
            x = np.concatenate([x, np.zeros((n - x.shape[0], 2), dtype=np.float32)])
        x = x[:n]
    return x


async def write_wav(x: np.ndarray, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    await run_cmd(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
                   "-c:a", "pcm_f32le", path], f"write {Path(path).name}",
                  stdin=np.ascontiguousarray(x, dtype="<f4").tobytes())


# ---------------------------------------------------------------------------- loudness


def parse_loudnorm_json(stderr: str) -> dict[str, float]:
    """Parse the JSON block ffmpeg's loudnorm prints on stderr. Values must be finite numbers."""
    end = stderr.rfind("}")
    start = stderr.rfind("{", 0, end + 1) if end >= 0 else -1
    if start < 0 or end < 0:
        raise LoudnormParseError("loudnorm printed no JSON block")
    try:
        data = json.loads(stderr[start:end + 1])
    except json.JSONDecodeError as e:
        raise LoudnormParseError(f"loudnorm JSON did not parse: {e}") from None
    out: dict[str, float] = {}
    for k, v in data.items():
        if k == "normalization_type":
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            raise LoudnormParseError(f"loudnorm field {k}={v!r} is not a number") from None
    for k in ("input_i", "input_tp", "input_lra", "input_thresh"):
        if k not in out:
            raise LoudnormParseError(f"loudnorm JSON lacks {k}")
        if not math.isfinite(out[k]):
            raise LoudnormParseError(f"loudnorm {k}={out[k]} is not finite (silent or unmeasurable audio)")
    if "normalization_type" in data:
        out["linear"] = 1.0 if data["normalization_type"] == "linear" else 0.0
    return out


async def measure_loudness(path: str, target: dict | None = None) -> dict[str, float]:
    """Single loudnorm analysis pass: integrated loudness, true peak, LRA (from stderr JSON)."""
    t = {**DEFAULT_LOUDNESS, **(target or {})}
    _, err = await run_cmd(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-vn", "-af",
         f"loudnorm=I={t['target_lufs']}:TP={t['tp_ceiling_dbtp'] - t['tp_margin_db']}:LRA={t['lra']}"
         ":print_format=json", "-f", "null", "-"], f"loudness {Path(path).name}")
    m = parse_loudnorm_json(err)
    return {"integrated_lufs": m["input_i"], "true_peak_dbtp": m["input_tp"], "lra": m["input_lra"],
            "threshold": m["input_thresh"]}


async def loudnorm_two_pass(src: str, dst: str, loud: dict) -> dict:
    """EBU R128 two-pass loudnorm. Pass 1 measures; pass 2 applies the measured values (linear)."""
    tp_target = loud["tp_ceiling_dbtp"] - loud["tp_margin_db"]
    base = f"loudnorm=I={loud['target_lufs']}:TP={tp_target}:LRA={loud['lra']}"
    _, err1 = await run_cmd(["ffmpeg", "-hide_banner", "-nostdin", "-i", src, "-af",
                             base + ":print_format=json", "-f", "null", "-"], "loudnorm pass 1")
    m1 = parse_loudnorm_json(err1)
    pass2 = (f"{base}:measured_I={m1['input_i']}:measured_TP={m1['input_tp']}:measured_LRA={m1['input_lra']}"
             f":measured_thresh={m1['input_thresh']}:offset={m1.get('target_offset', 0.0)}:linear=true"
             ":print_format=json")
    _, err2 = await run_cmd(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src, "-af", pass2,
                             "-ar", str(SR), "-c:a", "pcm_s24le", dst], "loudnorm pass 2")
    m2 = parse_loudnorm_json(err2)
    return {"pass1": m1, "pass2": m2, "linear": bool(m2.get("linear", 0.0))}


# ---------------------------------------------------------------------------- subtitles


def wrap_two_lines(text: str, max_chars: int) -> list[str]:
    """Split subtitle text into at most two balanced lines."""
    text = " ".join(str(text).split())
    if len(text) <= max_chars or " " not in text:
        return [text]
    words = text.split(" ")
    best, best_score = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = max(len(a), len(b))
        if best_score is None or score < best_score:
            best, best_score = [a, b], score
    return best or [text]


def build_cues(placed: list[dict], max_chars: int) -> list[dict]:
    cues = []
    for ln in sorted(placed, key=lambda x: x["at"]):
        if not ln.get("subtitle"):
            continue
        cues.append({"id": ln["id"], "start": round(ln["at"], 3), "end": round(ln["at"] + ln["dur"], 3),
                     "lines": wrap_two_lines(ln["subtitle"], max_chars)})
    return cues


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_escape(s: str) -> str:
    return s.replace("\\", "/").replace("{", "(").replace("}", ")")


def write_ass(cues: list[dict], path: str, subs: dict) -> None:
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{subs['font']},{int(subs['fontsize'])},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,4,1,2,80,80,{int(subs['margin_v'])},1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    rows = [f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},Default,,0,0,0,,"
            + "\\N".join(_ass_escape(x) for x in c["lines"]) for c in cues]
    Path(path).write_text(head + "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


def write_srt(cues: list[dict], path: str) -> None:
    blocks = [f"{i}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n" + "\n".join(c["lines"]) + "\n"
              for i, c in enumerate(cues, 1)]
    Path(path).write_text("\n".join(blocks), encoding="utf-8")


def parse_ass_cues(path: str) -> list[tuple[float, float, int]]:
    """(start, end, n_lines) for every Dialogue row of an ASS file."""
    def secs(s: str) -> float:
        h, m, rest = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    out = []
    for row in Path(path).read_text(encoding="utf-8").splitlines():
        if row.startswith("Dialogue:"):
            parts = row.split(",", 9)
            out.append((secs(parts[1]), secs(parts[2]), parts[9].count("\\N") + 1))
    return out


# ---------------------------------------------------------------------------- input key


def input_key(mute_sha: str, total_s: float, plan: dict, file_hashes: dict[str, str]) -> str:
    """Content-hash reuse key: picture hash + normalized plan with every input file replaced by
    its sha256 (so a re-rendered VO line or swapped library file invalidates the delivery)."""
    def strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (file_hashes.get(v, v) if k == "path" and isinstance(v, str) else strip(v))
                    for k, v in obj.items() if k != "fonts_dir"}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj
    payload = {"v": DELIVERY_VERSION, "mute": mute_sha, "dur": round(total_s, 3), "plan": strip(plan)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def reusable(manifest_path: str, key: str) -> dict | None:
    p = Path(manifest_path)
    if not p.is_file():
        return None
    try:
        old = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    if old.get("input_key") != key or old.get("dry_run"):
        return None
    for name, info in (old.get("outputs") or {}).items():
        f = Path(info.get("path", ""))
        if not f.is_file() or f.stat().st_size != info.get("size_bytes"):
            return None
    return old


# ---------------------------------------------------------------------------- mix + deliver


def _noop(_: dict) -> None:
    return None


async def build_vo_bus(placed: list[dict], total_s: float, out_wav: str, cwd: str) -> None:
    """VO bus: silent base + per-line aformat/volume/alimiter/adelay inputs, amix normalize=0."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-t", f"{total_s:.4f}",
           "-i", f"anullsrc=r={SR}:cl=stereo"]
    chains, labels = [], ["[0:a]"]
    for i, ln in enumerate(placed, 1):
        cmd += ["-i", ln["path"]]
        f = f"[{i}:a]aformat=sample_rates={SR}:channel_layouts=stereo"
        if ln["gain_db"]:
            f += f",volume={ln['gain_db']:.2f}dB"
            if ln["gain_db"] > 0:
                f += f",alimiter=limit={LINE_LIMIT}:attack=1:release=50:level=disabled"
        ms = int(round(ln["at"] * 1000))
        f += f",adelay={ms}|{ms}[v{i}]"
        chains.append(f)
        labels.append(f"[v{i}]")
    graph = ";".join(chains + ["".join(labels) + f"amix=inputs={len(labels)}:duration=first:"
                               "dropout_transition=0:normalize=0[vo]"])
    cmd += ["-filter_complex", graph, "-map", "[vo]", "-ar", str(SR), "-c:a", "pcm_f32le", out_wav]
    await run_cmd(cmd, "VO bus", cwd=cwd)


async def final_mix(stems: dict[str, str], total_s: float, out_wav: str) -> None:
    """Sum stems (VO trimmed), makeup gain and master true-peak limiter (reference chain)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    for k in ("sfx", "vo", "music"):
        cmd += ["-i", stems[k]]
    graph = (f"[0:a]apad=whole_dur={total_s:.4f},atrim=0:{total_s:.4f}[s];"
             f"[1:a]apad=whole_dur={total_s:.4f},atrim=0:{total_s:.4f},volume={VO_BUS_DB}dB[v];"
             f"[2:a]apad=whole_dur={total_s:.4f},atrim=0:{total_s:.4f}[m];"
             f"[s][v][m]amix=inputs=3:duration=longest:dropout_transition=1:normalize=0,"
             f"volume={MIX_MAKEUP_DB}dB,alimiter=limit={MASTER_LIMIT}:attack=1:release=60:level=disabled[out]")
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ar", str(SR), "-c:a", "pcm_f32le", out_wav]
    await run_cmd(cmd, "final mix")


def _phone_video_kbps(phone: dict, total_s: float, max_bytes: int) -> int:
    if phone.get("video_kbps"):
        return int(phone["video_kbps"])
    budget = max_bytes * 8 * 0.92 / max(total_s, 0.1) / 1000.0 - float(phone["audio_kbps"])
    return int(max(phone["min_video_kbps"], min(phone["max_video_kbps"], budget)))


async def encode_phone(mute: str, wav: str, ass: str | None, out: str, total_s: float, kbps: int,
                       phone: dict, cwd: str, fonts_dir: str | None) -> None:
    """Two-pass x264 phone encode from the picture + PCM mix (never re-encodes the AAC master)."""
    vf = ["-vf", _ass_filter(ass, fonts_dir)] if ass else []
    common = ["-c:v", "libx264", "-preset", str(phone["preset"]), "-b:v", f"{kbps}k", "-pix_fmt", "yuv420p",
              "-passlogfile", "phone2pass"]
    await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", mute, *vf, *common, "-pass", "1", "-an",
                   "-t", f"{total_s:.4f}", "-f", "mp4", os.devnull], "phone encode pass 1", cwd=cwd)
    await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", mute, "-i", wav, "-map", "0:v:0",
                   "-map", "1:a:0", *vf, *common, "-pass", "2", "-c:a", "aac", "-b:a", f"{phone['audio_kbps']}k",
                   "-ar", str(SR), "-t", f"{total_s:.4f}", "-movflags", "+faststart", out],
                  "phone encode pass 2", cwd=cwd)


def _ass_filter(ass: str, fonts_dir: str | None) -> str:
    # ffmpeg runs in the work dir, so the ASS path is a plain relative name (no filtergraph escaping).
    f = f"ass={Path(ass).name}"
    if fonts_dir:
        f += f":fontsdir={fonts_dir.replace(':', chr(92) + ':')}"
    return f


async def mix_and_deliver(
    episode_id: str, mute_path: str, plan: dict, *, root: Path | None = None,
    picture_duration: float | None = None, heartbeat: Heartbeat = _noop,
) -> dict:
    """Build the full delivery from a mute picture and resolved plan (every input path on disk).

    Returns the manifest. Outputs land atomically under {episode}/delivery/; an existing
    delivery with the same content-hash input key is reused as-is."""
    paths = delivery_paths(episode_id, root)
    mute = Path(mute_path)
    if not mute.is_file():
        raise AudioInputError(f"mute picture not found: {mute}")
    total_s = picture_duration or await probe_duration(mute)
    heartbeat({"stage": "inputs", "picture_s": total_s})

    lines = plan["vo"]["lines"]
    inputs: list[str] = [ln["path"] for ln in lines]
    if plan.get("music"):
        inputs.append(plan["music"]["path"])
    inputs += [c["path"] for c in plan.get("sfx", [])]
    missing = [p for p in inputs if not p or not Path(p).is_file()]
    if missing:
        raise AudioInputError(f"audio inputs missing: {missing}")
    hashes = {p: sha256_file(p) for p in inputs}
    key = input_key(sha256_file(mute), total_s, plan, hashes)
    old = reusable(paths["manifest"], key)
    if old:
        heartbeat({"stage": "reused", "input_key": key})
        return {**old, "reused": True}

    out_dir = Path(paths["dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=out_dir))
    try:
        n = int(round(total_s * SR))
        # ---- VO: durations, pickup match, placement
        placed, levels = [], {}
        for ln in lines:
            dur = await probe_duration(ln["path"])
            placed.append({**ln, "dur": round(dur, 4)})
            if plan.get("pickup_match", True) and ln.get("gain_db") is None:
                levels[ln["id"]] = active_rms_db(await decode(ln["path"]))
        auto = pickup_gains(levels) if levels else {}
        for ln in placed:
            ln["gain_db"] = ln["gain_db"] if ln.get("gain_db") is not None else auto.get(ln["id"], 0.0)
        placement = validate_placement(placed, total_s, plan["min_vo_gap_s"], plan.get("protected"))
        heartbeat({"stage": "vo_bus", "lines": len(placed)})
        vo_wav = str(work / "vo.wav")
        await build_vo_bus(placed, total_s, vo_wav, str(work))
        vo = await decode(vo_wav, n)
        act = activity_envelope(vo)

        # ---- music bed: level to base, duck under VO, fades
        heartbeat({"stage": "music"})
        music = np.zeros((n, 2), dtype=np.float32)
        music_info = None
        if plan.get("music"):
            m = plan["music"]
            off = int(round(m["offset_s"] * SR))
            seg_n = max(0, n - off)
            if seg_n:
                bed = await decode(m["path"], seg_n, m["src_start_s"], loop=m["loop"])
                base = rms_db(bed)
                gain = m["level_db"] - base if base > -100 else 0.0
                bed = fade(bed * db_to_lin(gain), m["fade_in_s"], m["fade_out_s"])
                env = duck_envelope(n, [(p["at"], p["at"] + p["dur"]) for p in placed], m["duck_db"])
                music[off:] = bed * env[off:, None]
                music_info = {"gain_db": round(gain, 2), "duck_db": m["duck_db"],
                              "ducked_windows": len(placed)}

        # ---- SFX: slice/loop, rms|peak normalize, fades, optional VO duck (beds / vox snippets)
        heartbeat({"stage": "sfx", "cues": len(plan.get("sfx", []))})
        sfx = np.zeros((n, 2), dtype=np.float32)
        sfx_info = []
        for c in plan.get("sfx", []):
            start = int(round(c["at"] * SR))
            if start >= n:
                raise PlacementError(f"sfx {c['id']} starts at {c['at']}s, after picture end {total_s:.2f}s")
            cn = min(int(round(c["dur"] * SR)), n - start)
            clip = await decode(c["path"], cn, c["src_start_s"], loop=c["loop"])
            ref = rms_db(clip) if c["norm"] == "rms" else peak_db(clip)
            g = c["level_db"] - ref if ref > -100 else 0.0
            clip = fade(clip * db_to_lin(g), c["fade_in_s"], c["fade_out_s"])
            if c["duck_db"] < 0:
                depth = 1.0 - db_to_lin(c["duck_db"])
                clip = clip * (1.0 - depth * act[start:start + cn])[:, None]
            sfx[start:start + cn] += clip
            sfx_info.append({"id": c["id"], "gain_db": round(g, 2), "ducked": c["duck_db"] < 0})

        stems = {"vo": vo_wav, "music": str(work / "music.wav"), "sfx": str(work / "sfx.wav")}
        await write_wav(music, stems["music"])
        await write_wav(sfx, stems["sfx"])

        # ---- master: sum + limiter, then EBU R128 two-pass loudnorm from the PCM premaster
        heartbeat({"stage": "loudnorm"})
        pre = str(work / "premaster.wav")
        await final_mix(stems, total_s, pre)
        ln_info = await loudnorm_two_pass(pre, str(work / "mix.wav"), plan["loudness"])
        mix_meas = await measure_loudness(str(work / "mix.wav"), plan["loudness"])
        safety_db = 0.0
        if mix_meas["true_peak_dbtp"] > plan["loudness"]["tp_ceiling_dbtp"] - 0.1:
            # loudnorm fell back to dynamic or overshot: trim to the ceiling (loudness drops with it).
            safety_db = round(plan["loudness"]["tp_ceiling_dbtp"] - 0.3 - mix_meas["true_peak_dbtp"], 2)
            await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(work / "mix.wav"), "-af",
                           f"volume={safety_db}dB", "-c:a", "pcm_s24le", str(work / "mix_trim.wav")], "TP trim")
            os.replace(work / "mix_trim.wav", work / "mix.wav")
            mix_meas = await measure_loudness(str(work / "mix.wav"), plan["loudness"])
        mix_wav = str(work / "mix.wav")

        # ---- subtitles
        subs = plan["subtitles"]
        cues = build_cues(placed, int(subs["max_line_chars"]))
        ass = str(work / Path(paths["ass"]).name)
        srt = str(work / Path(paths["srt"]).name)
        write_ass(cues, ass, subs)
        write_srt(cues, srt)
        burn = bool(subs.get("burn", True)) and bool(cues)

        # ---- picture + audio deliverables
        heartbeat({"stage": "mux"})
        nosubs = str(work / Path(paths["nosubs"]).name)
        akbps = f"{int(plan['master']['audio_kbps'])}k"
        await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(mute), "-i", mix_wav,
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", akbps,
                       "-ar", str(SR), "-t", f"{total_s:.4f}", "-movflags", "+faststart", nosubs], "mux nosubs")
        master = None
        if burn:
            heartbeat({"stage": "burn_subtitles", "cues": len(cues)})
            master = str(work / Path(paths["master_subs"]).name)
            await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(mute), "-i", mix_wav,
                           "-map", "0:v:0", "-map", "1:a:0", "-vf", _ass_filter(ass, subs.get("fonts_dir")),
                           "-c:v", "libx264", "-preset", str(plan["master"]["preset"]),
                           "-crf", str(plan["master"]["crf"]), "-pix_fmt", "yuv420p", "-c:a", "aac",
                           "-b:a", akbps, "-ar", str(SR), "-t", f"{total_s:.4f}", "-movflags", "+faststart",
                           master], "burn subtitles", cwd=str(work))

        heartbeat({"stage": "phone_encode"})
        phone_cfg = plan["phone"]
        max_bytes = int(phone_cfg["max_mb"] * 1_000_000)
        phone = str(work / Path(paths["phone"]).name)
        kbps = _phone_video_kbps(phone_cfg, total_s, max_bytes)
        attempts = []
        await encode_phone(str(mute), mix_wav, ass if burn else None, phone, total_s, kbps, phone_cfg,
                           str(work), subs.get("fonts_dir"))
        size = Path(phone).stat().st_size
        attempts.append({"video_kbps": kbps, "size_bytes": size})
        if size > max_bytes:
            # One re-encode at a bitrate scaled to the measured overshoot.
            kbps = max(int(phone_cfg["min_video_kbps"]), int(kbps * max_bytes / size * 0.9))
            heartbeat({"stage": "phone_reencode", "video_kbps": kbps})
            await encode_phone(str(mute), mix_wav, ass if burn else None, phone, total_s, kbps, phone_cfg,
                               str(work), subs.get("fonts_dir"))
            size = Path(phone).stat().st_size
            attempts.append({"video_kbps": kbps, "size_bytes": size})

        # ---- atomic publish
        Path(paths["vo_stem"]).parent.mkdir(parents=True, exist_ok=True)
        moves = {"vo_stem": stems["vo"], "music_stem": stems["music"], "sfx_stem": stems["sfx"],
                 "mix_wav": mix_wav, "nosubs": nosubs, "phone": phone, "ass": ass, "srt": srt}
        if master:
            moves["master_subs"] = master
        outputs = {}
        for name, src in moves.items():
            os.replace(src, paths[name])
            outputs[name] = {"path": paths[name], "size_bytes": Path(paths[name]).stat().st_size,
                             "sha256": sha256_file(paths[name])}
        if not master and Path(paths["master_subs"]).exists():
            Path(paths["master_subs"]).unlink()
        manifest = {
            "episode_id": episode_id, "version": DELIVERY_VERSION, "dry_run": False, "input_key": key,
            "mute_path": str(mute), "picture_duration_s": round(total_s, 4),
            "inputs": {p: h for p, h in hashes.items()},
            "vo_lines": [{k: ln[k] for k in ("id", "at", "dur", "gain_db", "path")} for ln in placed],
            "placement": placement, "min_vo_gap_s": plan["min_vo_gap_s"],
            "music": music_info, "sfx": sfx_info,
            "loudness_target": plan["loudness"], "loudnorm": ln_info, "mix_loudness": mix_meas,
            "tp_safety_trim_db": safety_db,
            "cues": cues, "subtitles_burned": burn,
            "phone": {"max_bytes": max_bytes, "attempts": attempts, "size_bytes": size,
                      "within_limit": size <= max_bytes},
            "outputs": outputs, "paths": paths,
        }
        tmp_manifest = work / "manifest.json"
        tmp_manifest.write_text(json.dumps(manifest, indent=2))
        os.replace(tmp_manifest, paths["manifest"])
        heartbeat({"stage": "done", "input_key": key})
        return {**manifest, "reused": False}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------- delivery QC


async def delivery_qc(manifest: dict, plan: dict, picture_duration: float | None = None) -> dict:
    """Measure the delivered files: streams, duration vs picture, loudness tolerance, true-peak
    ceiling, subtitle cues, VO gap and phone size. Verdict PASS or FAIL (blocking)."""
    loud = plan["loudness"]
    pic = float(picture_duration or manifest["picture_duration_s"])
    checks: list[dict] = []

    def add(name: str, ok: bool, value: Any = None, limit: Any = None, file: str | None = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "value": value, "limit": limit, "file": file})

    outs = manifest.get("outputs") or {}
    want = ["nosubs", "phone"] + (["master_subs"] if manifest.get("subtitles_burned") else [])
    for name in want:
        path = (outs.get(name) or {}).get("path")
        if not path or not Path(path).is_file():
            add(f"{name}_exists", False, None, "file on disk", path)
            continue
        info = await probe(path)
        streams = info.get("streams", [])
        a = [s for s in streams if s.get("codec_type") == "audio"]
        v = [s for s in streams if s.get("codec_type") == "video"]
        add(f"{name}_video_stream", bool(v), len(v), ">=1", path)
        add(f"{name}_audio_stream", bool(a) and a[0].get("codec_name") == "aac",
            a[0].get("codec_name") if a else None, "aac", path)
        dur = float(info.get("format", {}).get("duration") or 0.0)
        add(f"{name}_duration", abs(dur - pic) <= DURATION_TOLERANCE_S, round(dur, 3),
            f"{pic:.3f}+-{DURATION_TOLERANCE_S}", path)
        if a:
            m = await measure_loudness(path, loud)
            add(f"{name}_loudness", abs(m["integrated_lufs"] - loud["target_lufs"]) <= loud["tolerance_lu"],
                m["integrated_lufs"], f"{loud['target_lufs']}+-{loud['tolerance_lu']} LUFS", path)
            add(f"{name}_true_peak", m["true_peak_dbtp"] <= loud["tp_ceiling_dbtp"], m["true_peak_dbtp"],
                f"<={loud['tp_ceiling_dbtp']} dBTP", path)
    phone = manifest.get("phone") or {}
    ppath = (outs.get("phone") or {}).get("path")
    if ppath and Path(ppath).is_file():
        size = Path(ppath).stat().st_size
        add("phone_size", size <= phone.get("max_bytes", 0), size, phone.get("max_bytes"), ppath)
    cues = manifest.get("cues") or []
    if cues:
        ass = (outs.get("ass") or {}).get("path")
        found = parse_ass_cues(ass) if ass and Path(ass).is_file() else []
        add("subtitle_cue_count", len(found) == len(cues), len(found), len(cues), ass)
        # Timing of the WRITTEN subtitle file vs where each VO line was actually placed.
        lines = {ln["id"]: ln for ln in manifest.get("vo_lines", [])}
        devs = [max(abs(f[0] - lines[c["id"]]["at"]), abs(f[1] - (lines[c["id"]]["at"] + lines[c["id"]]["dur"])))
                for c, f in zip(cues, found) if c["id"] in lines]
        dev = max(devs) if devs else (0.0 if not found else float("inf"))
        add("subtitle_timing", bool(found) and dev <= CUE_TOLERANCE_S, round(dev, 3), CUE_TOLERANCE_S, ass)
        add("subtitle_max_two_lines", all(n <= 2 for _, _, n in found), max((n for *_, n in found), default=0),
            2, ass)
    gap = (manifest.get("placement") or {}).get("min_gap_s")
    if gap is not None:
        add("vo_min_gap", gap >= plan["min_vo_gap_s"] - 1e-6, gap, plan["min_vo_gap_s"])
    failed = [c for c in checks if not c["ok"]]
    verdict = "FAIL" if failed else "PASS"
    return {"verdict": verdict, "blocking": verdict in ("FAIL", "NEEDS_REVIEW"), "checks": checks,
            "failed": [c["name"] for c in failed], "picture_duration_s": pic,
            "input_key": manifest.get("input_key")}
