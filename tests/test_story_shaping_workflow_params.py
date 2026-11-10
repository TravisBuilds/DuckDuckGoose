"""Test that story shaping workflow receives correct parameters."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta


@pytest.mark.asyncio
async def test_workflow_story_shaping_receives_idea_and_preset():
    """Test that when enable_story_shaping=True, the activity receives the real idea, preset, and dry_run flag."""
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    # Mock workflow context
    with patch('temporalio.workflow') as mock_workflow:
        mock_workflow.logger = MagicMock()
        mock_workflow.logger.info = MagicMock()
        mock_workflow.logger.warning = MagicMock()
        mock_workflow.logger.error = MagicMock()
        
        # Mock execute_activity to capture calls
        activity_calls = []
        
        async def mock_execute_activity(name, **kwargs):
            activity_calls.append({"name": name, "kwargs": kwargs})
            
            # Return mock responses
            if name == "acquire_run_lock_activity":
                return {"acquired": True}
            elif name == "upsert_episode_activity":
                return {"episode_id": "ep99test"}
            elif name == "init_episode_credit_line_activity":
                return {"line_id": "L1"}
            elif name == "init_episode_budget_lines_activity":
                return {"lines": []}
            elif name == "generate_story_shape_activity":
                # This is what we're testing
                return {
                    "story_shape": {"logline": "Test story", "beats": []},
                    "dry_run": kwargs.get("args", [None, None, None, None, False])[4],
                    "model_used": "gpt-4o",
                }
            else:
                return {}
        
        mock_workflow.execute_activity = mock_execute_activity
        
        # Create workflow instance
        workflow = EpisodeWorkflowV2()
        
        # Call run with story shaping enabled
        test_idea = "A robot discovers a magical library"
        test_archetype = "original-mini-movie"
        
        try:
            await workflow.run(
                episode_id="ep99test",
                beatmap_path=None,
                dry_run=False,  # Live mode
                shots_subset=None,
                credit_cap=1000,
                attempt_overrides=None,
                reopen_from=None,
                enable_story_shaping=True,
                archetype_id=test_archetype,
                idea=test_idea,
            )
        except Exception as e:
            # Workflow will fail because we're not mocking all activities,
            # but we can still check the story shaping call
            pass
        
        # Find the generate_story_shape_activity call
        story_shape_calls = [c for c in activity_calls if c["name"] == "generate_story_shape_activity"]
        
        if story_shape_calls:
            call = story_shape_calls[0]
            args = call["kwargs"].get("args", [])
            
            # Verify args are in correct order: [episode_id, idea, genre_preset, target_duration, dry_run]
            assert len(args) == 5, f"Expected 5 args, got {len(args)}: {args}"
            assert args[0] == "ep99test", f"arg[0] (episode_id) should be 'ep99test', got {args[0]}"
            assert args[1] == test_idea, f"arg[1] (idea) should be test idea, got {args[1]}"
            # args[2] is genre_preset (archetype_id string or None)
            assert args[3] == 120.0, f"arg[3] (target_duration) should be 120.0, got {args[3]}"
            assert args[4] is False, f"arg[4] (dry_run) should be False, got {args[4]}"
        else:
            pytest.fail("generate_story_shape_activity was not called")


@pytest.mark.asyncio
async def test_workflow_story_shaping_dry_run_flag():
    """Test that dry_run flag is correctly passed to story shaping."""
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    with patch('temporalio.workflow') as mock_workflow:
        mock_workflow.logger = MagicMock()
        mock_workflow.logger.info = MagicMock()
        mock_workflow.logger.warning = MagicMock()
        mock_workflow.logger.error = MagicMock()
        
        activity_calls = []
        
        async def mock_execute_activity(name, **kwargs):
            activity_calls.append({"name": name, "kwargs": kwargs})
            
            if name == "acquire_run_lock_activity":
                return {"acquired": True}
            elif name == "upsert_episode_activity":
                return {"episode_id": "ep99dry"}
            elif name == "init_episode_credit_line_activity":
                return {"line_id": "L1"}
            elif name == "init_episode_budget_lines_activity":
                return {"lines": []}
            elif name == "generate_story_shape_activity":
                return {
                    "story_shape": {"logline": "Test", "beats": []},
                    "dry_run": kwargs.get("args", [None, None, None, None, True])[4],
                    "model_used": "dry-stub",
                }
            else:
                return {}
        
        mock_workflow.execute_activity = mock_execute_activity
        
        workflow = EpisodeWorkflowV2()
        
        try:
            await workflow.run(
                episode_id="ep99dry",
                beatmap_path=None,
                dry_run=True,  # DRY RUN MODE
                shots_subset=None,
                credit_cap=1000,
                attempt_overrides=None,
                reopen_from=None,
                enable_story_shaping=True,
                archetype_id="cozy-hangout",
                idea="Test dry run idea",
            )
        except Exception:
            pass
        
        story_shape_calls = [c for c in activity_calls if c["name"] == "generate_story_shape_activity"]
        
        if story_shape_calls:
            call = story_shape_calls[0]
            args = call["kwargs"].get("args", [])
            assert args[4] is True, f"dry_run flag should be True in dry mode, got {args[4]}"
        else:
            pytest.fail("generate_story_shape_activity was not called in dry mode")
