"""A requested fixed runtime must not silently launch the system browser."""

from __future__ import annotations

import json
from pathlib import Path

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


def _no_pin(tmp_path, monkeypatch):
    """没有 GXR_CHROME、没有运行时，PATH 和候选路径上都放一个“系统浏览器”。"""
    system_browser = tmp_path / "google-chrome"
    system_browser.write_text("#!/bin/sh\n")
    system_browser.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GXR_CHROME", raising=False)
    monkeypatch.delenv("GXR_PLAYWRIGHT_RUNTIME", raising=False)
    monkeypatch.setattr(gpu_support, "_REAL_HOME", tmp_path / "home")
    monkeypatch.setattr(gpu_support, "_CANDIDATES", (str(system_browser),))
    gpu_support._pinned_executable.cache_clear()
    return system_browser


def test_macos_never_falls_back_to_the_system_browser(tmp_path, monkeypatch):
    # 本机的 Google Chrome 每次启动都在内置盘复制一份应用，被强杀就留下（2026-11 积了 432 份）。
    _no_pin(tmp_path, monkeypatch)
    monkeypatch.setattr(gpu_support, "_IS_MAC", True)
    assert gpu_support.find_chrome() is None
    monkeypatch.delenv("GXR_REQUIRE_GPU_TEST", raising=False)
    with pytest.raises(pytest.skip.Exception, match="固定版本"):
        gpu_support.require_chrome()
    monkeypatch.setenv("GXR_REQUIRE_GPU_TEST", "1")
    with pytest.raises(pytest.fail.Exception, match="GXR_CHROME"):
        gpu_support.require_chrome()


def test_other_platforms_keep_the_system_browser_lookup(tmp_path, monkeypatch):
    system_browser = _no_pin(tmp_path, monkeypatch)
    monkeypatch.setattr(gpu_support, "_IS_MAC", False)
    assert gpu_support.find_chrome() == str(system_browser)


def test_the_pinned_runtime_browser_is_used_without_gxr_chrome(tmp_path, monkeypatch):
    if gpu_support.shutil.which("node") is None:
        pytest.skip("没有 Node.js")
    node_dir = Path(gpu_support.shutil.which("node")).parent
    _no_pin(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{tmp_path}:{node_dir}")
    monkeypatch.setattr(gpu_support, "_IS_MAC", True)
    pinned = tmp_path / "pinned-chromium"
    pinned.write_text("#!/bin/sh\n")
    runtime = tmp_path / "home" / ".codex" / "playwright-runtime" / "runtime.mjs"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(f"export const pinnedRuntime = Object.freeze({{ executablePath: {json.dumps(str(pinned))} }});\n")
    try:
        assert gpu_support.find_chrome() == str(pinned)
        # 运行时说的路径不存在就不算，macOS 上也不退回系统浏览器。
        pinned.unlink()
        gpu_support._pinned_executable.cache_clear()
        assert gpu_support.find_chrome() is None
    finally:
        gpu_support._pinned_executable.cache_clear()
