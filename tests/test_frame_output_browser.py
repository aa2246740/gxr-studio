"""真实浏览器的成品比例、即时尺寸和导出尺寸。"""

import json

import pytest

from test_viewer_browser import drive, live  # noqa: F401


def test_frame_ratio_and_size_change_locally_and_match_export(live, tmp_path, capsys):  # noqa: F811
    out = drive(live, tmp_path, "output-ratio", {"NAV_NO_WEBGPU": "1"})["outputRatio"]
    with capsys.disabled():
        print("FRAME_RATIO_DIAGNOSTIC", json.dumps(out["samples"][0], ensure_ascii=False))
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
        stage_ratio = sample["stage"][0] / sample["stage"][1]
        assert stage_ratio == pytest.approx(width / height, abs=0.002), json.dumps(sample, ensure_ascii=False)
        assert sample["immediate"].startswith(f"成品 {width} × {height} 像素"), sample
        assert sample["clickRenders"] == sample["clickQueries"] == 0, sample
        assert f'"output_ratio":"{sample["ratio"]}"' in sample["key"], sample
        if sample["ratio"] != "original":
            rw, rh = map(int, sample["ratio"].split(":"))
            assert width * rh == height * rw, sample
    print("output ratio click timings", json.dumps([round(s["ms"], 1) for s in out["samples"]]))


def test_frame_ratio_updates_while_the_server_preview_is_decoding(live, tmp_path, capsys):  # noqa: F811
    out = drive(live, tmp_path, "output-ratio-pending", {"NAV_NO_WEBGPU": "1"})["pendingFrameRatio"]
    with capsys.disabled():
        print("FRAME_RATIO_DIAGNOSTIC", json.dumps(out, ensure_ascii=False))
    why = json.dumps(out, ensure_ascii=False)
    assert out["gpu"] is False and out["held"] > 0 and out["originalStillVisible"] is True, why
    assert out["before"][0] / out["before"][1] == pytest.approx(3 / 4, abs=0.002), why
    assert out["immediate"] == "成品 692 × 692 像素 · 1:1", why
    for step in ("nextFrame", "heldStage", "afterDecode"):
        assert out[step][0] / out[step][1] == pytest.approx(1.0, abs=0.002), why
    assert out["clickRenders"] == 0, why
    print("server preview decode ratio", why)
