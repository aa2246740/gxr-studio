"""两段帧数各有自己的上限，不能靠答案到新帧很快来掩盖点击后的停顿。"""

import pytest

import test_warp_browser as warp


def test_auto_accepts_two_frames_for_each_half():
    warp.assert_auto_frames({"click": 10, "geometry": 12, "frame": 14})


@pytest.mark.parametrize("geometry,frame", [(13, 13), (15, 16), (12, 15), (9, 10), (11, 10)])
def test_auto_rejects_a_delayed_answer_or_frame(geometry, frame):
    with pytest.raises(AssertionError):
        warp.assert_auto_frames({"click": 10, "geometry": geometry, "frame": frame})


def test_a_delayed_auto_answer_fails_the_browser_acceptance_gate(monkeypatch):
    """旧判据接受 geometry-click=3、frame-geometry=0；直接调用整条浏览器验收，防止新增上限没有接进去。"""
    box = [0, 0, 100, 100]
    out = {
        "gpu": True,
        "sliders": [
            {
                "label": label,
                "gpu": True,
                "renders": 0,
                "boxRequests": 0,
                "steps": [{"changed": True, "frames": 1, "resized": True, "ms": 1}] * 10,
                "afterRelease": {"savedGeometry": True, "gpu": True, "box": box, "server": box},
            }
            for label in ("垂直", "水平", "畸变")
        ],
        "dragRendersTotal": 0,
        "auto": {
            "gpu": True,
            "renders": 0,
            "changed": True,
            "click": 10,
            "geometry": 13,
            "frame": 13,
            "savedGeometry": True,
            "box": box,
            "server": box,
        },
        "final": {
            "gpu": True,
            "renders": 0,
            "engineSize": {"w": 100, "h": 100},
            "exportBox": box,
            "canvasCss": [100, 100],
            "grayRing": 0,
            "engineCanvas": {"w": 100, "h": 100},
            "serverRect": [0, 0, 1, 1],
        },
    }
    monkeypatch.setattr(warp, "drive_warp", lambda *_args: out)
    with pytest.raises(AssertionError):
        warp.test_dragging_perspective_and_distortion_never_waits_for_the_server(None, None)
