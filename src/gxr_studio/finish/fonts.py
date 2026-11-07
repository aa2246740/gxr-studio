"""边框和时间戳用的字体。版面算式在 `layout.py`；这里管两件事：

- 画：`PilFonts.face` 给 Pillow 的字体对象，一个字一个字地画在 `layout` 算好的列上。
- 量：`FontMetrics` 给版面要的整数（步进宽度、字形外框、上沿高度），从字体文件里的表读，不问 Pillow 的排版引擎。

Pillow 的排版引擎因电脑而异（装了 libraqm 的走 RAQM，没有的走 BASIC），量字的结果也不同，所以所有字体对象
一律明确指定 BASIC（`open_face`），中文回退字体也一样；而且版面不靠它量，见 `sfnt.py`。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .sfnt import FontTable, Glyph, load_font

ASSETS = Path(__file__).resolve().parents[1] / "assets"
FONT_FILES = {
    "atkinson": "AtkinsonHyperlegible-Regular.ttf",
    "inter": "Inter-Regular.ttf",
    "courier": "CourierPrime-Regular.ttf",
    "patrick": "PatrickHand-Regular.ttf",
    "dseg": "DSEG7Classic-Regular.ttf",
}
LAYOUT_ENGINE = ImageFont.Layout.BASIC  # 每个字体对象都明确用它，不看这台电脑有没有 libraqm
CJK_REFERENCE_PX = 1000  # 中文字体读不到表：按这个字号量，当作 upm = 1000


def open_face(path: str, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, max(1, px), layout_engine=LAYOUT_ENGINE)


def font_table(font: str) -> FontTable:
    return load_font(ASSETS / "fonts" / FONT_FILES[font])


@cache
def font_table_json(font: str) -> bytes:
    """浏览器预览要的同一份表（`/api/finish/assets/<字体>.json`）。"""
    return json.dumps(font_table(font).to_json(), separators=(",", ":")).encode("ascii")


class PilFonts:
    """一次导出用的字体对象（只管画）。每次调用自己建一份，线程之间不共用 FreeType 的字体对象。"""

    def __init__(self) -> None:
        self._faces: dict[tuple[str, int, bool], ImageFont.FreeTypeFont] = {}
        self._probe = ImageDraw.Draw(Image.new("L", (1, 1)))

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
        return open_face(str(ASSETS / "fonts" / FONT_FILES[font]), px)


def draw_run(draw: ImageDraw.ImageDraw, item: dict[str, Any], fonts: PilFonts, fill: tuple[int, int, int] | int) -> None:
    """一行字一个字地画在 `layout` 算好的列（item["xs"]）和行（item["y"]）上，锚点是左上（字体上沿）。
    空格和控制字符不画（没有墨迹）；Pillow 一次只排一个字，排版引擎的字距、连字影响不到位置。"""
    face = fonts.face(item["font"], item["px"], item["cjk"])
    for ch, x in zip(item["text"], item["xs"], strict=True):
        if ch != " " and ord(ch) >= 0x20:
            draw.text((x, item["y"]), ch, font=face, fill=fill)


class FontMetrics:
    """`layout.Metrics`：内置字体读字体表；中文字体按 1000 像素的字号用 Pillow（BASIC）量。"""

    def __init__(self, fonts: PilFonts | None = None) -> None:
        self._fonts = fonts or PilFonts()
        self._cjk: dict[tuple[str, str], Glyph] = {}

    def upm(self, font: str, cjk: bool) -> int:
        return CJK_REFERENCE_PX if cjk else font_table(font).upm

    def glyph(self, font: str, ch: str, cjk: bool) -> Glyph:
        if not cjk:
            return font_table(font).glyph(ord(ch))
        key = (font, ch)
        if key not in self._cjk:
            face = self._fonts.face(font, CJK_REFERENCE_PX, True)
            left, top, right, bottom = face.getbbox(ch, anchor="ls")
            ink = right > left and bottom > top
            self._cjk[key] = (round(face.getlength(ch)), (int(left), -int(bottom), int(right), -int(top)) if ink else None)
        return self._cjk[key]

    def ascent(self, font: str, px: int, cjk: bool) -> int:
        from .layout import ascent_px

        if not cjk:
            table = font_table(font)
            return ascent_px(table.ascender, table.upm, px)
        return int(self._fonts.face(font, max(1, px), True).getmetrics()[0])


def table_dict(font: str) -> dict[str, Any]:
    """测试用：和 `font_table_json` 同一份内容的字典。"""
    return font_table(font).to_json()
