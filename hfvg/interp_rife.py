"""GAP-61 (ep05v2 r12): real motion-compensated frame interpolation for slowed shots (RIFE).

Reviewer/Studio 2026-10-28: slowed shots still stutter with ffmpeg ``minterpolate`` (block-matching
artefacts, and its tail holds the last 3-4 frames). ``interp: "rife"`` in a shot's ``shot_ops`` renders
the retime with RIFE (rife-ncnn-vulkan, a free local binary; CPU via Mesa lavapipe works, slowly).

Rules
  - Each segment is interpolated SEPARATELY (a segment join is a cut in the source; morphing across it
    would invent frames), with the output frames shared out over the segments in proportion to their
    source length (cumulative rounding, so the total is exact).
  - Output frame i of a segment shows source time a + i * step (step = source seconds per output
    frame * source fps), interpolated between the two neighbouring source frames; one source frame past
    the segment end is fed in when the clip has it, so the segment never ends on held frames.
  - Fails CLOSED: no binary / model -> RifeUnavailable (never a silent fallback to another interp).
  - Cached by (clip sha256, segments, frames, fps, model, version) under RIFE_CACHE_DIR as a near-lossless
    mezzanine, so a preview pre-warms the assembly and a timed-out activity attempt resumes.
  - GAP-81: fresh renders and cache hits are checked frame by frame against their source neighbours
    (RifeBadOutput, fail closed; a bad cache entry is moved to <cache>/bad and re-rendered).
  - Long runs heartbeat (Temporal heartbeat timeout is 2 min): the injected callback around renders,
    hfvg.subproc while the frame check runs.

Env: RIFE_NCNN_BIN (binary path), RIFE_MODEL_DIR (default <bin dir>/rife-v4.6), RIFE_THREADS
(default 1:2:2), RIFE_CACHE_DIR (default <EPISODES_DIR>/../cache/rife).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from hfvg import subproc
from hfvg.paths import episodes_dir

RIFE_FORMAT_VERSION = 1
HEARTBEAT_EVERY_S = 20.0


class RifeUnavailable(RuntimeError):
    pass


class RifeBadOutput(RuntimeError):
    """GAP-81 (ep05v2 r14): a RIFE render (fresh or cached) whose frames are not plausible in-betweens of
    their source frames. After the 2026-10-31 reboot the Vulkan ICD was gone: rife-ncnn-vulkan printed
    "vkCreateInstance failed -9", still exited 0 and wrote smeared/uninitialised frames, which were cached
    and assembled (V17/V18/V20/V23/V24/V26 in the interim v12 mute). Fail closed instead."""


# GAP-81 thresholds on 90x160 grey thumbnails (0-255): an output frame must be within
# max(CHECK_FLOOR, CHECK_PAIR_FACTOR * MAD(neighbour pair) + CHECK_PAIR_PAD) of its nearer neighbour.
# Measured: good renders 1.0-1.5 (pair 0.2-1.5); the broken lavapipe-less renders 20.9-21.4.
CHECK_SIZE = (90, 160)
CHECK_FLOOR = 6.0
CHECK_PAIR_FACTOR = 2.0
CHECK_PAIR_PAD = 3.0


def _thumb(path: Path):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize(CHECK_SIZE, Image.BILINEAR), dtype=np.float32)


def check_interp_frames(outs: list[Path], ins: list[Path], step: float, what: str, every: int = 1) -> dict[str, Any]:
    """GAP-81: every ``every``-th output i (time i*step in input frames) must look like an in-between of
    inputs floor(t) and floor(t)+1 (MAD to the nearer one within the bound). Raises RifeBadOutput."""
    if not outs or not ins:
        raise RifeBadOutput(f"{what}: nothing to check ({len(outs)} outputs, {len(ins)} inputs)")
    cache: dict[int, Any] = {}

    def src(j: int):
        j = max(0, min(len(ins) - 1, j))
        if j not in cache:
            cache[j] = _thumb(ins[j])
        return cache[j]

    worst = (0.0, -1, 0.0)
    for i in range(0, len(outs), max(1, int(every))):
        t = i * float(step)
        j = int(math.floor(t + 1e-9))
        a, b = src(j), src(j + 1)
        o = _thumb(outs[i])
        d = float(min(abs(o - a).mean(), abs(o - b).mean()))
        pair = float(abs(a - b).mean())
        bound = max(CHECK_FLOOR, CHECK_PAIR_FACTOR * pair + CHECK_PAIR_PAD)
        if d > bound:
            raise RifeBadOutput(f"{what}: output frame {i} (source t={t:.2f}) is not an in-between of source "
                                f"frames {j}/{j + 1}: MAD {d:.1f} > {bound:.1f} (pair {pair:.1f}); broken RIFE "
                                f"render (Vulkan ICD missing? 'vkCreateInstance failed')")
        if d - bound > worst[0] - worst[2] or worst[1] < 0:
            worst = (d, i, bound)
    return {"checked": len(range(0, len(outs), max(1, int(every)))), "worst_mad": round(worst[0], 2),
            "worst_frame": worst[1], "bound": round(worst[2], 2)}


def rife_config() -> dict[str, str]:
    """Binary/model/threads from env; raises RifeUnavailable if not usable (fail closed)."""
    binary = os.getenv("RIFE_NCNN_BIN", "").strip()
    if not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise RifeUnavailable(f"interp 'rife' needs RIFE_NCNN_BIN (an executable rife-ncnn-vulkan); got {binary!r}")
    model = os.getenv("RIFE_MODEL_DIR", "").strip() or str(Path(binary).parent / "rife-v4.6")
    if not (Path(model).is_dir() and any(Path(model).glob("*.param"))):
        raise RifeUnavailable(f"RIFE model dir missing or empty: {model!r}")
    return {"bin": binary, "model": model, "threads": os.getenv("RIFE_THREADS", "1:2:2").strip() or "1:2:2"}


def cache_dir() -> Path:
    d = os.getenv("RIFE_CACHE_DIR", "").strip()
    if d:
        return Path(d)
    return episodes_dir().resolve().parent / "cache" / "rife"


def allocate_frames(segments: list[list[float]], total_frames: int) -> list[int]:
    """Output frames per segment, proportional to source length, cumulative rounding (sum exact).
    Every segment gets at least 1 frame (raises if total_frames < number of segments)."""
    if total_frames < len(segments) or not segments:
        raise ValueError(f"{total_frames} output frames cannot cover {len(segments)} segment(s)")
    lens = [float(b) - float(a) for a, b in segments]
    src = sum(lens)
    out, acc, done = [], 0.0, 0
    for i, L in enumerate(lens):
        acc += L
        upto = total_frames if i == len(lens) - 1 else round(acc / src * total_frames)
        n = max(1, upto - done)
        out.append(n)
        done += n
    # repair (only if a min-1 bump overshot): take frames back from the largest segments
    while sum(out) > total_frames:
        j = max(range(len(out)), key=lambda k: out[k])
        out[j] -= 1
    return out


def source_frame_range(a: float, b: float, src_fps: float, src_frames: int) -> tuple[int, int]:
    """[first, last_exclusive) source frame indices of segment [a, b) (clamped to the clip)."""
    first = max(0, min(src_frames - 1, int(round(a * src_fps))))
    last = max(first + 1, min(src_frames, int(round(b * src_fps))))
    return first, last


def rife_request(m_frames: int, n_out: int, has_next: bool) -> tuple[int, int]:
    """(inputs to feed K, outputs to ask for n) so that RIFE's step K/n matches the wanted step
    m/n_out (rife-ncnn -n maps output i to input time i*K/n) and the first n_out outputs stay
    inside the segment. With a next frame available K = m + 1 (interpolate up to the boundary)."""
    if n_out <= 0 or m_frames <= 0:
        raise ValueError("need at least one source and one output frame")
    if has_next:
        k = m_frames + 1
        return k, max(n_out, int(round(k * n_out / m_frames)))
    # GAP-71 (ep05v2 r12): a segment that ends at the clip end has no frame past it. rife-ncnn HOLDS the
    # last input for any output time past it, so the old n = round(k*n_out/m) put the last kept output(s)
    # at t > k-1 = a duplicated frame (V11 v10: one held frame right before the V11->V12 cut). Ask for
    # enough outputs that output n_out-1 lands at or before the last input: (n_out-1)*k/n <= k-1.
    k = m_frames
    if k < 2:
        return k, n_out          # a single source frame: nothing to interpolate (a hold either way)
    n = max(n_out, -(-((n_out - 1) * k) // (k - 1)))
    return k, n


def cache_key(clip_sha256: str, segments: list[list[float]], frames: int, fps: int, model: str,
              clip_end: bool = False) -> str:
    """``clip_end`` (GAP-71): some segment reaches the clip end; those renders changed, so they get a new
    key. Every other key is byte-identical to before (existing cache entries stay valid)."""
    obj = {"v": RIFE_FORMAT_VERSION, "sha": clip_sha256, "segs": [[round(float(a), 6), round(float(b), 6)] for a, b in segments],
           "frames": int(frames), "fps": int(fps), "model": Path(model).name}
    if clip_end:
        obj["clip_end"] = "gap71"
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


Runner = Callable[[list[str], str], Awaitable[str]]
Beat = Callable[[dict[str, Any]], None]


async def _run_with_heartbeat(run: Runner, cmd: list[str], what: str, beat: Beat | None, info: dict) -> str:
    task = asyncio.ensure_future(run(cmd, what))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S)
            if done:
                return task.result()
            if beat:
                beat({"stage": "rife", **info})
    finally:
        # a cancelled attempt stops the render too: run() kills its child only when it is cancelled
        if not task.done():
            task.cancel()
            await asyncio.wait({task})


async def _probe_stream(run: Runner, path: Path) -> tuple[float, int]:
    out = await run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                     "stream=r_frame_rate,nb_read_frames", "-of", "json", str(path)], f"ffprobe {path.name}")
    st = json.loads(out)["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    return float(num) / float(den), int(st["nb_read_frames"])


async def verify_cached(mezz: Path, src: Path, per_segment: list[dict[str, Any]], run: Runner) -> dict[str, Any]:
    """GAP-81: decode a cached mezzanine and its source at thumbnail size and check every output frame
    against its source neighbours (per_segment src_frames/inputs/rife_n give the time mapping)."""
    if not per_segment:
        raise RifeBadOutput(f"cached {mezz.name}: no per_segment record to verify against")
    w, h = CHECK_SIZE
    with tempfile.TemporaryDirectory(dir=str(mezz.parent)) as td:
        t = Path(td)
        (t / "m").mkdir()
        (t / "s").mkdir()
        await run(["ffmpeg", "-y", "-v", "error", "-i", str(mezz), "-vsync", "0", "-vf", f"scale={w}:{h}",
                   "-start_number", "0", str(t / "m" / "%08d.png")], "ffmpeg gap81 mezz thumbs")
        await run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vsync", "0", "-vf", f"scale={w}:{h}",
                   "-start_number", "0", str(t / "s" / "%08d.png")], "ffmpeg gap81 source thumbs")
        mz = sorted((t / "m").glob("*.png"))
        sf = sorted((t / "s").glob("*.png"))
        done, res = 0, []
        for ps in per_segment:
            first, last = (int(x) for x in ps["src_frames"])
            k, n_req, n = int(ps["inputs"]), int(ps["rife_n"]), int(ps["frames"])
            outs = mz[done:done + n]
            if len(outs) < n:
                raise RifeBadOutput(f"cached {mezz.name}: {len(mz)} frames < expected")
            res.append(await asyncio.to_thread(check_interp_frames, outs, sf[first:first + k], k / n_req,
                                               f"cached {mezz.name} seg {ps['segment']}"))
            done += n
        return {"segments": res}


async def rife_retime(clip_path: str, clip_sha256: str, segments: list[list[float]], frames: int, fps: int,
                      run: Runner, beat: Beat | None = None) -> dict[str, Any]:
    """Render the RIFE retime of ``segments`` of ``clip_path`` to exactly ``frames`` frames at ``fps``
    (source resolution). Returns {"path", "cached", "key", "per_segment"}. Raises RifeUnavailable."""
    cfg = rife_config()
    src = Path(clip_path)
    src_fps, src_frames = await _probe_stream(run, src)
    clip_end = any(source_frame_range(a, b, src_fps, src_frames)[1] >= src_frames for a, b in segments)
    key = cache_key(clip_sha256, segments, frames, fps, cfg["model"], clip_end=clip_end)
    cdir = cache_dir()
    out = cdir / f"{key}.mp4"
    meta = cdir / f"{key}.json"
    alloc = allocate_frames(segments, frames)
    if out.is_file() and meta.is_file():
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            if m.get("frames") == frames and m.get("size_bytes") == out.stat().st_size:
                # GAP-81: a cache entry is re-checked against its source before it is reused (a broken
                # render must not live on in the cache); a bad one is moved aside and re-rendered
                try:
                    await verify_cached(out, src, m.get("per_segment") or [], run)
                    return {"path": str(out), "cached": True, "key": key, "per_segment": m.get("per_segment")}
                except RifeBadOutput as err:
                    bad = cdir / "bad"
                    bad.mkdir(parents=True, exist_ok=True)
                    os.replace(out, bad / out.name)
                    os.replace(meta, bad / meta.name)
                    (bad / f"{key}.reason.txt").write_text(str(err), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
    cdir.mkdir(parents=True, exist_ok=True)
    per_seg = []
    with tempfile.TemporaryDirectory(dir=str(cdir)) as td:
        t = Path(td)
        allf = t / "all"
        allf.mkdir()
        await run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vsync", "0", "-start_number", "0",
                   str(allf / "%08d.png")], "ffmpeg rife extract")
        n_done = 0
        final = t / "final"
        final.mkdir()
        for si, ((a, b), n_out) in enumerate(zip(segments, alloc)):
            first, last = source_frame_range(a, b, src_fps, src_frames)
            m = last - first
            has_next = last < src_frames
            k, n_req = rife_request(m, n_out, has_next)
            ind, outd = t / f"in{si}", t / f"out{si}"
            ind.mkdir()
            outd.mkdir()
            for j in range(k):
                os.link(allf / f"{first + j:08d}.png", ind / f"{j:08d}.png")
            if n_req == k:  # 1x (no interpolation needed): copy straight through
                for j in range(n_out):
                    os.link(ind / f"{j:08d}.png", outd / f"{j + 1:08d}.png")
            else:
                await _run_with_heartbeat(run, [cfg["bin"], "-i", str(ind), "-o", str(outd), "-m", cfg["model"],
                                                "-n", str(n_req), "-j", cfg["threads"], "-f", "%08d.png"],
                                          f"rife segment {si}", beat,
                                          {"segment": si, "frames": n_out, "of": frames})
            outs = sorted(outd.glob("*.png"))
            if len(outs) < n_out:
                raise RuntimeError(f"rife segment {si}: {len(outs)} frames < {n_out}")
            # GAP-81: fail closed on a render that is not made of in-betweens of its inputs
            # (GAP-82: CPU-bound -> worker thread, so the event loop stays free; hundreds of full-size
            # PNGs can outlast the heartbeat timeout, so the attempt heartbeats while it runs)
            await subproc.keep_alive(asyncio.to_thread(check_interp_frames, outs[:n_out], sorted(ind.glob("*.png")),
                                                       k / n_req, f"rife segment {si}"),
                                     f"rife segment {si} frame check")
            for j in range(n_out):
                os.link(outs[j], final / f"{n_done + j:08d}.png")
            n_done += n_out
            per_seg.append({"segment": [a, b], "src_frames": [first, last], "inputs": k, "rife_n": n_req,
                            "frames": n_out, "step_src_frames": round(k / n_req, 6)})
            shutil.rmtree(ind)
            shutil.rmtree(outd)
            if beat:
                beat({"stage": "rife", "segment_done": si, "frames_done": n_done, "of": frames})
        part = t / "mezz.mp4"
        await _run_with_heartbeat(run, ["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-start_number", "0",
                                        "-i", str(final / "%08d.png"), "-frames:v", str(frames), "-c:v", "libx264",
                                        "-preset", "medium", "-crf", "10", "-pix_fmt", "yuv420p", "-r", str(fps), str(part)],
                                  "ffmpeg rife mezzanine", beat, {"stage2": "encode"})
        os.replace(part, out)
    meta.write_text(json.dumps({"key": key, "clip": str(src), "sha256": clip_sha256, "segments": segments,
                                "frames": frames, "fps": fps, "model": Path(cfg["model"]).name,
                                "per_segment": per_seg, "size_bytes": out.stat().st_size}, indent=1), encoding="utf-8")
    return {"path": str(out), "cached": False, "key": key, "per_segment": per_seg}
