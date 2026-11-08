"""通过真实面板选择印记落点，既保存到 sidecar，也更新舞台预览。"""

from test_viewer_browser import drive, live  # noqa: F401  live 是夹具


def test_polaroid_border_stamp_is_saved_and_previewed(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "stamp-border")["stampBorder"]
    assert out["frame"] == "instant"
    assert out["stamp"]["enabled"] is True and out["stamp"]["placement"] == "border"
    assert out["preview"] is True
