"""面板操作栏不能遮住卡片或曲线；在真浏览器里量正常流与滚动中的位置。"""

from test_viewer_browser import drive, live  # noqa: F401  live 是夹具


def test_frame_default_button_never_overlaps_the_canvas_card(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "panel-frame")
    for row in out["panel"]:
        assert all(s["gap"] >= 6 for s in row["samples"]), row


def test_adjust_reset_never_overlaps_the_curve_at_any_panel_size(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "panel-adjust")
    for row in out["panel"]:
        assert all(s["gap"] >= 6 for s in row["samples"]), row
