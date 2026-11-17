# Actual costs: what providers charged

The dollar guard ([DOLLAR-LEDGER.md](./DOLLAR-LEDGER.md)) reserves an **estimate** before every paid
job. This page is about the other number: what Higgsfield and ElevenLabs say they **charged**, stored
next to the estimate per job so the master dashboard can show real costs. Nothing here reserves,
caps or gates spend: the estimates guard and the balance gate decide exactly as before.

All amounts are integer micro-dollars (`usd_micros`, 1 USD = 1_000_000).

## Per job (`job_ledger`)

Additive columns (added on the first `BudgetLedger.init_db()` after deploy):

| Column | Meaning |
|---|---|
| `estimate_usd_micros`, `estimate_source` | The estimate: `reservation` (the guard's hold; for Higgsfield the provider's estimate), `elevenlabs_price_estimate` (characters x price), `backfill_job_row` (copied from an old row), `not_recorded` |
| `actual_usd_micros` | What the provider reported it charged, in USD; NULL when nothing was reported |
| `actual_units`, `actual_unit` | The charge as the provider reported it (`credits`, `usd`, or the usage report's unit) |
| `actual_rate_usd` | The USD rate used to convert it (`1` for USD) |
| `actual_source`, `actual_ref` | Where it came from (below) and the field / usage bucket |
| `actual_backfilled`, `actual_recorded_at` | 1 when written by the one-off backfill of existing rows; when it was recorded |

`usd_micros` stays what the guard committed. A job's **spend** on the dashboard is its actual when
one is recorded, otherwise its estimate.

### Sources (`actual_source`)

* `elevenlabs_character_cost_header` - every ElevenLabs response carries a `character-cost` header
  (credits charged). Credits x `ELEVENLABS_USD_PER_CREDIT` (default `0.0002`, the Starter plan's
  per-credit price; set your plan's price / included credits) is the job's actual, recorded at commit
  with the rate used. Calls without the header keep only the price estimate.
* `higgsfield_usage_report` - Higgsfield bills in USD and its documented request status has no cost
  field. The account's usage report (`GET /models/usage`, `time_series[].results[]` with model,
  unit, quantity, cost, currency) is read per UTC day and matched to jobs (below).
* `higgsfield_status_charge` - if a completed status body ever states a charge (`charged_usd`,
  `cost_usd`, `price_usd`; or `credits_charged` / `charged_credits` / `credits_used` /
  `credits_spent`; or `cost` with `currency` USD / credits, also inside `billing` / `usage`), it is
  recorded after the guard's commit. Credits are converted only with `HIGGSFIELD_USD_PER_CREDIT`
  set; a bare `cost` with no unit is ignored. A per-request charge is never replaced by the usage
  report.
* `cost_tracker_<cost_source>` - actuals the older cost tracker wrote (e.g. 0 for a failed job),
  labelled by the backfill.

### Backfill

`BudgetLedger.init_db()` checks (read-only first, so it is cheap) for rows written before this
tracking existed and fills them; running it again changes nothing:

* estimate: Higgsfield rows and ElevenLabs rows committed at their estimate copy `usd_micros`
  (`backfill_job_row`); other ElevenLabs rows (the header replaced the number) are `not_recorded`;
* ElevenLabs rows committed from the header kept its credits: those credits at
  `ELEVENLABS_USD_PER_CREDIT` become the actual, `actual_backfilled = 1`, dated by the row's update;
* actuals the older cost tracker wrote get the source `cost_tracker_<cost_source>`;
* Higgsfield charges recorded in credits get a dollar value once `HIGGSFIELD_USD_PER_CREDIT` is set.

Nothing is invented: rows without provider data keep no actual. Higgsfield history is filled by the
usage report sync (up to `HIGGSFIELD_USAGE_LOOKBACK_DAYS`, default 35 days, back).

## Matching the Higgsfield usage report to jobs

`hfvg/provider_usage.py`, run by the scheduled balance check, by "Sync now" on the dashboard
(`POST /api/costs/sync`) and never in `DRY_RUN`:

1. Read daily buckets for the lookback window in 31-day ranges (the API limit), every page, with
   `expand=auth_method` so other API keys' spend can be told apart. Store them in
   `provider_usage_buckets`; a bucket whose numbers change is matched again.
2. Wait until a day has settled (`HIGGSFIELD_USAGE_SETTLE_HOURS`, default 6 hours after the UTC day
   ends): the report is not a billing record and can lag.
3. For each (day, model): the studio's committed jobs of that model that day (reference images from
   the `L1_refs` line count as generations of the reference model). If the provider's generation
   count equals the ledger's, the reported cost is split over those jobs pro rata to their estimates
   (exact integer split) and stored as their actual.
4. Anything else stays **unmatched** with the reason (different counts, no studio job that day, a
   unit that is not a generation count) and is shown on the dashboard. Rows of other API keys are
   `other_api_key`. Nothing is forced onto jobs.

Every sync is recorded in `cost_sync_runs` (errors redacted). A failed or never-run sync is a
dashboard warning.

## Dashboard (`GET /api/dashboard` -> `cost_truth`)

* **Actual vs estimate** per provider, stage (budget line) and episode: spend, provider-reported,
  estimated, coverage, and the difference *over jobs that have both numbers only* (a job without an
  actual never reads as an exact estimate).
* **Live mood boards** ([WORLDS.md](./WORLDS.md#live-mood-boards)) are booked under
  `universe-<series id>` on the line `U1_mood_board` (stage `mood_boards`), with the Universe's owner
  on each job. That key is not an episode: it is listed with the episodes, is never finished, and its
  spend counts in "all spend per finished episode" like that of an unfinished episode.
* **Coverage** = provider-reported dollars / spend dollars.
* **In flight**: jobs still holding a reservation are not spend yet and are listed separately;
  released jobs cost nothing.
* **Cost per finished episode** = spend of finished episodes / their number; **per finished minute**
  = spend of finished episodes with a delivery manifest / their minutes of picture. Finished =
  final cut approved (`approve_g_final_cut`) with paid spend, unless the delivery manifest says dry
  run; minutes come from `delivery/delivery_manifest.json` (`picture_duration_s`). "All spend per
  finished episode" also carries the spend of unfinished episodes.
* **Month-end projection** = this month's spend so far / days elapsed (at least 1, in fractions of a
  day) x days in the month, with the share that is provider-reported. Spend so far is committed jobs
  at their best number plus any charge a provider already reported for a job still in flight; a hold
  is not spend until its job commits. There is one projection: `monthly_runrate` (the ledger
  run-rate, also per person on `GET /api/users/{id}`) uses the same rule and, on the dashboard, the
  same moment, so its numbers equal `cost_truth.monthly`. The page no longer shows a separate
  run-rate card.
* **Headline** = the "Spend (all committed jobs)" card: committed jobs at their best number.
  `spend_summary.headline_*` carries the same figure (with coverage and whether it is actual,
  estimated or both), and the page's CSV and JSON exports lead with that headline, the projection and
  the in-flight holds, so they move with the page when a backfill adds actuals.
* **Spend by person**: the person who started the run each job belongs to (`episode_runs`, recorded
  at start, start again and reopen), unless an admin assigned the episode to someone
  (`PUT /api/dashboard/episodes/{id}/attribution`; an episode id the studio has never seen is a 404);
  then the episode's owner, then the owner on the
  job. The shared `ADMIN_SECRET` login (user id `admin`), `legacy/shared` and `system` name nobody:
  that spend is **unattributed** and the dashboard asks for an assignment. Assigning does not change
  who may edit the episode.
* **Warnings**: partial coverage, usage report never read or failing, unmatched provider spend,
  shared-login spend, a bad `HIGGSFIELD_USD_PER_CREDIT`, no alert destination, an overdue or disabled
  balance check, a Higgsfield balance entry that did not move after spend
  ([BALANCE-ALERTS.md](./BALANCE-ALERTS.md)).

## Admin routes

| Route | What it does |
|---|---|
| `GET /api/dashboard` | Adds `cost_truth`, `balances`, `alerts`, `cost_warnings` |
| `PUT /api/dashboard/episodes/{id}/attribution` `{"user_id", "note"}` | Attribute an episode's spend to a person |
| `DELETE /api/dashboard/episodes/{id}/attribution` | Back to automatic attribution |
| `POST /api/costs/sync` | Read the usage report now and match settled days |
| `POST /api/balance/check` | Read every balance now (stored as `on_demand` readings) |
| `GET /api/alerts/status`, `POST /api/alerts/test` | Alert destinations and a test alert |

All are admin-only and make no paid provider call.

## Environment

| Variable | Default | Use |
|---|---|---|
| `ELEVENLABS_USD_PER_CREDIT` | `0.0002` | USD per ElevenLabs credit (header actuals and reservations) |
| `HIGGSFIELD_USD_PER_CREDIT` | unset | Converts a status charge reported in credits; unset = not converted |
| `HIGGSFIELD_USAGE_SETTLE_HOURS` | `6` | Hours after a UTC day before its usage is matched |
| `HIGGSFIELD_USAGE_LOOKBACK_DAYS` | `35` | Days each sync reads back |
| `HIGGSFIELD_USAGE_CACHE_SECONDS` | `300` | Reuse of the usage figure by balance reads of the same entry |
