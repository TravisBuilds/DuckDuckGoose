"""Tests for Higgsfield credential handling."""

import os
import pytest

from hfvg.config import Config


def test_hf_key_combined():
    """Test HF_KEY (combined format) - highest priority."""
    # Set HF_KEY
    os.environ["HF_KEY"] = "test-key-id:test-secret"
    
    # Set other vars that should be ignored
    os.environ["HF_API_KEY"] = "other:value"
    os.environ["HF_API_KEY_ID"] = "other-id"
    os.environ["HF_API_KEY_SECRET"] = "other-secret"
    
    try:
        result = Config.get_higgsfield_credentials()
        assert result == "test-key-id:test-secret"
    finally:
        # Clean up
        os.environ.pop("HF_KEY", None)
        os.environ.pop("HF_API_KEY", None)
        os.environ.pop("HF_API_KEY_ID", None)
        os.environ.pop("HF_API_KEY_SECRET", None)


def test_hf_api_key_with_colon():
    """Test HF_API_KEY containing colon (combined format) - second priority."""
    # HF_KEY not set
    os.environ.pop("HF_KEY", None)
    
    # Set HF_API_KEY with colon
    os.environ["HF_API_KEY"] = "key-id:secret"
    
    # Set other vars that should be ignored
    os.environ["HF_API_KEY_ID"] = "other-id"
    os.environ["HF_API_KEY_SECRET"] = "other-secret"
    
    try:
        result = Config.get_higgsfield_credentials()
        assert result == "key-id:secret"
    finally:
        os.environ.pop("HF_API_KEY", None)
        os.environ.pop("HF_API_KEY_ID", None)
        os.environ.pop("HF_API_KEY_SECRET", None)


def test_hf_api_key_plus_secret():
    """Test HF_API_KEY + HF_API_SECRET (separate) - third priority."""
    # HF_KEY not set
    os.environ.pop("HF_KEY", None)
    
    # Set HF_API_KEY without colon and HF_API_SECRET
    os.environ["HF_API_KEY"] = "my-key-id"
    os.environ["HF_API_SECRET"] = "my-secret"
    
    # Set other vars that should be ignored
    os.environ["HF_API_KEY_ID"] = "other-id"
    os.environ["HF_API_KEY_SECRET"] = "other-secret"
    
    try:
        result = Config.get_higgsfield_credentials()
        assert result == "my-key-id:my-secret"
    finally:
        os.environ.pop("HF_API_KEY", None)
        os.environ.pop("HF_API_SECRET", None)
        os.environ.pop("HF_API_KEY_ID", None)
        os.environ.pop("HF_API_KEY_SECRET", None)


def test_hf_api_key_id_plus_secret():
    """Test HF_API_KEY_ID + HF_API_KEY_SECRET (docs naming) - fourth priority."""
    # Clear all higher priority vars
    os.environ.pop("HF_KEY", None)
    os.environ.pop("HF_API_KEY", None)
    os.environ.pop("HF_API_SECRET", None)
    
    # Set docs-style vars
    os.environ["HF_API_KEY_ID"] = "docs-key-id"
    os.environ["HF_API_KEY_SECRET"] = "docs-secret"
    
    try:
        result = Config.get_higgsfield_credentials()
        assert result == "docs-key-id:docs-secret"
    finally:
        os.environ.pop("HF_API_KEY_ID", None)
        os.environ.pop("HF_API_KEY_SECRET", None)


def test_no_credentials():
    """Test that None is returned when no credentials are set."""
    # Clear all credential vars
    os.environ.pop("HF_KEY", None)
    os.environ.pop("HF_API_KEY", None)
    os.environ.pop("HF_API_SECRET", None)
    os.environ.pop("HF_API_KEY_ID", None)
    os.environ.pop("HF_API_KEY_SECRET", None)
    
    result = Config.get_higgsfield_credentials()
    assert result is None


def test_hf_api_key_without_secret():
    """Test HF_API_KEY without colon and no HF_API_SECRET returns None."""
    os.environ.pop("HF_KEY", None)
    os.environ.pop("HF_API_SECRET", None)
    os.environ.pop("HF_API_KEY_ID", None)
    os.environ.pop("HF_API_KEY_SECRET", None)
    
    # HF_API_KEY without colon and no secret
    os.environ["HF_API_KEY"] = "just-a-key"
    
    try:
        result = Config.get_higgsfield_credentials()
        assert result is None
    finally:
        os.environ.pop("HF_API_KEY", None)


def test_priority_order():
    """Test that priority order is respected when multiple formats are set."""
    # Set all formats
    os.environ["HF_KEY"] = "from-hf-key:secret1"
    os.environ["HF_API_KEY"] = "from-api-key:secret2"
    os.environ["HF_API_SECRET"] = "secret3"
    os.environ["HF_API_KEY_ID"] = "from-key-id"
    os.environ["HF_API_KEY_SECRET"] = "secret4"
    
    try:
        # Should return HF_KEY value (highest priority)
        result = Config.get_higgsfield_credentials()
        assert result == "from-hf-key:secret1"
    finally:
        os.environ.pop("HF_KEY", None)
        os.environ.pop("HF_API_KEY", None)
        os.environ.pop("HF_API_SECRET", None)
        os.environ.pop("HF_API_KEY_ID", None)
        os.environ.pop("HF_API_KEY_SECRET", None)
