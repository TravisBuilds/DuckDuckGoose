# Gate policy (HARNESS-GATES.json)

The gate policy lists the harness gates, the budgets and rules, and the gate ids that need the
owner's approval. `hfvg.gates.load_policy()` loads it.

## Where the policy is loaded from

The search runs in this order. The first file found wins:

1. **`HFVG_GATE_POLICY`**: the path to an explicit policy file. If it is set but the file does not
   exist, the load **fails**. It never falls back silently to another policy.
2. **`HFVG_RUN_DIR`**: `<run dir>/HARNESS-GATES.json`, then `<run dir>/policy/HARNESS-GATES.json`.
3. **`examples/<project>/HARNESS-GATES.json`** in the repo. This is the public fallback, and
   `<project>` is `HFVG_PROJECT` (default `demo`).

Steps 1 and 2 apply only to the active project (`HFVG_PROJECT`). If code explicitly loads a
different project, it only uses the examples fallback, so a private run policy never leaks into
another project's lookup.

Keep private policies (real budgets, real names) in your run directory and point
`HFVG_GATE_POLICY` or `HFVG_RUN_DIR` at it. Never commit them.

## Validation

Each of these is a clear error that names the file:

- invalid project name: `ValueError`
- file not valid JSON: `ValueError`
- policy fails schema validation: `ValueError`
- missing `HFVG_GATE_POLICY` file: `FileNotFoundError`
- no policy in any location: `FileNotFoundError`, listing every path searched

## `owner_approval_points` (required)

```json
"owner_approval_points": ["G1.01", "G2.01", "G_FINAL.01"]
```

This is the list of gate id strings that need the owner's approval. The key is **required**. A
policy without it fails to load and names the key and the file, because an empty default would
silently drop every owner approval point. The value must be a list of strings.

The older key `project_owner_approval_points` is still accepted, with a `DeprecationWarning`. Rename
it to `owner_approval_points`. When both keys are present, `owner_approval_points` wins.
