"""边框和时间戳用的字体（Pillow 版）。版面算式在 `layout.py`，这里只管把字体代号变成能量能画的字体对象。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "assets"
FONT_FILES = {
    "atkinson": "AtkinsonHyperlegible-Regular.ttf",
    "inter": "Inter-Regular.ttf",
    "courier": "CourierPrime-Regular.ttf",
    "patrick": "PatrickHand-Regular.ttf",
    "dseg": "DSEG7Classic-Regular.ttf",
}


class PilFonts:
    """一次导出用的字体和量字（`layout.Metrics`）。每次调用自己建一份，线程之间不共用 FreeType 的字体对象。"""

    def __init__(self) -> None:
        self._faces: dict[tuple[str, int, bool], ImageFont.FreeTypeFont] = {}
        self._probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def face(self, font: str, px: int, cjk: bool) -> ImageFont.FreeTypeFont:
        key = (font, px, cjk)
        if key not in self._faces:
            self._faces[key] = self._load(font, px, cjk)
        return self._faces[key]

    @staticmethod
    def _load(font: str, px: int, cjk: bool) -> ImageFont.FreeTypeFont:
        """自定义文字缺中文字形时，按系统字体顺序回退。仓库里不打包中文字体。"""
        if cjk:
            from ..render.params import EditError
            from .stamp import cjk_font

            face = cjk_font(px)
            if face is None:
                raise EditError("missing_cjk", "无法显示中文。请改用英文或数字。")
            return face
        return ImageFont.truetype(str(ASSETS / "fonts" / FONT_FILES[font]), px)

    def advance(self, font: str, px: int, text: str, cjk: bool) -> float:
        return self._probe.textlength(text, font=self.face(font, px, cjk))

    def bbox(self, font: str, px: int, text: str, cjk: bool) -> tuple[int, int, int, int]:
        left, top, right, bottom = self._probe.textbbox((0, 0), text, font=self.face(font, px, cjk))
        return int(left), int(top), int(right), int(bottom)
