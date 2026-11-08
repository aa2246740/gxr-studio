"""滑块只接管自己的导航键，字母快捷键仍交给查看器，文本输入保持独立。"""

from test_viewer_browser import drive, live  # noqa: F401  live 是夹具


def test_viewer_shortcuts_work_from_a_slider_and_leave_native_keys_alone(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "slider-keys")["keys"]
    assert out["crop"] == "tab-crop"
    assert "左：原图" in out["compare"] and "右：编辑后" in out["compare"]
    assert out["original"] == out["pinned"] == "原图"
    assert out["afterRelease"] != "原图"
    expected_signs = {"ArrowLeft": -1, "ArrowRight": 1, "ArrowUp": 1, "ArrowDown": -1, "Home": -1, "End": 1, "PageUp": 1, "PageDown": -1}
    for row in out["native"]:
        assert row["tab"] == "tab-adjust" and "/photo/" in row["route"], row
        assert row["value"] * expected_signs[row["key"]] > 0, row
    for row in out["typing"]:
        assert row["tab"] == "tab-adjust", row
        assert all("原图" not in text for text in row["labels"]), row
