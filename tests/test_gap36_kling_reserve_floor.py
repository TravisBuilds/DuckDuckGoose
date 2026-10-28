"""Test GAP-36: Kling reserve floor opt-in."""
import pytest
import os
from unittest.mock import patch
from hfvg.providers.kling_video import kling_list_floor_enabled, kling_reserve_usd_micros


def test_kling_list_floor_disabled_by_default():
    """KLING_RESERVE_LIST_FLOOR is off by default."""
    with patch.dict(os.environ, {}, clear=True):
        assert not kling_list_floor_enabled()


def test_kling_list_floor_enabled_with_1():
    """KLING_RESERVE_LIST_FLOOR=1 enables the floor."""
    with patch.dict(os.environ, {"KLING_RESERVE_LIST_FLOOR": "1"}):
        assert kling_list_floor_enabled()


def test_kling_list_floor_enabled_with_true():
    """KLING_RESERVE_LIST_FLOOR=true enables the floor."""
    with patch.dict(os.environ, {"KLING_RESERVE_LIST_FLOOR": "true"}):
        assert kling_list_floor_enabled()


def test_kling_list_floor_enabled_with_yes():
    """KLING_RESERVE_LIST_FLOOR=yes enables the floor."""
    with patch.dict(os.environ, {"KLING_RESERVE_LIST_FLOOR": "yes"}):
        assert kling_list_floor_enabled()


def test_kling_reserve_usd_micros_uses_table_when_live_none():
    """When live estimate is None, use table rate."""
    with patch("hfvg.providers.kling_video.rate_table_estimate") as mock_rate:
        mock_rate.return_value = type('obj', (object,), {'usd_micros': 500000})()
        result = kling_reserve_usd_micros(None, 5.0)
        assert result == 500000


def test_kling_reserve_usd_micros_uses_live_when_floor_disabled():
    """When floor is disabled, use live estimate even if below table."""
    with patch.dict(os.environ, {}, clear=True):
        with patch("hfvg.providers.kling_video.rate_table_estimate") as mock_rate:
            mock_rate.return_value = type('obj', (object,), {'usd_micros': 500000})()
            result = kling_reserve_usd_micros(250000, 5.0)  # live is half of table
            assert result == 250000  # Uses live, no floor


def test_kling_reserve_usd_micros_uses_max_when_floor_enabled():
    """When floor is enabled and live < table, use table (max)."""
    with patch.dict(os.environ, {"KLING_RESERVE_LIST_FLOOR": "1"}):
        with patch("hfvg.providers.kling_video.rate_table_estimate") as mock_rate:
            mock_rate.return_value = type('obj', (object,), {'usd_micros': 500000})()
            result = kling_reserve_usd_micros(250000, 5.0)  # live is half of table
            assert result == 500000  # Uses table (max of live and table)


def test_kling_reserve_usd_micros_uses_live_when_above_table():
    """When floor is enabled but live >= table, use live."""
    with patch.dict(os.environ, {"KLING_RESERVE_LIST_FLOOR": "1"}):
        with patch("hfvg.providers.kling_video.rate_table_estimate") as mock_rate:
            mock_rate.return_value = type('obj', (object,), {'usd_micros': 500000})()
            result = kling_reserve_usd_micros(600000, 5.0)  # live is above table
            assert result == 600000  # Uses live


def test_kling_reserve_logs_warning_when_live_below_table():
    """When floor is enabled and live < table, log a warning."""
    with patch.dict(os.environ, {"KLING_RESERVE_LIST_FLOOR": "1"}):
        with patch("hfvg.providers.kling_video.rate_table_estimate") as mock_rate:
            with patch("hfvg.providers.kling_video.logger") as mock_logger:
                mock_rate.return_value = type('obj', (object,), {'usd_micros': 500000})()
                kling_reserve_usd_micros(250000, 5.0)
                mock_logger.warning.assert_called_once()
                call_args = mock_logger.warning.call_args
                assert "kling_live_estimate_below_list_rate" in call_args[0]
