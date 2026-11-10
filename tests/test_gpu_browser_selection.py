"""A requested fixed runtime must not silently launch the system browser."""

from __future__ import annotations

import pytest

import gpu_support


def test_a_selected_runtime_requires_an_explicit_browser(tmp_path, monkeypatch):
    system_browser = tmp_path / "system-chrome"
    system_browser.write_text("not an executable browser\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GXR_PLAYWRIGHT_RUNTIME", str(tmp_path / "runtime.mjs"))
    monkeypatch.delenv("GXR_CHROME", raising=False)
    monkeypatch.setattr(gpu_support, "_CANDIDATES", (str(system_browser),))

    with pytest.raises(pytest.fail.Exception, match="GXR_CHROME"):
        gpu_support.require_chrome()
