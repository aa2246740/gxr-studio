"""The offline guard permits only the event loop's own local socketpair."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

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


def test_a_rejected_connection_does_not_mark_the_next_test(tmp_path):
    (tmp_path / "conftest.py").write_text((Path(__file__).parent / "conftest.py").read_text(), encoding="utf-8")
    script = tmp_path / "test_records.py"
    script.write_text(
        "import socket, pytest\n"
        "from gxr_studio import audit\n"
        "def test_first():\n"
        "    with socket.socket() as client:\n"
        "        with pytest.raises(PermissionError):\n"
        "            client.connect(('127.0.0.1', 18766))\n"
        "def test_second():\n"
        "    assert audit.network_attempts() == []\n",
        encoding="utf-8",
    )
    report = tmp_path / "results.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--confcutdir=" + str(tmp_path),
            "--basetemp=" + str(tmp_path / "child-tmp"),
            "--junitxml=" + str(report),
            str(script),
        ],
        env={**os.environ, "PYTEST_ADDOPTS": "--force-reruns 0"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    cases = {case.attrib["name"]: case for case in ElementTree.parse(report).iter("testcase")}
    assert set(cases) == {"test_first", "test_second"}
    assert cases["test_first"].find("error") is not None
    assert cases["test_second"].find("error") is None and cases["test_second"].find("failure") is None
