# refs.json Schema

Reference images configuration for episode workflows. Defines which reference images (character sheets, lineup shots, etc.) need to be generated and maps them to shots.

## Location

`data/episodes/{episode_id}/refs.json`

Or override via environment variables:
- `REFS_JSON_PATH`: Full path to refs.json
- `EPISODES_DIR`: Base directory (refs.json expected at `{EPISODES_DIR}/{episode_id}/refs.json`)

## Schema

```json
{
  "episode_id": "ep04",
  "per_shot": {
    "A01": [
      {
        "key": "r_duck_sheet",
        "path": "data/episodes/ep04/refs/duck_sheet.png",
        "url": null,
        "exists": false
      }
    ]
  },
  "to_generate": { ... },
  "prompts": { ... }
}
```

### Required Fields

- **`episode_id`**: Episode identifier (string)
- **`per_shot`**: Map of shot IDs to their reference lists (object)

### Optional Fields

- **`to_generate`**: References to generate before workflow starts (map or list, see below)
- **`prompts`**: Prompt text for references (object, keys match to_generate keys)

## per_shot Structure

Maps each shot ID to a list of reference images it uses.

```json
"per_shot": {
  "A01": [
    {
      "key": "r_duck_sheet",
      "path": "data/episodes/ep04/refs/duck_sheet.png",
      "url": null,
      "exists": false
    },
    {
      "key": "r_lineup",
      "path": "data/episodes/ep04/refs/lineup.png",
      "url": "https://example.com/lineup.png",
      "exists": true
    }
  ],
  "A02": [...]
}
```

**Entry Fields:**
- `key`: Reference identifier (string, required)
- `path`: Local filesystem path (string, required)
- `url`: Optional URL if fetched from remote (string or null)
- `exists`: Whether file exists on disk at workflow start (boolean, optional)

## to_generate Structure

Defines which references need to be generated. Supports two formats:

### Format 1: Map (Recommended for Simplicity)

```json
{
  "to_generate": {
    "r_duck_sheet": "data/episodes/ep04/refs/duck_sheet.png",
    "r_birch_sheet": "data/episodes/ep04/refs/birch_sheet.png",
    "r_lineup": "data/episodes/ep04/refs/lineup.png"
  },
  "prompts": {
    "r_duck_sheet": "Duck character sheet, full body, front and side view, white background, character design",
    "r_birch_sheet": "Birch moose character sheet, full body, front and side view, white background",
    "r_lineup": "All characters in a lineup, side by side, same scale, white background"
  }
}
```

**Map Format:**
- Keys: Reference identifiers (must match keys in per_shot)
- Values: Output file paths
- Prompts: Required, provided in separate `prompts` object with matching keys
- Defaults: `type` = "character_sheet", `version` = 1

### Format 2: List (Full Control)

```json
{
  "to_generate": [
    {
      "key": "r_duck_sheet",
      "prompt": "Duck character sheet, full body, front and side view, white background, character design",
      "output_path": "data/episodes/ep04/refs/duck_sheet.png",
      "type": "character_sheet",
      "version": 1
    },
    {
      "key": "r_lineup",
      "prompt": "All characters in a lineup",
      "output_path": "data/episodes/ep04/refs/lineup.png",
      "type": "lineup",
      "version": 1
    }
  ]
}
```

**List Format Entry:**
- `key`: Reference identifier (string, required)
- `prompt`: Generation prompt (string, required - inline or from `prompts` map)
- `output_path`: Where to save generated image (string, required)
- `type`: Reference type (string, optional, default: "character_sheet")
  - Examples: "character_sheet", "lineup", "location", "prop"
- `version`: Version number for idempotency (integer, optional, default: 1)
  - Increment when regenerating/rerolling the same reference

**Mixing Formats:**
You can provide `prompt` inline in list entries OR reference the `prompts` map:

```json
{
  "to_generate": [
    {
      "key": "r_duck_sheet",
      "output_path": "data/episodes/ep04/refs/duck_sheet.png",
      "type": "character_sheet"
    }
  ],
  "prompts": {
    "r_duck_sheet": "Duck character sheet, full body view"
  }
}
```

## Live Mode Requirements

In live mode (DRY_RUN=false):

1. **refs.json must exist** - Workflow fails before lock if missing
2. **All needed refs must be available** - Each ref used by shots must be either:
   - Present on disk at its specified `path`, OR
   - Listed in `to_generate` for generation

If a shot needs a ref that is neither on disk nor in to_generate, the workflow fails fast with a clear error.

## Generation Flow

1. After G1 approval, workflow loads `to_generate` from refs.json
2. Filters to only refs needed by `shots_subset` (if canary mode)
3. For each ref to generate:
   - Reserves credits from episode cap
   - Submits image generation with idempotent job ID: `ref-{episode}-{key}-v{version}`
   - Polls until complete
   - Runs QC (file size, valid image, dimensions)
   - Writes to `output_path`
   - Commits credits on success, releases on failure
4. Generated refs are available for shots at G2 (presented with scene stills)

## Rerolling References

To regenerate a reference with a new prompt or settings:

1. Update the prompt in refs.json
2. Increment the `version` number
3. Restart the workflow

The new version gets a unique job ID: `ref-ep04-r_duck_sheet-v2`

## Example: Complete refs.json

```json
{
  "episode_id": "ep04",
  "per_shot": {
    "A01": [
      {"key": "r_duck_sheet", "path": "data/episodes/ep04/refs/duck_sheet.png", "exists": false},
      {"key": "r_lineup", "path": "data/episodes/ep04/refs/lineup.png", "exists": false}
    ],
    "A02": [
      {"key": "r_birch_sheet", "path": "data/episodes/ep04/refs/birch_sheet.png", "exists": false},
      {"key": "r_lineup", "path": "data/episodes/ep04/refs/lineup.png", "exists": false}
    ],
    "A03": [
      {"key": "r_duck_sheet", "path": "data/episodes/ep04/refs/duck_sheet.png", "exists": false}
    ]
  },
  "to_generate": {
    "r_duck_sheet": "data/episodes/ep04/refs/duck_sheet.png",
    "r_birch_sheet": "data/episodes/ep04/refs/birch_sheet.png",
    "r_lineup": "data/episodes/ep04/refs/lineup.png"
  },
  "prompts": {
    "r_duck_sheet": "Duck character, orange beak and feet, white body with grey-blue back, character sheet with front and side view, white background, detailed feathers",
    "r_birch_sheet": "Birch the moose, large brown antlers, tan-brown fur, kind eyes, character sheet with front and side view, white background, character design",
    "r_lineup": "Duck and Birch standing side by side at same scale for height comparison, both facing forward, white background"
  }
}
```

## Notes

- Character sheets typically need front + side views in prompt
- Use white backgrounds for character sheets (easier to composite)
- Lineup shots help maintain scale consistency across the episode
- Reference keys (e.g., `r_duck_sheet`) should be descriptive and consistent
- Generated images use SOUL V2 model by default (editorial/portrait quality)
