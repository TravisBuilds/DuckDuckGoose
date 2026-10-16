"""Test that pytest-socket blocks outbound connections."""

import pytest
import socket


def test_socket_blocked():
    """Test that outbound socket connections are blocked by pytest-socket."""
    from pytest_socket import SocketConnectBlockedError
    
    with pytest.raises(SocketConnectBlockedError) as exc_info:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("10.255.255.1", 9))
    
    # Should raise SocketConnectBlockedError with message about blocked host
    assert "10.255.255.1" in str(exc_info.value)
