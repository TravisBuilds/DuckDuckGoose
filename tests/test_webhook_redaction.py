"""Tests for webhook URL and error redaction (Defect 13)."""

import pytest
from hfvg.balance_notifier import BalanceNotifier


def test_webhook_error_redaction(tmp_path):
    """Test that webhook errors redact URLs and exception details."""
    db_path = str(tmp_path / "test.db")
    notifier = BalanceNotifier(db_path)
    
    # Create a fake exception with a URL
    webhook_url = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX"
    error = Exception(f"Failed to send to {webhook_url}: Connection refused")
    
    # Redact the error
    redacted = notifier._redact_webhook_error(error)
    
    # Verify URL is redacted
    assert webhook_url not in redacted
    assert "[URL_REDACTED]" in redacted or "redacted" in redacted.lower()
    
    # Verify exception type is preserved but details are redacted
    assert "Exception:" in redacted
    assert "redacted" in redacted.lower()


def test_webhook_error_redaction_no_url(tmp_path):
    """Test redaction of errors without URLs."""
    db_path = str(tmp_path / "test.db")
    notifier = BalanceNotifier(db_path)
    
    # Create an error without URL
    error = ValueError("Some generic error message")
    
    # Redact the error
    redacted = notifier._redact_webhook_error(error)
    
    # Should still be redacted
    assert "ValueError:" in redacted
    assert "redacted" in redacted.lower()
