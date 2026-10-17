"""Tests for PR #10 mutation coverage (items 3a-3j).

These tests ensure that all critical behaviors are covered by redgreen mutations.
Each test MUST FAIL when its corresponding mutation is applied.
"""

import pytest
import asyncio
from pathlib import Path
from datetime import timedelta
from temporalio import workflow, activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.common import RetryPolicy
from temporalio.client import WorkflowUpdateFailedError
from unittest.mock import AsyncMock, MagicMock, patch


# ============================================================================
# Helper functions
# ============================================================================

def _make_activities():
    """Helper to create test activities."""
    @activity.defn(name="acquire_run_lock")
    async def lock(*a): return True
    
    @activity.defn(name="release_run_lock")
    async def unlock(*a): return True
    
    @activity.defn(name="load_gate_policy_activity")
    async def policy(*a): return {}
    
    @activity.defn(name="parse_beatmap_activity")
    async def parse(*args):
        return {"shots": [{"shot_id": "A01", "prompt": "Test", "duration": 3.0}]}
    
    @activity.defn(name="load_refs_to_generate_activity")
    async def refs(*a): return []
    
    @activity.defn(name="generate_run_report")
    async def report(*a): return {}
    
    @activity.defn(name="compute_g1_plan_activity")
    async def g1_plan(*a):
        return {
            "shots": [{"shot_id": "A01", "still_credits": 0.06, "clip_credits": 0.15, "total_credits": 0.21}],
            "total_credits": 0.21,
            "total_usd": "0.21",
            "estimates_available": True,
        }
    
    async def noop(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}
    
    @activity.defn(name="init_episode_budget_lines_activity")
    async def budget_init(*a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}
    
    @activity.defn(name="approve_g2_shots")
    async def approve_g2(episode_id: str): return 0
    
    @activity.defn(name="list_anchor_stills")
    async def list_anchors(episode_id: str): return []
    
    names = ["init_episode_credit_line_activity", "preflight_validate_episode_activity",
             "upsert_episode_activity", "verify_episode_files_activity", "approve_g108_activity",
             "recheck_refs_activity", "update_credit_cap_activity"]
    
    def mk(n):
        async def f(*a): return await noop(*a)
        return activity.defn(name=n)(f)
    
    extra = [mk(n) for n in names]
    return [lock, unlock, policy, parse, refs, report, g1_plan, budget_init, approve_g2, list_anchors, *extra]


# ============================================================================
# Item 3a: End-frame anchor uses base shot ID (A02_end -> A02)
# ============================================================================

@pytest.mark.asyncio
async def test_end_frame_anchor_strips_suffix(tmp_path, monkeypatch):
    """
    Test that A02_end uses A01 anchor (strips '_end' suffix to look up A02's anchor).
    
    Mutation 122 should break this by NOT stripping the suffix, causing A02_end
    to not find its anchor.
    """
    import os
    from unittest.mock import AsyncMock, patch
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    # Track refs passed to submit_image
    captured_refs = []
    
    class FakeProvider:
        model_path = "unknown/model"
        
        async def close(self):
            pass
        
        async def estimate_usd_micros(self, **kwargs):
            return 50_000
        
        async def estimate_usd_and_credits(self, **kwargs):
            return (50_000, 1.0)
        
        async def submit_image(self, image_urls=None, **kwargs):
            captured_refs.append(image_urls or [])
            return "fake-job-id"
        
        async def upload_reference(self, path, version):
            return {"public_url": f"fake://ref/{path}"}
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("STILL_MAX_REFS", "3")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Set up ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget("ep99")
    await ledger.set_manual_balance(1_000_000_000, "test", "Test balance")
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Create anchor still file
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor image")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor image").hexdigest()
    
    # Set anchor: A01 is the anchor for A02 (NOTE: not A02_end explicitly!)
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],  # Targets A02, not A02_end
        str(anchor_file), 1, sha256, None
    )
    
    # Submit for A02_end (end-frame still)
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        result = await submit_still_job_enforced(
            episode_id="ep99",
            shot_id="A02_end",  # End-frame shot ID with _end suffix
            prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
            version=1,
            refs=[],
            resolution="1k",
            quality="medium",
            shot_meta={"count": 0},
        )
    
    # Verify anchor was found and injected (strips _end to find A02's anchor)
    assert len(captured_refs) == 1
    assert len(captured_refs[0]) == 1, "Anchor should be injected for A02_end"
    assert "fake://ref/" in captured_refs[0][0]


# ============================================================================
# Item 3b: Ref cap includes anchor on end frames
# ============================================================================

@pytest.mark.asyncio
async def test_end_frame_anchor_enforces_ref_cap(tmp_path, monkeypatch):
    """
    Test that anchor + refs respects cap on end-frame shots (A02_end).
    
    Mutation 123 should break this by checking only original refs, not refs_with_anchor.
    """
    import os
    from unittest.mock import AsyncMock, patch
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    class FakeProvider:
        model_path = "unknown/model"
        
        async def close(self):
            pass
        
        async def estimate_usd_micros(self, **kwargs):
            return 50_000
        
        async def estimate_usd_and_credits(self, **kwargs):
            return (50_000, 1.0)
        
        async def submit_image(self, **kwargs):
            return "fake-job-id"
        
        async def upload_reference(self, path, version):
            return {"public_url": f"fake://ref/{path}"}
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MODEL_PATH_STILL", "unknown/model")
    monkeypatch.setenv("STILL_MAX_REFS", "3")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget("ep99")
    await ledger.set_manual_balance(1_000_000_000, "test", "Test balance")
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Create anchor
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Create 3 regular refs
    ref1 = tmp_path / "ref1.png"
    ref1.write_bytes(b"ref1")
    ref2 = tmp_path / "ref2.png"
    ref2.write_bytes(b"ref2")
    ref3 = tmp_path / "ref3.png"
    ref3.write_bytes(b"ref3")
    
    # Try to submit A02_end with 3 refs (+ anchor = 4 total, over cap of 3)
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        # Should raise ValueError for exceeding cap with anchor
        raised_exception = None
        try:
            await submit_still_job_enforced(
                episode_id="ep99",
                shot_id="A02_end",  # End-frame shot
                prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
                version=1,
                refs=[str(ref1), str(ref2), str(ref3)],
                shot_meta={"count": 0},
            )
        except ValueError as e:
            raised_exception = e
        
        assert raised_exception is not None, "Expected ValueError for exceeding ref cap with anchor on end frame"
        assert "over the" in str(raised_exception) and "limit of 3" in str(raised_exception)
