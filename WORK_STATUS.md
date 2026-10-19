# Work Status - PR #6 Comprehensive Fixes

## Completed (Just Pushed)
1. ✅ Removed screenshots/e2e/ with private BEATMAP content (commit e61b4cc)
2. ✅ Fixed temporal_converter crash - using pydantic_data_converter
3. ✅ Added aiohttp to pyproject.toml dependencies
4. ✅ ShotWorkflow now imports and uses enforced activities only
5. ✅ Worker no longer registers legacy ungated activities
6. ✅ Fixed parse_beatmap_activity to return dict not list

## Critical Remaining Work

### A. Startup & Dependencies
- ✅ Converter fixed
- ✅ aiohttp added
- Need: Verify clean startup from fresh clone

### B. Safety - Enforced Activities
- ✅ Workflow uses enforced imports
- ⚠️  await_job_enforced signature mismatch - needs workflow adjustment
- TODO: QC must run BEFORE paid generation (currently runs after)
- TODO: Commit/release all reservations
- TODO: Remove retry loop that makes multiple paid stills

### C. Single Source of Truth
- TODO: G1.08 stored in DB only (remove from workflow state)
- TODO: Live mode stored in DB only

### D. Tests
- TODO: Mock all provider HTTP calls (respx/monkeypatch)
- TODO: Add pytest-socket guard
- TODO: Fix all 7 failing tests
- TODO: Remove test hangs

### E. Red-Green Script
- TODO: Rewrite with temp copies, proper cleanup, always print summary
- TODO: Fix fake mutations (#5, #6, #10)
- TODO: All 10 tests must go red because protection removed

### F. E2E Script
- TODO: Rewrite based on drive_ui4.py with real assertions
- TODO: Run against full stack
- TODO: Save PNGs outside repo
- TODO: Verify all steps pass

### G. API/UI Bugs
- TODO: /api/health should check auth
- TODO: Login should call /api/login endpoint
- TODO: Gates response structure mismatch
- TODO: Budget API auth issues
- TODO: Audit entries field name mismatch
- TODO: Live toggle body format
- TODO: Many more from PASS4 report...

## Next Steps
Working through remaining items systematically to meet all acceptance criteria.
