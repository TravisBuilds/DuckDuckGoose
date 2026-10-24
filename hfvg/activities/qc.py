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
                
                if actual_width != expected_width or actual_height != expected_height:
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
    
    # DRY_RUN forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)
        return {"passed": True, "issues": []}
    
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
            activity.logger.info(f"Motion QC passed for {shot_id}")
            return {"passed": True, "issues": []}
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
