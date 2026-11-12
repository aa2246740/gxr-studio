"""Run the native ``gxr-cli`` and load its JSON or ``.npy`` output.

Later ports reuse this helper. The binary is ``core/target/release/gxr-cli``
(``make core``). A test skips when the binary is missing, unless
``GXR_REQUIRE_CORE=1`` (``make check`` sets that), in which case the test fails.

JSON is parsed before it is compared. Python and Rust print floats differently,
so parity tests compare numbers, not the text. The ``.npy`` reader is numpy:
the CLI writes format 1.0, little-endian, C order.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "core" / "target" / "release" / "gxr-cli"
DEFAULT_TIMEOUT_S = 180.0


class CoreCliError(Exception):
    """The CLI exited 2 with ``{"error": {"kind", "message"}}``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


def require_cli() -> Path:
    """Return the binary, skip when it was not built, or fail under ``GXR_REQUIRE_CORE``."""
    if CLI.is_file() and os.access(CLI, os.X_OK):
        return CLI
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(f"gxr-cli is required but not built at {CLI}")
    pytest.skip(f"gxr-cli is not built ({CLI}); run make core")


def run(args: list[str], *, timeout: float = DEFAULT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run ``gxr-cli`` with ``args``. Does not raise on a non-zero exit."""
    return subprocess.run(
        [str(require_cli()), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_json(args: list[str], *, timeout: float = DEFAULT_TIMEOUT_S) -> tuple[int, Any]:
    """Return ``(exit_code, parsed stdout)``. A usage error (exit 1) fails the test."""
    proc = run(args, timeout=timeout)
    if proc.returncode == 1:
        pytest.fail(f"gxr-cli usage error: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"gxr-cli exit {proc.returncode} did not print JSON ({exc})")
    return proc.returncode, payload


def error_of(code: int, payload: Any) -> CoreCliError | None:
    """The core error when ``code`` is 2, otherwise None. Any other failure fails the test."""
    if code == 0:
        return None
    if code == 2 and isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        return CoreCliError(str(err.get("kind")), str(err.get("message")))
    pytest.fail(f"unexpected gxr-cli exit {code}")


def run_npy(args: list[str], dest: Path, *, timeout: float = DEFAULT_TIMEOUT_S) -> np.ndarray:
    """Run a subcommand that writes ``dest`` and return the array. A core error is raised."""
    code, payload = run_json([*args, "--out", str(dest)], timeout=timeout)
    err = error_of(code, payload)
    if err is not None:
        raise err
    return np.load(dest)
