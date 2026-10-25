"""Test G-AUD-KEY: Higgsfield credentials checked before reservation."""

import os
import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.studio_generation import _check_higgsfield_credentials


def test_check_higgsfield_credentials_refuses_missing_key():
    """Test: missing HIGGSFIELD_API_KEY raises non-retryable error."""
    os.environ["DRY_RUN"] = "false"
    if "HIGGSFIELD_API_KEY" in os.environ:
        del os.environ["HIGGSFIELD_API_KEY"]
    
    with pytest.raises(ApplicationError) as exc_info:
        _check_higgsfield_credentials()
    
    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "MissingCredentials"
    assert "HIGGSFIELD_API_KEY" in str(exc_info.value)


def test_check_higgsfield_credentials_refuses_malformed_key():
    """Test: malformed HIGGSFIELD_API_KEY (no colon) raises non-retryable error."""
    os.environ["DRY_RUN"] = "false"
    os.environ["HIGGSFIELD_API_KEY"] = "malformed-no-colon"
    
    with pytest.raises(ApplicationError) as exc_info:
        _check_higgsfield_credentials()
    
    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "InvalidCredentials"
    assert "format" in str(exc_info.value).lower()


def test_check_higgsfield_credentials_passes_valid_key():
    """Test: valid HIGGSFIELD_API_KEY passes check."""
    os.environ["DRY_RUN"] = "false"
    os.environ["HIGGSFIELD_API_KEY"] = "test-id:test-secret"
    
    # Should not raise
    _check_higgsfield_credentials()


def test_check_higgsfield_credentials_skips_dry_run():
    """Test: credential check skipped in DRY_RUN mode."""
    os.environ["DRY_RUN"] = "true"
    if "HIGGSFIELD_API_KEY" in os.environ:
        del os.environ["HIGGSFIELD_API_KEY"]
    
    # Should not raise - dry run doesn't need credentials
    _check_higgsfield_credentials()
