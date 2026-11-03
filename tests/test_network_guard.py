"""The offline guard permits only the event loop's own local socketpair."""

from __future__ import annotations

import asyncio
import socket

import pytest

from gxr_studio import audit


def test_event_loop_can_create_its_fallback_socketpair(monkeypatch):
    monkeypatch.setattr(socket, "socketpair", socket._fallback_socketpair)
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0, result="ready")) == "ready"
    finally:
        loop.close()
    assert audit.network_attempts() == []


@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.1"])
def test_ordinary_connections_are_still_refused(host):
    with socket.socket() as client:
        with pytest.raises(PermissionError, match="offline"):
            client.connect((host, 18766))
    attempts = audit.network_attempts(clear=True)
    assert len(attempts) == 1 and host in attempts[0]
    assert audit.network_attempts() == []


def test_direct_fallback_socketpair_is_still_refused():
    with pytest.raises(PermissionError, match="offline"):
        socket._fallback_socketpair()
    assert len(audit.network_attempts(clear=True)) == 1


def test_name_resolution_is_still_refused():
    with pytest.raises(PermissionError, match="offline"):
        socket.getaddrinfo("example.invalid", 80)
    attempts = audit.network_attempts(clear=True)
    assert len(attempts) == 1 and "socket.getaddrinfo" in attempts[0]


def test_cleared_records_do_not_carry_over():
    assert audit.network_attempts() == []
    for port in (18766, 18767):
        with socket.socket() as client:
            with pytest.raises(PermissionError, match="offline"):
                client.connect(("127.0.0.1", port))
        attempts = audit.network_attempts(clear=True)
        assert len(attempts) == 1 and str(port) in attempts[0]
