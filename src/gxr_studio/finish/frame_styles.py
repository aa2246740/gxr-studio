"""Approved R3 frame geometry. Keep the matching finish_frame_*.js equations equal."""

from __future__ import annotations

from typing import Any

from .frame_core import COLORS, LOGO_INK_Y, LOGO_SIZE, Type, join, make_unit, solve
from .layout import Metrics

Meta = dict[str, str]
RGB = tuple[int, int, int]


def _palette(color: str) -> dict[str, tuple[int, int, int]]:
    dark = color == "black"
    cream = color == "cream"
    return {
        "bg": COLORS.get(color, COLORS["white"]),
        "ink": (244, 244, 246) if dark else (22, 22, 26),
        "muted": (160, 160, 168) if dark else (112, 106, 96) if cream else (112, 112, 120),
        "rule": (82, 82, 90) if dark else (204, 202, 196),
    }


GALLERY_WEIGHTS = {"thin": (0.035, 1.2, 0.06), "medium": (0.06, 1.25, 0.09), "thick": (0.1, 1.28, 0.13)}


def gallery(
    w: int, h: int, meta: Meta, ratio: tuple[int, int] | None, m: Metrics, *, color: str = "white", weight: str = "medium"
) -> dict[str, Any]:
    short = min(w, h)
    r_side, bot_mult, f = GALLERY_WEIGHTS[weight]
    margin = max(14, round(r_side * short))
    mb = max(18, round(margin * bot_mult))
    bg = COLORS[color]

    def unit(_wref: int) -> dict[str, Any]:
        return make_unit(w, h, (0, 0), w, h, bg=bg)

    return solve(unit, ratio, w, natural=(margin, margin, mb), f_side=f, f_top=f, f_bot=f, split=0.45)


def leica(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    color: str = "white",
    show_logo: bool = True,
    show_params: bool = True,
    show_date: bool = True,
    text: str = "",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    pal = _palette(color)
    m0 = max(18, round(0.038 * short))
    base_bar = max(84, round(0.125 * short), round(0.075 * w))

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        type_bar = max(base_bar, round(0.082 * wref))
        px1 = max(sz.p1("inter_sb"), round(0.17 * base_bar))
        px2 = max(sz.p2("inter"), round(0.13 * base_bar))
        gxr_px = max(sz.cap("inter_sb", 0.0155), round(0.16 * base_bar))
        cap1, cap2 = (t.cap_h("inter_sb", px1), t.cap_h("inter", px2))
        gap_y = max(7, round(0.72 * cap2))
        l1 = join([meta.get(k, "") for k in ("lens_short", "aperture", "shutter", "iso")]) if show_params else ""
        l2_main = (
            meta.get("date", "") if show_date else meta.get("lens", "") if show_params and meta.get("lens") != meta.get("lens_short") else ""
        )
        l2 = join([l2_main, text], "  ·  ")
        if not l1 and l2:
            l1, l2 = (l2, "")
        gxr_cap = t.cap_h("inter_sb", gxr_px)
        lh = max(14, round(gxr_cap * 1.04 * 433.0 / 412.0))
        lw = round(LOGO_SIZE[0] * lh / LOGO_SIZE[1])
        model_txt = "GXR"
        gap_logo = max(10, round(0.55 * gxr_cap))
        mw = t.advance("inter_sb", gxr_px, model_txt, 0.02) if model_txt else 0
        left_w = lw + (gap_logo + mw if model_txt else 0) if show_logo else 0
        rule_gap = max(12, round(0.9 * cap1))

        def widths(a: str, b: str) -> float:
            return max(t.advance("inter_sb", px1, a, 0.01), t.advance("inter", px2, b, 0.02) if b else 0)

        avail = w - left_w - rule_gap - round(1.2 * cap1) - 8
        if l1 and widths(l1, l2) > avail and meta.get("lens_short") and show_params and l1.startswith(meta["lens_short"]):
            l1 = join([meta.get(k, "") for k in ("aperture", "shutter", "iso")])
            l2 = join([meta["lens_short"], l2], "  ·  ")
        stack_h = cap1 + gap_y + cap2 if l2 else cap1
        need = left_w + rule_gap + round(widths(l1, l2)) + round(1.2 * cap1) + 8 if l1 else left_w
        uw = max(w, round(need))
        bar = max(type_bar, round(stack_h * 2.3), round(gxr_cap * 3.2), round(lh * 1.9) if show_logo else 0)
        y_mid = h + 0.49 * bar
        items: list[dict[str, Any]] = []
        if show_logo:
            logo_mid = lh * (LOGO_INK_Y[0] + LOGO_INK_Y[1]) / (2.0 * LOGO_SIZE[1])
            items.append({"k": "logo", "x": 0, "y": round(y_mid - logo_mid), "w": lw, "h": lh})
            if model_txt:
                items.append(t.cap_text(lw + gap_logo, y_mid, gxr_px, "inter_sb", model_txt, pal["ink"], 0.02))
        if l1:
            wtxt = widths(l1, l2)
            x_text = uw - wtxt
            top = y_mid - stack_h / 2.0
            items.append(t.base_text(x_text, top + cap1, px1, "inter_sb", l1, pal["ink"], 0.01))
            if l2:
                items.append(t.base_text(x_text, top + cap1 + gap_y + cap2, px2, "inter", l2, pal["muted"], 0.02))
            rx = round(x_text - rule_gap)
            if rx > left_w + 8:
                items.append(
                    {
                        "k": "line",
                        "x1": rx,
                        "y1": round(top),
                        "x2": rx,
                        "y2": round(top + stack_h),
                        "w": max(1, round(0.016 * type_bar)),
                        "fill": pal["rule"],
                    }
                )
        return make_unit(uw, h + bar, ((uw - w) // 2, 0), w, h, over=items, bg=pal["bg"])

    return solve(unit, ratio, w, natural=(m0, m0, 0), f_side=0.06, f_top=0.06, f_bot=0.04, split=0.46)


def hasselblad(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    color: str = "white",
    show_params: bool = True,
    show_date: bool = True,
    text: str = "",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    pal = _palette(color)
    accent = (202, 154, 88) if color == "black" else (188, 138, 72)
    m0 = max(18, round(0.036 * short))
    base_bar = max(96, round(0.145 * short), round(0.085 * w))

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        type_bar = max(base_bar, round(0.095 * wref))
        px1 = max(sz.p1("inter_sb"), round(0.15 * base_bar))
        px2 = max(sz.p2("inter"), round(0.115 * base_bar))
        cap1, cap2 = (t.cap_h("inter_sb", px1), t.cap_h("inter", px2))
        bits = [meta.get(k, "") for k in ("lens_short", "aperture", "shutter", "iso")] if show_params else []
        tail = [meta.get("date", "") if show_date else "", text]
        one = join(bits + tail, "  ·  ")
        lines = [one] if one else []
        if one and t.advance("inter", px2, one, 0.03) > w - round(0.08 * short):
            lines = [x for x in (join(bits, "  ·  "), join(tail, "  ·  ")) if x]
        g = max(8, round(0.7 * cap1))
        rule_h = max(2, round(0.012 * type_bar))
        lgap = max(6, round(0.55 * cap2))
        sub_h = len(lines) * cap2 + max(0, len(lines) - 1) * lgap
        hdr_h = cap1
        hdr_w = t.advance("inter_sb", px1, "RICOH GXR", 0.26)
        stack = hdr_h + (g + rule_h + g + sub_h if lines else 0)
        bar = max(type_bar, round(stack * 2.1))
        top = h + 0.47 * bar - stack / 2.0
        widest = max([t.advance("inter", px2, ln, 0.03) for ln in lines] + [hdr_w])
        uw = max(w, round(widest) + round(0.08 * short))
        cx = uw / 2.0
        items = [t.base_text(cx, top + cap1, px1, "inter_sb", "RICOH GXR", pal["ink"], 0.26, "c")]
        if lines:
            rw = max(28, round(0.28 * type_bar))
            ry = round(top + hdr_h + g)
            items.append({"k": "line", "x1": round(cx - rw / 2), "y1": ry, "x2": round(cx + rw / 2), "y2": ry, "w": rule_h, "fill": accent})
            y = top + hdr_h + g + rule_h + g
            for ln in lines:
                items.append(t.base_text(cx, y + cap2, px2, "inter", ln, pal["muted"], 0.03, "c"))
                y += cap2 + lgap
        return make_unit(uw, h + bar, ((uw - w) // 2, 0), w, h, over=items, bg=pal["bg"])

    return solve(unit, ratio, w, natural=(m0, m0, 0), f_side=0.06, f_top=0.06, f_bot=0.04, split=0.46)
