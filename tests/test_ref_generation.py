"""Tests for reference generation stage in EpisodeWorkflowV2."""

import pytest
import json
from pathlib import Path
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.studio_db import init_studio_db, create_episode
from hfvg.credit_ledger import CreditLedger


@pytest.mark.asyncio
async def test_load_refs_to_generate_no_file(tmp_path, monkeypatch):
    """Test: load_refs_to_generate returns empty list when refs.json missing."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert result == []


@pytest.mark.asyncio
async def test_load_refs_to_generate_empty_list(tmp_path, monkeypatch):
    """Test: load_refs_to_generate returns empty list when no to_generate."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {}
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert result == []


@pytest.mark.asyncio
async def test_live_mode_fails_without_refs_json(tmp_path, monkeypatch):
    """Test: Live mode fails if refs.json is missing."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    # Should succeed in dry mode
    result = await load_refs_to_generate_activity("ep99", is_live=False)
    assert result == []
    
    # Should fail in live mode
    with pytest.raises(FileNotFoundError, match="refs.json required in live mode"):
        await load_refs_to_generate_activity("ep99", is_live=True)


@pytest.mark.asyncio
async def test_live_mode_fails_if_needed_ref_unavailable(tmp_path, monkeypatch):
    """Test: Live mode fails if shot needs ref that's not on disk or in to_generate."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {
            "A01": [
                {"key": "r_duck", "path": "/nonexistent/duck.png", "exists": False},
                {"key": "r_stag03", "path": "/nonexistent/stag03.png", "exists": False}
            ]
        },
        "to_generate": {
            "r_duck": "/fake/duck.png"
        },
        "prompts": {
            "r_duck": "Duck sheet"
        }
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    # r_stag03 is needed but not in to_generate and not on disk
    with pytest.raises(ValueError, match="r_stag03.*neither on disk nor in to_generate"):
        await load_refs_to_generate_activity("ep99", is_live=True)


@pytest.mark.asyncio
async def test_to_generate_map_format(tmp_path, monkeypatch):
    """Test: to_generate accepts map {key: path} format."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {},
        "to_generate": {
            "r_duck": "/fake/duck.png",
            "r_stag03": "/fake/stag03.png"
        },
        "prompts": {
            "r_duck": "Duck character sheet",
            "r_stag03": "Stag03 character sheet"
        }
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert len(result) == 2
    
    # Check defaults applied
    duck = next(r for r in result if r["key"] == "r_duck")
    assert duck["prompt"] == "Duck character sheet"
    assert duck["output_path"] == "/fake/duck.png"
    assert duck["type"] == "character_sheet"  # default
    assert duck["version"] == 1  # default


@pytest.mark.asyncio
async def test_to_generate_map_without_prompts_fails(tmp_path, monkeypatch):
    """Test: map format requires prompts map."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {},
        "to_generate": {
            "r_duck": "/fake/duck.png"
        }
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    with pytest.raises(ValueError, match="r_duck.*has no prompt"):
        await load_refs_to_generate_activity("ep99")


@pytest.mark.asyncio
async def test_to_generate_list_with_defaults(tmp_path, monkeypatch):
    """Test: list format applies defaults for type and version."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {},
        "to_generate": [
            {
                "key": "r_duck",
                "prompt": "Duck sheet",
                "output_path": "/fake/duck.png"
                # type and version omitted - should get defaults
            }
        ]
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert len(result) == 1
    assert result[0]["type"] == "character_sheet"
    assert result[0]["version"] == 1


@pytest.mark.asyncio
async def test_to_generate_list_can_use_prompts_map(tmp_path, monkeypatch):
    """Test: list format can get prompts from prompts map."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {},
        "to_generate": [
            {
                "key": "r_duck",
                "output_path": "/fake/duck.png",
                "type": "character_sheet"
            }
        ],
        "prompts": {
            "r_duck": "Duck from prompts map"
        }
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert len(result) == 1
    assert result[0]["prompt"] == "Duck from prompts map"


@pytest.mark.asyncio
async def test_load_refs_validates_required_fields(tmp_path, monkeypatch):
    """Test: load_refs_to_generate validates required fields and fails fast."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    
    # Test missing 'key'
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "to_generate": [
            {
                "prompt": "Duck sheet",
                "output_path": "/fake/duck.png",
                "type": "character_sheet"
            }
        ]
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    with pytest.raises(ValueError, match="missing required field 'key'"):
        await load_refs_to_generate_activity("ep99")
    
    # Test missing 'prompt'
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "to_generate": [
            {
                "key": "r_duck",
                "output_path": "/fake/duck.png",
                "type": "character_sheet"
            }
        ]
    }))
    
    with pytest.raises(ValueError, match="missing required field 'prompt'"):
        await load_refs_to_generate_activity("ep99")
    
    # Test missing 'output_path'
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "to_generate": [
            {
                "key": "r_duck",
                "prompt": "Duck sheet",
                "type": "character_sheet"
            }
        ]
    }))
    
    with pytest.raises(ValueError, match="missing required field 'output_path'"):
        await load_refs_to_generate_activity("ep99")
    
    # Note: 'type' is no longer required - it defaults to "character_sheet"
    # This is a design decision to support the flexible format


@pytest.mark.asyncio
async def test_load_refs_to_generate_with_refs(tmp_path, monkeypatch):
    """Test: load_refs_to_generate returns to_generate list."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {
            "A01": [{"key": "r_duck", "path": "/fake/duck.png"}],
            "A02": [{"key": "r_stag03", "path": "/fake/stag03.png"}]
        },
        "to_generate": [
            {
                "key": "r_duck",
                "prompt": "Duck character sheet",
                "output_path": "/fake/duck.png",
                "type": "character_sheet",
                "version": 1
            },
            {
                "key": "r_stag03",
                "prompt": "Stag03 character sheet",
                "output_path": "/fake/stag03.png",
                "type": "character_sheet",
                "version": 1
            }
        ]
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert len(result) == 2
    assert result[0]["key"] == "r_duck"
    assert result[1]["key"] == "r_stag03"


@pytest.mark.asyncio
async def test_load_refs_filters_by_shots_subset(tmp_path, monkeypatch):
    """Test: load_refs_to_generate filters to refs needed by shots_subset."""
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    
    ep_dir = tmp_path / "ep99"
    ep_dir.mkdir()
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": {
            "A01": [{"key": "r_duck", "path": "/fake/duck.png"}],
            "A02": [{"key": "r_stag03", "path": "/fake/stag03.png"}],
            "A03": [{"key": "r_lineup", "path": "/fake/lineup.png"}]
        },
        "to_generate": [
            {
                "key": "r_duck",
                "prompt": "Duck character sheet",
                "output_path": "/fake/duck.png",
                "type": "character_sheet"
            },
            {
                "key": "r_stag03",
                "prompt": "Stag03 character sheet",
                "output_path": "/fake/stag03.png",
                "type": "character_sheet"
            },
            {
                "key": "r_lineup",
                "prompt": "Lineup shot",
                "output_path": "/fake/lineup.png",
                "type": "lineup"
            }
        ]
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    # Only A01 and A02 in subset, so only r_duck and r_stag03 needed
    result = await load_refs_to_generate_activity("ep99", shots_subset=["A01", "A02"])
    assert len(result) == 2
    keys = {r["key"] for r in result}
    assert keys == {"r_duck", "r_stag03"}


@pytest.mark.asyncio
async def test_dry_run_writes_to_scratch_dir(tmp_path, monkeypatch):
    """Test: Dry mode writes refs to scratch dir, not real output_path."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from pathlib import Path
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    # Real output path (should NOT be written to in dry mode)
    real_path = str(tmp_path / "real_refs" / "duck.png")
    
    # Mock activity info to get workflow_id
    import temporalio.activity
    original_info = temporalio.activity.info
    
    class MockInfo:
        def workflow_id(self):
            return "test-workflow-123"
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-workflow-123'})()
        
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=real_path,
            version=1,
        )
        
        # Real path should NOT exist
        assert not Path(real_path).exists(), "Dry mode must not write to real output_path"
        
        # Output should be in scratch dir
        scratch_path = Path("data/dry_run_outputs/test-workflow-123/refs/duck.png")
        assert scratch_path.exists(), f"Expected output at {scratch_path}"
        assert scratch_path.read_bytes() == b"fake-ref-image"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_generate_ref_dry_run(tmp_path, monkeypatch):
    """Test: generate_ref creates fake output in dry mode."""
    from hfvg.activities.ref_generation import generate_ref_activity
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize credit ledger
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Mock activity info
    original_info = temporalio.activity.info
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=output_path,
            version=1,
        )
        
        assert result["status"] == "completed"
        assert result["job_id"] == "ref-ep99-r_duck-v1"
        # Check scratch dir, not original path
        scratch_path = Path("data/dry_run_outputs/test-wf/refs/duck.png")
        assert scratch_path.exists()
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_generate_ref_idempotent_job_id(tmp_path, monkeypatch):
    """Test: generate_ref uses idempotent job_id with version."""
    from hfvg.activities.ref_generation import generate_ref_activity
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Mock activity info
    original_info = temporalio.activity.info
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Generate version 1
        result1 = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck v1",
            output_path=output_path,
            version=1,
        )
        assert result1["job_id"] == "ref-ep99-r_duck-v1"
        
        # Generate version 2 (reroll)
        result2 = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck v2",
            output_path=output_path,
            version=2,
        )
        assert result2["job_id"] == "ref-ep99-r_duck-v2"
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_refs_counted_in_credit_cap(tmp_path, monkeypatch):
    """Test: ref generation reserves and commits credits from episode cap."""
    from hfvg.activities.ref_generation import generate_ref_activity
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=20)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Check initial state
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT reserved_credits, spent_credits FROM credit_lines WHERE episode_id = ?",
            ("ep99",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == 0  # reserved
            assert row[1] == 0  # spent
    
    # Mock activity info
    original_info = temporalio.activity.info
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Generate ref (will reserve and commit credits)
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=output_path,
            version=1,
        )
        
        credits_used = result["credits_used"]
        
        # Check credits were spent (committed)
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT reserved_credits, spent_credits FROM credit_lines WHERE episode_id = ?",
                ("ep99",)
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 0  # reserved (released after commit)
                assert row[1] == credits_used  # spent (committed)
    finally:
        temporalio.activity.info = original_info
