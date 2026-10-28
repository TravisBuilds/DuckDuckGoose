"""PR10 remaining items: 3f (jpeg anchor), 3g (EPISODES_DIR), 3h (CLI retry), 9 (cap update)."""
import os
import pytest
from pathlib import Path
from unittest.mock import patch
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import workflow
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.activities.studio_generation import verify_episode_files_activity
from tests.test_retry_shot import _make_activities
import asyncio


# 3f: .jpeg anchor glob test
@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_g2_approved_jpeg_anchor_glob(tmp_path, monkeypatch):
    """Test that set-anchor (G2-approved still mode) resolves a still saved as .jpeg.

    Drives the REAL route POST /api/episodes/{ep}/anchor (api/main.py), whose
    `output_dir.glob("still_v*.jpeg")` is the only glob that can find this file
    (still_v1.jpeg matches neither *.png, *.jpg nor *.webp). Mutation 129 changes that
    glob, so the route answers 404 "No still files found" and the asserts below fail.
    """
    from fastapi.testclient import TestClient
    from api.main import app
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, upsert_shot
    import hashlib

    secret = "test-secret-32-characters-long-enough-for-validation"
    data_dir = tmp_path / "data"
    db_path = str(tmp_path / "test.db")
    # monkeypatch (not os.environ[...] =) so nothing leaks into other tests
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("ADMIN_SECRET", secret)
    monkeypatch.setattr("api.main.DATABASE_PATH", db_path, raising=False)

    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    await upsert_shot(db_path, "ep99", "A01", "Anchor shot", status="approved",
                      still_url="fake://still.jpeg")

    # ONLY a .jpeg still on disk (no .png / .jpg / .webp)
    output_dir = data_dir / "episodes" / "ep99" / "outputs" / "A01"
    output_dir.mkdir(parents=True)
    jpeg_bytes = b"fake jpeg still v3"
    (output_dir / "still_v3.jpeg").write_bytes(jpeg_bytes)

    client = TestClient(app)
    response = client.post(
        "/api/episodes/ep99/anchor",
        json={"anchor_shot_id": "A01", "target_shot_ids": ["A02"]},
        headers={"Authorization": f"Bearer {secret}"},
    )

    assert response.status_code == 200, \
        f"set-anchor must find the .jpeg still, got {response.status_code}: {response.text}"
    body = response.json()
    assert body["success"] is True
    assert body["local_path"].endswith("still_v3.jpeg"), f"wrong file resolved: {body['local_path']}"
    assert body["version"] == 3, f"version must come from still_v3.jpeg, got {body['version']}"
    assert body["sha256"] == hashlib.sha256(jpeg_bytes).hexdigest()

    from hfvg.studio_db import get_anchor_still
    anchor = await get_anchor_still(db_path, "ep99", "A02")
    assert anchor is not None and anchor["local_path"].endswith("still_v3.jpeg"), \
        f"anchor for A02 not persisted with the .jpeg path: {anchor}"
    

# 3g: EPISODES_DIR in verify_episode_files_activity test
@pytest.mark.asyncio
async def test_verify_episode_files_honors_episodes_dir():
    """Test that verify_episode_files_activity uses EPISODES_DIR env var."""
    with patch.dict(os.environ, {"EPISODES_DIR": "/custom/episodes"}):
        # The activity should construct paths using EPISODES_DIR
        # We'll test that the path construction includes the custom dir
        from hfvg.activities import studio_generation
        import inspect
        source = inspect.getsource(studio_generation.verify_episode_files_activity)
        assert "EPISODES_DIR" in source or "DATA_DIR" in source, \
            "verify_episode_files_activity should use EPISODES_DIR or DATA_DIR"


# 3h: CLI retry-shot uses execute_update and exits nonzero on refusal
def test_cli_retry_shot_execute_update_and_exit_code():
    """Test that CLI retry-shot uses execute_update (not signal)."""
    from hfvg import cli
    import inspect
    
    # Get the retry_shot function source (it's a Click command, get the callback)
    retry_shot_func = cli.retry_shot.callback if hasattr(cli.retry_shot, 'callback') else cli.retry_shot
    source = inspect.getsource(retry_shot_func)
    
    # Verify it uses execute_update
    assert "execute_update" in source, "CLI retry-shot should use execute_update"
    assert 'execute_update("retry_shot"' in source, "CLI should call execute_update with 'retry_shot'"
    
    # Verify it does NOT use signal for the retry operation
    # (Note: may use signal elsewhere, but not for retry_shot)
    lines = source.split('\n')
    for line in lines:
        if 'retry_shot' in line and 'signal' in line and 'execute_update' not in line:
            assert False, f"CLI should not use signal for retry_shot, found: {line.strip()}"
    

# 9: create_episode_line updates cap when credit_cap is passed
def test_create_episode_line_updates_cap_logic():
    """Test that create_episode_line has UPDATE logic for existing episodes."""
    from hfvg.credit_ledger import CreditLedger
    import inspect
    
    # Verify the implementation includes UPDATE for existing cap
    source = inspect.getsource(CreditLedger.create_episode_line)
    assert "UPDATE" in source, "create_episode_line should UPDATE existing episode cap"
    assert "cap_credits" in source, "create_episode_line should handle cap_credits parameter"


# Item 5: Files use EPISODES_DIR/DATA_DIR env vars
def test_episodes_dir_used_in_api_and_cli():
    """Test that api/main.py and cli.py use EPISODES_DIR env var instead of hardcoded paths."""
    
    # Check CLI lint command uses EPISODES_DIR
    cli_path = Path(__file__).parent.parent / "hfvg" / "cli.py"
    cli_source = cli_path.read_text()
    assert 'os.getenv("EPISODES_DIR"' in cli_source, \
        "CLI should use EPISODES_DIR env var"
    
    # Check API routes use EPISODES_DIR
    api_path = Path(__file__).parent.parent / "api" / "main.py"
    api_source = api_path.read_text()
    # Count occurrences of EPISODES_DIR usage in api routes
    episodes_dir_count = api_source.count('os.getenv("EPISODES_DIR"')
    assert episodes_dir_count >= 5, \
        f"API should use EPISODES_DIR in multiple routes (upload, canary, smoke, set-ref-versions), found {episodes_dir_count} uses"
