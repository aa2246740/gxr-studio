"""真实浏览器的成品比例、即时尺寸和导出尺寸。"""

import json

import pytest

from test_viewer_browser import drive, live  # noqa: F401


def test_frame_ratio_and_size_change_locally_and_match_export(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "output-ratio", {"NAV_NO_WEBGPU": "1"})["outputRatio"]
    assert out["labels"] == [
        "1:1 · 方图",
        "4:5 · Instagram 竖图",
        "3:4 · 小红书",
        "2:3 · 竖幅",
        "9:16 · 快拍 · 短视频",
        "16:9 · 横屏视频",
        "3:2 · 横幅",
        "照片原比例",
    ]
    assert len(out["samples"]) == 40
    for sample in out["samples"]:
        width, height = sample["size"]
        assert sample["exported"] == [width, height], sample
        assert sample["stage"][0] / sample["stage"][1] == pytest.approx(width / height, abs=0.002), sample
        assert sample["immediate"].startswith(f"成品 {width} × {height} 像素"), sample
        assert sample["clickRenders"] == sample["clickQueries"] == 0, sample
        assert f'"output_ratio":"{sample["ratio"]}"' in sample["key"], sample
        if sample["ratio"] != "original":
            rw, rh = map(int, sample["ratio"].split(":"))
            assert width * rh == height * rw, sample
    print("output ratio click timings", json.dumps([round(s["ms"], 1) for s in out["samples"]]))
