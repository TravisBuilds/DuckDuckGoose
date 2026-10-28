"""QC activities: still_review and clip_qc with real motion QC."""

import asyncio
import os
import random
import subprocess
import tempfile
import json
from pathlib import Path

from temporalio import activity

from hfvg.config import config
from hfvg.qc.clip_constraints import (
    FAIL as CONSTRAINT_FAIL,
    PASS as CONSTRAINT_PASS,
    evaluate_clip_constraints,
    get_constraint_checker,
)

# Resolution tolerance: providers round output dims to codec-friendly multiples
# (live Seedance 2.5 720p 9:16 returns 720x1276 for 720x1280)
RESOLUTION_TOLERANCE_FRAC = 0.01  # per side: 1% (12.8 px on 1280)


def _dims_close(w: int, h: int, ew: int, eh: int, frac: float = RESOLUTION_TOLERANCE_FRAC) -> bool:
    """True when each side is within ``frac`` of the expected size."""
    return abs(w - ew) <= ew * frac and abs(h - eh) <= eh * frac


@activity.defn
async def precheck_still_qc(episode_id: str, shot_id: str, prompt: str, params: dict) -> dict:
    """
    Pre-flight QC checks before submitting still generation.
    
    Checks prompt and parameters for obvious issues that would fail QC.
    This runs BEFORE any paid generation to catch issues early.
    
    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    activity.heartbeat({"stage": "precheck_qc", "shot": shot_id})
    
    # In dry run, always pass precheck
    if config.DRY_RUN:
        return {"passed": True, "issues": []}
    
    # In live mode, check for basic issues
    issues = []
    
    # Check prompt length
    if not prompt or len(prompt) < 10:
        issues.append("Prompt too short")
    
    # Check for banned words (example)
    banned_words = ["nsfw", "explicit", "gore"]
    if any(word in prompt.lower() for word in banned_words):
        issues.append("Prompt contains banned content")
    
    passed = len(issues) == 0
    
    activity.logger.info(
        f"Pre-flight QC for {shot_id}: {'PASS' if passed else 'FAIL'} - {issues}"
    )
    
    return {"passed": passed, "issues": issues}


@activity.defn
async def precheck_clip_qc(episode_id: str, shot_id: str, prompt: str, duration: float) -> dict:
    """
    Pre-flight QC checks before submitting clip generation.
    
    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    activity.heartbeat({"stage": "precheck_clip_qc", "shot": shot_id})
    
    # In dry run, always pass precheck
    if config.DRY_RUN:
        return {"passed": True, "issues": []}
    
    # In live mode, check for basic issues
    issues = []
    
    # Check duration
    if duration < 3.0 or duration > 15.0:
        issues.append(f"Duration {duration}s outside valid range (3-15s)")
    
    # Check prompt
    if not prompt or len(prompt) < 10:
        issues.append("Prompt too short")
    
    passed = len(issues) == 0
    
    activity.logger.info(
        f"Pre-flight clip QC for {shot_id}: {'PASS' if passed else 'FAIL'} - {issues}"
    )
    
    return {"passed": passed, "issues": issues}


@activity.defn
async def review_still(asset_url: str, rubric: dict, episode_id: str = None, shot_id: str = None) -> dict:
    """
    Agent-based still review against a rubric.

    Args:
        asset_url: URL of the still asset to review
        rubric: Review rubric (not used in current stub)
        episode_id: Episode identifier (required for live mode checks)
        shot_id: Shot identifier (for logging)

    Returns:
        dict with 'passed' (bool), 'issues' (list of strings), and 'escalate' (bool) in live mode
    """
    # Heartbeat only if in activity context
    try:
        activity.heartbeat({"stage": "reviewing_still", "url": asset_url})
    except RuntimeError:
        # Not in activity context (e.g., direct test call) - skip heartbeat
        pass


    # Check live mode from DB
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # DRY_RUN forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)
        return {"passed": True, "issues": []}
    
    # Fail closed: if episode_id is missing, cannot verify live mode, so escalate
    if not episode_id:
        activity.logger.error(
            f"[FAIL CLOSED] Still review called without episode_id. "
            f"Cannot verify live mode. Escalating to human review. Asset: {asset_url}"
        )
        return {
            "passed": False,
            "issues": ["episode_id not provided - cannot verify live mode"],
            "escalate": True,
        }
    
    # Check DB requirements
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
                (episode_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    # Episode not found - fail closed
                    activity.logger.error(
                        f"[FAIL CLOSED] Episode {episode_id} not found in DB. "
                        f"Escalating to human review. Asset: {asset_url}"
                    )
                    return {
                        "passed": False,
                        "issues": [f"Episode {episode_id} not found"],
                        "escalate": True,
                    }
                
                live_mode = bool(row[0])
                g108_approved = bool(row[1])
                
                # Fail closed: if DRY_RUN=false but live requirements not met, escalate
                # Don't fall back to dry mode when DRY_RUN=false is explicitly set
                if not live_mode or not g108_approved:
                    activity.logger.error(
                        f"[FAIL CLOSED] Still review: DRY_RUN=false but live requirements not met "
                        f"(live_mode={live_mode}, g108={g108_approved}). Escalating."
                    )
                    return {
                        "passed": False,
                        "issues": [f"DRY_RUN=false but episode not in live mode or G1.08 not approved"],
                        "escalate": True,
                    }
    except Exception as e:
        # Fail closed on DB error
        activity.logger.error(f"[FAIL CLOSED] DB error checking live mode: {e}. Escalating.")
        return {
            "passed": False,
            "issues": [f"DB error: {str(e)[:100]}"],
            "escalate": True,
        }
    
    # Live mode confirmed: always escalate to human review (real QC not implemented)
    activity.logger.info(
        f"[LIVE MODE] Still review for {episode_id}/{shot_id}. "
        f"Escalating to human review. Asset: {asset_url}"
    )
    return {
        "passed": False,
        "issues": ["Real QC agent not implemented - escalate to human review"],
        "escalate": True,  # Signal that this needs human review, not retry
    }


async def check_motion_qc(
    video_path: str,
    expected_duration: float,
    expected_resolution: str = "1024x576",
    expected_aspect: str = "16:9"
) -> dict:
    """
    Real motion QC using ffmpeg/ffprobe.
    
    Checks:
    - Duration within ±0.3s of expected
    - Resolution/aspect matches
    - Non-black (blackdetect <10%)
    - Not frozen (freezedetect: no freeze >1s)
    - Has decodable frames
    
    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    issues = []
    
    try:
        # 1. Check duration and decodable frames with ffprobe
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=width,height,r_frame_rate,nb_frames",
            "-of", "json",
            video_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            issues.append(f"ffprobe failed: {result.stderr[:200]}")
            return {"passed": False, "issues": issues}
        
        probe_data = json.loads(result.stdout)
        
        # Check duration
        if "format" not in probe_data or "duration" not in probe_data["format"]:
            issues.append("No duration found in video")
        else:
            actual_duration = float(probe_data["format"]["duration"])
            duration_diff = abs(actual_duration - expected_duration)
            if duration_diff > 0.3:
                issues.append(f"Duration {actual_duration:.2f}s differs from expected {expected_duration:.2f}s by {duration_diff:.2f}s (>0.3s)")
        
        # Check resolution and aspect ratio
        if "streams" not in probe_data or len(probe_data["streams"]) == 0:
            issues.append("No video stream found")
        else:
            stream = probe_data["streams"][0]
            if "width" not in stream or "height" not in stream:
                issues.append("No resolution found in video")
            else:
                actual_width = stream["width"]
                actual_height = stream["height"]
                expected_width, expected_height = map(int, expected_resolution.split("x"))
                
                # Providers round output dims to codec-friendly multiples (live Seedance 2.5 720p 9:16
                # returns 720x1276 for 720x1280): a few pixels are not a defect - the edit scales and
                # pads to the delivery frame. Fail only beyond RESOLUTION_TOLERANCE_FRAC per side
                # (the aspect check below still applies).
                if not _dims_close(actual_width, actual_height, expected_width, expected_height):
                    issues.append(f"Resolution {actual_width}x{actual_height} doesn't match expected {expected_resolution}")
                
                # Check aspect ratio (allow small tolerance)
                actual_aspect = actual_width / actual_height
                if ":" in expected_aspect:
                    exp_w, exp_h = map(int, expected_aspect.split(":"))
                    expected_aspect_ratio = exp_w / exp_h
                    aspect_diff = abs(actual_aspect - expected_aspect_ratio)
                    if aspect_diff > 0.05:  # 5% tolerance
                        issues.append(f"Aspect ratio {actual_aspect:.2f} doesn't match expected {expected_aspect_ratio:.2f}")
        
        # 2. Check for black frames (blackdetect)
        blackdetect_cmd = [
            "ffmpeg",
            "-i", video_path,
            "-vf", "blackdetect=d=0.1:pix_th=0.05",
            "-f", "null",
            "-"
        ]
        result = subprocess.run(blackdetect_cmd, capture_output=True, text=True, timeout=60)
        
        # Parse blackdetect output from stderr
        black_duration = 0.0
        for line in result.stderr.split("\n"):
            if "black_duration:" in line:
                try:
                    # Extract duration like "black_duration:1.234"
                    duration_str = line.split("black_duration:")[1].split()[0]
                    black_duration += float(duration_str)
                except:
                    pass
        
        # Check if black frames exceed 10% of video
        if probe_data.get("format", {}).get("duration"):
            total_duration = float(probe_data["format"]["duration"])
            black_percent = (black_duration / total_duration) * 100 if total_duration > 0 else 0
            if black_percent >= 10.0:
                issues.append(f"Video is {black_percent:.1f}% black (threshold: <10%)")
        
        # 3. Check for frozen frames (freezedetect)
        freezedetect_cmd = [
            "ffmpeg",
            "-i", video_path,
            "-vf", "freezedetect=n=-60dB:d=1.0",
            "-f", "null",
            "-"
        ]
        result = subprocess.run(freezedetect_cmd, capture_output=True, text=True, timeout=60)
        
        # Parse freezedetect output from stderr
        freeze_detected = False
        for line in result.stderr.split("\n"):
            if "lavfi.freezedetect.freeze_duration" in line or "freeze_start" in line.lower():
                try:
                    # Check if freeze duration >1s
                    if "freeze_duration" in line:
                        duration_str = line.split("freeze_duration:")[1].split()[0] if "freeze_duration:" in line else "0"
                        freeze_dur = float(duration_str)
                        if freeze_dur > 1.0:
                            freeze_detected = True
                            issues.append(f"Frozen frames detected (duration: {freeze_dur:.2f}s >1s)")
                            break
                except:
                    pass
        
    except subprocess.TimeoutExpired:
        issues.append("ffmpeg/ffprobe timed out")
    except FileNotFoundError:
        issues.append("ffmpeg/ffprobe not found")
    except Exception as e:
        issues.append(f"Motion QC error: {str(e)[:200]}")
    
    passed = len(issues) == 0
    return {"passed": passed, "issues": issues}


@activity.defn
async def review_clip(asset_url: str, rubric: dict, episode_id: str = None, shot_id: str = None) -> dict:
    """
    Agent-based clip QC against a rubric.

    Args:
        asset_url: URL of the clip asset to review
        rubric: Review rubric (not used in current stub)
        episode_id: Episode identifier (required for live mode checks)
        shot_id: Shot identifier (for logging)

    Returns:
        dict with 'passed' (bool), 'issues' (list of strings), and 'escalate' (bool) in live mode
    """
    # Heartbeat only if in activity context
    try:
        activity.heartbeat({"stage": "reviewing_clip", "url": asset_url})
    except RuntimeError:
        # Not in activity context (e.g., direct test call) - skip heartbeat
        pass


    # Check live mode from DB
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Per-shot qc_fail_if (shots.json): free-text failure conditions checked on every clip.
    constraints = list((rubric or {}).get("qc_fail_if") or [])

    # DRY_RUN forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)
        if not constraints:
            return {"passed": True, "issues": []}
        # Dry clips are placeholders: the constraints are recorded per clip as not checked
        dry = await evaluate_clip_constraints(None, None, constraints, dry_run=True)
        return {"passed": True, "issues": [], "constraints": dry}
    
    # Fail closed: if episode_id is missing, cannot verify live mode, so escalate
    if not episode_id:
        activity.logger.error(
            f"[FAIL CLOSED] Clip review called without episode_id. "
            f"Cannot verify live mode. Escalating to human review. Asset: {asset_url}"
        )
        return {
            "passed": False,
            "issues": ["episode_id not provided - cannot verify live mode"],
            "escalate": True,
        }
    
    # Check DB requirements
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
                (episode_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    # Episode not found - fail closed
                    activity.logger.error(
                        f"[FAIL CLOSED] Episode {episode_id} not found in DB. "
                        f"Escalating to human review. Asset: {asset_url}"
                    )
                    return {
                        "passed": False,
                        "issues": [f"Episode {episode_id} not found"],
                        "escalate": True,
                    }
                
                live_mode = bool(row[0])
                g108_approved = bool(row[1])
                
                # Fail closed: if DRY_RUN=false but live requirements not met, escalate
                # Don't fall back to dry mode when DRY_RUN=false is explicitly set
                if not live_mode or not g108_approved:
                    activity.logger.error(
                        f"[FAIL CLOSED] Clip review: DRY_RUN=false but live requirements not met "
                        f"(live_mode={live_mode}, g108={g108_approved}). Escalating."
                    )
                    return {
                        "passed": False,
                        "issues": [f"DRY_RUN=false but episode not in live mode or G1.08 not approved"],
                        "escalate": True,
                    }
    except Exception as e:
        # Fail closed on DB error
        activity.logger.error(f"[FAIL CLOSED] DB error checking live mode: {e}. Escalating.")
        return {
            "passed": False,
            "issues": [f"DB error: {str(e)[:100]}"],
            "escalate": True,
        }

    # Live mode confirmed: run real motion QC
    activity.logger.info(
        f"[LIVE MODE] Running motion QC for {episode_id}/{shot_id}. Asset: {asset_url}"
    )
    
    # Download clip to temp file for analysis
    import httpx
    temp_dir = tempfile.mkdtemp()
    temp_video = Path(temp_dir) / "clip.mp4"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(asset_url)
            response.raise_for_status()
            temp_video.write_bytes(response.content)
        
        # Get expected duration from rubric or default to 5s
        expected_duration = rubric.get("duration", 5.0)
        # G-QC-1: no landscape default. The workflow derives the expected dimensions before the
        # paid submit (hfvg.qc.clip_expect); a rubric without them cannot be checked -> escalate.
        expected_resolution = rubric.get("resolution")
        expected_aspect = rubric.get("aspect_ratio")
        if not expected_resolution or not expected_aspect:
            return {
                "passed": False,
                "issues": ["Motion QC rubric has no expected resolution/aspect_ratio; cannot verify the clip"],
                "escalate": True,
            }
        
        # Run motion QC
        qc_result = await check_motion_qc(
            str(temp_video),
            expected_duration,
            expected_resolution,
            expected_aspect
        )
        
        if qc_result["passed"]:
            # Per-clip stillgate (G4.10): a technically valid clip that is still-only is a FAIL
            # (reroll, like any failed motion QC); WATCH passes but is reported for review.
            motion = await asyncio.to_thread(clip_stillgate, str(temp_video), shot_id or "clip", expected_duration)
            if motion["verdict"] == "FAIL":
                activity.logger.warning(f"Stillgate FAIL for {shot_id}: {motion}")
                return {
                    "passed": False,
                    "issues": [f"still-only clip (stillgate FAIL: peak_fd={motion['peak_fd']}, "
                               f"mean_fd={motion['mean_fd']})"],
                    "escalate": False,
                    "motion": motion,
                }
            activity.logger.info(f"Motion QC passed for {shot_id} (stillgate {motion['verdict']})")
            issues = [f"stillgate WATCH: low motion (peak_fd={motion['peak_fd']}, mean_fd={motion['mean_fd']})"] \
                if motion["verdict"] == "WATCH" else []
            if constraints:
                # Fail closed: no checker / no file / low confidence -> NEEDS_REVIEW (escalate),
                # a violated condition -> FAIL; never a silent pass.
                checked = await evaluate_clip_constraints(
                    get_constraint_checker(), str(temp_video), constraints,
                    {"episode_id": episode_id, "shot_id": shot_id,
                     "prompt": (rubric or {}).get("prompt"), "duration": expected_duration},
                )
                if checked["status"] == CONSTRAINT_FAIL:
                    activity.logger.warning(f"qc_fail_if FAIL for {shot_id}: {checked['reason']}")
                    return {"passed": False, "issues": issues + [f"qc_fail_if violated: {checked['reason']}"],
                            "escalate": False, "motion": motion, "constraints": checked}
                if checked["status"] != CONSTRAINT_PASS:
                    activity.logger.warning(f"qc_fail_if NEEDS_REVIEW for {shot_id}: {checked['reason']}")
                    return {"passed": False, "issues": issues + [f"qc_fail_if needs review: {checked['reason']}"],
                            "escalate": True,
                            "motion": motion, "constraints": checked}
                return {"passed": True, "issues": issues, "motion": motion, "constraints": checked}
            return {"passed": True, "issues": issues, "motion": motion}
        else:
            activity.logger.warning(f"Motion QC failed for {shot_id}: {qc_result['issues']}")
            return {
                "passed": False,
                "issues": qc_result["issues"],
                "escalate": False,  # Failed QC = reroll, not escalate
            }
    
    except Exception as e:
        activity.logger.error(f"Failed to download or analyze clip: {e}")
        return {
            "passed": False,
            "issues": [f"Failed to analyze clip: {str(e)[:200]}"],
            "escalate": True,  # Download/analysis error = escalate
        }
    finally:
        # Clean up temp file
        try:
            if temp_video.exists():
                temp_video.unlink()
            Path(temp_dir).rmdir()
        except:
            pass


def clip_stillgate(clip_path: str, shot_id: str, seg_s: float) -> dict:
    """Stillgate on ONE clip (whole clip is one segment). Returns the per-shot stillgate row."""
    from hfvg.qc.motion import check_motion

    r = check_motion(clip_path, {"clips": [{"id": shot_id, "seg_s": float(seg_s)}], "dissolves": {}})[0]
    return {"shot_id": r.shot_id, "t0": r.t0, "t1": r.t1, "mean_fd": r.mean_fd,
            "peak_fd": r.peak_fd, "verdict": r.verdict}


ASSEMBLY_BLOCKING_VERDICTS = ("FAIL", "NEEDS_REVIEW")


@activity.defn
async def check_assembly_qc(
    assembly_path: str,
    manifest: dict,
    episode_id: str,
    shot_meta_by_id: dict | None = None,
    continuity_hard_checks: list | None = None,
) -> dict:
    """QC of the mute picture edit, run before G_FINAL_CUT.

    continuity_hard_checks (shots.json top level, hfvg.qc.continuity): which continuity checks are
    HARD (blocking) vs ADVISORY (reported, verdict WATCH at worst). None -> default (headcount hard,
    the size / order checks not run). When the episode declares the key, the size / order checks
    run on every shot through the ContinuityChecker interface (unverified -> NEEDS_REVIEW when hard).

    - Motion (stillgate G4.10) per shot window of the assembled edit (FAIL / WATCH / PASS).
    - Animal count (shot_meta.count) and duck presence (shot_meta.duck) per shot, through the
      AnimalCounter interface (hfvg.qc.animal_count). No counter configured -> NEEDS_REVIEW.

    Verdict (worst wins): FAIL (still-only shot or count/duck mismatch) > NEEDS_REVIEW (could not
    verify: no checker, low confidence, QC error, missing edit) > WATCH (low motion, human look)
    > PASS. FAIL and NEEDS_REVIEW are ``blocking``: the episode holds G_FINAL_CUT until a clip retry
    produces a passing edit or a human accepts the QC result. DRY_RUN placeholder edits (nothing
    rendered) return verdict DRY_RUN and are not blocking.
    """
    from hfvg.qc import continuity as cont

    activity.heartbeat({"stage": "assembly_qc", "episode": episode_id})
    clips = list((manifest or {}).get("clips") or [])
    policy = cont.continuity_policy(continuity_hard_checks)
    if (manifest or {}).get("dry_run"):
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)
        return {"verdict": "DRY_RUN", "blocking": False, "escalate": False,
                "reason": "dry run: placeholder clips, no edit rendered, nothing to check",
                "motion_results": [], "animal_results": [], "continuity_policy": policy,
                "continuity_results": []}

    def result(verdict: str, reason: str, motion=None, animals=None, continuity_rows=None) -> dict:
        motion = motion or []
        animals = animals or []
        continuity_rows = continuity_rows or []
        hard_animals = cont.HEADCOUNT in policy["hard"]
        return {
            "verdict": verdict,
            "blocking": verdict in ASSEMBLY_BLOCKING_VERDICTS,
            "escalate": verdict == "NEEDS_REVIEW",
            "reason": reason,
            "motion_results": motion,
            "animal_results": animals,
            "continuity_policy": policy,
            "continuity_results": continuity_rows,
            "failed_shots": sorted({m["shot_id"] for m in motion if m["verdict"] == "FAIL"}
                                   | {a["shot_id"] for a in animals if hard_animals and a["status"] == "FAIL"}
                                   | {c["shot_id"] for c in continuity_rows if c["hard"] and c["status"] == "FAIL"}),
            "review_shots": sorted({a["shot_id"] for a in animals if hard_animals and a["status"] == "NEEDS_REVIEW"}
                                   | {c["shot_id"] for c in continuity_rows
                                      if c["hard"] and c["status"] == "NEEDS_REVIEW"}),
            "watch_shots": sorted({m["shot_id"] for m in motion if m["verdict"] == "WATCH"}
                                  | {a["shot_id"] for a in animals if not hard_animals and a["status"] != "PASS"}
                                  | {c["shot_id"] for c in continuity_rows if not c["hard"] and c["status"] != "PASS"}),
        }

    if not assembly_path or not Path(assembly_path).is_file() or not clips:
        return result("NEEDS_REVIEW", f"mute edit missing or empty: {assembly_path}")

    from hfvg.qc.animal_count import check_shot_animals, get_animal_counter
    from hfvg.qc.motion import check_motion

    try:
        rows = await asyncio.to_thread(check_motion, assembly_path, manifest)
    except Exception as e:  # could not analyse -> a human must look; never a silent pass
        activity.logger.error(f"Assembly motion QC error for {episode_id}: {e}")
        return result("NEEDS_REVIEW", f"motion QC error: {str(e)[:200]}")
    motion = [{"shot_id": r.shot_id, "t0": r.t0, "t1": r.t1, "mean_fd": r.mean_fd,
               "peak_fd": r.peak_fd, "verdict": r.verdict} for r in rows]

    activity.heartbeat({"stage": "assembly_qc_animals", "episode": episode_id})
    counter = get_animal_counter()
    metas = shot_meta_by_id or {}
    animals = []
    for clip in clips:
        sid = clip["id"]
        # Reused locked clips (edit plan) carry their own shot_meta in the manifest
        res = await check_shot_animals(counter, clip.get("source") or assembly_path,
                                       metas.get(sid) or clip.get("shot_meta"))
        animals.append({"shot_id": sid, **res})

    # Size / order continuity checks: only when the episode declares continuity_hard_checks.
    checker_rows: list[dict] = []
    if policy["source"] == "episode":
        activity.heartbeat({"stage": "assembly_qc_continuity", "episode": episode_id})
        checker = cont.get_continuity_checker()
        for clip in clips:
            sid = clip["id"]
            for check in cont.CHECKER_CHECKS:
                try:
                    r = await cont.run_checker_check(checker, clip.get("source") or assembly_path, check,
                                                     {"shot_id": sid, "shot_meta": metas.get(sid) or clip.get("shot_meta")})
                except Exception as e:  # a checker error is never a pass
                    r = {"check": check, "status": cont.NEEDS_REVIEW, "reason": f"{check}: checker error: {str(e)[:200]}"}
                checker_rows.append({"shot_id": sid, **r})
    continuity = cont.classify(checker_rows, policy)
    continuity_rows = continuity["results"]
    hard_animals = cont.HEADCOUNT in policy["hard"]

    fails = [f"{m['shot_id']} still-only (peak_fd={m['peak_fd']}, mean_fd={m['mean_fd']})"
             for m in motion if m["verdict"] == "FAIL"]
    reviews: list[str] = []
    watches = [f"{m['shot_id']} low motion (peak_fd={m['peak_fd']}, mean_fd={m['mean_fd']})"
               for m in motion if m["verdict"] == "WATCH"]
    for a in animals:  # headcount: hard by default; advisory when the episode says so
        if a["status"] == "PASS":
            continue
        line = f"{a['shot_id']}: {a['reason']}"
        if not hard_animals:
            watches.append(f"{line} (advisory headcount)")
        elif a["status"] == "FAIL":
            fails.append(line)
        else:
            reviews.append(line)
    fails += [f"{c['shot_id']}: {c['reason']}" for c in continuity["hard_fail"]]
    reviews += [f"{c['shot_id']}: {c['reason']}" for c in continuity["hard_review"]]
    watches += [f"{c['shot_id']}: {c['reason']} (advisory)" for c in continuity["advisory"]]
    if fails:
        verdict, reason = "FAIL", "; ".join(fails + reviews)
    elif reviews:
        verdict, reason = "NEEDS_REVIEW", "; ".join(reviews)
    elif watches:
        verdict, reason = "WATCH", "; ".join(watches)
    else:
        verdict, reason = "PASS", "all shots passed motion and animal checks"
    activity.logger.info(f"Assembly QC {episode_id}: {verdict}: {reason}")
    return result(verdict, reason, motion, animals, continuity_rows)
