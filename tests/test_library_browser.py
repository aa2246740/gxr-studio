"""复用真服务器 / CDP 驱动，检查图库选择、编辑器顺序和实际缩略图边缘像素。"""

import json

from test_viewer_browser import drive, live  # noqa: F401  pytest fixture


def test_library_selection_navigation_export_scope_and_cover_pixels(live, tmp_path):  # noqa: F811  imported fixture
    out = drive(live, tmp_path, "library")["library"]
    (tmp_path / "library-browser.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    a, b, c, d = out["order"][:4]
    assert out["order"] == out["apiOrder"]
    assert out["defaultLabel"] == "导出" and out["selectedLabel"] == "导出选中的 1 张"
    assert out["tick"] == [a] and out["plainOpened"] is True and out["selectionInEditor"] == [a]
    assert out["next"] == c and out["previous"] == a and out["back"] == [a]
    assert out["command"] == [a, c] and out["shift"] == [c, d]
    assert out["dateSelection"] == [] and "已清除 2 张选择" in out["clearToast"] and "恢复选择" in out["clearToast"]
    assert out["dateLabel"] == f"导出 {out['date']['date']} 的 {out['date']['count']} 张"
    assert "HDR" in out["exportDialog"]
    assert out["exportFolder"] == out["expectedFolder"]
    assert out["restored"] == [c, d] and out["escape"] == []
    assert out["gridKeyboardOpened"] == b
    assert out["hdrChips"] == 0
    assert len(out["cover"]) == 2 and all(p["maxError"] <= 20 for p in out["cover"]), out["cover"]
    assert out["heightAfter"] == out["heightBefore"] and out["fullPersisted"] is True
