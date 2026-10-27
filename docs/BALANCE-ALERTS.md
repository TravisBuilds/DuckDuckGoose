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
2. **Periodically** via Temporal schedule (default: hourly, when enabled)
3. **On demand** via CLI (`hfvg balance`) or API (`GET /api/balance`)

### Preflight Gate

Every paid generation (still, clip, audio) runs a preflight balance check before reserving funds. The check:

- Calculates required balance = estimate + reserve% (default 20%)
- Blocks if balance < required, **unless** auto top-up is enabled for that provider
- For Higgsfield, blocks if manual entry is stale (> max age hours) **unless** auto top-up is enabled
- Logs the check result and alerts on failure

With auto top-up enabled, a low balance only warns but allows the generation (the provider will auto-reload). A stale manual entry or an observed 403 still blocks.

## Alerts

Alerts fire on:

- **Low balance**: Balance < threshold (default $10)
- **Critical balance**: Balance < critical threshold (default $5)
- **Stale manual entry**: Higgsfield manual balance > max age (default 24h)
- **403 Insufficient Balance**: Provider returned 403; workflow paused
- **Auto top-up failure**: Balance didn't recover after expected auto reload
- **Spend summary**: Daily/monthly spend summary (optional, with configurable ceiling)

### Alert Channels

Alerts are sent through:

1. **Email**: SMTP (configured via env vars)
2. **Webhook**: HTTP POST (e.g., Slack incoming webhook)
3. **Logs**: Always logged to `hfvg.balance_notifier`
4. **Database**: Recorded in `balance_alerts` table

### Deduplication

Alerts are deduplicated by `(alert_type, subject)` within a configurable window (default 1 hour). The same alert won't be sent twice within the window.

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

# Email alerts (SMTP)
BALANCE_ALERT_SMTP_HOST=smtp.example.com      # SMTP server hostname
BALANCE_ALERT_SMTP_PORT=587                   # SMTP port (default 587 for STARTTLS)
BALANCE_ALERT_SMTP_USER=alerts@example.com    # SMTP username
BALANCE_ALERT_SMTP_PASSWORD=                  # SMTP password (use secrets manager)
BALANCE_ALERT_SMTP_FROM=alerts@example.com    # From address (default: SMTP_USER)
BALANCE_ALERT_SMTP_TO=ops@example.com,team@example.com  # Comma-separated recipient list

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
export HIGGSFIELD_API_ID=test-id
export HIGGSFIELD_API_SECRET=test-secret
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
1. Check logs for delivery errors: `grep "balance_notifier" <log-file>`
2. Verify SMTP/webhook config in `.env`
3. Check deduplication: same alert within 1 hour is suppressed
4. Test SMTP manually:
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
