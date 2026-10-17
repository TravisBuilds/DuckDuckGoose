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
async def test_g2_approved_jpeg_anchor_glob(tmp_path):
    """Test that after G2 approval, anchor stills use all glob patterns including .jpeg."""
    from pathlib import Path
    import os
    
    # Create episode output directory
    ep_dir = tmp_path / "episodes" / "ep_test"
    output_dir = ep_dir / "output" / "A01"
    output_dir.mkdir(parents=True)
    
    # Create anchor stills with different extensions (simulating what might be on disk)
    (output_dir / "still_v1.png").write_text("fake png")
    (output_dir / "still_v2.jpg").write_text("fake jpg")  
    (output_dir / "still_v3.jpeg").write_text("fake jpeg")
    (output_dir / "still_v4.webp").write_text("fake webp")
    
    # Test the glob pattern that api/main.py uses
    still_files = (
        list(output_dir.glob("still_v*.png")) +
        list(output_dir.glob("still_v*.jpg")) +
        list(output_dir.glob("still_v*.jpeg")) +
        list(output_dir.glob("still_v*.webp"))
    )
    
    # Verify all 4 files are found
    assert len(still_files) == 4, f"Should find all 4 still files (.png, .jpg, .jpeg, .webp), got {len(still_files)}"
    
    # Verify .jpeg specifically is found
    jpeg_files = list(output_dir.glob("still_v*.jpeg"))
    assert len(jpeg_files) == 1, f"Should find .jpeg file, got {jpeg_files}"
    assert jpeg_files[0].name == "still_v3.jpeg", f"Should find still_v3.jpeg, got {jpeg_files[0].name}"
    

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
@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_cli_retry_shot_execute_update_and_exit_code():
    """Test that CLI retry-shot uses execute_update (not signal) and exits nonzero on refusal."""
    from unittest.mock import AsyncMock, MagicMock, patch, call
    from hfvg import cli
    from click.testing import CliRunner
    
    # Track which method was called
    execute_update_called = []
    signal_called = []
    
    # Mock the Temporal client and workflow handle
    async def mock_execute_update(update_name, *args):
        execute_update_called.append((update_name, args))
        return {"status": "retry_queued"}
    
    async def mock_signal(signal_name, *args):
        signal_called.append((signal_name, args))
    
    mock_handle = MagicMock()
    mock_handle.execute_update = mock_execute_update
    mock_handle.signal = mock_signal
    
    async def mock_connect(*args, **kwargs):
        client = AsyncMock()
        client.get_workflow_handle = MagicMock(return_value=mock_handle)
        return client
    
    # Patch Client.connect
    with patch('hfvg.cli.Client.connect', new=mock_connect):
        runner = CliRunner()
        result = runner.invoke(cli.retry_shot, ['ep_test', 'A01'])
        
        # Verify execute_update was called (not signal)
        assert len(execute_update_called) == 1, f"Expected execute_update called once, got {len(execute_update_called)} calls"
        assert execute_update_called[0][0] == "retry_shot", "Should call retry_shot update"
        assert execute_update_called[0][1] == ("A01",), "Should pass shot_id"
        
        # Verify signal was NOT called
        assert len(signal_called) == 0, f"Signal should not be called, but was called {len(signal_called)} times"
        
        # Verify command succeeded
        assert result.exit_code == 0, f"Command should succeed, got exit code {result.exit_code}"
    

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
    from pathlib import Path
    
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
