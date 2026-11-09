"""真实 Chromium 首帧中的裁切握柄，覆盖无 WebGPU、减少动态效果和显卡预览。"""

import shutil
import sys

import pytest

from test_viewer_browser import drive
from test_viewer_browser import live as live

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
@pytest.mark.parametrize("preview", ["server", "gpu"])
def test_crop_handles_are_painted_on_the_first_frame_without_pointer_input(live, tmp_path, preview, motion):
    if preview == "gpu" and sys.platform != "darwin":
        pytest.skip("WebGPU 上屏在 macOS 验证；CI 的服务器预览用例明确关闭 navigator.gpu")
    env = {"VIEWER_REDUCED_MOTION": motion}
    if preview == "server":
        env["NAV_NO_WEBGPU"] = "1"
    out = drive(live, tmp_path, f"handles-{preview}", env)
    frames = out["handles"]
    assert [frame["label"] for frame in frames] == [
        "first-open",
        "return-from-adjust",
        "switch-photo",
        "saved-crop",
        "no-crop",
        "resize-stage",
    ]
    for frame in frames:
        assert frame["gpu"] is (preview == "gpu"), frame
        assert frame["reduced"] is (motion == "reduce"), frame
        assert frame["pointerEvents"] == 0, frame
        if preview == "server":
            assert frame["navigatorGpu"] is False, frame
        assert [grip["corner"] for grip in frame["grips"]] == ["tl", "tr", "bl", "br"], frame
        for grip in frame["grips"]:
            assert grip["visible"] and all(width > 0 for width in grip["borders"]), (frame["label"], grip)
            assert grip["paintedInsidePhoto"] == [True, True], (frame["label"], grip)
            assert grip["unobscured"] == [True, True], (frame["label"], grip)
