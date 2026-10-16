# Budget Ledger vs Credit Ledger: Design

## System Overview

The harness has **two separate ledgers**:

1. **Budget Ledger** (`hfvg/budget.py`): Per-line USD caps for dry runs
2. **Credit Ledger** (`hfvg/credit_ledger.py`): Real Higgsfield API credits for live mode

## Usage Rules

### Dry Run Mode (`DRY_RUN=true`)
- Uses **Budget Ledger** only
- Budget lines (L1_refs, L2_drafts, L3_final_stills, L4_video, etc.) **must exist** in database
- Budget lines track USD caps per line (e.g. L2_drafts: $100 cap, $80 stop)
- Each generation step:
  1. Estimates USD cost
  2. Reserves from appropriate budget line
  3. Commits on success or releases on failure
- No real API credits are consumed

### Live Mode (`DRY_RUN=false`, `live_mode=true`)
- Uses **Credit Ledger** only
- Budget lines are **not required** and **not checked**
- Credit ledger tracks real Higgsfield API credits (consumed from user's account)
- Each generation step:
  1. Estimates cost in credits
  2. Reserves credits via `CreditLedger.reserve_credits()`
  3. Deducts credits on provider submit
  4. Releases reservation on failure **before submit**
- Budget ledger is completely bypassed

## Implementation

### Still Generation (`hfvg/activities/studio_generation.py`)

```python
if DRY_RUN:
    # Dry run: use budget ledger with line caps
    ledger = BudgetLedger(db_path)
    await ledger.reserve(episode_id, line_name, estimated_cost, ...)
else:
    # Live mode: use credit ledger only (skip budget lines)
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.reserve_credits(episode_id, job_id, estimated_credits, ...)
```

### Clip Generation (similar pattern)

Clips follow the same rule: dry run uses budget ledger (L4_video line), live mode uses credit ledger only.

## Episode Setup

### Dry Run Setup
- Must create budget lines for the episode (import from CREDIT-PLAN.md or cap/plan JSON)
- Budget lines track remaining budget per line
- Example: `ledger.set_line(episode_id, "L2_drafts", cap_usd_micros=100_000_000, stop_usd_micros=80_000_000)`

### Live Mode Setup
- Only creates credit ledger entry (episode row with credit balance)
- Budget lines are **not created** (saves setup complexity)
- Credit ledger tracks total credits, not per-line caps

## Preflight Validation

Preflight **must verify the ledger mode matches the run mode**:

- Dry run mode:
  - Check budget lines exist for all lines the episode will use (L2_drafts, L4_video, etc.)
  - Warn if budget line caps are too low for the planned shots
- Live mode:
  - Check credit balance exists and is sufficient
  - **Do not** require budget lines (they won't exist)
  - Warn if credit balance is too low for estimated episode cost

## Credit Leak Prevention

When a generation step fails **before** submitting to the provider:
1. The credit reservation must be **released immediately**
2. Use try/finally blocks to ensure release on any error path
3. Example:
```python
reservation = await credit_ledger.reserve_credits(...)
try:
    result = await provider.generate(...)
    # Credits deducted by provider on success
except Exception:
    # Release reservation if we never submitted
    await credit_ledger.release_credits(episode_id, job_id, reservation["amount"])
    raise
```

## Summary

- **Dry run = Budget Ledger** (per-line USD caps, no real cost)
- **Live mode = Credit Ledger** (real API credits, no budget lines)
- Never mix the two in a single run
- Always release reservations on failure before submit
