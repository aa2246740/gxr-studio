"""卡片里的整张成片必须放得下，版面比例与导出和舞台一致。"""

import json

import pytest

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import parse_frame
from test_viewer_browser import drive, live  # noqa: F401


def test_frame_cards_fit_every_output_ratio_and_update_locally(live, tmp_path):  # noqa: F811
    browser = drive(live, tmp_path, "frame-cards", {"NAV_NO_WEBGPU": "1", "VIEWER_REDUCED_MOTION": "reduce"})
    out = browser["frameCards"]
    (tmp_path / "frame-cards.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not [request for request in browser["failedRequests"] if request[:3].isdigit()], browser["failedRequests"]
    assert out["reducedMotion"] is True and out["gpu"] is False
    assert len(out["samples"]) == 24
    metrics = FontMetrics()
    for sample in out["samples"]:
        why = json.dumps(sample, ensure_ascii=False)
        w, h = sample["photo"]
        assert w < h if sample["kind"] == "portrait" else w > h, why
        if sample["kind"] == "xpan":
            assert w / h == pytest.approx(65 / 24, abs=0.02), why
        for card in sample["cards"]:
            spec = parse_frame({**sample["spec"], "style": card["style"]})
            assert spec is not None
            layout = frame_layout(w, h, spec, sample["meta"], metrics)
            aspect = layout.width / layout.height if layout else w / h
            # 小位图取整最多差一个像素；用户看到的 CSS 比例必须更精确。
            assert abs(card["bitmap"][0] - aspect * card["bitmap"][1]) <= max(1, aspect), why
            content, box = card["content"], card["box"]
            assert content["w"] / content["h"] == pytest.approx(aspect, abs=0.01), why
            assert 0 < box["h"] <= 120, why
            assert content["x"] >= box["x"] + 3 and content["y"] >= box["y"] + 3, why
            assert content["x"] + content["w"] <= box["x"] + box["w"] - 3, why
            assert content["y"] + content["h"] <= box["y"] + box["h"] - 3, why
            assert card["outline"] != "none", why
            assert card["objectFit"] == "fill", why
            assert card["photoError"] < 20, why
        for selected in sample["stages"]:
            stage, card = selected["stage"], selected["card"]
            assert card["w"] / card["h"] == pytest.approx(stage["w"] / stage["h"], abs=0.01), why
        assert sample["clickRenders"] == 0, why
    for update in out["updates"]:
        assert update["clickRenders"] == 0, update
        if "corner" in update:
            assert update["corner"] == update["expected"], update
            assert update["changed"] == ["gallery", "leica", "hasselblad"], update
        else:
            assert update["aspect"] == pytest.approx(update["expectedAspect"], abs=0.01), update
    assert len(out["navigation"]) == 3
    assert len({step["signature"] for step in out["navigation"]}) == 3, out["navigation"]
    assert all(step["key"].startswith(step["id"] + "|") for step in out["navigation"]), out["navigation"]
    print("frame card click timings", json.dumps([round(s["ms"], 1) for s in out["samples"] + out["updates"]]))
