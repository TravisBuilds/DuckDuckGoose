"""Tests for 2-shot canary API route with shots_subset and credit_cap."""

import pytest


def test_request_model_accepts_shots_subset():
    """
    Test: StartEpisodeRequest model accepts shots_subset parameter.
    
    The shots_subset parameter allows specifying which shots to run (e.g., 2-shot canary).
    """
    from api.main import StartEpisodeRequest
    
    # Request with shots_subset for 2-shot canary
    request = StartEpisodeRequest(
        episode_id="ep99",
        shots_subset=["A01", "A02"],
        credit_cap=100
    )
    
    assert request.episode_id == "ep99"
    assert request.shots_subset == ["A01", "A02"]
    assert request.credit_cap == 100
    assert request.dry_run is True  # Defaults to True


def test_request_model_accepts_credit_cap():
    """
    Test: StartEpisodeRequest model accepts credit_cap parameter.
    
    The credit_cap parameter limits credit usage for the run.
    """
    from api.main import StartEpisodeRequest
    
    # Request with credit_cap
    request = StartEpisodeRequest(
        episode_id="ep99",
        credit_cap=50
    )
    
    assert request.credit_cap == 50


def test_default_dry_run_is_true():
    """
    Test: Default dry_run value is True (safe default, live requires explicit False).
    
    When no dry_run parameter is provided, it should default to True.
    Live mode requires explicit opt-in with dry_run=False.
    """
    from api.main import StartEpisodeRequest
    
    # Create request without dry_run parameter
    request = StartEpisodeRequest(episode_id="ep99")
    
    # Should default to True (safe default - dry mode)
    assert request.dry_run is True
    
    # Explicit False should work (live mode opt-in)
    request_live = StartEpisodeRequest(episode_id="ep99", dry_run=False)
    assert request_live.dry_run is False


def test_workflow_signature_accepts_parameters():
    """
    Test: Workflow run method accepts shots_subset and credit_cap parameters.
    
    The workflow should be able to receive these parameters from the API.
    """
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    import inspect
    
    # Check that run method has the correct signature
    sig = inspect.signature(EpisodeWorkflowV2.run)
    params = list(sig.parameters.keys())
    
    assert 'episode_id' in params
    assert 'shots_subset' in params
    assert 'credit_cap' in params
    assert 'dry_run' in params
    
    # Check defaults
    assert sig.parameters['dry_run'].default is True
    assert sig.parameters['shots_subset'].default is None
    assert sig.parameters['credit_cap'].default is None
