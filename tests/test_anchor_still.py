"""Test anchor still feature."""

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_set_anchor_still_basic(tmp_path):
    """Test setting an anchor still."""
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still, get_anchor_still
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    # Create episode
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Set anchor
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"fake image data")
    
    import hashlib
    sha256 = hashlib.sha256(b"fake image data").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02", "A03"],
        str(anchor_file), 1, sha256, None
    )
    
    # Verify A02 has anchor
    anchor = await get_anchor_still(db_path, "ep99", "A02")
    assert anchor is not None
    assert anchor["anchor_shot_id"] == "A01"
    assert anchor["local_path"] == str(anchor_file)
    assert anchor["version"] == 1
    assert anchor["sha256"] == sha256
    assert anchor["approved_by"] is None
    
    # Verify A03 has anchor
    anchor = await get_anchor_still(db_path, "ep99", "A03")
    assert anchor is not None
    assert anchor["anchor_shot_id"] == "A01"


@pytest.mark.asyncio
async def test_set_anchor_still_with_approved_by(tmp_path):
    """Test setting an anchor still from external file with approved_by note."""
    from hfvg.studio_db import init_studio_db, upsert_episode, set_anchor_still, get_anchor_still
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    
    # Set anchor from external file
    anchor_file = tmp_path / "external.png"
    anchor_file.write_bytes(b"external image")
    
    import hashlib
    sha256 = hashlib.sha256(b"external image").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 0, sha256, "Travis approved via Studio"
    )
    
    anchor = await get_anchor_still(db_path, "ep99", "A02")
    assert anchor is not None
    assert anchor["approved_by"] == "Travis approved via Studio"


@pytest.mark.asyncio
async def test_anchor_still_injected_into_refs(tmp_path, monkeypatch):
    """Test that anchor still is injected into refs at submit time (live path with fake provider)."""
    import os
    from unittest.mock import AsyncMock, patch
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    # Track refs passed to submit_image
    captured_refs = []
    
    class FakeProvider:
        model_path = "unknown/model"  # Unknown model forces STILL_MAX_REFS
        
        async def close(self):
            pass
        
        async def estimate_usd_micros(self, **kwargs):
            return 50_000
        
        async def estimate_usd_and_credits(self, **kwargs):
            return (50_000, 1.0)
        
        async def submit_image(self, image_urls=None, **kwargs):
            # Capture the refs
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
    
    # Set anchor: A01 is the anchor for A02
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Patch and submit
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        result = await submit_still_job_enforced(
            episode_id="ep99",
            shot_id="A02",
            prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
            version=1,
            refs=[],
            resolution="1k",
            quality="medium",
            shot_meta={"count": 0},
        )
    
    # Verify anchor was injected
    assert len(captured_refs) == 1
    # The anchor should be the first (index 0) element in the refs list
    assert len(captured_refs[0]) == 1
    assert "fake://ref/" in captured_refs[0][0]


@pytest.mark.asyncio
async def test_anchor_still_enforces_ref_cap(tmp_path, monkeypatch):
    """Test that anchor still + refs respects model ref cap (live path with fake provider)."""
    import os
    from unittest.mock import AsyncMock, patch
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    class FakeProvider:
        model_path = "unknown/model"  # Unknown model forces STILL_MAX_REFS
        
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
    monkeypatch.setenv("MODEL_PATH_STILL", "unknown/model")  # Use unknown model
    monkeypatch.setenv("STILL_MAX_REFS", "3")  # Cap at 3 refs for unknown models
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
    
    # Try to submit with 3 regular refs (+ anchor = 4 total, over cap of 3)
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        with pytest.raises(ValueError, match="over the .* limit of 3"):
            await submit_still_job_enforced(
                episode_id="ep99",
                shot_id="A02",
                prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
                version=1,
                refs=[str(ref1), str(ref2), str(ref3)],
                shot_meta={"count": 0},
            )


@pytest.mark.asyncio
async def test_anchor_still_job_id_uses_sha256_prefix(tmp_path, monkeypatch):
    """Test that anchor job_id suffix uses sha256 prefix, not just version."""
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
    
    # Create anchor with known SHA256
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor v1")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor v1").hexdigest()
    sha256_prefix = sha256[:8]  # First 8 chars
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Submit with anchor
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        result = await submit_still_job_enforced(
            episode_id="ep99",
            shot_id="A02",
            prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
            version=1,
            refs=[],
            shot_meta={"count": 0},
        )
    
    # Verify job_id includes sha256 prefix (not just version)
    job_id = result["job_id"]
    assert f"-anchor{sha256_prefix}" in job_id, f"Expected job_id to contain '-anchor{sha256_prefix}', got {job_id}"


@pytest.mark.asyncio
async def test_api_rejects_self_anchoring(tmp_path, monkeypatch):
    """Test that API rejects anchor_shot_id == target_shot_id."""
    from fastapi.testclient import TestClient
    from api.main import app
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, upsert_shot
    import os
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-32-characters-long-enough-for-validation")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Create a shot and mark it as approved
    await upsert_shot(
        db_path, "ep99", "A01", "Test shot", status="approved",
        still_url="fake://still.png"
    )
    
    # Try to anchor A01 to itself
    client = TestClient(app)
    response = client.post(
        "/api/episodes/ep99/anchor",
        json={
            "anchor_shot_id": "A01",
            "target_shot_ids": ["A01", "A02"],  # A01 trying to anchor to itself
        },
        headers={"Authorization": "Bearer test-secret-32-characters-long-enough-for-validation"}
    )
    
    assert response.status_code == 400
    assert "Cannot anchor shot A01 to itself" in response.json()["detail"]


@pytest.mark.asyncio
async def test_cli_rejects_self_anchoring(tmp_path, monkeypatch, capsys):
    """Test that CLI rejects anchor_shot_id in target_shot_ids."""
    from click.testing import CliRunner
    from hfvg.cli import set_anchor
    import sys
    
    runner = CliRunner()
    result = runner.invoke(set_anchor, ["ep99", "A01", "--for", "A01,A02,A03"])
    
    # CLI should exit with nonzero code
    assert result.exit_code == 1
    assert "Cannot anchor shot A01 to itself" in result.output


# Module-level workflow stub for test_episode_workflow_skips_anchor_shots
from temporalio import workflow as wf_module

STARTED_SHOTS_GLOBAL = []

@wf_module.defn
class StubShotWorkflowForAnchorTest:
    @wf_module.run
    async def run(self, episode_id: str, shot_plan: dict):
        STARTED_SHOTS_GLOBAL.append(shot_plan["shot_id"])
        return {"status": "completed"}


@pytest.mark.asyncio
async def test_episode_workflow_skips_anchor_shots(tmp_path, monkeypatch):
    """Test that episode workflow skips regenerating shots used as anchors."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still, list_anchor_stills, approve_g2_shots
    from hfvg.activities.credit_activities import init_episode_credit_line_activity, init_episode_budget_lines_activity
    import hfvg.activities as activities
    from temporalio import activity
    
    STARTED_SHOTS_GLOBAL.clear()
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=False)
    await approve_g108(db_path, "ep99")
    await init_episode_credit_line_activity("ep99", cap_credits=10_000)
    await init_episode_budget_lines_activity("ep99")
    
    # Create anchor: A01 is the anchor for A02
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Create beatmap
    beatmap = tmp_path / "beat.md"
    beatmap.write_text("""
# Episode 99

## A01
A first shot.

## A02
A second shot (uses A01 as anchor).
""")
    
    @activity.defn(name="parse_beatmap_activity")
    async def mock_parse_beatmap(*args):
        return [
            {"shot_id": "A01", "prompt": "A first shot."},
            {"shot_id": "A02", "prompt": "A second shot."},
        ]
    
    @activity.defn(name="compute_g1_plan_activity")
    async def mock_g1_plan(*args):
        return {
            "shots": [],
            "total_still_credits": 0.0,
            "total_clip_credits": 0.0,
            "total_credits": 0.0,
            "total_still_usd": "0.00",
            "total_clip_usd": "0.00",
            "total_usd": "0.00",
            "estimates_available": True,
        }
    
    @activity.defn(name="preflight_validate_episode_activity")
    async def mock_preflight(*args):
        return {"validated": True, "warnings": []}
    
    @activity.defn(name="load_gate_policy_activity")
    async def mock_load_gate(*args):
        return []
    
    @activity.defn(name="load_refs_to_generate_activity")
    async def mock_load_refs(*args):
        return []
    
    @activity.defn(name="recheck_refs_activity")
    async def mock_recheck(*args):
        return {}
    
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        worker = Worker(
            env.client,
            task_queue="test-q",
            workflows=[EpisodeWorkflowV2, StubShotWorkflowForAnchorTest],
            activities=[
                mock_parse_beatmap, mock_g1_plan, mock_preflight, mock_load_gate,
                mock_load_refs, mock_recheck,
                list_anchor_stills,
                approve_g2_shots,
                activities.check_live_mode_and_g108,
                activities.upsert_episode_activity,
                init_episode_credit_line_activity,
                init_episode_budget_lines_activity,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99", str(beatmap), True],  # dry_run=True
                id="ep99-workflow",
                task_queue="test-q",
            )
            
            # Workflow should skip A01 (it's an anchor shot)
            # Let the workflow start shots (before G2 gate)
            import asyncio
            await asyncio.sleep(0.2)
            
            # Terminate before gates
            await handle.terminate()
    
    # Verify: A02 should have been started, but NOT A01 (it's the anchor)
    assert "A02" in STARTED_SHOTS_GLOBAL, "A02 should be generated"
    assert "A01" not in STARTED_SHOTS_GLOBAL, "A01 should be skipped (it's an anchor shot)"


@pytest.mark.asyncio
async def test_g2_approval_marks_shots_approved(tmp_path, monkeypatch):
    """Test that G2 approval marks all completed shots as approved."""
    from hfvg.studio_db import init_studio_db, upsert_shot, approve_g2_shots
    import aiosqlite
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    # Create shots with different statuses
    await upsert_shot(db_path, "ep99", "A01", "Shot 1", status="pending", still_url="fake://a01.png")
    await upsert_shot(db_path, "ep99", "A02", "Shot 2", status="pending", still_url="fake://a02.png")
    await upsert_shot(db_path, "ep99", "A03", "Shot 3", status="pending", still_url=None)  # No still
    
    # Approve G2
    count = await approve_g2_shots(db_path, "ep99")
    
    # Should have approved 2 shots (A01 and A02 have still_url)
    assert count == 2
    
    # Verify statuses
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT shot_id, status FROM shots WHERE episode_id = ? ORDER BY shot_id", ("ep99",)) as cursor:
            rows = await cursor.fetchall()
    
    statuses = {row[0]: row[1] for row in rows}
    assert statuses["A01"] == "approved"
    assert statuses["A02"] == "approved"
    assert statuses["A03"] == "pending"  # No still_url, should stay pending


@pytest.mark.asyncio
async def test_anchor_path_supports_webp(tmp_path, monkeypatch):
    """Test that anchor path resolution supports .webp files."""
    from fastapi.testclient import TestClient
    from api.main import app
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, upsert_shot
    import os
    
    # Set up data directory
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-32-characters-long-enough-for-validation")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Create a shot and mark it as approved with .webp file
    await upsert_shot(
        db_path, "ep99", "A01", "Test shot", status="approved",
        still_url="fake://still.png"
    )
    
    # Create output directory and .webp file
    output_dir = data_dir / "episodes" / "ep99" / "outputs" / "A01"
    output_dir.mkdir(parents=True)
    webp_file = output_dir / "still_v1.webp"
    webp_file.write_bytes(b"fake webp data")
    
    # Try to set anchor with the .webp file
    client = TestClient(app)
    response = client.post(
        "/api/episodes/ep99/anchor",
        json={
            "anchor_shot_id": "A01",
            "target_shot_ids": ["A02"],
        },
        headers={"Authorization": "Bearer test-secret-32-characters-long-enough-for-validation"}
    )
    
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True
    assert "still_v1.webp" in result["local_path"]


@pytest.mark.asyncio
async def test_anchor_path_relative_to_data_dir(tmp_path, monkeypatch):
    """Test that anchor paths are resolved relative to DATA_DIR, not current directory."""
    from fastapi.testclient import TestClient
    from api.main import app
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, upsert_shot
    import os
    
    # Set up custom data directory
    custom_data_dir = tmp_path / "custom_data"
    custom_data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(custom_data_dir))
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-32-characters-long-enough-for-validation")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Create a shot and mark it as approved
    await upsert_shot(
        db_path, "ep99", "A01", "Test shot", status="approved",
        still_url="fake://still.png"
    )
    
    # Create output directory in custom data dir
    output_dir = custom_data_dir / "episodes" / "ep99" / "outputs" / "A01"
    output_dir.mkdir(parents=True)
    still_file = output_dir / "still_v1.png"
    still_file.write_bytes(b"fake image data")
    
    # Try to set anchor - should find file in custom_data_dir
    client = TestClient(app)
    response = client.post(
        "/api/episodes/ep99/anchor",
        json={
            "anchor_shot_id": "A01",
            "target_shot_ids": ["A02"],
        },
        headers={"Authorization": "Bearer test-secret-32-characters-long-enough-for-validation"}
    )
    
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True
    # Path should contain custom_data_dir
    assert str(custom_data_dir) in result["local_path"]

