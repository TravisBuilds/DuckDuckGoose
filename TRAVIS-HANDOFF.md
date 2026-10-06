# Studio Console Handoff for Travis

**PR**: https://github.com/TravisBuilds/DuckDuckGoose/pull/5  
**Status**: MVP complete, CI passing, ready for review  
**Branch**: `cursor/studio-console-82c1`

## What You Can Do Today (Dry Run)

### Setup (5 minutes)

```bash
# 1. Generate admin secret
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# 2. Set environment variables
export ADMIN_SECRET="your-secret-from-step-1"
export STUDIO_ADMIN_SECRET="your-secret-from-step-1"
export NEXT_PUBLIC_API_URL="http://localhost:8000"

# 3. Start services (4 terminals)
temporal server start-dev              # Terminal 1
python -m hfvg.worker                 # Terminal 2
python api/main.py                    # Terminal 3
cd web && npm run dev                 # Terminal 4
```

### Use Console

1. Visit `http://localhost:3000/studio`
2. Log in with your admin secret
3. See budget meters, approval gates
4. Approve gates: G1.01 → G1.03 → G1.08 → GC.02 → G2.12 → G4.09
5. Watch real-time updates (3s polling)

**Dry-run mode**: No charges, placeholders only.

## Decisions Needed

### 1. Still Model (BLOCKING)
Problem: GPT Image 2 not in Higgsfield API.  
Options: (A) Different model (B) Configurable (C) Soul 2  
**Your decision**: ___________________________

### 2. Episode Data Upload
Problem: Can't commit beatmaps to public repo.  
Options: (A) File upload UI (B) Mount directory (C) Text paste  
**Your decision**: ___________________________

### 3. OpenAI Vision (~$1.40 for Ep04)
Problem: Duck identity needs vision judge.  
Options: (A) Use OpenAI (B) Different provider (C) Manual only  
**Your decision**: ___________________________

### 4. Temporal Hosting
Options: (A) Temporal Cloud (B) Self-host (C) Local dev  
**Your decision**: ___________________________

### 5. API Deployment
Options: (A) Container (B) Systemd (C) Same as worker  
**Your decision**: ___________________________

## What's Built

✅ Backend API with access control  
✅ Console with budget meters + approval gates  
✅ 80% budget stops (tested)  
✅ Idempotent jobs (no double-charge)  
✅ Dry-run default  

## What's Deferred (~15-20 hours)

After your decisions:
1. Real vision judge (OpenAI)
2. Shot status tracking
3. Still strip review UI
4. Clip review UI
5. Episode upload
6. Canary action
7. Live mode enforcement
8. Audit trail

## Ep04 Budget

Higgsfield: 885.4 / 1,250 cap  
ElevenLabs: 3,500 / 4,000 cap (after picture lock)  
API cap: $60

⚠️ L2/L3 exceed 80% stops (report points)

## Files to Review

- `STUDIO-CONSOLE.md`: Full implementation status
- `api/README.md`: Deployment guide
- `api/main.py`: Backend (400 lines)
- `web/app/studio/`: Console pages
- `.env.example`: All env vars

## Next Actions

1. Review PR #5
2. Make decisions (above)
3. Test locally (5 min setup)
4. Approve/merge when ready

**Zero real credits spent**. Dry-run only until you approve.
