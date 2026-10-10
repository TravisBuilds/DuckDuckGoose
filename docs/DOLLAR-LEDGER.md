# Dollar ledger (P1)

All budget math is in **integer micro-dollars** (`usd_micros`; 1 USD = 1_000_000). Floats are
refused by the ledger (`TypeError`). Credits are no longer a cap currency.

## Defaults (env-overridable)

| Setting | Default | Env |
|---|---|---|
| Episode cap | 60.00 USD (`60_000_000`) | `EPISODE_CAP_USD` |
| Episode stop | 80 % = 48.00 USD (`48_000_000`, integer math) | `EPISODE_STOP_PERCENT` |
| GC.01 headroom | 5.00 USD (`5_000_000`) | `GC01_HEADROOM_USD` |
| Revision reserve | 10.00 USD (`10_000_000`), line `L7_revision` | `REVISION_RESERVE_USD` |

* Past the episode **stop** a reserve returns `False`; past the episode **cap** (or a line cap) it raises.
  At exactly the stop the next reserve is refused. Each line also has its own cap and 80 % stop.
* **GC.01**: Higgsfield has no balance endpoint, so an admin records the balance
  (`POST /api/budget/balance {"balance_usd": "82.40"}`, timestamped, audit-logged). A new paid job is
  refused when `manual_balance - committed spend since the entry - reserved - new hold < headroom`.
  Live paid jobs (`require_balance=True`) are refused outright if no balance was ever recorded.
* **Revision reserve** (`L7_revision`) is outside the episode cap/stop and can only be reserved with the
  approval tag `revision` (`tier="revision"` on the submit activities). It has no 80 % stop (stop == cap).
* Reserve / commit / release are atomic (`BEGIN IMMEDIATE`), never drive a hold negative, and commit/release
  are idempotent per provider job id (`job_ledger.status`).
* Commit on overage spends the **actual** cost and logs an `overage_warning`.

## Cost source

1. Provider estimate USD (`POST /estimate/{model}` -> `{"credits": "...", "usd": "..."}`), converted exactly with
   `Decimal`, rounded **up** to the next micro-dollar.
2. If an estimate carries no USD (pricing *description*, e.g. Seedance), the per-second rate table in
   `hfvg/config.py` (`MODEL_USD_PER_SECOND_MICROS`, override `MODEL_USD_PER_SECOND_JSON`). These rates are
   **LIST PRICE BEFORE DISCOUNT** and **derived** (not quoted by an API); real billing may be lower.
   Stills have no rate table: no USD in the estimate => refused (fail closed).
3. Status responses carry credits, not USD, so a completed job commits the held estimate.

## Per-job ledger

Table `job_ledger`: job id, shot id, model, tier (`draft|final|retry|revision`), `usd_micros`, status
(`reserved|pending_reconcile|committed|released`), verdict, used/unused.
`GET /api/episodes/{id}/jobs` returns it; `POST /api/episodes/{id}/jobs/{job_id}/verdict` sets verdict/used.

## Legacy

CREDIT-PLAN.md and HARNESS-GATES.json were authored in Higgsfield app credits. Episode lines come from the
policy's USD section (`usd.ep04_lines_app_usd`) when present; a plan file is converted once at 0.0475 USD per
app credit. An old credit-unit `budget_lines` table is renamed `budget_lines_legacy_credits` (kept, never read).
