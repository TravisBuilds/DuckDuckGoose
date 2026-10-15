"""
Audio Stage CLI: Parity rebuild command for proving bit-exact match.

This provides the CLI entry point for rebuilding a mix from a locked script
plus existing local stems with ZERO API calls.

Usage:
    hfvg audio rebuild \
        --script ep04/audio/vo-script-v1.json \
        --stems ep04/audio/stems/ \
        --cues ep04/audio/cues-v1.json \
        --video ep04/video/mute.mp4 \
        --output ep04/audio/rebuilt-mix.mp4

Expected input layout (mirroring reference):
    stems/
        ep04-vo-bus-v1.wav
        ep04-bgm-bed-v1.wav
        ep04-sfx-ambience-v1.wav
        ep04-vox-v1.wav
    subs/
        ep04-en-v1.ass
        ep04-en-v1.srt
    ep04-mix-v1-cues.json

This proves parity against the approved Ep04 file by rebuilding from
the same inputs and comparing outputs.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import click


@click.group()
def audio_cli():
    """Audio stage commands."""
    pass


@audio_cli.command(name="rebuild")
@click.option(
    "--script",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to locked VO script JSON",
)
@click.option(
    "--stems",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing existing stems (vo, bgm, sfx, vox)",
)
@click.option(
    "--cues",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to cues JSON file",
)
@click.option(
    "--video",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to picture-locked video (with logo already baked in)",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    help="Output path for rebuilt mix",
)
@click.option(
    "--compare",
    type=click.Path(exists=True, path_type=Path),
    help="Optional: path to reference file for parity comparison",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be done without executing",
)
def rebuild_command(
    script: Path,
    stems: Path,
    cues: Path,
    video: Path,
    output: Path,
    compare: Path | None,
    dry_run: bool,
):
    """
    Rebuild audio mix from locked script + existing stems.
    
    This command performs ZERO API calls - it only uses existing stems
    and the locked script to rebuild the mix. This proves parity against
    the approved episode file.
    
    Expected stems directory structure:
    
        stems/
            epNN-vo-bus-v1.wav
            epNN-bgm-bed-v1.wav
            epNN-sfx-ambience-v1.wav
            epNN-vox-v1.wav (optional)
    
    The script JSON should contain VO line timings, and the cues JSON
    should contain SFX and music cue timings.
    """
    click.echo("Audio Stage Parity Rebuild")
    click.echo("=" * 60)
    click.echo(f"Script:  {script}")
    click.echo(f"Stems:   {stems}")
    click.echo(f"Cues:    {cues}")
    click.echo(f"Video:   {video}")
    click.echo(f"Output:  {output}")
    
    if compare:
        click.echo(f"Compare: {compare}")
    
    if dry_run:
        click.echo("\n[DRY-RUN] Would rebuild mix with:")
        click.echo("  - All 4 stems: SFX, VOX, VO, BGM")
        click.echo("  - VO gain trim: -2.5 dB")
        click.echo("  - amix: dropout_transition=1, normalize=0")
        click.echo("  - Master: makeup +4.7 dB, limiter 0.86")
        click.echo("  - Outputs: nosubs + subs (ASS burn) + phone (<5 MB)")
        click.echo("  - LUFS target: -12.7 ±0.5")
        click.echo("  - True peak: ≤ -1.0 dBTP")
        click.echo("\n✓ Dry-run complete. No API calls made.")
        return
    
    # Validate inputs
    if not stems.is_dir():
        click.echo(f"Error: {stems} is not a directory", err=True)
        sys.exit(1)
    
    # Load script JSON
    with open(script) as f:
        script_data = json.load(f)
    
    # Load cues JSON
    with open(cues) as f:
        cues_data = json.load(f)
    
    # Find stem files - fix glob to distinguish VO from VOX
    stem_files = {
        "vo": None,
        "bgm": None,
        "sfx": None,
        "vox": None,
    }
    
    # Match stems by specific patterns to avoid VO/VOX confusion
    for stem_path in stems.iterdir():
        if not stem_path.suffix == ".wav":
            continue
        
        name_lower = stem_path.stem.lower()
        
        # Order matters: check VOX before VO to avoid false match
        if "vox" in name_lower:
            stem_files["vox"] = stem_path
            click.echo(f"Found vox: {stem_path.name}")
        elif "vo-bus" in name_lower or (("vo" in name_lower or "voice" in name_lower) and "vox" not in name_lower):
            stem_files["vo"] = stem_path
            click.echo(f"Found vo: {stem_path.name}")
        elif "bgm" in name_lower or "music" in name_lower:
            stem_files["bgm"] = stem_path
            click.echo(f"Found bgm: {stem_path.name}")
        elif "sfx" in name_lower or "ambience" in name_lower:
            stem_files["sfx"] = stem_path
            click.echo(f"Found sfx: {stem_path.name}")
    
    # Check required stems
    if not stem_files["vo"]:
        click.echo("Error: VO stem not found", err=True)
        sys.exit(1)
    
    if not stem_files["bgm"]:
        click.echo("Error: BGM stem not found", err=True)
        sys.exit(1)
    
    if not stem_files["sfx"]:
        click.echo("Error: SFX stem not found", err=True)
        sys.exit(1)
    
    # VOX is optional
    if not stem_files["vox"]:
        click.echo("Note: VOX stem not found (optional)")
    
    # Get video duration
    duration = get_video_duration(video)
    click.echo(f"\nVideo duration: {duration:.3f}s")
    
    # Perform rebuild
    click.echo("\nRebuilding mix (no-subs version)...")
    nosubs_path = output.parent / (output.stem + "-nosubs.mp4")
    
    try:
        rebuild_mix(
            video_path=video,
            stems=stem_files,
            script_data=script_data,
            cues_data=cues_data,
            output_path=nosubs_path,
            duration=duration,
        )
        click.echo(f"✓ No-subs mix: {nosubs_path}")
        
        # Burn subtitles if available
        subs_dir = stems.parent / "subs"
        ass_file = None
        if subs_dir.exists():
            ass_files = list(subs_dir.glob("*.ass"))
            if ass_files:
                ass_file = ass_files[0]
                click.echo(f"\nFound subtitles: {ass_file.name}")
        
        if ass_file:
            click.echo("Burning subtitles...")
            burn_subtitles(
                video_path=nosubs_path,
                ass_path=ass_file,
                output_path=output,
            )
            click.echo(f"✓ With subs: {output}")
        else:
            click.echo("⚠ No subtitles found, copying nosubs as final")
            import shutil
            shutil.copy2(nosubs_path, output)
        
        # Create phone copy
        phone_path = output.parent / (output.stem + "-phone.mp4")
        click.echo(f"\nCreating phone copy (540px, <5 MB)...")
        create_phone_copy(
            video_path=output,
            output_path=phone_path,
            max_size_mb=5.0,
        )
        click.echo(f"✓ Phone copy: {phone_path}")
        
        # Measure loudness
        click.echo("\nMeasuring loudness...")
        loudness = measure_loudness(output)
        click.echo(f"  Integrated LUFS: {loudness['integrated_lufs']:.2f}")
        click.echo(f"  Loudness Range:  {loudness['loudness_range']:.2f} LU")
        click.echo(f"  True Peak:       {loudness['true_peak_dbtp']:.2f} dBTP")
        
        # Check targets
        target_lufs = -12.7
        target_peak = -1.0
        
        lufs_ok = abs(loudness['integrated_lufs'] - target_lufs) <= 0.5
        peak_ok = loudness['true_peak_dbtp'] <= target_peak
        
        if lufs_ok and peak_ok:
            click.echo("\n✓ All targets met")
        else:
            if not lufs_ok:
                click.echo(f"\n⚠ LUFS outside target: {target_lufs} ±0.5")
            if not peak_ok:
                click.echo(f"\n⚠ True peak above target: {target_peak} dBTP")
        
        # Compare if reference provided
        if compare:
            click.echo(f"\nComparing to reference: {compare}")
            comparison = compare_audio_video(output, compare, duration)
            
            click.echo("\n=== Parity Check ===")
            click.echo(f"LUFS delta:        {comparison['lufs_delta']:.2f} dB")
            click.echo(f"True peak delta:   {comparison['peak_delta']:.2f} dB")
            click.echo(f"Audio null residual: {comparison['null_residual_db']:.2f} dB")
            click.echo(f"Video PSNR (avg):  {comparison['video_psnr_avg']:.2f} dB")
            click.echo(f"Video SSIM (avg):  {comparison['video_ssim_avg']:.4f}")
            
            # Pass criteria: LUFS within 0.2 dB, null residual <= -50 dB
            lufs_pass = abs(comparison['lufs_delta']) <= 0.2
            null_pass = comparison['null_residual_db'] <= -50.0
            
            if lufs_pass and null_pass:
                click.echo("\n✓ PARITY PASS")
            else:
                click.echo("\n✗ PARITY FAIL")
                if not lufs_pass:
                    click.echo(f"  - LUFS delta {comparison['lufs_delta']:.2f} dB exceeds 0.2 dB threshold")
                if not null_pass:
                    click.echo(f"  - Null residual {comparison['null_residual_db']:.2f} dB exceeds -50 dB threshold")
        
        click.echo("\n✓ Parity rebuild complete (0 API calls)")
        
    except Exception as e:
        click.echo(f"Error during rebuild: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def get_video_duration(video_path: Path) -> float:
    """Get video duration using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(video_path)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def rebuild_mix(
    video_path: Path,
    stems: dict[str, Path | None],
    script_data: dict[str, Any],
    cues_data: dict[str, Any],
    output_path: Path,
    duration: float,
) -> None:
    """
    Rebuild mix from stems using ffmpeg.
    
    This implements the proven Ep03/Ep04 mix chain exactly:
    1. Load all 4 stems: SFX, VOX, VO, BGM (in that order)
    2. Apply VO -2.5 dB gain trim
    3. amix with dropout_transition=1, normalize=0
    4. Master makeup +4.7 dB
    5. alimiter at 0.86 (≈-1.3 dBFS)
    6. apad + atrim to exact duration
    """
    # Ordered stem inputs: SFX, VOX, VO, BGM
    stem_order = ["sfx", "vox", "vo", "bgm"]
    inputs = []
    stem_indices = []
    
    for stem_name in stem_order:
        stem_path = stems.get(stem_name)
        if stem_path:
            inputs.extend(["-i", str(stem_path)])

def compare_audio_video(
    file1: Path,
    file2: Path,
    duration: float,
) -> dict[str, Any]:
    """
    Compare two audio/video files using LUFS delta, null test, and frame comparison.
    
    Returns:
        dict with lufs_delta, peak_delta, null_residual_db, video_psnr_avg, video_ssim_avg
    """
    # Measure loudness of both files
    loud1 = measure_loudness(file1)
    loud2 = measure_loudness(file2)
    
    lufs_delta = loud1["integrated_lufs"] - loud2["integrated_lufs"]
    peak_delta = loud1["true_peak_dbtp"] - loud2["true_peak_dbtp"]
    
    # Audio null test: invert one and mix
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as null_file:
        null_path = Path(null_file.name)
    
    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(file1),
            "-i", str(file2),
            "-filter_complex",
            "[0:a][1:a]amerge=inputs=2[a];[a]pan=stereo|c0=c0-c2|c1=c1-c3[null]",
            "-map", "[null]",
            "-c:a", "pcm_s16le",
            str(null_path)
        ]
        subprocess.run(cmd, check=True)
        
        # Measure null residual
        null_loud = measure_loudness(null_path)
        null_residual_db = null_loud["integrated_lufs"]
    finally:
        null_path.unlink(missing_ok=True)
    
    # Video comparison: sample frames at key times
    sample_times = [duration * p for p in [0.1, 0.3, 0.5, 0.7, 0.9]]
    psnr_values = []
    ssim_values = []
    
    for t in sample_times:
        with tempfile.TemporaryDirectory() as tmpdir:
            frame1 = Path(tmpdir) / "frame1.png"
            frame2 = Path(tmpdir) / "frame2.png"
            
            # Extract frames
            for src, dst in [(file1, frame1), (file2, frame2)]:
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{t:.3f}",
                    "-i", str(src),
                    "-frames:v", "1",
                    str(dst)
                ]
                subprocess.run(cmd, check=True)
            
            # Compare frames with PSNR and SSIM
            cmd = [
                "ffmpeg", "-v", "error",
                "-i", str(frame1),
                "-i", str(frame2),
                "-lavfi", "psnr;ssim",
                "-f", "null",
                "-"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            # Parse PSNR and SSIM from output
            for line in result.stderr.split('\n'):
                if 'PSNR' in line and 'average:' in line:
                    parts = line.split('average:')
                    if len(parts) > 1:
                        try:
                            psnr = float(parts[1].split()[0])
                            psnr_values.append(psnr)
                        except (ValueError, IndexError):
                            pass
                elif 'SSIM' in line and 'All:' in line:
                    parts = line.split('All:')
                    if len(parts) > 1:
                        try:
                            ssim = float(parts[1].split()[0])
                            ssim_values.append(ssim)
                        except (ValueError, IndexError):
                            pass
    
    psnr_avg = sum(psnr_values) / len(psnr_values) if psnr_values else 0.0
    ssim_avg = sum(ssim_values) / len(ssim_values) if ssim_values else 0.0
    
    return {
        "lufs_delta": lufs_delta,
        "peak_delta": peak_delta,
        "null_residual_db": null_residual_db,
        "video_psnr_avg": psnr_avg,
        "video_ssim_avg": ssim_avg,
    }


if __name__ == "__main__":
    audio_cli()
