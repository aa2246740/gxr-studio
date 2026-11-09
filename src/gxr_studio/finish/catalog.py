"""`/api/frames` 的名称。措辞按 COPY.md 第 3.4、3.5 节，不另起名字。"""

from __future__ import annotations

from typing import Any

from .spec import parse_frame


def frames_document() -> dict[str, Any]:
    color = [
        {"id": "white", "name": "白"},
        {"id": "black", "name": "黑"},
        {"id": "cream", "name": "米白"},
    ]
    weight = [
        {"id": "thin", "name": "细"},
        {"id": "medium", "name": "中"},
        {"id": "thick", "name": "粗"},
    ]
    show = [
        {"id": "show_logo", "name": "标志"},
        {"id": "show_params", "name": "参数"},
        {"id": "show_date", "name": "日期"},
    ]
    ratios = [
        {"id": "1:1", "name": "1:1 · 方图"},
        {"id": "4:5", "name": "4:5 · Instagram 竖图"},
        {"id": "3:4", "name": "3:4 · 小红书"},
        {"id": "2:3", "name": "2:3 · 竖幅"},
        {"id": "9:16", "name": "9:16 · 快拍 · 短视频"},
        {"id": "16:9", "name": "16:9 · 横屏视频"},
        {"id": "3:2", "name": "3:2 · 横幅"},
        {"id": "original", "name": "照片原比例"},
    ]
    styles: list[dict[str, Any]] = [
        {"id": "none", "name": "无", "options": []},
        {"id": "leica", "name": "徕卡水印", "options": ["color", "show_logo", "show_params", "show_date", "text"]},
        {"id": "gallery", "name": "白边", "options": ["color", "weight"]},
        {"id": "hasselblad", "name": "哈苏水印", "options": ["color", "show_params", "show_date", "text"]},
        {"id": "instant", "name": "拍立得", "options": ["fmt", "show_date", "text"]},
        {"id": "film", "name": "胶片", "options": ["fmt", "table"]},
    ]
    for style in styles:
        if style["id"] != "none":
            style["options"].append("output_ratio")
        spec = parse_frame({"style": style["id"]})
        style["defaults"] = spec.to_json() if spec else None
    return {
        "styles": styles,
        "options": {"color": color, "weight": weight, "show": show, "output_ratio": ratios},
        "defaults": {"frame": {"style": "none", "color": "white", "weight": "medium", "output_ratio": "3:4"}},
        "stamp": {
            "placements": [{"id": "photo", "name": "照片上"}, {"id": "border", "name": "拍立得边框上"}],
            "content": [
                {"id": "date", "name": "日期"},
                {"id": "text", "name": "文字"},
                {"id": "date+text", "name": "日期和文字"},
            ],
            "formats": [
                {"id": "'yy mm dd", "name": "'26 10 30"},
                {"id": "yy mm dd", "name": "26 10 30"},
                {"id": "yyyy.mm.dd", "name": "2026.10.30"},
                {"id": "mm dd 'yy", "name": "10 30 '26"},
            ],
            "fonts": [
                {"id": "seg7", "name": "数码管"},
                {"id": "typewriter", "name": "打字机"},
                {"id": "sans", "name": "无衬线"},
            ],
            "colors": [
                {"id": "orange", "name": "橙", "hex": "#FF6A00"},
                {"id": "red", "name": "红", "hex": "#DC2020"},
                {"id": "yellow", "name": "黄", "hex": "#FFC428"},
                {"id": "white", "name": "白", "hex": "#FFFFFF"},
            ],
            "sizes": [
                {"id": "small", "name": "小"},
                {"id": "medium", "name": "中"},
                {"id": "large", "name": "大"},
            ],
            "corners": [
                {"id": "tl", "name": "左上"},
                {"id": "tr", "name": "右上"},
                {"id": "bl", "name": "左下"},
                {"id": "br", "name": "右下"},
            ],
            "text_max_length": 24,
            "defaults": {
                "enabled": False,
                "content": "date",
                "date_format": "'yy mm dd",
                "font": "seg7",
                "color": "orange",
                "size": "medium",
                "corner": "br",
                "glow": True,
            },
        },
    }
