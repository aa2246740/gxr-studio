"""A requested fixed runtime must not silently launch the system browser."""

from __future__ import annotations

import pytest

import gpu_support
import test_full_recipe_flow


def test_a_selected_runtime_requires_an_explicit_browser(tmp_path, monkeypatch):
    system_browser = tmp_path / "system-chrome"
    system_browser.write_text("not an executable browser\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GXR_PLAYWRIGHT_RUNTIME", str(tmp_path / "runtime.mjs"))
    monkeypatch.delenv("GXR_CHROME", raising=False)
    monkeypatch.setattr(gpu_support, "_CANDIDATES", (str(system_browser),))

    with pytest.raises(pytest.fail.Exception, match="GXR_CHROME"):
        gpu_support.require_chrome()


@pytest.mark.parametrize("missing", ["runtime", "node"])
def test_full_recipe_gpu_fails_before_browser_selection_when_pin_is_missing(tmp_path, monkeypatch, missing):
    monkeypatch.delenv("GXR_PLAYWRIGHT_RUNTIME", raising=False)
    monkeypatch.delenv("GXR_CHROME", raising=False)
    monkeypatch.setattr(test_full_recipe_flow, "REAL_HOME", tmp_path)
    if missing == "node":
        runtime = tmp_path / ".codex/playwright-runtime/runtime.mjs"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("// never imported by this missing-node case\n")
    monkeypatch.setattr(test_full_recipe_flow.shutil, "which", lambda _name: None if missing == "node" else "/synthetic/node")

    def reject_browser_selection():
        pytest.fail("Missing pin reached legacy system-browser selection")

    monkeypatch.setattr(gpu_support, "require_chrome", reject_browser_selection)
    with pytest.raises(RuntimeError, match="pinned"):
        test_full_recipe_flow._gpu_pixels(tmp_path, b"", b"", {"key": "unused"}, {})
