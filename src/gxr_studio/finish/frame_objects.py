"""Approved R3 frame geometry. Keep the matching finish_frame_*.js equations equal."""

from __future__ import annotations

import math
from typing import Any

from .frame_core import Type, frame_index, join, make_unit, rotate_unit_ccw, solve
from .layout import Metrics

Meta = dict[str, str]
RGB = tuple[int, int, int]


def _tri(x: float, y_mid: float, size: int, fill: RGB) -> dict[str, Any]:
    return {
        "k": "poly",
        "pts": [[round(x), round(y_mid - size / 2)], [round(x), round(y_mid + size / 2)], [round(x + size * 0.9), round(y_mid)]],
        "fill": fill,
    }


def _strip(w: int, h: int, meta: Meta, ratio: tuple[int, int] | None, kind: str, table: str, m: Metrics) -> dict[str, Any]:
    t = Type(m)
    portrait = h > w
    a, c = (h, w) if portrait else (w, h)
    short = c
    light = table != "black"
    table_col = (243, 241, 236) if light else (24, 24, 26)
    strip_col = (13, 12, 12)
    amber = (214, 140, 56) if kind == "135" else (210, 148, 66)
    n = frame_index(meta)

    def unit(wref: int) -> dict[str, Any]:
        if kind == "135":
            rebate, e = (round(0.229 * short), round(0.1 * short))
            hw, hh, pitch = (round(0.117 * short), round(0.0825 * short), round(0.198 * short))
            hy = round(0.083 * short - hh / 2)
            mark_mid = (hy + hh + rebate) / 2.0
            px = t.px_for_cap("inter_sb", 0.026 * short)
        else:
            e = round(0.07 * short)
            px = t.px_for_cap("inter_sb", 0.017 * short)
            rebate = max(round(0.065 * short), round(3.2 * t.cap_h("inter_sb", px)))
            mark_mid = rebate / 2.0
        strip_w, strip_h = (a + 2 * e, c + 2 * rebate)
        under: list[dict[str, Any]] = [
            {
                "k": "shadow",
                "x": 0,
                "y": 0,
                "w": strip_w,
                "h": strip_h,
                "blur": max(6, round(0.024 * short)),
                "dy": round(0.012 * short),
                "alpha": 0.4 if light else 0.0,
                "radius": round(0.006 * short),
            },
            {"k": "rect", "x": 0, "y": 0, "w": strip_w, "h": strip_h, "fill": strip_col, "radius": round(0.006 * short)},
            {
                "k": "rect",
                "x": 1,
                "y": 1,
                "w": strip_w - 2,
                "h": strip_h - 2,
                "fill": None,
                "outline": (46, 42, 38),
                "stroke_w": 1,
                "radius": round(0.006 * short),
            },
        ]
        if kind == "135":
            cnt = (strip_w - 2 * round(0.03 * short)) // pitch
            x0 = (strip_w - ((cnt - 1) * pitch + hw)) // 2
            for i in range(cnt):
                for yy in (hy, strip_h - hy - hh):
                    if light:
                        under.append({"k": "hole", "x": x0 + i * pitch, "y": yy, "w": hw, "h": hh, "radius": round(0.2 * hh)})
                    else:
                        under.append(
                            {"k": "rect", "x": x0 + i * pitch, "y": yy, "w": hw, "h": hh, "fill": (232, 228, 218), "radius": round(0.2 * hh)}
                        )
        else:
            for yy in (round(0.012 * short), strip_h - round(0.012 * short)):
                under.append(
                    {
                        "k": "line",
                        "x1": round(0.03 * short),
                        "y1": yy,
                        "x2": strip_w - round(0.03 * short),
                        "y2": yy,
                        "w": 1,
                        "fill": (40, 37, 34),
                    }
                )
        over: list[dict[str, Any]] = [
            {"k": "rect", "x": e - 1, "y": rebate - 1, "w": a + 2, "h": c + 2, "fill": None, "outline": (30, 28, 26), "stroke_w": 1}
        ]
        xl, xr = (round(0.05 * short) if kind == "135" else e, strip_w - (round(0.05 * short) if kind == "135" else e))
        tri = max(8, round(0.62 * t.cap_h("inter_sb", px)))
        top_mid, bot_mid = (mark_mid, strip_h - mark_mid)
        top_l = "RICOH GXR 135" if kind == "135" else "RICOH GXR 120"
        over.append(t.cap_text(xl, top_mid, px, "inter_sb", top_l, amber, 0.08))
        label = str(n) if kind == "135" else str((n - 1) % 12 + 1)
        if kind == "135":
            cx = strip_w * 0.52
            over.append(_tri(cx - tri * 1.4, top_mid, tri, amber))
            over.append(t.cap_text(cx, top_mid, px, "inter_sb", label, amber, 0.06))
            over.append(t.cap_text(xr, top_mid, px, "inter_sb", f"{label}A", amber, 0.06, "r"))
            over.append(t.cap_text(xl, bot_mid, px, "inter_sb", join(["RICOH GXR", meta.get("lens_short", "")], "  ·  "), amber, 0.06))
            over.append(_tri(cx - tri * 1.4, bot_mid, tri, amber))
            over.append(t.cap_text(cx, bot_mid, px, "inter_sb", f"{label}A", amber, 0.06))
            over.append(t.cap_text(xr, bot_mid, px, "inter_sb", meta.get("date", ""), amber, 0.06, "r"))
        else:
            over.append(_tri(xr - t.advance("inter_sb", px, label, 0.06) - tri * 1.5, top_mid, tri, amber))
            over.append(t.cap_text(xr, top_mid, px, "inter_sb", label, amber, 0.06, "r"))
            spec = join([meta.get(k, "") for k in ("lens_short", "aperture", "shutter", "iso")], "   ")
            over.append(t.cap_text(xl, bot_mid, px, "inter_sb", spec, amber, 0.05))
            over.append(t.cap_text(xr, bot_mid, px, "inter_sb", meta.get("date", ""), amber, 0.05, "r"))
        u = make_unit(
            strip_w, strip_h, (e, rebate), a, c, under=under, over=over, bg=table_col, bgfx={"kind": "glow", "falloff": 0.07 if light else 0.0}
        )
        u["bgfx_always"] = True
        if portrait:
            for item in over:
                if item["k"] == "text":
                    item["tw"] = math.ceil(t.advance(item["font"], item["px"], item["text"], item["track"])) + 2
        return rotate_unit_ccw(u) if portrait else u

    margin = round(0.07 * short)
    return solve(unit, ratio, w, natural=(margin, margin, margin), f_side=0.06, f_top=0.06, f_bot=0.06, split=0.5)


INSTANT_FORMATS = {
    "polaroid": (0.057, 0.062, 0.29),
    "instax_square": (0.081, 0.081, 0.306),
    "instax_wide": (0.073, 0.065, 0.32),
    "instax_mini": (0.087, 0.087, 0.43),
}


def instant(
    w: int, h: int, meta: Meta, ratio: tuple[int, int] | None, m: Metrics, *, text: str = "", show_date: bool = False, fmt: str = "polaroid"
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    rs, rt, rc = INSTANT_FORMATS[fmt]
    side, top = (max(22, round(rs * short)), max(22, round(rt * short)))
    chin = max(4 * side if fmt == "polaroid" else 3 * side, round(rc * short))
    ivory, stone, ink = ((250, 248, 243), (232, 228, 220), (43, 43, 43))

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        card_w, card_h = (w + 2 * side, h + top + chin)
        rad = round(0.023 * card_w)
        under = [
            {
                "k": "shadow",
                "x": 0,
                "y": 0,
                "w": card_w,
                "h": card_h,
                "blur": max(8, round(0.02 * short)),
                "dy": max(4, round(0.012 * short)),
                "alpha": 0.3,
                "radius": rad,
                "fx_only": True,
            },
            {
                "k": "rect",
                "x": 0,
                "y": 0,
                "w": card_w,
                "h": card_h,
                "fill": ivory,
                "outline": (214, 210, 200),
                "stroke_w": 1,
                "radius": rad,
                "fx_only": True,
            },
            {"k": "rect", "x": side - 1, "y": top - 1, "w": w + 2, "h": h + 2, "fill": None, "outline": (222, 218, 208), "stroke_w": 1},
        ]
        over: list[dict[str, Any]] = []
        cy = top + h + 0.46 * chin
        if text:
            px = max(round(0.15 * chin), sz.p2("patrick"))
            px = max(14, min(px, 99))
            cap = t.clip_text(text, "patrick", px, w - 2 * side, 0.01)
            over.append(t.cap_text(card_w / 2.0, cy - (0.1 * chin if show_date else 0), px, "patrick", cap, ink, 0.01, "c"))
        if show_date and meta.get("date"):
            dpx = max(round(0.105 * chin), sz.p2("patrick"))
            if text:
                over.append(t.cap_text(card_w - side, top + h + 0.8 * chin, dpx, "patrick", meta["date"], (96, 94, 90), 0.02, "r"))
            else:
                over.append(t.cap_text(card_w / 2.0, cy, dpx, "patrick", meta["date"], ink, 0.02, "c"))
        return make_unit(card_w, card_h, (side, top), w, h, under=under, over=over, bg=ivory, table=stone)

    return solve(unit, ratio, w, natural=(0, 0, 0), f_side=0.06, f_top=0.06, f_bot=0.06, split=0.47)
