# Budget Ledger vs Credit Ledger: Design

## System Overview

The harness has **two separate ledgers that run together**:

1. **Budget Ledger** (`hfvg/budget.py`): Per-line USD caps with 80% stop, GC.01 headroom, revision reserve
2. **Credit Ledger** (`hfvg/credit_ledger.py`): Real Higgsfield API credits tracking

## Usage Rules

### Dry Run Mode (`DRY_RUN=true`)
- Uses **Budget Ledger** only
- Budget lines (L1_refs, L2_drafts, L3_final_stills, L4_video, L7_revision) **must exist** in database
- Budget lines track USD caps per line (e.g. L2_drafts: $100 cap, $80 stop)
- Each generation step:
  1. Estimates USD cost
  2. Reserves from appropriate budget line
  3. Commits on success or releases on failure
- No real API credits are consumed

### Live Mode (`DRY_RUN=false`, `live_mode=true`)
- Uses **BOTH ledgers together**
- Budget ledger provides $60 cap, $48 stop (80%), GC.01 headroom, $10 revision reserve
- Credit ledger tracks real Higgsfield API credits consumed from user's account
- Each generation step:
  1. Estimates cost in USD (budget) and credits (credit ledger)
  2. **Reserves from BOTH ledgers** (budget line + credit line)
  3. Commits to both on success
  4. **Releases from BOTH on failure before submit** (prevents leaks)
- Budget ledger enforces spend caps and stops workflow at 80% threshold
- Credit ledger tracks actual API credit consumption

## Implementation

### Still Generation (`hfvg/activities/studio_generation.py`)

```python
# Dry run: budget ledger only
if DRY_RUN:
    ledger = BudgetLedger(db_path)
    await ledger.reserve(episode_id, line_name, estimated_cost, ...)
    # No credit ledger in dry mode
    
# Live mode: BOTH ledgers
else:
    # Reserve from budget ledger (USD caps, stops)
    ledger = BudgetLedger(db_path)
    reserved = await ledger.reserve(episode_id, line_name, estimated_cost, ...)
    if not reserved:
        raise ValueError("Budget reserve failed (at 80% stop threshold)")
    
    # Reserve from credit ledger (real API credits)
    credit_ledger = CreditLedger(db_path)
    credit_result = await credit_ledger.reserve_credits(episode_id, job_id, estimated_credits, ...)
    if not credit_result["reserved"]:
        # Release budget reservation before raising
        await ledger.release(episode_id, line_name, estimated_cost, ...)
        raise InsufficientCreditsError(...)
    
    # Submit to provider
    try:
        provider_job_id = await provider.submit(...)
    except Exception as e:
        # Release BOTH reservations on failure before submit
        await ledger.release(episode_id, line_name, estimated_cost, ...)
        await credit_ledger.release_credits(episode_id, job_id, ...)
        raise
```

### Clip Generation (similar pattern)

Clips follow the same pattern: both ledgers in live mode, budget only in dry mode.

## Episode Setup

### Dry Run Setup
- Creates **budget lines only** (no credit ledger)
- Budget lines imported from gate policy or CREDIT-PLAN.md
- Lines: L1_refs, L2_drafts, L3_final_stills, L4_video, L7_revision
- Example: `ledger.init_episode_budget(episode_id)` creates all lines from policy

### Live Mode Setup
- Creates **BOTH ledgers**:
  1. **Credit ledger**: `init_episode_credit_line_activity(episode_id, cap_credits)`
  2. **Budget ledger**: `init_episode_budget_lines_activity(episode_id)` creates L1-L7 from policy
- Budget lines provide USD caps ($60 episode cap, $48 stop, per-line limits)
- Credit ledger tracks real API credits
- Both are initialized at workflow start (Gate 1) before any generation

## Preflight Validation

Preflight **must verify both ledgers are set up correctly**:

- Dry run mode:
  - Check budget lines exist for all lines the episode will use (L2_drafts, L4_video, etc.)
  - Warn if budget line caps are too low for the planned shots
  - Credit ledger not required in dry mode
- Live mode:
  - Check **budget lines exist** (L1-L7 from policy)
  - Fail clearly if budget lines missing (cannot be created mid-run)
  - Check credit balance exists and is sufficient
  - Warn if either ledger caps are too low for estimated episode cost
  - Both ledgers must be initialized before preflight passes

## Reservation Leak Prevention

When a generation step fails **before** submitting to the provider:
1. **BOTH** ledger reservations must be **released immediately**
2. Use try/except blocks to ensure release on any error path
3. Example:
```python
# Reserve from both ledgers
budget_reserved = await ledger.reserve(episode_id, line_name, estimated_cost, ...)
credit_reserved = await credit_ledger.reserve_credits(episode_id, job_id, estimated_credits, ...)

try:
    result = await provider.submit(...)
    # On success, both reservations stay held until commit
except Exception:
    # Release BOTH reservations if we never submitted
    await ledger.release(episode_id, line_name, estimated_cost, ...)
    await credit_ledger.release_credits(episode_id, job_id, ...)
    raise
```

## Summary

- **Dry run = Budget Ledger only** (per-line USD caps, no real cost)
- **Live mode = BOTH ledgers together** (USD caps + real API credits)
- Budget ledger enforces $60 cap, $48 stop, revision reserve in live mode
- Credit ledger tracks real Higgsfield API credit consumption
- Always release from **BOTH** ledgers on failure before submit (prevents leaks)
