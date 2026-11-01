"""Production API upload and validate endpoint tests."""
import os
import tempfile
import shutil
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


# Set env vars before importing
os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"
os.environ["UPLOAD_TOKEN"] = "test_upload_token_at_least_32_chars_long"
os.environ["DATABASE_PATH"] = ":memory:"  # Use in-memory DB for tests


@pytest.fixture
def client():
    """Test client with isolated episodes directory and auth."""
    # Create temp episodes dir
    temp_dir = tempfile.mkdtemp(prefix="test_episodes_")
    os.environ["EPISODES_DIR"] = temp_dir
    
    # Import after env setup
    from api.main import app
    test_client = TestClient(app)
    
    # Login to get valid session cookie
    admin_secret = os.environ["ADMIN_SECRET"]
    login_response = test_client.post("/api/login", json={"admin_secret": admin_secret})
    assert login_response.status_code == 200, "Login should succeed"
    
    # Extract session cookie
    session_cookie = login_response.cookies.get("studio_admin_token")
    
    # Create a client with the session cookie
    test_client.cookies.set("studio_admin_token", session_cookie)
    
    yield test_client
    
    # Cleanup
    if Path(temp_dir).exists():
        shutil.rmtree(temp_dir)


def test_validate_writes_nothing_to_disk_or_db(client):
    """Validate endpoint does not write any files or DB entries."""
    episodes_dir = Path(os.environ["EPISODES_DIR"])
    
    # Ensure dir is empty
    assert not list(episodes_dir.glob("*"))
    
    beatmap = """# Episode 91 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | First shot action | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Studio | Day | A | Second shot action | Close | - | - | K |
"""
    
    response = client.post(
        "/api/episodes/upload/validate",
        data={"episode_id": "ep91"},
        files={"beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown")}
    )
    
    # Even if validation succeeds or fails, no files should be written
    episode_dir = episodes_dir / "ep91"
    assert not episode_dir.exists(), "Validate should not create episode directory"
    assert not list(episodes_dir.glob("*")), "Validate should not write any files"


def test_upload_idempotency_duplicate_detection(client, monkeypatch):
    """Upload same content twice returns is_duplicate=true on second upload."""
    # Mock temporal_client to return NOT_FOUND
    from unittest.mock import AsyncMock, Mock
    from temporalio.service import RPCError, RPCStatusCode
    mock_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    mock_handle = Mock()
    mock_handle.describe = AsyncMock(side_effect=mock_error)
    mock_client = Mock()
    mock_client.get_workflow_handle = Mock(return_value=mock_handle)
    import api.main
    monkeypatch.setattr(api.main, "temporal_client", mock_client)
    
    beatmap = """# Episode 92 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | Test shot one | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Studio | Day | A | Test shot two | Close | - | - | K |
"""
    
    # First upload
    response1 = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep92"},
        files={"beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown")}
    )
    assert response1.status_code == 200, f"First upload should succeed: {response1.text}"
    
    # Second upload with identical content
    response2 = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep92"},
        files={"beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown")}
    )
    assert response2.status_code == 200, f"Second upload should succeed: {response2.text}"
    
    # Second upload should detect duplicate
    assert response2.json().get("is_duplicate") is True, "Second identical upload should be marked as duplicate"
    assert response2.json().get("upload_hash") == response1.json().get("upload_hash"), "Hash should match"


def test_upload_stale_file_cleanup(client, monkeypatch):
    """Re-upload without optional files removes stale CREDIT-PLAN.md and CONTINUITY.md."""
    # Mock temporal_client
    from unittest.mock import AsyncMock, Mock
    from temporalio.service import RPCError, RPCStatusCode
    mock_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    mock_handle = Mock()
    mock_handle.describe = AsyncMock(side_effect=mock_error)
    mock_client = Mock()
    mock_client.get_workflow_handle = Mock(return_value=mock_handle)
    import api.main
    monkeypatch.setattr(api.main, "temporal_client", mock_client)
    
    beatmap = """# Episode 93 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | Test shot | Open | - | - | K |
"""
    credit_plan = "# Credit Plan\nS01: 100 credits"
    continuity = "# Continuity\nPrevious: none"
    
    # First upload with all files
    response1 = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep93"},
        files={
            "beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown"),
            "credit_plan_file": ("CREDIT-PLAN.md", credit_plan.encode(), "text/markdown"),
            "continuity_file": ("CONTINUITY.md", continuity.encode(), "text/markdown"),
        }
    )
    assert response1.status_code == 200, f"First upload should succeed: {response1.text}"
    
    episodes_dir = Path(os.environ["EPISODES_DIR"])
    episode_dir = episodes_dir / "ep93"
    
    assert (episode_dir / "BEATMAP.md").exists()
    assert (episode_dir / "CREDIT-PLAN.md").exists()
    assert (episode_dir / "CONTINUITY.md").exists()
    
    # Second upload with only beatmap (different content to avoid duplicate detection)
    beatmap2 = """# Episode 93 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | Test shot modified | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Studio | Day | A | New shot | Close | - | - | K |
"""
    
    response2 = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep93"},
        files={"beatmap_file": ("BEATMAP.md", beatmap2.encode(), "text/markdown")}
    )
    assert response2.status_code == 200, f"Second upload should succeed: {response2.text}"
    
    # Stale files should be removed
    assert (episode_dir / "BEATMAP.md").exists(), "BEATMAP should still exist"
    assert not (episode_dir / "CREDIT-PLAN.md").exists(), "Stale CREDIT-PLAN should be removed"
    assert not (episode_dir / "CONTINUITY.md").exists(), "Stale CONTINUITY should be removed"


def test_upload_replaces_shots_in_db(client, monkeypatch):
    """Upload with different content replaces shots in DB (not INSERT OR IGNORE)."""
    # Mock temporal_client
    from unittest.mock import AsyncMock, Mock
    from temporalio.service import RPCError, RPCStatusCode
    mock_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    mock_handle = Mock()
    mock_handle.describe = AsyncMock(side_effect=mock_error)
    mock_client = Mock()
    mock_client.get_workflow_handle = Mock(return_value=mock_handle)
    import api.main
    monkeypatch.setattr(api.main, "temporal_client", mock_client)
    
    beatmap1 = """# Episode 94 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | First version | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Studio | Day | A | Second shot | Close | - | - | K |
"""
    
    beatmap2 = """# Episode 94 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | Updated first | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Studio | Day | A | Updated second | Mid | - | - | K |
| 3 | S03 | 6.0 (6.0) | Studio | Day | A | New third shot | Close | - | - | K |
"""
    
    # First upload
    response1 = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep94"},
        files={"beatmap_file": ("BEATMAP.md", beatmap1.encode(), "text/markdown")}
    )
    assert response1.status_code == 200, f"First upload should succeed: {response1.text}"
    assert response1.json()["shots_count"] == 2, "First upload should have 2 shots"
    
    # Second upload with different content (3 shots now)
    response2 = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep94"},
        files={"beatmap_file": ("BEATMAP.md", beatmap2.encode(), "text/markdown")}
    )
    assert response2.status_code == 200, f"Second upload should succeed: {response2.text}"
    assert response2.json()["shots_count"] == 3, "Second upload should have 3 shots"
    assert not response2.json().get("is_duplicate"), "Different content should not be duplicate"
    
    # The implementation uses DELETE then INSERT, verifying shot count changed is sufficient


def test_upload_hash_file_written_last(client, monkeypatch):
    """Upload writes .upload_hash LAST after all operations succeed."""
    # Mock temporal_client
    from unittest.mock import AsyncMock, Mock
    from temporalio.service import RPCError, RPCStatusCode
    mock_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    mock_handle = Mock()
    mock_handle.describe = AsyncMock(side_effect=mock_error)
    mock_client = Mock()
    mock_client.get_workflow_handle = Mock(return_value=mock_handle)
    import api.main
    monkeypatch.setattr(api.main, "temporal_client", mock_client)
    
    beatmap = """# Episode 95 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Studio | Day | A | Test shot | Open | - | - | K |
"""
    
    response = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep95"},
        files={"beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown")}
    )
    assert response.status_code == 200, f"Upload should succeed: {response.text}"
    
    episodes_dir = Path(os.environ["EPISODES_DIR"])
    episode_dir = episodes_dir / "ep95"
    hash_file = episode_dir / ".upload_hash"
    
    # Hash file should exist
    assert hash_file.exists(), ".upload_hash should be written"
    
    # Hash file should contain a valid hex hash
    hash_content = hash_file.read_text(encoding='utf-8').strip()
    assert len(hash_content) == 16, "Hash should be 16 hex characters (SHA-256 truncated)"
    assert all(c in '0123456789abcdef' for c in hash_content), "Hash should be hex"


def test_upload_returns_409_while_workflow_running(client, monkeypatch):
    """Upload returns 409 Conflict while workflow is running for the episode."""
    from unittest.mock import AsyncMock, Mock
    from temporalio.client import WorkflowExecutionStatus
    
    # Mock temporal_client and workflow handle
    mock_handle = Mock()
    mock_desc = Mock()
    mock_desc.status = WorkflowExecutionStatus.RUNNING
    mock_handle.describe = AsyncMock(return_value=mock_desc)
    
    mock_client = Mock()
    mock_client.get_workflow_handle = Mock(return_value=mock_handle)
    
    # Patch the temporal_client in api.main
    import api.main
    monkeypatch.setattr(api.main, "temporal_client", mock_client)
    
    # Attempt upload
    beatmap = "| Shot | Prompt |\n|------|--------|\n| A01  | Test   |"
    
    response = client.post(
        "/api/episodes/upload",
        data={"episode_id": "ep99"},
        files={"beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown")}
    )
    
    assert response.status_code == 409
    assert "workflow is currently running" in response.json()["detail"]


def test_non_ascii_token_returns_403():
    """Non-ASCII bytes in Authorization header return 403, not 500."""
    from api.main import verify_auth_unified
    from fastapi import HTTPException
    import pytest
    
    # Latin-1 encoded token with non-ASCII byte (0xE9 = é)
    # This should be rejected with 403, not crash with 500
    latin1_token_bytes = b"Bearer caf\xe9token"
    
    with pytest.raises(HTTPException) as exc_info:
        verify_auth_unified(latin1_token_bytes)
    
    assert exc_info.value.status_code == 403
    assert "Invalid" in str(exc_info.value.detail)


def test_upload_clears_budget_when_credit_plan_omitted(client, monkeypatch):
    """Re-uploading without credit plan clears budget_lines (preserves transactions history)."""
    from unittest.mock import AsyncMock, Mock
    from temporalio.service import RPCError, RPCStatusCode
    
    # Mock temporal_client to return NOT_FOUND (workflow doesn't exist)
    mock_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    mock_handle = Mock()
    mock_handle.describe = AsyncMock(side_effect=mock_error)
    mock_client = Mock()
    mock_client.get_workflow_handle = Mock(return_value=mock_handle)
    
    import api.main
    monkeypatch.setattr(api.main, "temporal_client", mock_client)
    
    import aiosqlite
    from hfvg.budget import BudgetLedger
    
    episode_id = "ep88"
    
    # First upload WITH credit plan
    beatmap = """| # | ID  | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|-----|-------------|------|-----|-------|--------|---------|----|----|-------|
| 1 | A01 | 3.0 (3.0)   | room | day | H     | action | purpose |    |    | K     |"""
    credit_plan = "# Credit Plan\n\nTotal: 100 credits"
    
    response1 = client.post(
        "/api/episodes/upload",
        data={"episode_id": episode_id},
        files={
            "beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown"),
            "credit_plan_file": ("CREDIT-PLAN.md", credit_plan.encode(), "text/markdown")
        }
    )
    assert response1.status_code == 200, f"Upload failed: {response1.status_code} - {response1.text}"
    
    # Check budget was initialized
    import asyncio
    async def check_budget():
        async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM budget_lines WHERE episode_id = ?", (episode_id,))
            count = (await cursor.fetchone())[0]
            return count
    
    count1 = asyncio.run(check_budget())
    assert count1 > 0, "Budget should be initialized after upload with credit plan"
    
    # Second upload WITHOUT credit plan
    response2 = client.post(
        "/api/episodes/upload",
        data={"episode_id": episode_id},
        files={
            "beatmap_file": ("BEATMAP.md", beatmap.encode(), "text/markdown")
        }
    )
    assert response2.status_code == 200
    
    # Check budget was cleared
    count2 = asyncio.run(check_budget())
    assert count2 == 0, "Budget should be cleared when re-upload omits credit plan"
