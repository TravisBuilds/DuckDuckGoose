# refs.json Schema

Episode reference images are specified in `data/episodes/{episode_id}/refs.json`.

## Schema Versions

### 1. per_shot Schema (Shot Execution)

Used by `load_shot_plans` for shot-level ref resolution:

```json
{
  "per_shot": {
    "A01": [
      {"key": "r_duck", "path": "data/episodes/ep05/refs/duck.png"},
      {"key": "r_pool", "url": "https://example.com/pool.jpg"}
    ],
    "A02": []
  }
}
```

- `per_shot`: Object mapping shot_id to array of ref entries
- Each ref entry:
  - `key`: Unique identifier (e.g., `r_duck`)
  - `path`: Local file path (checked at shot runtime)
  - `url`: Remote URL (alternative to path)

### 2. available/to_generate Schema (Ref Generation Workflow)

Used by ref generation activities to track which refs exist vs need generation:

```json
{
  "available": [
    {
      "key": "r_duck",
      "output_path": "data/episodes/ep05/refs/duck.png",
      "type": "character"
    }
  ],
  "to_generate": [
    {
      "key": "r_pool",
      "prompt": "Olympic-sized swimming pool, aerial view, sunny day",
      "output_path": "data/episodes/ep05/refs/pool.png",
      "type": "scene",
      "version": 2
    }
  ],
  "versions": {
    "r_pool": 2,
    "r_duck": 1
  }
}
```

- `available`: Refs that already exist on disk (ready to use)
- `to_generate`: Refs that need generation
- `versions`: Optional map of `{key: version_number}` (defaults to 1 if omitted)

## Versioning

### Version Tracking

Each ref has a version number (default 1). Versions are tracked in:
1. `versions` map in refs.json
2. Versioned output paths OR version sidecar files

### Versioned Storage

When a ref is versioned, old versions are automatically archived:

```
data/episodes/ep05/refs/
├── pool.png              # Current version (v2)
├── _v1/
│   └── pool.png          # Archived v1
└── .versions/
    └── pool.json         # Sidecar: {"version": 2}
```

### Reuse Logic

Ref generation checks for reuse:
1. File exists at `output_path`
2. File's version matches entry's version
3. File passes QC (dimensions, format)

If version mismatch: regenerate and archive old version to `refs/_v{old_version}/`.

### Version Bumps

To regenerate a ref:
1. Increment version in `versions` map or `to_generate` entry
2. Old file automatically moved to `refs/_v{n}/` on next generation
3. New file written to `output_path`

Example:
```json
{
  "to_generate": [
    {
      "key": "r_pool",
      "prompt": "Pool with updated lighting",
      "output_path": "data/episodes/ep05/refs/pool.png",
      "version": 3
    }
  ],
  "versions": {
    "r_pool": 3
  }
}
```

## Preflight Validation

Both schemas are validated:
- `per_shot`: Checks that ref paths/URLs are accessible
- `available`/`to_generate`: Checks that available refs exist, to_generate have prompts
- Version consistency: available refs match their declared version

## Migration

Episodes can use either schema independently:
- Only `per_shot`: Shot execution works, no ref generation
- Only `available`/`to_generate`: Ref generation works, must be converted to per_shot for execution
- Both: Full workflow support

Preflight accepts both schemas and validates accordingly.
