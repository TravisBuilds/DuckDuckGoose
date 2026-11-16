# Balance Alerts and Monitoring

Provider balance monitoring prevents mid-episode stalls from exhausted credits and alerts operators to low balances early.

## Overview

The balance monitor tracks:

- **ElevenLabs**: Live balance from `GET /v1/user/subscription` (character_limit - character_count, converted to USD)
- **Higgsfield**: Derived balance = manual entry - max(harness ledger committed spend, `/models/usage` API spend)

Each balance is labeled with:
- **Source**: `live` (from provider API), `derived` (calculated), or `manual` (admin entry)
- **Age**: seconds since the data was recorded or derived from

## Balance Checks

Balance checks run:

1. **Before any paid stage** (preflight gate): Blocks when `known_balance < estimate + reserve%`
2. **On a schedule**: the worker keeps the Temporal schedule `hfvg-balance-check-schedule`
   (workflow `BalanceCheckWorkflow`, default every 360 minutes, `BALANCE_CHECK_INTERVAL_MINUTES`;
   `BALANCE_CHECK_SCHEDULE_ENABLED=false` removes it). Each run syncs the Higgsfield usage report
   ([ACTUAL-COSTS.md](./ACTUAL-COSTS.md)), reads every balance, sends low / critical / stale alerts
   and checks the manual Higgsfield entry (below). It spends nothing and is skipped in `DRY_RUN`.
3. **On demand** via CLI (`hfvg balance`), API (`GET /api/budget/all-balances`,
   `POST /api/balance/check`) or **Check now** on the master dashboard

### Stored readings

Every read is stored in `provider_balance_readings` with its time, source (`live`, `derived`,
`manual`), why it was read (`preflight`, `schedule`, `on_demand`) and any error, so a failed read
is visible instead of looking like an unchanged balance. The master dashboard shows the latest
readings with their age, the manual entry with the remaining figure, and when the scheduled check
last ran (a warning when it is overdue by more than twice the interval or disabled). `DRY_RUN`
reads mock data and stores nothing.

### Higgsfield: why the reading did not move

Higgsfield has no balance API: the API bills a USD balance on the API console, and the studio's
figure is the admin's manual entry minus the studio's spend since it (the larger of the ledger's
committed spend and the usage report's). Before this change the reading could not move:

* the dashboard showed the manual entry itself ("Manual Balance"), not the remaining figure, so it
  only changed when someone typed a new entry;
* the usage cross-check parsed `usage[].data[].usd`, a shape `/models/usage` does not return (it
  returns `time_series[].results[].cost`), so provider-reported spend always read as nothing.

Both are fixed. The usage figure is read for the hours since the entry (`timeframe=hour`, starting
at the entry, `bound_to_timeframe=false`) and reused for `HIGGSFIELD_USAGE_CACHE_SECONDS` (default
300) per entry; if it fails the ledger figure is used, as before.

A figure that still does not move after spend means the entry is copied from the wrong place. A
balance in **credits** (such as a web app subscription's credits) is a different wallet than the
API's USD balance and is not drawn down by API calls; a different organisation or personal account
than the one owning `HIGGSFIELD_API_KEY` has the same effect. Each scheduled check compares the last
two manual entries: when they are equal although at least $1.00 of Higgsfield spend was committed
between them, it sends an **unchanged balance** alert and the dashboard warns. Enter the USD
balance shown on the API console's billing page for the account that owns the API key.

### Preflight Gate

Every paid generation (still, clip, audio) runs a preflight balance check before reserving funds. The check:

- Calculates required balance = estimate + reserve% (default 20%)
- Blocks if balance < required, **unless** auto top-up is enabled for that provider
- For Higgsfield, warns if manual entry is stale (> max age hours) but doesn't block
- Logs the check result and alerts on failure

With auto top-up enabled, a low balance only warns but allows the generation (the provider will auto-reload). A stale manual entry also warns but doesn't block. Only an observed 403 blocks.

## Alerts

Alerts fire on:

- **Low balance**: Balance < threshold (default $10)
- **Critical balance**: Balance < critical threshold (default $5)
- **Stale manual entry**: Higgsfield manual balance > max age (default 24h)
- **Unchanged balance entry**: two Higgsfield entries are equal although spend happened between them
- **403 Insufficient Balance**: Provider returned 403; workflow paused
- **Auto top-up failure**: Balance didn't recover after expected auto reload
- **Spend summary**: Daily/monthly spend summary (optional, with configurable ceiling)

### Alert Channels

Alerts are sent through:

1. **Email**: SMTP (configured via env vars)
2. **Webhook**: HTTP POST of JSON (`text` and `attachments` for Slack, plus an `alert` object for
   any other receiver)
3. **Logs**: Always logged to `hfvg.balance_notifier`. The webhook URL is a secret and never
   reaches the logs: the API and the worker keep the HTTP client loggers (`httpx`, `httpcore`)
   at WARNING and cut any URL they still log to scheme and host (`hfvg/log_safety.py`)
4. **Database**: Recorded in `balance_alerts` with the destinations tried and each outcome
   (`sent`, the redacted error, or `already sent at <time>` for a destination that delivered it
   earlier in the window) and whether this attempt delivered it

The destination is read from the process environment when an alert is sent (the API and the worker
both send alerts, so set it for both). With no destination (or only incomplete settings) the alert
is recorded as not delivered and the master dashboard shows a warning; incomplete settings are
listed there too.

Admins can send a **test alert** from the master dashboard (`POST /api/alerts/test`; the
destination summary is `GET /api/alerts/status`, with the recipients masked and only the webhook's
host shown). The result per destination is shown next to the button and in the recent alerts list.

### Deduplication

Alerts are deduplicated by alert type, subject, episode / job (for 403s) and the set of
destinations, within a configurable window (default 1 hour). Delivery is tracked per
destination: one that delivered the alert is not sent it again within the window (counted from
that delivery), but:

- a destination whose delivery failed does not count: the next check retries it, and only it
  (when email fails and the webhook delivers, or the other way round, the next check sends the
  failed one again and records the other as `already sent at <time>`);
- a newly configured destination is not held back by an alert recorded before it existed;
- a test alert is held back for 30 seconds only.

## Configuration

### Environment Variables

Add these to `.env`:

```bash
# Balance reserve and thresholds
BALANCE_RESERVE_PERCENT=20                    # Preflight reserve % above estimate (default 20)
BALANCE_LOW_THRESHOLD_USD=10.0                # Low balance warning threshold (default $10)
BALANCE_CRITICAL_THRESHOLD_USD=5.0            # Critical balance alert threshold (default $5)

# Higgsfield manual balance staleness
HFVG_BALANCE_MAX_AGE_HOURS=24                 # Max age for manual Higgsfield entry (default 24h)

# Auto top-up flags (when enabled, low balance warns but doesn't block)
HIGGSFIELD_AUTO_TOPUP_ENABLED=false           # Set to 'true' if Higgsfield auto top-up is ON
ELEVENLABS_AUTO_TOPUP_ENABLED=false           # Set to 'true' if ElevenLabs auto top-up is ON

# Scheduled balance check (Temporal schedule hfvg-balance-check-schedule, kept by the worker)
BALANCE_CHECK_SCHEDULE_ENABLED=true           # false removes the schedule
BALANCE_CHECK_INTERVAL_MINUTES=360            # Minutes between checks (default 360)

# Email alerts (SMTP): HOST and TO required; USER and PASSWORD go together
BALANCE_ALERT_SMTP_HOST=smtp.example.com      # SMTP server hostname
BALANCE_ALERT_SMTP_PORT=587                   # SMTP port (default 587, or 465 with SMTP_SSL)
BALANCE_ALERT_SMTP_USER=alerts@example.com    # SMTP username
BALANCE_ALERT_SMTP_PASSWORD=                  # SMTP password (use secrets manager)
BALANCE_ALERT_SMTP_FROM=alerts@example.com    # From address (default: SMTP_USER)
BALANCE_ALERT_SMTP_TO=ops@example.com,team@example.com  # Comma-separated recipient list
BALANCE_ALERT_SMTP_SSL=false                  # true: implicit TLS (port 465)
BALANCE_ALERT_SMTP_STARTTLS=true              # STARTTLS on a plain connection (default true)

# Webhook alerts (e.g., Slack incoming webhook)
BALANCE_ALERT_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# Alert deduplication
BALANCE_ALERT_DEDUPE_WINDOW_SECONDS=3600      # Dedupe window in seconds (default 1 hour)

# Optional spend summaries
DAILY_SPEND_CEILING_USD=50.0                  # Daily spend ceiling for alerts (optional)
MONTHLY_SPEND_CEILING_USD=1500.0              # Monthly spend ceiling for alerts (optional)
```

**Important**: Never commit real SMTP passwords or webhook URLs to the repository. Use environment variables or a secrets manager.

## Provider Auto Top-Up Setup

### Higgsfield

1. Go to Higgsfield API Console → **Billing** → **Auto top-up**
2. Set minimum balance threshold and reload amount
3. Add payment method
4. Set `HIGGSFIELD_AUTO_TOPUP_ENABLED=true` in your `.env`

Alternatively, via the Higgsfield website:
1. Log in → **Manage Account** → **Subscription**
2. Enable **Credits auto-refill**
3. Set threshold and reload amount

### ElevenLabs

**Important**: The ElevenLabs API key must have TTS, SFX (sound effects), and music generation permissions enabled, plus Voices read access: the create page's voice picker (`POST /api/voices/pick`) lists the account's and the premade voices (listing spends no credits). When creating or regenerating the key in ElevenLabs settings, ensure all of these permission scopes are selected. A key missing any of these permissions will fail with 401 errors when the harness attempts that generation type or lists voices.

1. Log in to ElevenLabs → **Developer settings** → **Top Up**
2. Set auto top-up threshold and reload amount
3. Add payment method
4. Set `ELEVENLABS_AUTO_TOPUP_ENABLED=true` in your `.env`

## CLI Commands

### Check Balance

```bash
# Human-readable output
hfvg balance

# JSON output
hfvg balance --json-output
```

Example output:
```
Provider Balance Status
============================================================
Checked at: 2026-10-27T13:45:23.123456+00:00

ELEVENLABS:
  Balance: $42.50
  Source: live
  Details:
    character_limit: 500000
    character_count: 287500
    remaining_characters: 212500
    status: active

HIGGSFIELD:
  Balance: $78.20
  Source: derived
  Age: 2.3 hours
  Details:
    manual_entry_usd: 82.40
    manual_entry_age_hours: 2.3
    ledger_spend: 4.20
```

## API Endpoints

### Check All Provider Balances

```http
GET /api/balance
Authorization: Bearer <admin_secret>
```

Returns:
```json
{
  "timestamp": "2026-10-27T13:45:23.123456+00:00",
  "providers": {
    "elevenlabs": {
      "provider": "elevenlabs",
      "balance_usd": "42.50",
      "balance_usd_micros": 42500000,
      "source": "live",
      "age_seconds": 0.0,
      "details": {
        "character_limit": 500000,
        "character_count": 287500,
        "remaining_characters": 212500,
        "status": "active"
      }
    },
    "higgsfield": {
      "provider": "higgsfield",
      "balance_usd": "78.20",
      "balance_usd_micros": 78200000,
      "source": "derived",
      "age_seconds": 8280.0,
      "details": {
        "manual_entry_usd": "82.40",
        "manual_entry_age_hours": 2.3,
        "ledger_spend": "4.20"
      }
    }
  },
  "warnings": [],
  "errors": []
}
```

### Get Higgsfield Manual Balance (Legacy GC.01)

```http
GET /api/budget/balance
Authorization: Bearer <admin_secret>
```

See [DOLLAR-LEDGER.md](./DOLLAR-LEDGER.md) for details.

## Testing Locally

### Dry-Run with Mocked Providers

Set these env vars to test balance checks without live provider calls:

```bash
export DRY_RUN=true
export ELEVENLABS_API_KEY=test-key-not-real
export HIGGSFIELD_API_KEY=test-id:test-secret  # format id:secret
```

In dry-run mode:
- Balance checks use mock data
- Preflight gates log checks but don't block
- Alerts are logged but not sent via email/webhook

### Verify Balance Gate

1. Set a low manual Higgsfield balance:
   ```bash
   curl -X POST http://localhost:8000/api/budget/balance \
     -H "Authorization: Bearer $ADMIN_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"balance_usd": "2.00", "note": "Test low balance"}'
   ```

2. Try to start an episode - it should be blocked at preflight with:
   ```
   Higgsfield balance insufficient: $2.00 < required $X.XX
   ```

3. Set auto top-up flag and retry - it should warn but proceed:
   ```bash
   export HIGGSFIELD_AUTO_TOPUP_ENABLED=true
   ```

## 403 Insufficient Balance Handling

When a provider returns 403 Insufficient Balance:

1. The generation activity raises a clean, non-retried hard stop
2. Reserved budget is released back to the line
3. The workflow is paused at a resumable point
4. An alert is sent to all configured channels
5. The event is recorded in `balance_stops` table

The operator should:
1. Add funds to the provider account
2. Resume the workflow (it will retry from the paused point)
3. No double charges occur on resume

## Troubleshooting

### Stale Higgsfield Balance

**Symptom**: Warning or block on stale manual entry

**Fix**: Update the manual balance via API:
```bash
curl -X POST http://localhost:8000/api/budget/balance \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"balance_usd": "100.00", "note": "Updated after top-up"}'
```

### ElevenLabs 401 Unauthorized

**Symptom**: Balance check returns 401 error

**Cause**: API key lacks subscription read permission

**Fix**: The balance check handles this gracefully and returns a $0 balance with an error note. This won't block generations if you have auto top-up enabled. To fix the permission, regenerate the API key in ElevenLabs settings with full permissions.

### Alerts Not Sending

**Symptom**: No emails or webhook notifications

**Debug**:
1. Open the master dashboard: the alerts panel shows the configured destination (or a warning
   when there is none or it is incomplete) and each recent alert's outcome per destination
2. Press **Send test alert** and read the result
3. Check logs for delivery errors: `grep "balance_notifier" <log-file>`
4. Verify SMTP/webhook config in `.env` for both the API and the worker (restart them after a change)
5. Check deduplication: a destination that delivered an alert is not sent it again within the
   window (1 hour by default); a destination that failed is retried at the next check
6. Test SMTP manually:
   ```python
   import smtplib
   server = smtplib.SMTP("smtp.example.com", 587)
   server.starttls()
   server.login("user", "password")
   server.sendmail("from@example.com", "to@example.com", "Subject: Test\n\nBody")
   ```

### Ledger vs Usage API Disagreement

**Symptom**: Warning about spend disagreement

**Cause**: Higgsfield `/models/usage` may lag behind real-time spend, or ledger commits have not yet hit the billing API

**Expected**: Small differences (< $1) are normal. Large disagreements indicate a ledger or API issue.

**Action**: If difference is large and persistent, investigate ledger transactions and provider billing dashboard.
