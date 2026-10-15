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
            "A02": [{"key": "r_birch", "path": "/fake/birch.png"}]
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
                "key": "r_birch",
                "prompt": "Birch character sheet",
                "output_path": "/fake/birch.png",
                "type": "character_sheet",
                "version": 1
            }
        ]
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    result = await load_refs_to_generate_activity("ep99")
    assert len(result) == 2
    assert result[0]["key"] == "r_duck"
    assert result[1]["key"] == "r_birch"


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
            "A02": [{"key": "r_birch", "path": "/fake/birch.png"}],
            "A03": [{"key": "r_lineup", "path": "/fake/lineup.png"}]
        },
        "to_generate": [
            {
                "key": "r_duck",
                "prompt": "Duck character sheet",
                "output_path": "/fake/duck.png"
            },
            {
                "key": "r_birch",
                "prompt": "Birch character sheet",
                "output_path": "/fake/birch.png"
            },
            {
                "key": "r_lineup",
                "prompt": "Lineup shot",
                "output_path": "/fake/lineup.png"
            }
        ]
    }))
    
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    
    # Only A01 and A02 in subset, so only r_duck and r_birch needed
    result = await load_refs_to_generate_activity("ep99", shots_subset=["A01", "A02"])
    assert len(result) == 2
    keys = {r["key"] for r in result}
    assert keys == {"r_duck", "r_birch"}


@pytest.mark.asyncio
async def test_generate_ref_dry_run(tmp_path, monkeypatch):
    """Test: generate_ref creates fake output in dry mode."""
    from hfvg.activities.ref_generation import generate_ref_activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    from hfvg.studio_db import init_studio_db, create_episode
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize credit ledger
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    result = await generate_ref_activity(
        episode_id="ep99",
        ref_key="r_duck",
        prompt="Duck character sheet",
        output_path=output_path,
        version=1,
    )
    
    assert result["status"] == "completed"
    assert result["job_id"] == "ref-ep99-r_duck-v1"
    assert Path(output_path).exists()
    assert Path(output_path).read_bytes() == b"fake-ref-image"


@pytest.mark.asyncio
async def test_generate_ref_idempotent_job_id(tmp_path, monkeypatch):
    """Test: generate_ref uses idempotent job_id with version."""
    from hfvg.activities.ref_generation import generate_ref_activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    from hfvg.studio_db import init_studio_db, create_episode
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
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


@pytest.mark.asyncio
async def test_refs_counted_in_credit_cap(tmp_path, monkeypatch):
    """Test: ref generation reserves and commits credits from episode cap."""
    from hfvg.activities.ref_generation import generate_ref_activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    from hfvg.studio_db import init_studio_db, create_episode
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
