"""Shared pytest fixtures for all tests."""

import pytest


@pytest.fixture
def all_production_activities():
    """
    All activities registered in hfvg/worker.py PRODUCTION_ACTIVITIES.
    
    Use this fixture in workflow tests to ensure the worker has all required activities,
    preventing drift between production and test worker configurations.
    """
    from hfvg.worker import PRODUCTION_ACTIVITIES
    return PRODUCTION_ACTIVITIES


@pytest.fixture
def all_production_workflows():
    """All workflows registered in hfvg/worker.py."""
    from hfvg.workflows.episode import EpisodeWorkflow
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg.workflows.posting import PostingWorkflow
    
    return [EpisodeWorkflow, EpisodeWorkflowV2, ShotWorkflow, PostingWorkflow]
