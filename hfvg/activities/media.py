"""Media processing activities: ffmpeg operations."""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from temporalio import activity

from hfvg.config import config


@activity.defn
async def assemble_mute_edit(
    episode_id: str,
    shots: list[dict[str, Any]],
    clip_paths: dict[str, str],
    output_dir: str | None = None
) -> dict[str, Any]:
    """
    Assemble mute 9:16 picture edit from approved clips.
    
    Per-shot trim (in/out from shot plan if provided), concatenate in shot order,
    normalized to 1080x1920 @ 30fps yuv420p, deterministic output path.
    
    Args:
        episode_id: Episode identifier
        shots: List of shot dicts with shot_id, duration, and optional trim_in/trim_out
        clip_paths: Dict mapping shot_id to local clip file path
        output_dir: Output directory (default: ./data/episodes/{episode_id}/edit)
    
    Returns:
        Dict with:
            - assembly_path: Path to mute assembly video
            - manifest: Manifest dict with clips[] and metadata
            - duration: Total duration in seconds
    """
    activity.heartbeat({"stage": "assembly_start", "shots": len(shots)})
    
    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Assembled {len(shots)} clips into mute edit")
        return {
            "assembly_path": f"./data/episodes/{episode_id}/edit/mute_assembly.mp4",
            "manifest": {
                "clips": [{"id": s["shot_id"], "seg_s": s.get("duration", 5.0)} for s in shots],
                "dissolves": {},
            },
            "duration": sum(s.get("duration", 5.0) for s in shots),
        }
    
    # Validate inputs
    if not shots:
        raise ValueError("No shots provided for assembly")
    
    missing = [sid for sid in (s["shot_id"] for s in shots) if sid not in clip_paths]
    if missing:
        raise ValueError(f"Missing clip paths for shots: {', '.join(missing)}")
    
    # Determine output directory and ensure it exists
    if output_dir is None:
        output_dir = f"./data/episodes/{episode_id}/edit"
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # Deterministic output path
    assembly_path = Path(output_dir) / "mute_assembly.mp4"
    
    # If assembly already exists, return it (idempotency)
    if assembly_path.exists():
        activity.logger.info(f"Assembly already exists at {assembly_path}, reusing")
        manifest_path = Path(output_dir) / "assembly_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            return {
                "assembly_path": str(assembly_path),
                "manifest": manifest,
                "duration": sum(c["seg_s"] for c in manifest["clips"]),
            }
    
    activity.heartbeat({"stage": "preparing_clips", "shots": len(shots)})
    
    # Create temp directory for intermediate files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Process each clip: trim if needed, normalize format
        normalized_clips = []
        manifest_clips = []
        total_duration = 0.0
        
        for idx, shot in enumerate(shots):
            shot_id = shot["shot_id"]
            clip_path = clip_paths[shot_id]
            duration = shot.get("duration", 5.0)
            trim_in = shot.get("trim_in", 0.0)
            trim_out = shot.get("trim_out", duration)
            
            # Validate clip exists
            if not Path(clip_path).exists():
                raise FileNotFoundError(f"Clip not found: {clip_path}")
            
            activity.heartbeat({
                "stage": "processing_clip",
                "shot": shot_id,
                "progress": f"{idx + 1}/{len(shots)}"
            })
            
            # Normalize and trim clip
            norm_path = tmp_path / f"{shot_id}_normalized.mp4"
            
            # Build ffmpeg command: trim, scale to 1080x1920, 30fps, yuv420p
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(trim_in),
                "-t", str(trim_out - trim_in),
                "-i", clip_path,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-r", "30",
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "23",
                "-an",  # Remove audio (mute)
                str(norm_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed for {shot_id}: {result.stderr}")
            
            normalized_clips.append(str(norm_path))
            seg_duration = trim_out - trim_in
            manifest_clips.append({
                "id": shot_id,
                "seg_s": seg_duration,
                "trim_in": trim_in,
                "trim_out": trim_out,
            })
            total_duration += seg_duration
        
        activity.heartbeat({"stage": "concatenating", "clips": len(normalized_clips)})
        
        # Create concat file for ffmpeg
        concat_file = tmp_path / "concat.txt"
        with open(concat_file, "w") as f:
            for clip in normalized_clips:
                f.write(f"file '{clip}'\n")
        
        # Concatenate all clips
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(assembly_path)
        ]
        
        result = subprocess.run(concat_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {result.stderr}")
    
    # Create manifest
    manifest = {
        "clips": manifest_clips,
        "dissolves": {},  # No dissolves in this version
        "total_duration": total_duration,
        "format": {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "pix_fmt": "yuv420p",
        },
    }
    
    # Save manifest
    manifest_path = Path(output_dir) / "assembly_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    
    activity.logger.info(f"Assembly complete: {assembly_path} ({total_duration:.2f}s)")
    
    return {
        "assembly_path": str(assembly_path),
        "manifest": manifest,
        "duration": total_duration,
    }


@activity.defn
async def trim_clips(clip_urls: list[str], edit_decisions: dict) -> str:
    """
    Trim clips according to edit decisions (legacy signature).

    Returns URL of trimmed output.
    """
    activity.heartbeat({"stage": "trimming", "clips": len(clip_urls)})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Trimmed {len(clip_urls)} clips")
        return f"https://example.com/edits/trimmed-{len(clip_urls)}.mp4"

    raise NotImplementedError("Use assemble_mute_edit for real implementation")


@activity.defn
async def render_edit(trimmed_url: str, transitions: dict) -> str:
    """
    Apply transitions and render picture lock (legacy signature).

    Returns URL of picture-locked output.
    """
    activity.heartbeat({"stage": "rendering_edit"})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info("[DRY-RUN] Rendered edit with transitions")
        return "https://example.com/edits/picture-lock.mp4"

    raise NotImplementedError("Use assemble_mute_edit for real implementation")


@activity.defn
async def mix_audio(video_url: str, audio_tracks: list[str]) -> str:
    """
    Mix audio tracks (VO, SFX, music) with video.

    Returns URL of final mixed output.
    """
    activity.heartbeat({"stage": "mixing_audio", "tracks": len(audio_tracks)})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Mixed {len(audio_tracks)} audio tracks")
        return "https://example.com/final/mixed.mp4"

    raise NotImplementedError("Real ffmpeg not implemented in milestone 1")
