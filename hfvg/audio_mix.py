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
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import numpy as np

SR = 48000
DELIVERY_VERSION = 6  # 6: GAP-60 picture sync guard; 5: GAP-55 CFR picture encodes + frame-count QC; 2: GAP-20 codec TP guard changes the encoded audio; 3: GAP-30 window duck, GAP-31 guard loudness
# 4: GAP-34 bed_dips (music+SFX gain dips over reviewer-flagged bursts), GAP-35 per-line subtitle_dur
MIN_VO_GAP_RANGE = (0.3, 0.4)
DEFAULT_MIN_VO_GAP_S = 0.4
TAIL_GUARD_S = 0.3            # last VO line must end this far before the picture ends
ACTIVITY_FLOOR_DB = -45.0     # VO activity threshold (20 ms moving average of |x|)
PICKUP_MAX_GAIN_DB = 6.0
DURATION_TOLERANCE_S = 0.1    # delivered file vs locked picture
CUE_TOLERANCE_S = 0.05        # subtitle cue vs VO line placement
SUBTITLE_SPEECH_SLACK_S = 0.1  # GAP-35: a subtitle_dur may end at most this much before the last spoken word
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


async def count_frames(path: str | Path) -> int:
    """GAP-55: number of video frames (packets) in the first video stream."""
    out, _ = await run_cmd(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                           f"ffprobe frames {Path(path).name}")
    txt = out.decode().strip().split(",")[0]
    return int(txt) if txt.isdigit() else 0


def video_rate(info: dict) -> str:
    """r_frame_rate of the first video stream (e.g. '24/1'); '24/1' when unknown."""
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    r = str(v.get("r_frame_rate") or "")
    return r if r and not r.startswith("0") else "24/1"


def cfr_args(rate: str, frames: int | None) -> list[str]:
    """GAP-55: constant frame rate output with the exact source frame count (no vfr drops)."""
    return ["-fps_mode", "cfr", "-r", rate, *(["-frames:v", str(frames)] if frames else [])]


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


def validate_bed_dips(raw: Any) -> list[dict]:
    """GAP-34: reviewer-flagged bursts in the beds. Each dip {start, end, gain_db, ramp_s} lowers the
    music AND SFX buses (never VO) by gain_db between start and end (edit seconds), with linear
    ramps of ramp_s outside the window so the dip itself does not click."""
    out = []
    for i, d in enumerate(raw or []):
        if not isinstance(d, dict):
            raise AudioInputError(f"bed_dips[{i}] must be an object")
        a = _num(d.get("start"), f"bed_dips[{i}].start", 0.0)
        b = _num(d.get("end"), f"bed_dips[{i}].end", 0.0)
        if b <= a:
            raise AudioInputError(f"bed_dips[{i}] window {a}-{b} is empty")
        out.append({"start": a, "end": b,
                    "gain_db": _num(d.get("gain_db"), f"bed_dips[{i}].gain_db", -30.0, -0.5),
                    "ramp_s": _num(d.get("ramp_s", 0.15), f"bed_dips[{i}].ramp_s", 0.0, 2.0),
                    "note": str(d.get("note") or "")})
    return out


def dip_envelope(n: int, dips: list[dict]) -> np.ndarray:
    """GAP-34: linear gain curve (1.0 = untouched) for bed_dips; overlapping dips take the deepest."""
    g = np.ones(n)
    if n == 0 or not dips:
        return g
    t = np.arange(n) / SR
    for d in dips:
        r = max(d["ramp_s"], 1.0 / SR)
        act = np.clip(np.minimum((t - (d["start"] - r)) / r, ((d["end"] + r) - t) / r), 0.0, 1.0)
        g = np.minimum(g, np.power(10.0, (d["gain_db"] * act) / 20.0))
    return g


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
    # VO tempo post step (handbook: atempo 0.90 per line in post). vo.atempo applies to GENERATED
    # lines (text only); a locked file is already final unless the line sets its own "atempo".
    vo_atempo = None if vo_raw.get("atempo") is None else _num(vo_raw["atempo"], "vo.atempo", 0.5, 2.0)
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
            # GAP-35: optional subtitle length from the line start (cue ends with the spoken words when
            # the locked file carries trailing room tone); never longer than the line, checked at mix
            "subtitle_dur": None if ln.get("subtitle_dur") is None
            else _num(ln["subtitle_dur"], f"{lid}.subtitle_dur", 0.2, 60.0),
            "take": int(_num(ln.get("take", 1), f"vo.lines[{lid}].take", 1, 20)),
            "gain_db": None if ln.get("gain_db") is None else _num(ln["gain_db"], f"{lid}.gain_db", -24, 12),
            "atempo": (None if ln.get("atempo") is None else _num(ln["atempo"], f"{lid}.atempo", 0.5, 2.0))
            if "atempo" in ln else (None if ln.get("path") else vo_atempo),
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
        if m.get("end_align"):
            # GAP-23: place the bed so its natural END lands on the picture end (src_start computed
            # at mix time from the file length), e.g. after the edit got shorter
            music["end_align"] = True
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
            "duck_mode": _duck_mode(c.get("duck_mode", "activity"), cid),
            "fade_in_s": _num(c.get("fade_in_s", 0.01), f"sfx[{cid}].fade_in_s", 0.0),
            "fade_out_s": _num(c.get("fade_out_s", 0.05), f"sfx[{cid}].fade_out_s", 0.0),
            # optional 2-pole lowpass applied twice (library beds made softer / more distant)
            "lowpass_hz": None if c.get("lowpass_hz") is None
            else _num(c["lowpass_hz"], f"sfx[{cid}].lowpass_hz", 100.0, 20000.0),
        })
        if c.get("event") is not None:  # GAP-59: point SFX anchored to an on-screen event frame
            if sfx[-1]["loop"]:
                raise AudioInputError(f"sfx {cid}: a loop bed cannot carry an event anchor")
            sfx[-1]["event"] = validate_sfx_event(c["event"], cid)
    protected = []
    for w in raw.get("protected") or []:
        a, b = _num(w[0], "protected.start", 0.0), _num(w[1], "protected.end", 0.0)
        if b <= a:
            raise AudioInputError(f"protected window {w} is empty")
        protected.append([a, b])
    bed_dips = validate_bed_dips(raw.get("bed_dips"))
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
    # GAP-19: optional phone downscale (e.g. 720x1280 so a 2-min 5 MB copy does not band); both or none
    if (phone.get("width") is None) != (phone.get("height") is None):
        raise AudioInputError("phone.width and phone.height must be set together")
    if phone.get("width") is not None:
        for k in ("width", "height"):
            v = _num(phone[k], f"phone.{k}", 16, 4096)
            if int(v) != v or int(v) % 2:
                raise AudioInputError(f"phone.{k} must be an even integer, got {phone[k]!r}")
            phone[k] = int(v)
    logo = None
    if raw.get("logo"):
        lg = raw["logo"]
        if not lg.get("path"):
            raise AudioInputError("logo needs a path")
        logo = {
            "path": _resolve(lg.get("path"), base),
            "width_frac": _num(lg.get("width_frac", 0.62), "logo.width_frac", 0.05, 1.0),
            "fade_in_s": _num(lg.get("fade_in_s", 2.5), "logo.fade_in_s", 0.0, 30.0),
            "visible_s": _num(lg.get("visible_s", 6.5), "logo.visible_s", 0.1, 600.0),
            # vertical centre of the logo as a fraction of frame height (0.5 = centred, Ep01 template)
            "y_frac": _num(lg.get("y_frac", 0.5), "logo.y_frac", 0.0, 1.0),
        }
        # Optional: px width instead of width_frac; start the fade a set gap after a VO line ends
        # (instead of total - visible_s); a soft darkening of the lower frame fading in with it.
        if lg.get("width_px") is not None:
            logo["width_px"] = int(_num(lg["width_px"], "logo.width_px", 16, 4096))
        if lg.get("after_vo_line"):
            logo["after_vo_line"] = str(lg["after_vo_line"])
            logo["after_vo_gap_s"] = _num(lg.get("after_vo_gap_s", 0.6), "logo.after_vo_gap_s", 0.0, 10.0)
        if lg.get("min_hold_s") is not None:
            logo["min_hold_s"] = _num(lg["min_hold_s"], "logo.min_hold_s", 0.0, 60.0)
        if lg.get("darken_lower"):
            dk = lg["darken_lower"]
            logo["darken_lower"] = {"frac": _num(dk.get("frac", 0.5), "logo.darken_lower.frac", 0.05, 1.0),
                                    "amount": _num(dk.get("amount", 0.15), "logo.darken_lower.amount", 0.0, 0.9)}
    subs = {**DEFAULT_SUBTITLES, **(raw.get("subtitles") or {})}
    if subs.get("ass_style") is not None:
        st = str(subs["ass_style"])
        if not st.startswith("Style: ") or len(st.split(",")) != 23:
            raise AudioInputError("subtitles.ass_style must be one full ASS 'Style: ...' line (23 fields)")
    if subs.get("fonts_dir"):
        subs["fonts_dir"] = _resolve(subs["fonts_dir"], base)
    timeline_ref = None
    if raw.get("timeline_ref") is not None:
        timeline_ref = validate_timeline(raw["timeline_ref"], "timeline_ref")
    out = {
        "version": DELIVERY_VERSION,
        "min_vo_gap_s": validate_min_gap(raw.get("min_vo_gap_s", DEFAULT_MIN_VO_GAP_S)),
        "pickup_match": bool(vo_raw.get("pickup_match", True)),
        "vo": {"voice_id": vo_raw.get("voice_id"), "model": vo_raw.get("model", "eleven_v3"), "lines": lines},
        "music": music, "sfx": sfx, "protected": protected, "bed_dips": bed_dips, "loudness": loud,
        "phone": phone, "master": {**DEFAULT_MASTER, **(raw.get("master") or {})}, "subtitles": subs,
        "logo": logo,
    }
    if timeline_ref is not None:
        out["timeline_ref"] = timeline_ref
    return out


def load_plan_file(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise AudioInputError(f"audio plan not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AudioInputError(f"audio plan {p} is not valid JSON: {e}") from None
    # Paths in the plan are relative to the episode dir (the parent of audio/).
    return normalize_plan(raw, p.parent.parent)


# ---------------------------------------------------------------------------- GAP-23 timeline remap
# An audio plan authored against one edit (absolute seconds) follows later edit changes (a shot
# trimmed or dropped at G_FINAL_CUT) when it carries ``timeline_ref``: the edit layout its times were
# written against. Every time is re-expressed as (shot, position in that shot's source clip) and
# placed in the CURRENT edit. Times at/after the old picture end keep their distance to the end.

def timeline_from_manifest(manifest: dict) -> dict:
    """Edit layout {clips: [{id, start, seg_s, trim_in, trim_out[, sha256, ops]}], total_s, fps} from an
    assembly manifest. GAP-59: each clip also carries its source sha256 and, for a retimed shot, its
    shot_ops {segments, speed} so an SFX event anchored in the SOURCE clip lands on the right frame."""
    clips, t = [], 0.0
    dissolves = manifest.get("dissolves") or {}
    for c in manifest.get("clips") or []:
        seg = float(c["seg_s"])
        row = {"id": str(c["id"]), "start": round(t, 6), "seg_s": seg,
               "trim_in": float(c.get("trim_in", 0.0)), "trim_out": float(c.get("trim_out", seg))}
        if c.get("source_sha256"):
            row["sha256"] = str(c["source_sha256"])
        ops = c.get("shot_ops")
        if ops and ops.get("segments"):
            row["ops"] = {"segments": [[float(a), float(b)] for a, b in ops["segments"]],
                          "speed": float(ops.get("speed") or 1.0)}
        clips.append(row)
        t += seg - float(dissolves.get(c["id"], 0.0))
    total = float(manifest.get("total_duration") or t)
    out = {"clips": clips, "total_s": round(total, 6)}
    if manifest.get("fps"):
        out["fps"] = float(manifest["fps"])
    return out


def validate_timeline(tl: Any, name: str) -> dict:
    if not isinstance(tl, dict) or not isinstance(tl.get("clips"), list) or not tl["clips"]:
        raise AudioInputError(f"{name} must be {{clips: [...], total_s}}")
    clips = []
    for c in tl["clips"]:
        if not isinstance(c, dict) or not c.get("id"):
            raise AudioInputError(f"{name}: every clip needs an id")
        clips.append({"id": str(c["id"]), "start": _num(c.get("start"), f"{name}.{c['id']}.start", 0.0),
                      "seg_s": _num(c.get("seg_s"), f"{name}.{c['id']}.seg_s", 0.01),
                      "trim_in": _num(c.get("trim_in", 0.0), f"{name}.{c['id']}.trim_in", 0.0),
                      "trim_out": _num(c.get("trim_out", c.get("seg_s")), f"{name}.{c['id']}.trim_out", 0.01)})
        if c.get("sha256"):
            clips[-1]["sha256"] = str(c["sha256"])
        if c.get("ops") is not None:
            clips[-1]["ops"] = _validate_ops(c["ops"], f"{name}.{c['id']}.ops")
    if len({c["id"] for c in clips}) != len(clips):
        raise AudioInputError(f"{name}: duplicate clip ids")
    out = {"clips": clips, "total_s": _num(tl.get("total_s"), f"{name}.total_s", 0.01)}
    if tl.get("fps") is not None:
        out["fps"] = _num(tl["fps"], f"{name}.fps", 1.0, 240.0)
    return out


def _validate_ops(ops: Any, name: str) -> dict:
    if not isinstance(ops, dict) or not isinstance(ops.get("segments"), list) or not ops["segments"]:
        raise AudioInputError(f"{name} must be {{segments: [[a, b], ...], speed}}")
    segs = []
    for sg in ops["segments"]:
        if not isinstance(sg, (list, tuple)) or len(sg) != 2:
            raise AudioInputError(f"{name}: every segment is [start, end]")
        a, b = _num(sg[0], f"{name}.segment.start", 0.0), _num(sg[1], f"{name}.segment.end", 0.0)
        if b <= a:
            raise AudioInputError(f"{name}: empty segment {sg}")
        segs.append([a, b])
    return {"segments": segs, "speed": _num(ops.get("speed", 1.0), f"{name}.speed", 0.01, 100.0)}


# ---------------------------------------------------------------------------- GAP-59 SFX event anchors
# A point SFX may carry ``event``: the on-screen moment it belongs to, written as a position in the
# SOURCE clip (shot id + source sha256 + src_s) plus hit_s, the transient's offset inside the SFX file.
# At load the event is mapped through the CURRENT edit (trim, shot_ops segments and speed) and the SFX
# is placed so its transient lands on the event frame. GAP-23's time remap ignores retiming, so a
# slowed shot would leave the SFX early; an anchored SFX bypasses it. Fail closed on a sha mismatch
# (the clip changed: the anchor must be re-picked); an event whose source moment was cut away (or whose
# shot left the edit) removes the SFX with a reason, like GAP-23 does for a point SFX.
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def validate_sfx_event(ev: Any, cid: str) -> dict:
    if not isinstance(ev, dict):
        raise AudioInputError(f"sfx {cid}.event must be {{shot, clip_sha256, src_s, hit_s}}")
    shot = str(ev.get("shot") or "").strip()
    if not shot:
        raise AudioInputError(f"sfx {cid}.event needs the shot id")
    sha = str(ev.get("clip_sha256") or "").strip().lower()
    if not SHA256_HEX.match(sha):
        raise AudioInputError(f"sfx {cid}.event.clip_sha256 must be the source clip's sha256 (64 hex)")
    out = {"shot": shot, "clip_sha256": sha,
           "src_s": _num(ev.get("src_s"), f"sfx[{cid}].event.src_s", 0.0),
           "hit_s": _num(ev.get("hit_s", 0.0), f"sfx[{cid}].event.hit_s", 0.0, 30.0)}
    if ev.get("note"):
        out["note"] = str(ev["note"])
    return out


def source_to_screen(clip: dict, src_s: float, eps: float = 1e-6) -> float | None:
    """Offset (s) from the clip's start in the edit at which source time src_s plays, or None when that
    source moment is not on screen (trimmed away / in a cut window of shot_ops)."""
    ops = clip.get("ops")
    if ops:
        cum = 0.0
        for a, b in ops["segments"]:
            if a - eps <= src_s <= b + eps:
                off = (cum + min(max(src_s, a), b) - a) / float(ops["speed"])
                return off if off <= clip["seg_s"] + eps else None
            cum += b - a
        return None
    if src_s < clip["trim_in"] - eps or src_s > clip["trim_out"] + eps:
        return None
    return src_s - clip["trim_in"]


def resolve_sfx_events(plan: dict, cur_timeline: dict, fps: float | None = None) -> dict:
    """GAP-59: place every event-anchored point SFX on its event frame in the current edit. Returns a
    new plan; plan["timeline"]["events"] lists each placement, removals go to "removed_sfx"."""
    if not any(c.get("event") for c in plan.get("sfx") or []):
        return plan
    cur = validate_timeline(cur_timeline, "current edit")
    fps = float(fps or cur.get("fps") or 24.0)
    by_id = {c["id"]: c for c in cur["clips"]}
    out = json.loads(json.dumps(plan))
    tl = out.setdefault("timeline", {"moves": [], "removed_sfx": []})
    tl.setdefault("removed_sfx", [])
    events, kept, problems = [], [], []
    for c in out.get("sfx") or []:
        ev = c.get("event")
        if not ev:
            kept.append(c)
            continue
        clip = by_id.get(ev["shot"])
        if clip is None:
            tl["removed_sfx"].append({"id": c["id"], "at": c["at"], "reason": f"event shot {ev['shot']} left the edit"})
            continue
        if clip.get("sha256", "").lower() != ev["clip_sha256"]:
            problems.append(f"sfx {c['id']}: event anchored in {ev['shot']} clip {ev['clip_sha256'][:12]} but the "
                            f"edit plays {str(clip.get('sha256'))[:12] if clip.get('sha256') else 'an unknown clip'}; "
                            f"re-pick the event frame")
            continue
        off = source_to_screen(clip, ev["src_s"])
        if off is None:
            tl["removed_sfx"].append({"id": c["id"], "at": c["at"], "reason":
                                      f"event src {ev['src_s']}s cut away from {ev['shot']} (trim / shot_ops)"})
            continue
        event_t = clip["start"] + off
        at = round(event_t - ev["hit_s"], 6)
        if at < 0:
            problems.append(f"sfx {c['id']}: hit_s {ev['hit_s']} puts the SFX start before 0")
            continue
        events.append({"id": c["id"], "shot": ev["shot"], "src_s": ev["src_s"], "hit_s": ev["hit_s"],
                       "event_t": round(event_t, 6), "event_frame": int(round(event_t * fps)),
                       "from": c["at"], "at": at})
        kept.append({**c, "at": at})
    if problems:
        raise AudioInputError("; ".join(problems))
    out["sfx"] = kept
    tl["events"] = events
    return out


class TimelineMap:
    """Maps a time on the reference edit to the current edit: (t', status, shot_id).
    status: ok | tail (after the old end: same distance to the new end) | clamped (the position was
    trimmed away: moved to the nearest kept edge of the shot) | dropped (the shot left the edit: moved
    to the cut point, where the next surviving shot now starts)."""

    EPS = 1e-6

    def __init__(self, ref: dict, cur: dict):
        self.ref, self.cur = ref, cur
        self.cur_by_id = {c["id"]: c for c in cur["clips"]}

    def _cut_point(self, idx: int) -> float:
        for c in self.ref["clips"][idx + 1:]:
            if c["id"] in self.cur_by_id:
                return self.cur_by_id[c["id"]]["start"]
        return self.cur["total_s"]

    def map(self, t: float) -> tuple[float, str, str | None]:
        if t >= self.ref["total_s"] - self.EPS:
            return round(self.cur["total_s"] - (self.ref["total_s"] - t), 6), "tail", None
        idx = 0
        for i, c in enumerate(self.ref["clips"]):
            if c["start"] <= t + self.EPS:
                idx = i
        rc = self.ref["clips"][idx]
        src = rc["trim_in"] + (t - rc["start"])
        cc = self.cur_by_id.get(rc["id"])
        if cc is None:
            return round(self._cut_point(idx), 6), "dropped", rc["id"]
        if src < cc["trim_in"] - self.EPS:
            return round(cc["start"], 6), "clamped", rc["id"]
        if src > cc["trim_out"] + self.EPS:
            return round(cc["start"] + cc["seg_s"], 6), "clamped", rc["id"]
        return round(cc["start"] + (src - cc["trim_in"]), 6), "ok", rc["id"]

    def map_edge_relative(self, t: float) -> tuple[float, str, str | None]:
        """Like map(), but a position in a trimmed-away part keeps its DISTANCE to the new cut instead
        of snapping onto it (VO is not tied to a frame: a line that started 0.2 s after the old
        trim point starts 0.2 s after the new cut). status "edge" for such moves."""
        t2, st, sid = self.map(t)
        if st != "clamped":
            return t2, st, sid
        idx = max(i for i, c in enumerate(self.ref["clips"]) if c["start"] <= t + self.EPS)
        rc = self.ref["clips"][idx]
        cc = self.cur_by_id[rc["id"]]
        src = rc["trim_in"] + (t - rc["start"])
        if src > cc["trim_out"]:
            return round(cc["start"] + cc["seg_s"] + (src - cc["trim_out"]), 6), "edge", sid
        return round(cc["start"] - (cc["trim_in"] - src), 6), "edge", sid


def music_src_start(music: dict, total_s: float, src_len: float | None) -> float:
    """Source offset of the music bed. end_align: start late enough that the file's natural end lands
    on the picture end (never before the file start)."""
    if not music.get("end_align"):
        return float(music["src_start_s"])
    if src_len is None:
        raise AudioInputError("music.end_align needs the music file length")
    return round(max(0.0, float(src_len) - (float(total_s) - float(music["offset_s"]))), 6)


def remap_plan_to_edit(plan: dict, cur_timeline: dict) -> dict:
    """GAP-23: re-place a normalized plan's VO, SFX, music and protected windows on the current edit.
    VO in a dropped shot is an error (the line needs a human re-place); VO in a trimmed-away part keeps
    its distance to the new cut (placement is re-validated at mix); a point SFX whose moment was
    trimmed away or dropped is removed; beds (loop) keep the surviving part of their span. Returns a
    new plan with plan["timeline"] describing every move (recorded in the delivery manifest)."""
    ref = plan.get("timeline_ref")
    if not ref:
        return plan
    cur = validate_timeline(cur_timeline, "current edit")
    tm = TimelineMap(ref, cur)
    moves: list[dict] = []
    removed: list[dict] = []
    out = json.loads(json.dumps(plan))
    problems = []
    for ln in out["vo"]["lines"]:
        t, st, sid = tm.map_edge_relative(ln["at"])
        if st == "dropped":
            problems.append(f"VO line {ln['id']} at {ln['at']}s sits in dropped shot {sid}; re-place it")
            continue
        if abs(t - ln["at"]) > 1e-6 or st != "ok":
            moves.append({"id": ln["id"], "from": ln["at"], "to": t, "status": st, "shot": sid})
        ln["at"] = t
    if problems:
        raise PlacementError("; ".join(problems))
    kept = []
    for c in out.get("sfx", []):
        if c.get("event"):  # GAP-59: placed by resolve_sfx_events on its event frame, not by time
            kept.append(c)
            continue
        a, st_a, sid_a = tm.map(c["at"])
        if c.get("loop"):
            b, _, _ = tm.map(c["at"] + c["dur"])
            if b - a < 0.05:
                removed.append({"id": c["id"], "at": c["at"], "reason": f"span left the edit ({sid_a})"})
                continue
            new = {**c, "at": a, "dur": round(b - a, 6)}
        else:
            if st_a in ("dropped", "clamped"):
                removed.append({"id": c["id"], "at": c["at"], "reason": f"moment {st_a} with shot {sid_a}"})
                continue
            new = {**c, "at": a}
        if abs(new["at"] - c["at"]) > 1e-6 or abs(new["dur"] - c["dur"]) > 1e-6:
            moves.append({"id": c["id"], "from": c["at"], "to": new["at"], "dur": new["dur"], "status": st_a,
                          "shot": sid_a})
        kept.append(new)
    out["sfx"] = kept
    if out.get("music"):
        m = out["music"]
        t, st, sid = tm.map(m["offset_s"])
        if abs(t - m["offset_s"]) > 1e-6:
            moves.append({"id": m["id"], "from": m["offset_s"], "to": t, "status": st, "shot": sid})
        m["offset_s"] = t
    prot = []
    for a0, b0 in out.get("protected") or []:
        a, b = tm.map(a0)[0], tm.map(b0)[0]
        if b - a >= 0.05:
            prot.append([a, b])
    out["protected"] = prot
    dips = []
    for d in out.get("bed_dips") or []:  # GAP-34: dips follow the picture like protected windows
        a, b = tm.map(d["start"])[0], tm.map(d["end"])[0]
        if b - a >= 0.05:
            dips.append({**d, "start": a, "end": b})
    out["bed_dips"] = dips
    out["timeline"] = {"ref_total_s": ref["total_s"], "cur_total_s": cur["total_s"],
                       "dropped_shots": [c["id"] for c in ref["clips"] if c["id"] not in tm.cur_by_id],
                       "moves": moves, "removed_sfx": removed}
    return out


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


DUCK_MODES = ("activity", "window")


def _duck_mode(v, cid: str) -> str:
    """sfx duck_mode: "activity" follows the VO signal (default); "window" holds for each line."""
    if v not in DUCK_MODES:
        raise AudioInputError(f"sfx {cid}.duck_mode must be one of {DUCK_MODES}")
    return v


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


async def decode(path: str, n: int | None = None, start_s: float = 0.0, loop: bool = False,
                 afilter: str | None = None) -> np.ndarray:
    """Decode to float32 stereo at SR; pad/trim to n samples when given (optional ffmpeg -af)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    if start_s:
        cmd += ["-ss", f"{start_s:.4f}"]
    cmd += ["-i", path]
    if n is not None:
        cmd += ["-t", f"{n / SR:.6f}"]
    if afilter:
        cmd += ["-af", afilter]
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


MAX_LOUDNESS_RESTORE_LU = 3.0  # GAP-31: largest loudness miss the codec guard restores


async def codec_safe_pcm(mix_wav: str, audio_kbps: int, loud: dict, work: str, tag: str,
                         heartbeat: "Heartbeat | None" = None) -> tuple[str, dict]:
    """GAP-20: the TP guard above only checks the PCM mix, but AAC re-encoding overshoots true peak
    (~+0.9 dB at 192 kbps, ~+2.5 dB at 64 kbps on ep05full), so the delivered files failed a -2.0 dBTP
    ceiling the PCM met. Test-encode the mix at the deliverable's AAC bitrate (the native encoder is
    deterministic); if the encoded TP is over ceiling-0.1, pre-limit the PCM (alimiter) with makeup
    gain back to the loudness target, stepping the limiter down 1 dB at a time, mildest pass wins.
    The same steps run when the encode meets the TP ceiling but misses the loudness band (GAP-31).
    Returns (pcm path to encode from, info). If nothing passes, the mildest-overshoot candidate is
    returned with ok=False and delivery QC reports the failure (never silently relaxed).
    GAP-38: up to 16 limiter tries of several ffmpeg passes each can outlast the activity's 2-minute
    heartbeat timeout (ep05v2 g3r2: mix_and_deliver cancelled mid-guard), so every pass heartbeats."""
    beat = heartbeat or (lambda _d: None)
    ceiling, target, tol = loud["tp_ceiling_dbtp"], loud["target_lufs"], loud["tolerance_lu"]
    test = str(Path(work) / f"tpguard_{tag}.m4a")

    async def encoded(src: str) -> dict:
        await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src, "-c:a", "aac", "-b:a",
                       f"{int(audio_kbps)}k", "-ar", str(SR), test], f"TP guard test encode {tag}")
        return await measure_loudness(test, loud)

    def passes(m: dict) -> bool:
        return m["true_peak_dbtp"] <= ceiling - 0.1 and abs(m["integrated_lufs"] - target) <= tol - 0.1

    beat({"stage": "codec_tp_guard", "tag": tag, "try": 0})
    m0 = await encoded(mix_wav)
    info = {"audio_kbps": int(audio_kbps), "encoded_tp_unlimited": m0["true_peak_dbtp"], "limit_dbfs": None,
            "makeup_db": 0.0, "encoded": m0, "ok": m0["true_peak_dbtp"] <= ceiling - 0.1, "tries": []}
    # GAP-31: loudnorm's dynamic mode under-shoots the target when its own TP limit caps the gain
    # (ep05v2: -14.0 LUFS at -2.1 dBTP, target -12.6) and that file used to ship unguarded. A small
    # miss like that is restored by the limiter + makeup steps below; a gross level error (more than
    # MAX_LOUDNESS_RESTORE_LU off) is a mix problem the guard does not paper over: it is left to
    # delivery QC, as before.
    miss = abs(m0["integrated_lufs"] - target)
    if info["ok"] and (passes(m0) or miss > MAX_LOUDNESS_RESTORE_LU):
        return mix_wav, info
    info["ok"] = False
    pcm_tp = (await measure_loudness(mix_wav, loud))["true_peak_dbtp"]
    best = None
    # aim at the target first; a low-bitrate encode may need the low half of the tolerance band
    plan_steps = [(target, st) for st in range(1, 9)] + [(round(target - tol + 0.15, 2), st) for st in range(1, 9)]
    for aim, step in plan_steps:
        beat({"stage": "codec_tp_guard", "tag": tag, "try": len(info["tries"]) + 1, "aim_lufs": aim, "step_db": step})
        limit = round(max(-24.0, min(pcm_tp, ceiling) - step), 2)  # alimiter floor is 0.0625 (-24 dB)
        lim_wav = str(Path(work) / f"tpguard_{tag}_lim.wav")
        await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", mix_wav, "-af",
                       f"alimiter=limit={db_to_lin(limit):.6f}:attack=1:release=50:level=disabled",
                       "-c:a", "pcm_s24le", lim_wav], f"TP guard limiter {tag}")
        makeup = round(aim - (await measure_loudness(lim_wav, loud))["integrated_lufs"], 2)
        cand = str(Path(work) / f"tpguard_{tag}_{len(info['tries'])}.wav")
        await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", lim_wav, "-af", f"volume={makeup}dB",
                       "-c:a", "pcm_s24le", cand], f"TP guard makeup {tag}")
        m = await encoded(cand)
        info["tries"].append({"aim_lufs": aim, "limit_dbfs": limit, "makeup_db": makeup, "encoded_tp": m["true_peak_dbtp"],
                              "encoded_lufs": m["integrated_lufs"]})
        if best is None or m["true_peak_dbtp"] < best[2]["true_peak_dbtp"]:
            best = (cand, (limit, makeup), m)
        if passes(m):
            info.update(limit_dbfs=limit, makeup_db=makeup, aim_lufs=aim, encoded=m, ok=True)
            return cand, info
    cand, (limit, makeup), m = best
    info.update(limit_dbfs=limit, makeup_db=makeup, encoded=m, ok=False)
    return cand, info


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
    """Split subtitle text into at most two balanced lines.

    An explicit line break in the plan text ("\n") is honoured when it gives at most two lines;
    otherwise the text is re-balanced at a space."""
    raw = str(text)
    if "\n" in raw:
        parts = [" ".join(x.split()) for x in raw.split("\n") if x.strip()]
        if 1 <= len(parts) <= 2:
            return parts
    text = " ".join(raw.split())
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


def cue_dur(ln: dict) -> float:
    """GAP-35: a line's subtitle length: its subtitle_dur when set (never past the line), else the line."""
    sd = ln.get("subtitle_dur")
    return float(ln["dur"]) if sd is None else min(float(ln["dur"]), float(sd))


def build_cues(placed: list[dict], max_chars: int) -> list[dict]:
    cues = []
    for ln in sorted(placed, key=lambda x: x["at"]):
        if not ln.get("subtitle"):
            continue
        cues.append({"id": ln["id"], "start": round(ln["at"], 3), "end": round(ln["at"] + cue_dur(ln), 3),
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
        + (subs["ass_style"].strip() + "\n\n[Events]\n" if subs.get("ass_style") else
           f"Style: Default,{subs['font']},{int(subs['fontsize'])},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
           f"0,0,0,0,100,100,0,0,1,4,1,2,80,80,{int(subs['margin_v'])},1\n\n[Events]\n")
        + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    style = subs["ass_style"].split(":", 1)[1].split(",")[0].strip() if subs.get("ass_style") else "Default"
    # Optional whole-line fade (ms, in/out) and edge blur (soft shadow) as ASS override tags.
    tags = ""
    if subs.get("fade_ms"):
        f = int(_num(subs["fade_ms"], "subtitles.fade_ms", 0, 2000))
        tags += f"\\fad({f},{f})"
    if subs.get("blur"):
        tags += f"\\blur{_num(subs['blur'], 'subtitles.blur', 0, 10):g}"
    prefix = "{" + tags + "}" if tags else ""
    rows = [f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},{style},,0,0,0,,"
            + prefix + "\\N".join(_ass_escape(x) for x in c["lines"]) for c in cues]
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
        old = json.loads(p.read_text(encoding="utf-8"))
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


async def apply_atempo(src: str, dst: str, tempo: float) -> None:
    """VO tempo post step: ffmpeg -af atempo=<tempo> on one line (before placement)."""
    await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src, "-af", f"atempo={tempo:g}",
                   "-ar", str(SR), "-c:a", "pcm_s24le", dst], "VO atempo")


# ---------------------------------------------------------------- GAP-60 picture sync guard
# r11: a loaded worker produced picture_logo encodes whose frames 591-596 froze on mute frame 590
# (nondeterministic; frame count and CFR were fine, so GAP-55 could not see it). Every re-encoded
# picture deliverable is now compared frame by frame against the mute assembly (grey, 135x240,
# subtitle/logo regions excluded) and re-encoded or failed closed when a frame shows a neighbour.
PICTURE_SYNC_W, PICTURE_SYNC_H = 135, 240
PICTURE_SYNC_RADIUS = 3
PICTURE_SYNC_TOL = 0.1        # mean |grey| a neighbour must win by
PICTURE_SYNC_MIN_STEP = 0.5   # neighbour must really differ from the expected frame
PICTURE_SYNC_RATIO = 1.5      # ...and be this many times closer (codec lag after cuts is ~1.1x)
PICTURE_SYNC_TRIES = 3
PICTURE_SYNC_SUBS_CROP = 0.55   # burned cues sit in the bottom quarter (margin_v 240 of 1920)


class PictureSyncError(RuntimeError):
    pass


async def grey_frames(path: str | Path, upto: int, crop_top_frac: float | None = None) -> np.ndarray:
    vf = (f"crop=iw:trunc(ih*{crop_top_frac:.4f}/2)*2:0:0," if crop_top_frac else "")
    h = int(round(PICTURE_SYNC_H * (crop_top_frac or 1.0)))
    vf += f"scale={PICTURE_SYNC_W}:{h},format=gray"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-frames:v", str(upto),
        "-vf", vf, "-f", "rawvideo", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise PictureSyncError(f"cannot decode {path} for the picture sync check: {err.decode()[-300:]}")
    return np.frombuffer(out, np.uint8).reshape(-1, h, PICTURE_SYNC_W)


def misaligned_frames(ref: np.ndarray, pic: np.ndarray, upto: int | None = None,
                      radius: int = PICTURE_SYNC_RADIUS, tol: float = PICTURE_SYNC_TOL,
                      min_step: float = PICTURE_SYNC_MIN_STEP,
                      ratio: float = PICTURE_SYNC_RATIO) -> list[tuple[int, int]]:
    """Frames i (< upto) whose picture is clearly closer to ref[j], j != i within radius, than to
    ref[i] (a freeze, drop or shift). Returns [(i, j)]; a frame-count mismatch is reported as
    (len(pic), -1)."""
    n = min(len(ref), upto if upto is not None else len(ref))
    if len(pic) < n:
        return [(len(pic), -1)]
    bad = []
    for i in range(n):
        p = pic[i].astype(np.int16)
        own = float(np.abs(p - ref[i]).mean())
        best_j, best = i, own
        for j in range(max(0, i - radius), min(len(ref), i + radius + 1)):
            if j == i:
                continue
            d = float(np.abs(p - ref[j]).mean())
            if d + tol < best and d * ratio < own and float(np.abs(ref[i].astype(np.int16) - ref[j]).mean()) >= min_step:
                best_j, best = j, d
        if best_j != i:
            bad.append((i, best_j))
    return bad


async def encode_picture_checked(encode: Callable[[], Any], ref: str | Path, out: str | Path,
                                 upto: int, what: str, crop_top_frac: float | None = None,
                                 ref_frames: np.ndarray | None = None,
                                 tries: int = PICTURE_SYNC_TRIES, heartbeat=None) -> dict:
    """Run encode() (writes out) until its picture matches ref frame for frame, up to tries."""
    if ref_frames is None:
        ref_frames = await grey_frames(ref, upto, crop_top_frac)
    log = []
    for attempt in range(1, tries + 1):
        await encode()
        bad = misaligned_frames(ref_frames, await grey_frames(out, upto, crop_top_frac), upto)
        log.append({"attempt": attempt, "misaligned": len(bad), "frames": [list(b) for b in bad[:12]]})
        if heartbeat:
            heartbeat({"stage": "picture_sync", "what": what, "attempt": attempt, "misaligned": len(bad)})
        if not bad:
            return {"what": what, "checked_frames": int(upto), "attempts": log}
    raise PictureSyncError(f"GAP-60: {what} picture is out of step with the mute assembly after "
                           f"{tries} encodes (frame, shows): {log[-1]['frames']}")


def logo_start_s(logo: dict, total_s: float, placed: list[dict] | None = None) -> float:
    """Logo fade start: after_vo_line end + after_vo_gap_s when set (and placed), else the
    Ep01 template total - visible_s."""
    if logo.get("after_vo_line"):
        line = next((p for p in placed or [] if p["id"] == logo["after_vo_line"]), None)
        if line is None:
            raise AudioInputError(f"logo.after_vo_line {logo['after_vo_line']} is not a VO line id")
        return round(line["at"] + line["dur"] + logo.get("after_vo_gap_s", 0.6), 4)
    return round(max(0.0, total_s - logo["visible_s"]), 4)


async def overlay_logo(mute: str, logo: dict, total_s: float, out: str, master: dict,
                       frames: int | None = None, start: float | None = None) -> None:
    """Ep01 ending template: logo scaled to width_frac (or width_px) of the frame, centred, alpha
    fade-in over fade_in_s from ``start`` (default: the last visible_s of the picture). Optional
    darken_lower: the lower frac of the frame darkens by amount, fading in with the logo."""
    info = await probe(mute)
    v = next(s for s in info.get("streams", []) if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    rate = video_rate(info)
    if frames is None:
        frames = await count_frames(mute)
    if start is None:
        start = max(0.0, total_s - logo["visible_s"])
    lw = int(logo["width_px"]) // 2 * 2 if logo.get("width_px") else int(w * logo["width_frac"]) // 2 * 2
    base = "[0:v]"
    pre = ""
    dk = logo.get("darken_lower")
    if dk and dk["amount"] > 0:
        dh = int(h * dk["frac"]) // 2 * 2
        pre = (f"color=c=black:s={w}x{dh}:r=24:d={total_s:.4f},format=rgba,"
               f"colorchannelmixer=aa={dk['amount']:.3f},fade=t=in:st={start:.4f}:d={logo['fade_in_s']:.4f}:alpha=1[dk];"
               f"[0:v][dk]overlay=0:H-h:shortest=1:enable='gte(t,{start:.4f})'[bg];")
        base = "[bg]"
    graph = (pre + f"[1:v]scale={lw}:-1,format=rgba,fade=t=in:st={start:.4f}:d={logo['fade_in_s']:.4f}:alpha=1[lg];"
             f"{base}[lg]overlay=(W-w)/2:max(0\\,min(H-h\\,H*{logo.get('y_frac', 0.5):.4f}-h/2)):shortest=1:enable='gte(t,{start:.4f})',format=yuv420p[v]")
    await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", mute, "-framerate", rate, "-loop", "1",
                   # GAP-55: logo on the picture's frame grid and 1 s longer, so shortest=1 can never
                   # end the overlay before the picture's last frame (-frames:v / -t cap the output)
                   "-t", f"{total_s + 1.0:.4f}", "-i", logo["path"], "-filter_complex", graph,
                   "-map", "[v]", "-an", "-c:v", "libx264",
                   "-preset", str(master["preset"]), "-crf", str(master["crf"]), "-pix_fmt", "yuv420p",
                   *cfr_args(rate, frames), "-t", f"{total_s:.4f}", out], "logo overlay")


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
    """Two-pass x264 phone encode from the picture + PCM mix (never re-encodes the AAC master).
    GAP-55: ``phone["cfr"]`` pins constant frame rate + the source frame count (see cfr_args)."""
    # subtitles burn at full picture size first, then the downscale (GAP-19), so they scale with it
    filters = ([_ass_filter(ass, fonts_dir)] if ass else []) + (
        [f"scale={phone['width']}:{phone['height']}:flags=lanczos"] if phone.get("width") else [])
    vf = ["-vf", ",".join(filters)] if filters else []
    common = ["-c:v", "libx264", "-preset", str(phone["preset"]), "-b:v", f"{kbps}k", "-pix_fmt", "yuv420p",
              "-passlogfile", "phone2pass", *(phone.get("cfr") or [])]
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
    if plan.get("logo"):
        inputs.append(plan["logo"]["path"])
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
        # GAP-55: every picture deliverable must carry exactly the mute assembly's frames at CFR.
        src_frames = await count_frames(mute)
        src_rate = video_rate(await probe(mute))
        cfr = cfr_args(src_rate, src_frames)
        logo_info = None
        # GAP-60: every re-encoded picture is checked frame by frame against the mute assembly
        # (frames before the logo window; subtitle area cropped off for the burned encodes).
        ref_mute = str(mute)
        sync_upto = src_frames
        if plan.get("logo"):
            fps_f = float(Fraction(src_rate))
            sync_upto = min(src_frames, int(math.floor(max(0.0, total_s - plan["logo"]["visible_s"]) * fps_f)))
        picture_sync = []
        sync_ref_full = sync_ref_top = None
        if plan.get("logo"):
            sync_ref_full = await grey_frames(ref_mute, sync_upto)
        # ---- VO: tempo post step, durations, pickup match, placement
        tempo_lines = []
        for ln in lines:
            if ln.get("atempo") and abs(float(ln["atempo"]) - 1.0) > 1e-9:
                out = str(work / f"vo_{ln['id']}_atempo.wav")
                await apply_atempo(ln["path"], out, float(ln["atempo"]))
                tempo_lines.append({**ln, "source_path": ln["path"], "path": out})
            else:
                tempo_lines.append(ln)
        lines = tempo_lines
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
        for ln in placed:
            if ln.get("subtitle_dur") is not None:
                # GAP-35: a shortened cue must still cover the spoken words of the (tempo-adjusted) file
                mask = activity_mask(await decode(ln["path"]))
                idx = np.flatnonzero(mask)
                speech_end = float(idx[-1] + 1) / SR if idx.size else 0.0
                ln["speech_end_s"] = round(speech_end, 3)
                if cue_dur(ln) < speech_end - SUBTITLE_SPEECH_SLACK_S:
                    raise AudioInputError(
                        f"{ln['id']}.subtitle_dur {ln['subtitle_dur']}s ends before the speech "
                        f"({speech_end:.2f}s into the line)")
        if plan.get("logo"):
            heartbeat({"stage": "logo"})
            pic = str(work / "picture_logo.mp4")
            start = logo_start_s(plan["logo"], total_s, placed)

            async def overlay_counted() -> None:
                await overlay_logo(ref_mute, plan["logo"], total_s, pic, plan["master"], frames=src_frames, start=start)
                got = await count_frames(pic)
                if got != src_frames:
                    raise RuntimeError(f"logo overlay frame count {got} != mute assembly {src_frames} (GAP-55)")

            picture_sync.append(await encode_picture_checked(
                overlay_counted, ref_mute, pic, sync_upto, "picture_logo", ref_frames=sync_ref_full,
                heartbeat=heartbeat))
            logo_info = {**plan["logo"], "start_s": start,
                         "hold_s": round(total_s - start - plan["logo"]["fade_in_s"], 4)}
            mute = Path(pic)
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
                src_start = music_src_start(
                    m, total_s, await probe_duration(m["path"]) if m.get("end_align") else None)
                bed = await decode(m["path"], seg_n, src_start, loop=m["loop"])
                base = rms_db(bed)
                gain = m["level_db"] - base if base > -100 else 0.0
                bed = fade(bed * db_to_lin(gain), m["fade_in_s"], m["fade_out_s"])
                env = duck_envelope(n, [(p["at"], p["at"] + p["dur"]) for p in placed], m["duck_db"])
                music[off:] = bed * env[off:, None]
                music_info = {"gain_db": round(gain, 2), "duck_db": m["duck_db"],
                              "offset_s": m["offset_s"], "src_start_s": round(src_start, 4),
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
            if c["duck_db"] < 0 and c.get("duck_mode") == "window":
                # GAP-30: hold the duck for the whole line window (pauses inside a line stay
                # ducked), the same curve the music bed uses
                env = duck_envelope(n, [(p["at"], p["at"] + p["dur"]) for p in placed], c["duck_db"])
                clip = clip * env[start:start + cn, None]
            elif c["duck_db"] < 0:
                depth = 1.0 - db_to_lin(c["duck_db"])
                clip = clip * (1.0 - depth * act[start:start + cn])[:, None]
            sfx[start:start + cn] += clip
            sfx_info.append({"id": c["id"], "gain_db": round(g, 2), "ducked": c["duck_db"] < 0,
                             "duck_mode": c.get("duck_mode", "activity")})

        dips = plan.get("bed_dips") or []
        if dips:
            # GAP-34: pull reviewer-flagged bed bursts down (music + SFX, VO untouched)
            denv = dip_envelope(n, dips)[:, None]
            music = (music * denv).astype(np.float32)
            sfx = (sfx * denv).astype(np.float32)
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
        # GAP-20: per-bitrate codec TP guard (AAC overshoot); the published mix.wav stays untouched.
        heartbeat({"stage": "codec_tp_guard"})
        master_pcm, master_tp = await codec_safe_pcm(mix_wav, int(plan["master"]["audio_kbps"]),
                                                     plan["loudness"], str(work), "master", heartbeat=heartbeat)
        phone_pcm, phone_tp = await codec_safe_pcm(mix_wav, int(plan["phone"]["audio_kbps"]),
                                                   plan["loudness"], str(work), "phone", heartbeat=heartbeat)
        await run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(mute), "-i", master_pcm,
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", akbps,
                       "-ar", str(SR), "-t", f"{total_s:.4f}", "-movflags", "+faststart", nosubs], "mux nosubs")
        master = None
        if burn:
            heartbeat({"stage": "burn_subtitles", "cues": len(cues)})
            master = str(work / Path(paths["master_subs"]).name)
            sync_ref_top = await grey_frames(ref_mute, sync_upto, PICTURE_SYNC_SUBS_CROP)
            picture_sync.append(await encode_picture_checked(
                lambda: run_cmd(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(mute), "-i", master_pcm,
                           "-map", "0:v:0", "-map", "1:a:0", "-vf", _ass_filter(ass, subs.get("fonts_dir")),
                           "-c:v", "libx264", "-preset", str(plan["master"]["preset"]),
                           "-crf", str(plan["master"]["crf"]), "-pix_fmt", "yuv420p", *cfr, "-c:a", "aac",
                           "-b:a", akbps, "-ar", str(SR), "-t", f"{total_s:.4f}", "-movflags", "+faststart",
                           master], "burn subtitles", cwd=str(work)),
                ref_mute, master, sync_upto, "subs", crop_top_frac=PICTURE_SYNC_SUBS_CROP,
                ref_frames=sync_ref_top, heartbeat=heartbeat))

        heartbeat({"stage": "phone_encode"})
        phone_cfg = {**plan["phone"], "cfr": cfr}  # GAP-55
        max_bytes = int(phone_cfg["max_mb"] * 1_000_000)
        phone = str(work / Path(paths["phone"]).name)
        kbps = _phone_video_kbps(phone_cfg, total_s, max_bytes)
        attempts = []
        if sync_ref_top is None:
            sync_ref_top = await grey_frames(ref_mute, sync_upto, PICTURE_SYNC_SUBS_CROP)

        async def phone_checked(kb: int) -> None:
            picture_sync.append(await encode_picture_checked(
                lambda: encode_phone(str(mute), phone_pcm, ass if burn else None, phone, total_s, kb, phone_cfg,
                                     str(work), subs.get("fonts_dir")),
                ref_mute, phone, sync_upto, f"phone@{kb}k", crop_top_frac=PICTURE_SYNC_SUBS_CROP,
                ref_frames=sync_ref_top, heartbeat=heartbeat))

        await phone_checked(kbps)
        size = Path(phone).stat().st_size
        attempts.append({"video_kbps": kbps, "size_bytes": size})
        if size > max_bytes:
            # One re-encode at a bitrate scaled to the measured overshoot.
            kbps = max(int(phone_cfg["min_video_kbps"]), int(kbps * max_bytes / size * 0.9))
            heartbeat({"stage": "phone_reencode", "video_kbps": kbps})
            await phone_checked(kbps)
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
            "source_mute_path": str(mute_path), "picture_frames": src_frames, "picture_fps": src_rate,
            "inputs": {p: h for p, h in hashes.items()},
            "vo_lines": [{**{k: ln[k] for k in ("id", "at", "dur", "gain_db", "path")},
                          "atempo": ln.get("atempo"), "source_path": ln.get("source_path", ln["path"]),
                          "subtitle_dur": ln.get("subtitle_dur"), "speech_end_s": ln.get("speech_end_s")}
                         for ln in placed],
            "logo": logo_info,
            "placement": placement, "min_vo_gap_s": plan["min_vo_gap_s"],
            "music": music_info, "sfx": sfx_info, "bed_dips": dips,
            "loudness_target": plan["loudness"], "loudnorm": ln_info, "mix_loudness": mix_meas,
            "tp_safety_trim_db": safety_db, "codec_tp_guard": {"master": master_tp, "phone": phone_tp},
            "cues": cues, "subtitles_burned": burn,
            "phone": {"max_bytes": max_bytes, "attempts": attempts, "size_bytes": size,
                      "within_limit": size <= max_bytes,
                      "width": phone_cfg.get("width"), "height": phone_cfg.get("height")},
            "outputs": outputs, "paths": paths,
            "timeline": plan.get("timeline"),
            "picture_sync": {"reference": ref_mute, "checked_frames": int(sync_upto), "encodes": picture_sync},
        }
        tmp_manifest = work / "manifest.json"
        tmp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
        # GAP-55: frame count must equal the mute assembly's (a vfr drop keeps the duration).
        want_frames = manifest.get("picture_frames")
        if not want_frames:
            num, _, den = video_rate(info).partition("/")
            want_frames = int(round(pic * float(num) / float(den or 1)))
        frames = await count_frames(path) if v else 0
        add(f"{name}_frame_count", frames == int(want_frames), frames, int(want_frames), path)
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
        if phone.get("width"):
            pv = [s for s in (await probe(ppath)).get("streams", []) if s.get("codec_type") == "video"]
            dims = f"{pv[0].get('width')}x{pv[0].get('height')}" if pv else None
            add("phone_dims", dims == f"{phone['width']}x{phone['height']}", dims,
                f"{phone['width']}x{phone['height']}", ppath)
    cues = manifest.get("cues") or []
    if cues:
        ass = (outs.get("ass") or {}).get("path")
        found = parse_ass_cues(ass) if ass and Path(ass).is_file() else []
        add("subtitle_cue_count", len(found) == len(cues), len(found), len(cues), ass)
        # Timing of the WRITTEN subtitle file vs where each VO line was actually placed.
        lines = {ln["id"]: ln for ln in manifest.get("vo_lines", [])}
        devs = [max(abs(f[0] - lines[c["id"]]["at"]), abs(f[1] - (lines[c["id"]]["at"] + cue_dur(lines[c["id"]]))))
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
