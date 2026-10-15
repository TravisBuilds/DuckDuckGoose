# Audio Stage Parity CLI - Implementation Complete

## Summary

All 9 defects identified in your real Ep04 parity test have been fixed. The parity CLI now implements the exact reference mix chain from `build-ep04-mix-v1.py`.

## Changes Made

### 1. ✅ Fixed 4-Stem Mix (was dropping VOX)
**Problem**: Only mixed 3 stems (VO, BGM, SFX), dropping VOX entirely.

**Fix**: Now mixes all 4 stems in correct order: **SFX, VOX, VO, BGM**
- Exact reference filter chain matching `build-ep04-mix-v1.py`:
  - Each stem: `aformat`, `apad` to duration, `atrim` to exact length
  - VO gain trim: **-2.5 dB**
  - `amix` with **inputs=4**, **dropout_transition=1**, **normalize=0**
  - Master makeup: **+4.7 dB**
  - `alimiter=limit=0.86:attack=1:release=60:level=disabled`
  - Final `apad` + `atrim` to exact duration

### 2. ✅ Fixed Stem Detection
**Problem**: `*vo*` glob caught both `vo-bus` and `vox` stems, causing confusion.

**Fix**: Explicit pattern matching with order priority:
```python
# Check 'vox' FIRST before 'vo' to avoid false match
if "vox" in name_lower:
    stem_files["vox"] = stem_path
elif "vo-bus" in name_lower or ("vo" in name_lower and "vox" not in name_lower):
    stem_files["vo"] = stem_path
```

### 3. ✅ Implemented ASS Subtitle Burning
**Problem**: Subtitles were never burned in; only nosubs version was created.

**Fix**: Real ASS burn implementation:
- Finds ASS file in `subs/` directory automatically
- Burns with `ass=subfile.ass:fontsdir=/usr/share/fonts/truetype/sand-box/google/Comfortaa`
- Encodes with `libx264 -preset fast -crf 18 -pix_fmt yuv420p`
- Outputs both:
  - `*-nosubs.mp4` (AAC 192k, copy video, faststart)
  - Final `*.mp4` with burned subs

### 4. ✅ Added Phone Copy Generation
**Problem**: No phone copy was created.

**Fix**: Phone-optimized encode:
- 540px width, auto height (`-vf scale=540:-2`)
- Target **<5 MB** via bitrate calculation:
  - `target_kbps = (5 MB - 0.5 MB audio) * 8 / duration`
  - AAC 128k audio
- `movflags +faststart` for streaming
- Verified: test phone copy is 111 KB (well under 5 MB)

### 5. ✅ Implemented Real Loudness Measurement
**Problem**: 
- Loudness values were hardcoded
- `-v error` hid the ebur128 summary line
- Parser looked for `'peak:'` (lowercase) instead of `'Peak:'`

**Fix**: Real measurement via ffmpeg:
```python
cmd = ["ffmpeg", "-v", "info",  # Changed from "error"
       "-i", str(audio_path),
       "-filter:a", "ebur128=peak=true",
       "-f", "null", "-"]
```
- Parse stderr for:
  - `I:` → integrated LUFS
  - `LRA:` → loudness range
  - `Peak:` (not `peak:`) → true peak dBTP

### 6. ✅ Replaced SHA256 with Audio/Video Null-Test
**Problem**: SHA256 of whole file can never pass after re-encode.

**Fix**: Proper parity comparison:
```python
# Audio null test: invert one track and mix
filter_complex = "[0:a][1:a]amerge=inputs=2[a];[a]pan=stereo|c0=c0-c2|c1=c1-c3[null]"
# Measure residual LUFS
null_residual_db = measure_loudness(null_wav)["integrated_lufs"]

# Video PSNR/SSIM at sample timestamps
sample_times = [duration * p for p in [0.1, 0.3, 0.5, 0.7, 0.9]]
# Compare frames with ffmpeg psnr;ssim filter
```

**Pass criteria**:
- LUFS delta: **≤ 0.2 dB**
- Null residual: **≤ -50 dBFS**
- Clear **PASS/FAIL** output with thresholds

### 7. ✅ Added numpy Dependency
**Fix**: Added `numpy>=1.24.0` to `pyproject.toml` dependencies.

### 8. ✅ CLI Entry Point Working
**Verified**: `hfvg audio rebuild` is properly registered via:
```python
cli.add_command(audio_cli, name="audio")
```

### 9. ✅ All Tests Pass
- 12/12 tests passing
- Tests run in DRY_RUN mode with synthetic fixtures
- No network calls, no real assets committed
- CI is green

## Usage

### Command Structure
```bash
hfvg audio rebuild \
  --script <path-to-script-json> \
  --stems <directory-with-stems> \
  --cues <path-to-cues-json> \
  --video <path-to-picture-locked-video> \
  --output <output-path> \
  [--compare <reference-file-for-parity>]
```

### Real Ep04 Example
Based on your file layout:
```bash
hfvg audio rebuild \
  --script ep04-moose/audio/final/ep04-mix-v1-cues.json \
  --stems ep04-moose/audio/final/stems/ \
  --cues ep04-moose/audio/final/ep04-mix-v1-cues.json \
  --video ep04-moose/drafts/video/EP04-v18c-logo.mp4 \
  --output ep04-moose/drafts/video/EP04-mix-v1-rebuilt.mp4 \
  --compare ep04-moose/drafts/video/EP04-mix-v1.mp4
```

### Expected Inputs
```
ep04-moose/audio/final/
├── stems/
│   ├── ep04-vo-bus-v1.wav
│   ├── ep04-bgm-bed-v1.wav
│   ├── ep04-sfx-ambience-v1.wav
│   └── ep04-vox-v1.wav (optional)
├── subs/
│   ├── ep04-en-v1.ass
│   └── ep04-en-v1.srt
└── ep04-mix-v1-cues.json

ep04-moose/drafts/video/
├── EP04-v18c-logo.mp4 (picture input with logo already baked in)
└── EP04-mix-v1.mp4 (approved reference for comparison)
```

### Expected Outputs
```
ep04-moose/drafts/video/
├── EP04-mix-v1-rebuilt-nosubs.mp4  (AAC 192k, no subs)
├── EP04-mix-v1-rebuilt.mp4         (final with burned subs)
└── EP04-mix-v1-rebuilt-phone.mp4   (540px, <5 MB)
```

## Parity Targets

When comparing against the approved reference:

| Metric | Target | Pass Threshold |
|--------|--------|----------------|
| LUFS delta | ≤ 0.2 dB | Critical |
| Audio null residual | ≤ -50 dBFS | Critical |
| True peak delta | Report only | Info |
| Video PSNR | Report only | Info |
| Video SSIM | Report only | Info |

### Parity Check Output Example
```
=== Parity Check ===
LUFS delta:        0.08 dB
True peak delta:   0.03 dB
Audio null residual: -52.34 dB
Video PSNR (avg):  42.15 dB
Video SSIM (avg):  0.9987

✓ PARITY PASS
```

## Integration Test Results

Verified with synthetic fixtures:
```bash
cd /workspace
python3 -m hfvg.cli audio rebuild \
  --script <synthetic>/script.json \
  --stems <synthetic>/ \
  --cues <synthetic>/cues.json \
  --video <synthetic>/video.mp4 \
  --output <synthetic>/rebuilt.mp4
```

**Results**:
- ✅ All 4 stems detected and mixed
- ✅ No-subs version created (130 KB)
- ✅ Final with subs created (156 KB)
- ✅ Phone copy created (111 KB, well under 5 MB target)
- ✅ Loudness measured correctly
- ✅ 0 API calls made

## Branch & PR Status

- **Branch**: `cursor/audio-stage-with-proven-pipeline-391d`
- **PR**: #8 - https://github.com/TravisBuilds/DuckDuckGoose/pull/8
- **Commit**: `acbd7d8` - "Fix parity CLI: implement real 4-stem mix..."
- **Status**: All tests passing, CI green, ready for real Ep04 testing
- **Constraints Met**:
  - ✅ No real assets committed
  - ✅ No network calls in tests
  - ✅ Parity CLI works with 0 API calls
  - ✅ PR remains unmerged

## Next Steps

1. **Run parity test against real Ep04 files** using the command above
2. **Verify LUFS delta ≤ 0.2 dB** and **null residual ≤ -50 dB**
3. **If parity passes**: The implementation is proven correct
4. **If parity fails**: Report specific measurements and I'll debug further

## Exact Rebuild Command

For your convenience, the exact command to run against your real Ep04 files:

```bash
cd /workspace  # Or wherever your ep04-moose directory lives

hfvg audio rebuild \
  --script ep04-moose/audio/final/ep04-mix-v1-cues.json \
  --stems ep04-moose/audio/final/stems/ \
  --cues ep04-moose/audio/final/ep04-mix-v1-cues.json \
  --video ep04-moose/drafts/video/EP04-v18c-logo.mp4 \
  --output ep04-moose/drafts/video/EP04-mix-v1-rebuilt.mp4 \
  --compare ep04-moose/drafts/video/EP04-mix-v1.mp4
```

Look for the final parity check output showing:
- LUFS delta (should be ≤ 0.2 dB)
- Audio null residual (should be ≤ -50 dBFS)
- Clear **✓ PARITY PASS** or **✗ PARITY FAIL**
