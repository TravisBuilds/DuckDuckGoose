# Episode Input Files

An episode workflow requires several input files in `data/episodes/{episode_id}/`:

## Required Files

### 1. `beatmap.txt`
- **Purpose:** Timeline structure defining shot IDs, scenes, and screen times
- **Format:** Text file with shot definitions (e.g., `A01 (2.5s)`)
- **Validation:** Must be parseable, shot IDs must match `shots.json`
- **Used by:** Episode workflow to structure timeline

### 2. `shots.json`
- **Purpose:** Per-shot generation parameters (prompts, model, duration, aspect ratio)
- **Format:** JSON object with shot_id keys
- **Validation:** 
  - Shot IDs must match beatmap
  - Each shot must have `prompt`, `params.model`, `params.duration`, `params.aspect_ratio`
  - Live mode: refs in prompts must exist or be in `to_generate`
- **Used by:** Shot generation activities

### 3. `refs.json`
- **Purpose:** Reference image specifications for character sheets and scene refs
- **Format:** JSON object with `available` and optional `to_generate` arrays
- **Fields:**
  - `key`: Unique ref identifier (e.g., `r_duck`)
  - `output_path`: Where the ref image is/will be stored
  - `type`: "character" or "scene"
  - For `to_generate`: `prompt` and `version` (default 1)
- **Validation:**
  - All `available` refs must have existing files at `output_path`
  - All `to_generate` refs must have valid prompts
  - Combined set must cover all refs used in `shots.json`
- **Used by:** Ref generation and shot prompts

### 4. `prompt_kit.json`
- **Purpose:** Prompt engineering guide and templates
- **Format:** JSON object with prompt guidance, style templates, and ref usage examples
- **Validation:** Must be valid JSON
- **Used by:** Shot generation for prompt formatting

### 5. Reference Images (if using pre-existing refs)
- **Purpose:** Character sheets, scene references, style guides
- **Location:** Paths specified in `refs.json` `available` entries
- **Format:** PNG or JPG images
- **Validation:** 
  - Files must exist at specified paths
  - Must pass dimension/format QC (min 512px, valid image)
- **Used by:** Shot generation prompts

## Configuration Files

### `.cursor/environment.json` (for live mode)
- **Purpose:** Environment configuration including API keys
- **Required for live mode:** `HIGGSFIELD_API_KEY` environment variable
- **Format:** JSON with `env` object
- **Validation:** API key must be present and valid format in live mode

## Preflight Validation

Before spending credits at gate 1, the episode workflow validates:
1. ✅ Beatmap exists and is parseable
2. ✅ `shots.json` exists, shot IDs match beatmap
3. ✅ `refs.json` exists and is valid
4. ✅ `prompt_kit.json` exists and is valid JSON
5. ✅ All required refs (used in shots) either exist or are in `to_generate`
6. ✅ Model configs are valid (supported model, valid params)
7. ✅ Live mode: API key is configured

If validation fails, all problems are reported in one error before any generation starts.

## Example Structure

```
data/episodes/ep05/
├── beatmap.txt          # Required: timeline structure
├── shots.json           # Required: shot prompts and params
├── refs.json           # Required: ref specifications
├── prompt_kit.json     # Required: prompt engineering guide
├── refs/               # Optional: pre-existing refs
│   ├── r_duck.png
│   └── r_scene_park.png
└── outputs/            # Created during generation
    ├── A01.mp4
    └── A02.mp4
```

## Validation Errors

### Missing Files
```
PreflightError: Episode ep05 missing required files:
  - prompt_kit.json
  - refs.json
```

### Shot/Beatmap Mismatch
```
PreflightError: shots.json IDs differ from beatmap:
  - Only in beatmap: ['A03']
  - Only in shots.json: ['B04']
```

### Missing Refs
```
PreflightError: Refs used in shots but not available or to_generate:
  - r_duck (used in shots: A01, A02)
  - r_scene_park (used in shots: A03)
```

### Invalid Model Config
```
PreflightError: Invalid model configs:
  - Shot A01: unsupported model 'invalid-model'
  - Shot A02: invalid aspect_ratio '16:9:1'
```

### Missing API Key (live mode)
```
PreflightError: Live mode requires HIGGSFIELD_API_KEY environment variable
```
