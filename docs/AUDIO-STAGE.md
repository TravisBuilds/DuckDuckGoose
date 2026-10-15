# Audio Stage - DuckDuckGoose Harness

The Audio Stage implements STEP 5 (Script Lock) and STEP 6 (Audio) of the HARNESS-HANDBOOK v1.1 proven pipeline from Mid-Mountain Rest episode production.

## Overview

This module is built from the proven episode pipeline reference (`build-ep04-mix-v1.py`) and implements all the audio production gates and activities:

- **G5.01**: Script gate (draft VO script, extract frames for picture-match check) → **HARD STOP** for approval
- **G6.01**: VO generation (ElevenLabs, voice 'Sora', model `eleven_v3`, time-stretched 0.90x)
- **G6.02**: Music (reuse library first, or ElevenLabs Music behind spend gate ~1,725 credits)
- **G6.03**: SFX + ambience (library + ElevenLabs as needed)
- **G6.05**: Mix assertions (no VO in protected windows, gaps ≥0.4s, LUFS target -12.7±0.5)
- **G6.10**: Final approval gate

## Features

### 1. Script Gate (G5.01)

Draft a VO script (JP + EN) with timecodes, one line per shot:

```python
from hfvg.activities.audio_stage import draft_vo_script

result = await draft_vo_script(
    beatmap_path="ep04/BEATMAP.md",
    shot_list=shots,
)

# Returns:
# - script_json: Path to VO script JSON
# - frame_extracts: List of frame paths for picture-match check
# - lines: List of VO lines with timings
```

**Hard stop**: No audio generation until Travis approves the script.

### 2. VO Generation (G6.01)

Generate ONE voiceover take covering all lines using ElevenLabs:

```python
from hfvg.activities.audio_stage import generate_vo_take, slice_vo_lines

# Generate full take
vo_result = await generate_vo_take(
    script_lines=lines,
    voice_id="4sirbXwrtRlmPV80MJkQ",  # Sora
    model="eleven_v3",
    tags=["[softly]", "[gently]", "[warm smile]"],
)

# Slice into individual lines with time-stretch
sliced = await slice_vo_lines(
    take_path=vo_result["take_url"],
    script_lines=lines,
    stretch_factor=0.90,  # atempo 0.90x
    peak_db=-2.0,
)
```

Includes Whisper transcription for stutter detection.

### 3. Music (G6.02)

Reuse library takes first (0 credits), or generate new:

```python
from hfvg.activities.audio_stage import generate_music

music_result = await generate_music(
    duration=115.0,
    style="Ghibli chamber score",
    reuse_library=True,  # Try library first
    library_path="series/music/",
)

# If reused: cost_credits = 0
# If new: cost_credits ≈ 1,725 (behind spend gate)
```

### 4. SFX (G6.03)

Real species vocalisations + water FX + ambience:

```python
from hfvg.activities.audio_stage import generate_sfx_library

sfx_result = await generate_sfx_library(
    cue_list=[
        {"type": "ambience", "description": "morning mountain air", ...},
        {"type": "event", "description": "door opening", ...},
    ],
    library_path="series/sfx/",
)
```

### 5. Mix (G6.05)

Full mix chain matching Ep03/Ep04 proven settings:

```python
from hfvg.activities.audio_stage import mix_audio_stems, MixConfig

mix_result = await mix_audio_stems(
    video_path="ep04/video/mute.mp4",
    vo_lines=sliced["lines"],
    vo_timings=script_lines,
    music_path=music_result["music_url"],
    sfx_cues=sfx_result["cues"],
    protected_windows=[
        {"start": 83.45, "end": 84.40, "label": "antler drop"},
        {"start": 94.96, "end": 97.46, "label": "logo fade-in"},
    ],
    mix_config=MixConfig().__dict__,
    output_dir="ep04/audio/final/",
)

# Returns:
# - master_path: Final mix WAV
# - nosubs_path: Video with audio, no subs
# - subs_path: Video with burned-in EN subs
# - phone_path: Phone version (<5 MB, 540px wide)
# - loudness: {integrated_lufs, true_peak_dbtp}
# - assertions_passed: True if all G6.05 checks passed
```

### 6. Logo Overlay

Burn logo onto video following Ep01 template:

```python
from hfvg.activities.audio_stage import burn_logo_overlay

logo_path = await burn_logo_overlay(
    video_path="ep04/final/nosubs.mp4",
    logo_path="logo/locked/quacked-concierge-text-watermark-white-trim.png",
    width_pct=0.62,  # 62% of frame width
    fade_in_duration=2.5,
    visible_duration=6.5,  # Last 6.5s
    output_path="ep04/final/with_logo.mp4",
)
```

## Mix Configuration

The `MixConfig` dataclass encapsulates all proven audio settings from Ep03/Ep04:

```python
from hfvg.activities.audio_stage import MixConfig

config = MixConfig(
    # VO settings
    vo_voice="4sirbXwrtRlmPV80MJkQ",  # Sora
    vo_model="eleven_v3",
    vo_tags=["[softly]", "[gently]", "[warm smile]"],
    vo_stretch=0.90,  # atempo
    vo_peak_db=-2.0,
    vo_gain_db=-2.5,  # VO bus trim
    
    # Music settings
    music_bed_db=-30.2,  # Base level
    music_duck_db=-4.0,  # Duck under VO
    music_floor_db=-9.0,  # Never go below this
    
    # SFX settings
    sfx_bed_duck_db=-3.0,  # Ambience duck under VO
    
    # Master settings
    master_makeup_db=4.7,
    limiter_threshold=0.86,  # About -1.3 dBFS
    target_lufs=-12.7,
    target_lufs_tolerance=0.5,
    target_peak_dbtp=-1.0,
    
    # Subtitle settings (WarmVacation style)
    subtitle_style="WarmVacation",
    subtitle_font="Comfortaa",
    subtitle_fontsize=58,
    subtitle_primary_color="&H00E8F8FF",  # Cream
    subtitle_outline_color="&H002A3A5C",  # Navy
    subtitle_outline=5,
    subtitle_shadow=2,
    subtitle_margin_v=240,
    
    # Logo settings
    logo_width_pct=0.62,
    logo_fade_duration=2.5,
    logo_visible_duration=6.5,
)
```

## Parity CLI: Rebuild from Locked Script

The parity CLI proves bit-exact match against approved episodes by rebuilding from locked script + existing stems with **ZERO API calls**:

```bash
# Rebuild mix from locked inputs
hfvg audio rebuild \
    --script ep04/audio/vo-script-v1.json \
    --stems ep04/audio/stems/ \
    --cues ep04/audio/cues-v1.json \
    --video ep04/video/mute.mp4 \
    --output ep04/audio/rebuilt.mp4

# Compare to reference
hfvg audio rebuild \
    --script ep04/audio/vo-script-v1.json \
    --stems ep04/audio/stems/ \
    --cues ep04/audio/cues-v1.json \
    --video ep04/video/mute.mp4 \
    --output ep04/audio/rebuilt.mp4 \
    --compare ep04/audio/approved-mix.mp4
```

Expected input layout (mirroring reference):

```
ep04/audio/
├── stems/
│   ├── ep04-vo-bus-v1.wav
│   ├── ep04-bgm-bed-v1.wav
│   ├── ep04-sfx-ambience-v1.wav
│   └── ep04-vox-v1.wav
├── subs/
│   ├── ep04-en-v1.ass
│   └── ep04-en-v1.srt
├── vo-script-v1.json
└── cues-v1.json
```

The parity command:
1. Loads existing stems (no generation)
2. Applies mix chain from script timings
3. Measures LUFS and true peak
4. Optionally compares bit-exact match to reference

## Gates and Assertions

### G6.05: Mix Assertions (Blocking)

The mixer asserts:

1. **No VO in protected windows**:
   ```python
   for line in vo_lines:
       for window in protected_windows:
           assert line["out"] <= window["start"] or line["in"] >= window["end"]
   ```

2. **Minimum gaps between lines** (≥0.4s):
   ```python
   for i in range(len(lines) - 1):
       gap = lines[i+1]["in"] - lines[i]["out"]
       assert gap >= 0.4
   ```

3. **Last line ends before video end** (≥0.3s):
   ```python
   assert lines[-1]["out"] <= duration - 0.3
   ```

4. **LUFS target** (-12.7 ±0.5):
   ```python
   assert abs(measured_lufs - target_lufs) <= 0.5
   ```

5. **True peak** (≤ -1.0 dBTP):
   ```python
   assert true_peak_dbtp <= -1.0
   ```

### Spend Gates

- **Music generation**: Behind explicit spend approval (≈1,725 credits per take)
- **SFX generation**: Library first, generate only when needed
- **VO generation**: One take per approval cycle, pickups for changes only

## Testing

All activities support dry-run mode (zero API calls):

```bash
# Run tests
pytest tests/test_audio_stage.py -v

# Tests use DRY_RUN=true by default
# Tests create synthetic fixtures (tones, silence)
# No real API calls in CI
```

### Test Coverage

- ✅ Draft VO script
- ✅ Extract frames at timecode
- ✅ Generate VO take (dry-run)
- ✅ Slice VO lines
- ✅ Generate music (library reuse)
- ✅ Generate SFX library
- ✅ Mix audio stems
- ✅ Burn logo overlay
- ✅ MixConfig dataclass
- ✅ Protected windows validation
- ✅ VO line spacing validation
- ✅ Integration test: draft → VO → mix

## Example: Full Audio Pipeline

```python
from hfvg.activities.audio_stage import (
    draft_vo_script,
    generate_vo_take,
    slice_vo_lines,
    generate_music,
    generate_sfx_library,
    mix_audio_stems,
    burn_logo_overlay,
    MixConfig,
)

# 1. Draft script (G5.01)
script = await draft_vo_script(
    beatmap_path="ep04/BEATMAP.md",
    shot_list=shots,
)

# 🛑 HARD STOP: Travis approves script

# 2. Generate VO (G6.01)
vo_take = await generate_vo_take(
    script_lines=script["lines"],
)

# 3. Slice into lines
vo_lines = await slice_vo_lines(
    take_path=vo_take["take_url"],
    script_lines=script["lines"],
    stretch_factor=0.90,
)

# 4. Music (G6.02)
music = await generate_music(
    duration=101.5,
    reuse_library=True,
)

# 5. SFX (G6.03)
sfx = await generate_sfx_library(
    cue_list=cues,
    library_path="series/sfx/",
)

# 6. Mix (G6.05)
mix = await mix_audio_stems(
    video_path="ep04/video/mute.mp4",
    vo_lines=vo_lines["lines"],
    vo_timings=script["lines"],
    music_path=music["music_url"],
    sfx_cues=sfx["cues"],
    protected_windows=protected_windows,
    mix_config=MixConfig().__dict__,
    output_dir="ep04/audio/final/",
)

# 7. Logo
final = await burn_logo_overlay(
    video_path=mix["nosubs_path"],
    logo_path="logo/locked/logo.png",
)

# ✓ Done: final video ready for G6.10 approval
```

## Integration with Temporal

The audio stage integrates with the existing Temporal workflow:

```python
from temporalio import workflow
from hfvg.activities.audio_stage import (
    draft_vo_script,
    generate_vo_take,
    mix_audio_stems,
)

@workflow.defn
class EpisodeWorkflowV2:
    async def run(self, episode_id: str):
        # ... picture lock ...
        
        # STEP 5: Script lock
        workflow.logger.info("=== STEP 5: SCRIPT ===")
        
        script = await workflow.execute_activity(
            draft_vo_script,
            args=[beatmap_path, shot_list],
            start_to_close_timeout=timedelta(minutes=10),
        )
        
        # G5.01: Script approval gate
        await workflow.wait_condition(lambda: self.approved_g501)
        
        # STEP 6: Audio
        workflow.logger.info("=== STEP 6: AUDIO ===")
        
        vo_take = await workflow.execute_activity(
            generate_vo_take,
            args=[script["lines"]],
            start_to_close_timeout=timedelta(minutes=15),
        )
        
        # ... music, sfx, mix ...
        
        mix_result = await workflow.execute_activity(
            mix_audio_stems,
            args=[video_path, vo_lines, ...],
            start_to_close_timeout=timedelta(minutes=20),
        )
        
        # G6.10: Final audio approval
        await workflow.wait_condition(lambda: self.approved_g610)
```

## Reference Implementation

The audio stage is built from the proven Ep04 production pipeline:

- **Reference**: `build-ep04-mix-v1.py` (313 ElevenLabs credits VO, 0 music reused)
- **Handbook**: HARNESS-HANDBOOK.md v1.1, PIPELINE-LESSONS.md §5-6
- **Targets**: -12.7 LUFS integrated ±0.5, true peak ≤ -1.0 dBTP
- **Series**: Mid-Mountain Rest / Quacked Concierge Peak Glow Up

## Constraints

1. **No real assets in public repo**: Use synthetic fixtures for tests
2. **Paid API calls behind gates**: All ElevenLabs calls require approval in production
3. **Tests make no network calls**: DRY_RUN=true in CI
4. **Parity CLI for verification**: Rebuild from locked script with 0 API calls

## Credits Tracking

The audio stage logs all spend:

```python
# Example spend log
{
    "vo_take": {"job_id": "...", "credits": 313},
    "music": {"job_id": None, "credits": 0, "reused": True},
    "sfx": {"generated": [], "credits": 0},
    "total_credits": 313,
}
```

## License

Part of the DuckDuckGoose harness (public repository).
See main README for license details.
