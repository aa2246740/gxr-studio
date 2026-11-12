"""Seven approved R3 styles on the R4 frame solver. JavaScript mirrors the equations."""

from __future__ import annotations

import math
import re
from typing import Any

from .frame_core import Type, frame_index, join, make_unit, solve
from .frame_styles import _palette
from .layout import Metrics
from .logo_math import LogoInfo, clogo_item, fit_logo

Meta = dict[str, str]


def _kind(logo_kind: str, custom: LogoInfo | None) -> str:
    return "custom" if logo_kind == "custom" and custom is not None else ("none" if logo_kind == "none" else "ricoh")


def thin_keyline(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    color: str = "cream",
    logo_kind: str = "ricoh",
    custom: LogoInfo | None = None,
    logo_mode: str = "auto",
    logo_size: str = "medium",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    pal = _palette(color)
    line_col = (90, 90, 96) if color == "black" else (52, 50, 46)
    g = max(10, round(0.017 * short))
    stroke = max(2, round(0.0024 * short))
    ms = max(36, round(0.08 * short))
    kind = _kind(logo_kind, custom)

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        px1 = max(sz.p1("inter_sb"), round(0.016 * short))
        px2 = max(sz.p2("inter"), round(0.015 * short))
        cap1 = t.cap_h("inter_sb", px1)
        gap_c = max(12, round(1.1 * cap1))
        uw, kh = (w + 2 * g, h + 2 * g)
        items: list[dict[str, Any]] = [{"k": "rect", "x": 0, "y": 0, "w": uw, "h": kh, "fill": None, "outline": line_col, "stroke_w": stroke}]
        if kind == "custom" and custom is not None:
            lw, lh = fit_logo(custom, cap1, round(3.4 * cap1), round(0.30 * uw), logo_size)
            row_h = max(cap1, lh)
            y_mid = kh + gap_c + row_h / 2.0
            bg_l = 18.0 if color == "black" else (238.0 if color == "cream" else 245.0)
            items.append(clogo_item(custom, 0, round(y_mid - lh / 2), lw, lh, logo_mode, pal["ink"], bg_l))
            brand_w: float = lw
            base_y: float = y_mid + t.cap_h("inter", px2) / 2.0
        elif kind == "none":
            row_h = cap1
            base_y = kh + gap_c + cap1
            brand_w = 0.0
        else:
            row_h = cap1
            base_y = kh + gap_c + cap1
            items.append(t.base_text(0, base_y, px1, "inter_sb", "RICOH GXR", pal["ink"], 0.12))
            brand_w = t.advance("inter_sb", px1, "RICOH GXR", 0.12)
        right = join([meta.get(k, "") for k in ("lens_short", "aperture", "shutter", "iso", "date")], "  ·  ")
        pad_left = brand_w + 40 if brand_w else 0
        while right and t.advance("inter", px2, right, 0.02) + pad_left > uw:
            right = "  ·  ".join(right.split("  ·  ")[:-1]) if "  ·  " in right else ""
        if right:
            items.append(t.base_text(uw, base_y, px2, "inter", right, pal["muted"], 0.02, "r"))
        return make_unit(uw, kh + gap_c + row_h + round(0.4 * cap1), (g, g), w, h, over=items, bg=pal["bg"])

    return solve(unit, ratio, w, natural=(ms, ms, round(0.07 * short)), f_side=0.07, f_top=0.07, f_bot=0.07, split=0.44)


def exhibition_label(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    color: str = "white",
    text: str = "",
    logo_kind: str = "ricoh",
    custom: LogoInfo | None = None,
    logo_mode: str = "auto",
    logo_size: str = "medium",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    pal = _palette(color)
    dark = color == "black"
    card_bg = (30, 30, 34) if dark else (251, 250, 247)
    card_line = (62, 62, 70) if dark else (220, 216, 208)
    ms = max(40, round(0.085 * short))
    kind = _kind(logo_kind, custom)

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        px1 = max(sz.cap("inter_sb", 0.017), round(0.022 * short))
        px2 = max(sz.cap("inter", 0.013), round(0.018 * short))
        cap1, cap2 = (t.cap_h("inter_sb", px1), t.cap_h("inter", px2))
        ricoh = kind == "ricoh"
        head = text or ("RICOH GXR" if ricoh else "")
        details = (["RICOH GXR"] if (ricoh and text) else []) + [
            join([meta.get("lens_short", ""), meta.get("aperture", "")]),
            join([meta.get("shutter", ""), meta.get("iso", "")]),
            meta.get("date", ""),
        ]
        details = [d for d in details if d]
        pad_x, pad_y = (round(1.3 * px2), round(1.1 * px2))
        lgap = round(0.6 * cap2)
        want = max([t.advance("inter_sb", px1, head, 0.02) if head else 0, *(t.advance("inter", px2, d, 0.02) for d in details)])
        card_w = min(round(0.52 * w), max(round(0.26 * w), math.ceil(want) + 2 * pad_x + 2))
        head = t.clip_text(head, "inter_sb", px1, card_w - 2 * pad_x, 0.02) if head else ""
        lw, lh = fit_logo(custom, cap1, round(3.2 * cap1), card_w - 2 * pad_x, logo_size) if kind == "custom" and custom is not None else (0, 0)
        head_h = (lh + round(0.7 * cap1) if kind == "custom" else 0) + (cap1 + round(0.9 * cap1) if head else 0)
        detail_gaps = (len(details) - 1) * lgap if ricoh else max(0, len(details) - 1) * lgap
        card_h = 2 * pad_y + head_h + len(details) * cap2 + detail_gaps + round(0.3 * cap2)
        gap_c = max(26, round(0.055 * short))
        cx, cy = (w - card_w, h + gap_c)
        items: list[dict[str, Any]] = [
            {
                "k": "shadow",
                "x": cx,
                "y": cy,
                "w": card_w,
                "h": card_h,
                "blur": max(4, round(0.006 * short)),
                "dy": max(2, round(0.004 * short)),
                "alpha": 0.2 if not dark else 0.5,
            },
            {"k": "rect", "x": cx, "y": cy, "w": card_w, "h": card_h, "fill": card_bg, "outline": card_line, "stroke_w": 1},
        ]
        y = cy + pad_y
        if kind == "custom" and custom is not None:
            items.append(clogo_item(custom, cx + pad_x, y, lw, lh, logo_mode, pal["ink"], 22.0 if dark else 250.0))
            y += lh + round(0.7 * cap1)
        if head:
            items.append(t.base_text(cx + pad_x, y + cap1, px1, "inter_sb", head, pal["ink"], 0.02))
            y += cap1 + round(0.9 * cap1)
        for d in details:
            items.append(t.base_text(cx + pad_x, y + cap2, px2, "inter", d, pal["muted"], 0.02))
            y += cap2 + lgap
        return make_unit(w, h + gap_c + card_h, (0, 0), w, h, over=items, bg=pal["bg"])

    return solve(unit, ratio, w, natural=(ms, ms, ms), f_side=0.07, f_top=0.07, f_bot=0.05, split=0.4)


def blur_fill(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    show_params: bool = True,
    logo_kind: str = "ricoh",
    custom: LogoInfo | None = None,
    logo_mode: str = "auto",
    logo_size: str = "medium",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    m0 = max(28, round(0.065 * short))
    white, soft = ((248, 248, 250), (226, 228, 232))
    kind = _kind(logo_kind, custom)

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        under = [
            {
                "k": "shadow",
                "x": 0,
                "y": 0,
                "w": w,
                "h": h,
                "blur": max(16, round(0.045 * short)),
                "dy": max(8, round(0.022 * short)),
                "alpha": 0.55,
            }
        ]
        over: list[dict[str, Any]] = []
        total = h
        if show_params or kind == "custom":
            px1, px2 = (max(sz.p1("inter_sb"), round(0.02 * short)), max(sz.p2("inter"), round(0.017 * short)))
            cap1, cap2 = (t.cap_h("inter_sb", px1), t.cap_h("inter", px2))
            sub = join([meta.get(k, "") for k in ("lens_short", "aperture", "shutter", "iso")], "  ·  ")
            gap0, g = (max(22, round(1.9 * cap1)), max(8, round(0.8 * cap2)))
            y = h + gap0
            if kind == "custom" and custom is not None:
                lw, hdr_h = fit_logo(custom, cap1, round(3.4 * cap1), round(0.30 * w), logo_size)
                it = clogo_item(custom, round(w / 2.0 - lw / 2), round(y), lw, hdr_h, logo_mode, white, 128.0)
                it["auto_ink"] = [list(white), [28, 28, 32]]
                over.append(it)
                if show_params:
                    sub_txt = t.base_text(w / 2.0, y + hdr_h + g + cap2, px2, "inter", sub, soft, 0.04, "c")
                    over.append({**sub_txt, "auto_ink": [soft, (52, 52, 58)]})
                    total = y + hdr_h + g + cap2
                else:
                    total = y + hdr_h
            elif kind == "ricoh":
                hdr_h = cap1
                hdr_txt = t.base_text(w / 2.0, y + cap1, px1, "inter_sb", "RICOH GXR", white, 0.18, "c")
                sub_txt = t.base_text(w / 2.0, y + hdr_h + g + cap2, px2, "inter", sub, soft, 0.04, "c")
                over.append({**hdr_txt, "auto_ink": [white, (28, 28, 32)]})
                over.append({**sub_txt, "auto_ink": [soft, (52, 52, 58)]})
                total = y + hdr_h + g + cap2
            else:
                sub_txt = t.base_text(w / 2.0, y + cap2, px2, "inter", sub, soft, 0.04, "c")
                over.append({**sub_txt, "auto_ink": [soft, (52, 52, 58)]})
                total = y + cap2
        result = make_unit(
            w,
            total,
            (0, 0),
            w,
            h,
            under=under,
            over=over,
            bg=(24, 24, 28),
            bgfx={"kind": "blur", "sigma": max(90, round(0.16 * max(w, h))), "veil": 0.9, "sat": 1.15},
        )
        result["bgfx_always"] = True
        return result

    return solve(unit, ratio, w, natural=(m0, m0, round(0.065 * short)), f_side=0.06, f_top=0.06, f_bot=0.06, split=0.45)


def cinema(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    text: str = "",
    logo_kind: str = "ricoh",
    custom: LogoInfo | None = None,
    logo_mode: str = "auto",
    logo_size: str = "medium",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    black, yellow, dim = ((8, 8, 10), (244, 198, 68), (150, 152, 160))
    gate = max(20, round(0.03 * short))
    kind = _kind(logo_kind, custom)

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        px_m = max(sz.p2("inter_sb"), round(0.015 * short))
        px_s = max(sz.p1("inter_sb"), round(0.022 * short))
        px_e = max(sz.cap("courier", 0.011 if text else 0.015), round(0.016 * short))
        cap_m, cap_s, cap_e = (t.cap_h("inter_sb", px_m), t.cap_h("inter_sb", px_s), t.cap_h("courier", px_e))
        uw = w + 2 * gate
        bt = max(round(0.1 * short), round(3.4 * cap_m))
        lw, lh = fit_logo(custom, cap_m, round(0.70 * bt), round(0.30 * w), logo_size) if kind == "custom" and custom is not None else (0, 0)
        bb = max(bt, round(0.15 * short), round(1.3 * cap_s + 1.4 * cap_e + 2.2 * cap_s)) if text else bt
        items: list[dict[str, Any]] = [
            {"k": "rect", "x": gate - 1, "y": bt - 1, "w": w + 2, "h": h + 2, "fill": None, "outline": (38, 40, 46), "stroke_w": 1}
        ]
        ym = bt / 2.0
        if kind == "custom" and custom is not None:
            items.append(clogo_item(custom, gate, round(ym - lh / 2), lw, lh, logo_mode, (236, 236, 240), 8.0))
            if meta.get("lens_short"):
                items.append(t.cap_text(gate + lw + round(1.4 * cap_m), ym, px_m, "inter_sb", meta["lens_short"], dim, 0.16))
        elif kind == "ricoh":
            items.append(t.cap_text(gate, ym, px_m, "inter_sb", join(["RICOH GXR", meta.get("lens_short", "")], "  ·  "), dim, 0.16))
        elif meta.get("lens_short"):
            items.append(t.cap_text(gate, ym, px_m, "inter_sb", meta["lens_short"], dim, 0.16))
        if meta.get("date"):
            items.append(t.cap_text(gate + w, ym, px_m, "inter_sb", meta["date"], dim, 0.1, "r"))
        exp = join([meta.get(k, "") for k in ("aperture", "shutter", "iso")], "   ·   ")
        y0 = bt + h
        if text:
            sub = t.clip_text(text, "inter_sb", px_s, w - 2 * gate, 0.02)
            items.append(t.cap_text(uw / 2.0, y0 + bb * 0.36, px_s, "inter_sb", sub, yellow, 0.02, "c"))
            items.append(t.cap_text(uw / 2.0, y0 + bb * 0.74, px_e, "courier", exp, dim, 0.08, "c"))
        else:
            items.append(t.cap_text(uw / 2.0, y0 + bb / 2.0, px_e, "courier", exp, dim, 0.08, "c"))
        return make_unit(uw, bt + h + bb, (gate, bt), w, h, over=items, bg=black)

    return solve(unit, ratio, w, natural=(0, 0, 0), f_side=0.05, f_top=0.05, f_bot=0.03, split=0.48)


def slide_mount(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    color: str = "cream",
    logo_kind: str = "ricoh",
    custom: LogoInfo | None = None,
    logo_mode: str = "auto",
    logo_size: str = "medium",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    dark = color == "black"
    mount = (30, 31, 35) if dark else (242, 238, 229)
    ink = (232, 230, 224) if dark else (38, 36, 34)
    red = (224, 62, 52) if dark else (198, 44, 38)
    rule = (64, 66, 72) if dark else (206, 200, 188)
    table = (226, 222, 214) if dark else (22, 22, 26)
    kind = _kind(logo_kind, custom)

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        px1, px2 = (max(sz.p1("inter_sb"), round(0.017 * short)), max(sz.p2("courier"), round(0.016 * short)))
        cap1 = t.cap_h("inter_sb", px1)
        ry = max(round(0.145 * short), round(3.6 * cap1))
        rx = max(round(0.115 * short), round(0.11 * w))
        w0, h0 = (w + 2 * rx, h + 2 * ry)
        bz = max(6, round(0.012 * short))
        rad = max(8, round(0.018 * short))
        under = [
            {
                "k": "shadow",
                "x": 0,
                "y": 0,
                "w": w0,
                "h": h0,
                "blur": max(8, round(0.022 * short)),
                "dy": max(4, round(0.012 * short)),
                "alpha": 0.45 if not dark else 0.3,
                "radius": rad,
                "fx_only": True,
            },
            {"k": "rect", "x": 0, "y": 0, "w": w0, "h": h0, "fill": mount, "outline": rule, "stroke_w": 1, "radius": rad, "fx_only": True},
            {
                "k": "rect",
                "x": rx - bz,
                "y": ry - bz,
                "w": w + 2 * bz,
                "h": h + 2 * bz,
                "fill": (18, 19, 22) if dark else (222, 216, 204),
                "outline": rule,
                "stroke_w": 2,
                "radius": max(4, round(0.01 * short)),
            },
            {"k": "line", "x1": rx, "y1": round(ry * 0.22), "x2": w0 - rx, "y2": round(ry * 0.22), "w": 1, "fill": rule},
            {"k": "line", "x1": rx, "y1": h0 - round(ry * 0.22), "x2": w0 - rx, "y2": h0 - round(ry * 0.22), "w": 1, "fill": rule},
        ]
        ym_t, ym_b = (ry * 0.6, h0 - ry * 0.58)
        over: list[dict[str, Any]] = [
            t.cap_text(rx, ym_t, px1, "inter_sb", f"{frame_index(meta)}", red, 0.06),
            t.cap_text(rx, ym_b, px2, "courier", meta.get("date", ""), red, 0.06),
        ]
        if kind == "custom" and custom is not None:
            lw, lh = fit_logo(custom, cap1, round(0.60 * ry), round(0.40 * w), logo_size)
            over.append(clogo_item(custom, round(w0 / 2.0 - lw / 2), round(ym_t - lh / 2), lw, lh, logo_mode, ink, 30.0 if dark else 240.0))
        elif kind == "ricoh":
            over.append(t.cap_text(w0 / 2.0, ym_t, px1, "inter_sb", join(["RICOH GXR", meta.get("lens_short", "")], "  ·  "), ink, 0.14, "c"))
        spec = join([meta.get(k, "") for k in ("aperture", "shutter", "iso")], "  ")
        while spec and t.advance("courier", px2, spec, 0.03) + t.advance("courier", px2, meta.get("date", ""), 0.06) + 40 > w:
            spec = "  ".join(spec.split("  ")[:-1])
        if spec:
            over.append(t.cap_text(w0 - rx, ym_b, px2, "courier", spec, ink, 0.03, "r"))
        return make_unit(w0, h0, (rx, ry), w, h, under=under, over=over, bg=mount, table=table)

    return solve(unit, ratio, w, natural=(0, 0, 0), f_side=0.06, f_top=0.06, f_bot=0.06, split=0.48)


def darkroom(w: int, h: int, meta: Meta, ratio: tuple[int, int] | None, m: Metrics, *, text: str = "") -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    paper, black, pencil = ((246, 242, 233), (14, 14, 15), (104, 102, 98))
    c = max(10, round(0.017 * short))
    jit = max(4, round(0.5 * c))
    b = c + jit + 2
    ms = max(38, round(0.082 * short))

    def unit(wref: int) -> dict[str, Any]:
        under = [{"k": "carrier", "x": b, "y": b, "w": w, "h": h, "c": c, "jit": jit, "seed": 19790324, "fill": black, "paper": paper}]
        sz = t.sizes(wref)
        px1, px2 = (max(sz.cap("patrick", 0.019), round(0.032 * short)), max(sz.cap("patrick", 0.016), round(0.028 * short)))
        note_y = b + h + b + max(10, round(0.014 * short))
        capn = t.cap_h("patrick", px1)
        left = text or meta.get("date", "")
        right = join([meta.get("lens_short", ""), meta.get("aperture", ""), meta.get("shutter", "")], "  ")
        over = []
        if left:
            over.append(t.base_text(b, note_y + capn, px1, "patrick", left, pencil, 0.01))
        if right and t.advance("patrick", px2, right, 0.01) + t.advance("patrick", px1, left, 0.01) + 40 < w:
            over.append(t.base_text(b + w, note_y + capn, px2, "patrick", right, pencil, 0.01, "r"))
        uw, uh = (w + 2 * b, note_y + capn + round(0.35 * capn))
        return make_unit(uw, uh, (b, b), w, h, under=under, over=over, bg=paper, grain=0.016)

    return solve(
        unit,
        ratio,
        w,
        natural=(ms - b if ms > b else 0, ms - b if ms > b else 0, round(0.05 * short)),
        f_side=0.07,
        f_top=0.07,
        f_bot=0.06,
        split=0.44,
    )


def postage_stamp(
    w: int,
    h: int,
    meta: Meta,
    ratio: tuple[int, int] | None,
    m: Metrics,
    *,
    logo_kind: str = "ricoh",
    custom: LogoInfo | None = None,
    logo_mode: str = "auto",
    logo_size: str = "medium",
) -> dict[str, Any]:
    t = Type(m)
    short = min(w, h)
    stock, paper, ink, muted, eng = ((22, 23, 27), (250, 247, 238), (32, 32, 36), (96, 92, 86), (78, 74, 68))
    pad = max(32, round(0.068 * short))
    hr, pitch = (max(6, round(0.013 * short)), max(18, round(0.042 * short)))
    eg = max(6, round(0.011 * short))
    focal = re.search(r"(\d+(?:\.\d+)?)\s*mm", meta.get("lens_short", ""))
    inscription = join(["RICOH GXR", focal[1] if focal else ""], "  ·  ")
    kind = _kind(logo_kind, custom)

    def unit(wref: int) -> dict[str, Any]:
        sz = t.sizes(wref)
        px1, px2 = (max(sz.p1("inter_sb"), round(0.018 * short)), max(sz.p2("inter"), round(0.016 * short)))
        cap1 = t.cap_h("inter_sb", px1)
        lw, lh = fit_logo(custom, cap1, round(3.2 * cap1), round(0.30 * w), logo_size) if kind == "custom" and custom is not None else (0, 0)
        pad_bot = max(round(0.105 * short), round(3.4 * cap1) + eg, lh + 3 * eg + hr)
        sw, sh = (w + 2 * pad, h + pad + pad_bot)
        under: list[dict[str, Any]] = [
            {"k": "rect", "x": 0, "y": 0, "w": sw, "h": sh, "fill": paper},
            {"k": "rect", "x": pad - eg, "y": pad - eg, "w": w + 2 * eg, "h": h + 2 * eg, "fill": None, "outline": eng, "stroke_w": 1},
        ]
        nx, ny = (max(4, round(sw / pitch)), max(4, round(sh / pitch)))
        for i in range(nx):
            cx = (i + 0.5) * sw / nx
            for cy in (0, sh):
                under.append({"k": "ellipse", "cx": round(cx), "cy": cy, "r": hr, "fill": stock})
        for j in range(ny):
            center_y = (j + 0.5) * sh / ny
            for cx in (0, sw):
                under.append({"k": "ellipse", "cx": cx, "cy": round(center_y), "r": hr, "fill": stock})
        yb = pad + h + eg + (pad_bot - eg - hr) * 0.52 + cap1 / 2
        if kind == "custom" and custom is not None:
            over: list[dict[str, Any]] = [clogo_item(custom, pad, round(yb - cap1 / 2 - lh / 2), lw, lh, logo_mode, ink, 250.0)]
            left_w: float = lw
        elif kind == "none":
            over = []
            left_w = 0.0
        else:
            over = [t.base_text(pad, yb, px1, "inter_sb", inscription, ink, 0.1)]
            left_w = t.advance("inter_sb", px1, inscription, 0.1)
        right = join([meta.get(k, "") for k in ("lens_short", "aperture", "shutter", "date")], "  ·  ")
        pad_left = left_w + 30 if left_w else 0
        while right and t.advance("inter", px2, right, 0.02) + pad_left > w:
            right = "  ·  ".join(right.split("  ·  ")[:-1]) if "  ·  " in right else ""
        if right:
            over.append(t.base_text(pad + w, yb, px2, "inter", right, muted, 0.02, "r"))
        return make_unit(sw, sh, (pad, pad), w, h, under=under, over=over, bg=stock)

    margin = max(34, round(0.072 * short))
    return solve(unit, ratio, w, natural=(margin, margin, margin), f_side=0.07, f_top=0.07, f_bot=0.07, split=0.47)
