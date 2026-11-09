"""GPU 对拍的启动和模块错误应直接报告，不能伪装成着色器超时。"""

from __future__ import annotations

import sys

import pytest

import gpu_support


def test_browser_exit_reports_its_code_without_waiting_for_page_timeout(tmp_path, monkeypatch):
    # Python 不接受 Chrome 参数，会真实退出；不需要启动浏览器。
    monkeypatch.setattr(gpu_support, "require_chrome", lambda: sys.executable)
    with pytest.raises(pytest.fail.Exception, match="浏览器提前退出") as caught:
        gpu_support.run_engine(tmp_path, {}, [], timeout=15)
    assert '"exit_code": 2' in str(caught.value)
    assert '"requests": []' in str(caught.value)


def test_module_import_failure_is_reported_by_the_page(tmp_path, monkeypatch):
    engine = tmp_path / "broken_engine.js"
    engine.write_text("export const Engine = ;\n")
    monkeypatch.setattr(gpu_support, "ENGINE", engine)
    with pytest.raises(pytest.fail.Exception, match="页面出错") as caught:
        gpu_support.run_engine(tmp_path, {}, [], timeout=15)
    assert "SyntaxError" in str(caught.value)


def test_pending_engine_creation_reports_the_stalled_phase(tmp_path, monkeypatch):
    engine = tmp_path / "pending_engine.js"
    engine.write_text("export class Engine { static create() { return new Promise(() => {}); } }\n")
    monkeypatch.setattr(gpu_support, "ENGINE", engine)
    with pytest.raises(pytest.fail.Exception, match="页面超时没有结束") as caught:
        gpu_support.run_engine(tmp_path, {}, [], timeout=15)
    assert '"phase": "create"' in str(caught.value)
    assert '"completed": []' in str(caught.value)
    assert '"exit_code": null' in str(caught.value)
