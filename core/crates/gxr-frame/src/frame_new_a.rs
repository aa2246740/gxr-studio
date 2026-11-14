//! Port of `gxr_studio/finish/frame_new.py`, first half: thin_keyline, exhibition_label,
//! blur_fill (and `_kind`).

use gxr_raw::CoreError;

use crate::Meta;
use crate::frame_core::{Anchor, Solve, Type, Unit, join, make_unit, meta_get, solve};
use crate::frame_styles::palette;
use crate::item::{Num, float, int, obj, rgb, set, text, with};
use crate::layout::{FrameLayout, LogoKw};
use crate::logo::{clogo_item, fit_logo};
use crate::metrics::Fonts;
use crate::py::{ceil, f, round};
use crate::value::Val;

/// Port of gxr_studio/finish/frame_new.py:17 _kind
pub fn kind_of(kw: &LogoKw) -> &'static str {
    match (kw.kind, kw.custom) {
        ("custom", Some(_)) => "custom",
        ("none", _) => "none",
        _ => "ricoh",
    }
}

/// `"  ·  ".join(right.split("  ·  ")[:-1]) if "  ·  " in right else ""`.
pub fn drop_last(right: &str, sep: &str) -> String {
    if !right.contains(sep) {
        return String::new();
    }
    let parts: Vec<&str> = right.split(sep).collect();
    parts[..parts.len() - 1].join(sep)
}

fn pair(a: [i64; 3], b: [i64; 3]) -> Val {
    Val::Arr(vec![rgb(a), rgb(b)])
}

/// Port of gxr_studio/finish/frame_new.py:21 thin_keyline
pub fn thin_keyline(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    color: &str,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let pal = palette(color);
    let line_col = if color == "black" {
        [90, 90, 96]
    } else {
        [52, 50, 46]
    };
    let g = 10.max(round(0.017 * f(short))?);
    let stroke = 2.max(round(0.0024 * f(short))?);
    let ms = 36.max(round(0.08 * f(short))?);
    let kind = kind_of(&kw);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let px1 = sz.p1("inter_sb")?.max(round(0.016 * f(short))?);
        let px2 = sz.p2("inter")?.max(round(0.015 * f(short))?);
        let cap1 = t.cap_h("inter_sb", px1)?;
        let gap_c = 12.max(round(1.1 * f(cap1))?);
        let (uw, kh) = (w + 2 * g, h + 2 * g);
        let mut items = vec![obj(vec![
            ("k", text("rect")),
            ("x", int(0)),
            ("y", int(0)),
            ("w", int(uw)),
            ("h", int(kh)),
            ("fill", Val::Null),
            ("outline", rgb(line_col)),
            ("stroke_w", int(stroke)),
        ])];
        let (row_h, base_y, brand_w): (i64, f64, f64) = match (kind, kw.custom) {
            ("custom", Some(c)) => {
                let (lw, lh) = fit_logo(
                    c,
                    cap1,
                    round(3.4 * f(cap1))?,
                    round(0.30 * f(uw))?,
                    kw.size,
                )?;
                let row_h = cap1.max(lh);
                let y_mid = f(kh + gap_c) + f(row_h) / 2.0;
                let bg_l = if color == "black" {
                    18.0
                } else if color == "cream" {
                    238.0
                } else {
                    245.0
                };
                items.push(clogo_item(
                    c,
                    0,
                    round(y_mid - f(lh) / 2.0)?,
                    lw,
                    lh,
                    kw.mode,
                    pal.ink,
                    bg_l,
                ));
                (row_h, y_mid + f(t.cap_h("inter", px2)?) / 2.0, f(lw))
            }
            ("none", _) => (cap1, f(kh + gap_c + cap1), 0.0),
            _ => {
                let base_y = kh + gap_c + cap1;
                items.push(t.base_text(
                    Num::I(0),
                    f(base_y),
                    px1,
                    "inter_sb",
                    "RICOH GXR",
                    pal.ink,
                    0.12,
                    Anchor::L,
                )?);
                (
                    cap1,
                    f(base_y),
                    t.advance("inter_sb", px1, "RICOH GXR", 0.12)?,
                )
            }
        };
        let mut right = join(
            &["lens_short", "aperture", "shutter", "iso", "date"].map(|k| meta_get(meta, k)),
            "  ·  ",
        );
        let pad_left = if brand_w != 0.0 { brand_w + 40.0 } else { 0.0 };
        while !right.is_empty() && t.advance("inter", px2, &right, 0.02)? + pad_left > f(uw) {
            right = drop_last(&right, "  ·  ");
        }
        if !right.is_empty() {
            items.push(t.base_text(
                Num::I(uw),
                base_y,
                px2,
                "inter",
                &right,
                pal.muted,
                0.02,
                Anchor::R,
            )?);
        }
        Ok(make_unit(
            uw,
            kh + gap_c + row_h + round(0.4 * f(cap1))?,
            (g, g),
            w,
            h,
            vec![],
            items,
            pal.bg,
        ))
    };
    let s = Solve {
        natural: (ms, ms, round(0.07 * f(short))?),
        f_side: 0.07,
        f_top: 0.07,
        f_bot: 0.07,
        split: 0.44,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_new.py:79 exhibition_label
#[allow(clippy::too_many_arguments)]
pub fn exhibition_label(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    color: &str,
    caption: &str,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let pal = palette(color);
    let dark = color == "black";
    let card_bg = if dark { [30, 30, 34] } else { [251, 250, 247] };
    let card_line = if dark { [62, 62, 70] } else { [220, 216, 208] };
    let ms = 40.max(round(0.085 * f(short))?);
    let kind = kind_of(&kw);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let px1 = sz.cap("inter_sb", 0.017)?.max(round(0.022 * f(short))?);
        let px2 = sz.cap("inter", 0.013)?.max(round(0.018 * f(short))?);
        let (cap1, cap2) = (t.cap_h("inter_sb", px1)?, t.cap_h("inter", px2)?);
        let ricoh = kind == "ricoh";
        let mut head = if !caption.is_empty() {
            caption.to_string()
        } else if ricoh {
            "RICOH GXR".to_string()
        } else {
            String::new()
        };
        let mut details: Vec<String> = Vec::new();
        if ricoh && !caption.is_empty() {
            details.push("RICOH GXR".into());
        }
        details.push(join(
            &[meta_get(meta, "lens_short"), meta_get(meta, "aperture")],
            "  ",
        ));
        details.push(join(
            &[meta_get(meta, "shutter"), meta_get(meta, "iso")],
            "  ",
        ));
        details.push(meta_get(meta, "date").to_string());
        details.retain(|d| !d.is_empty());
        let (pad_x, pad_y) = (round(1.3 * f(px2))?, round(1.1 * f(px2))?);
        let lgap = round(0.6 * f(cap2))?;
        // max([advance(head) if head else 0, *advances]): first maximal value.
        let mut want = if head.is_empty() {
            0.0
        } else {
            t.advance("inter_sb", px1, &head, 0.02)?
        };
        for d in &details {
            let a = t.advance("inter", px2, d, 0.02)?;
            if a > want {
                want = a;
            }
        }
        let card_w = round(0.52 * f(w))?.min(round(0.26 * f(w))?.max(ceil(want)? + 2 * pad_x + 2));
        if !head.is_empty() {
            head = t.clip_text(&head, "inter_sb", px1, f(card_w - 2 * pad_x), 0.02)?;
        }
        let (lw, lh) = match (kind, kw.custom) {
            ("custom", Some(c)) => {
                fit_logo(c, cap1, round(3.2 * f(cap1))?, card_w - 2 * pad_x, kw.size)?
            }
            _ => (0, 0),
        };
        let custom_h = if kind == "custom" {
            lh + round(0.7 * f(cap1))?
        } else {
            0
        };
        let head_h = custom_h
            + if head.is_empty() {
                0
            } else {
                cap1 + round(0.9 * f(cap1))?
            };
        let nd = i64::try_from(details.len())
            .map_err(|_| CoreError::new("overflow_error", "too many lines"))?;
        let detail_gaps = if ricoh {
            (nd - 1) * lgap
        } else {
            0.max(nd - 1) * lgap
        };
        let card_h = 2 * pad_y + head_h + nd * cap2 + detail_gaps + round(0.3 * f(cap2))?;
        let gap_c = 26.max(round(0.055 * f(short))?);
        let (cx, cy) = (w - card_w, h + gap_c);
        let mut items = vec![
            obj(vec![
                ("k", text("shadow")),
                ("x", int(cx)),
                ("y", int(cy)),
                ("w", int(card_w)),
                ("h", int(card_h)),
                ("blur", int(4.max(round(0.006 * f(short))?))),
                ("dy", int(2.max(round(0.004 * f(short))?))),
                ("alpha", float(if dark { 0.5 } else { 0.2 })),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(cx)),
                ("y", int(cy)),
                ("w", int(card_w)),
                ("h", int(card_h)),
                ("fill", rgb(card_bg)),
                ("outline", rgb(card_line)),
                ("stroke_w", int(1)),
            ]),
        ];
        let mut y = cy + pad_y;
        if let ("custom", Some(c)) = (kind, kw.custom) {
            items.push(clogo_item(
                c,
                cx + pad_x,
                y,
                lw,
                lh,
                kw.mode,
                pal.ink,
                if dark { 22.0 } else { 250.0 },
            ));
            y += lh + round(0.7 * f(cap1))?;
        }
        if !head.is_empty() {
            items.push(t.base_text(
                Num::I(cx + pad_x),
                f(y + cap1),
                px1,
                "inter_sb",
                &head,
                pal.ink,
                0.02,
                Anchor::L,
            )?);
            y += cap1 + round(0.9 * f(cap1))?;
        }
        for d in &details {
            items.push(t.base_text(
                Num::I(cx + pad_x),
                f(y + cap2),
                px2,
                "inter",
                d,
                pal.muted,
                0.02,
                Anchor::L,
            )?);
            y += cap2 + lgap;
        }
        Ok(make_unit(
            w,
            h + gap_c + card_h,
            (0, 0),
            w,
            h,
            vec![],
            items,
            pal.bg,
        ))
    };
    let s = Solve {
        natural: (ms, ms, ms),
        f_side: 0.07,
        f_top: 0.07,
        f_bot: 0.05,
        split: 0.4,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_new.py:154 blur_fill
pub fn blur_fill(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    show_params: bool,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let m0 = 28.max(round(0.065 * f(short))?);
    let (white, soft) = ([248, 248, 250], [226, 228, 232]);
    let kind = kind_of(&kw);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let under = vec![obj(vec![
            ("k", text("shadow")),
            ("x", int(0)),
            ("y", int(0)),
            ("w", int(w)),
            ("h", int(h)),
            ("blur", int(16.max(round(0.045 * f(short))?))),
            ("dy", int(8.max(round(0.022 * f(short))?))),
            ("alpha", float(0.55)),
        ])];
        let mut over = Vec::new();
        let mut total = h;
        if show_params || kind == "custom" {
            let px1 = sz.p1("inter_sb")?.max(round(0.02 * f(short))?);
            let px2 = sz.p2("inter")?.max(round(0.017 * f(short))?);
            let (cap1, cap2) = (t.cap_h("inter_sb", px1)?, t.cap_h("inter", px2)?);
            let sub = join(
                &["lens_short", "aperture", "shutter", "iso"].map(|k| meta_get(meta, k)),
                "  ·  ",
            );
            let (gap0, g) = (22.max(round(1.9 * f(cap1))?), 8.max(round(0.8 * f(cap2))?));
            let y = h + gap0;
            let mid = Num::F(f(w) / 2.0);
            match (kind, kw.custom) {
                ("custom", Some(c)) => {
                    let (lw, hdr_h) =
                        fit_logo(c, cap1, round(3.4 * f(cap1))?, round(0.30 * f(w))?, kw.size)?;
                    let mut it = clogo_item(
                        c,
                        round(f(w) / 2.0 - f(lw) / 2.0)?,
                        y,
                        lw,
                        hdr_h,
                        kw.mode,
                        white,
                        128.0,
                    );
                    set(&mut it, "auto_ink", pair(white, [28, 28, 32]));
                    over.push(it);
                    if show_params {
                        let sub_txt = t.base_text(
                            mid,
                            f(y + hdr_h + g + cap2),
                            px2,
                            "inter",
                            &sub,
                            soft,
                            0.04,
                            Anchor::C,
                        )?;
                        over.push(with(&sub_txt, "auto_ink", pair(soft, [52, 52, 58])));
                        total = y + hdr_h + g + cap2;
                    } else {
                        total = y + hdr_h;
                    }
                }
                ("ricoh", _) => {
                    let hdr_h = cap1;
                    let hdr_txt = t.base_text(
                        mid,
                        f(y + cap1),
                        px1,
                        "inter_sb",
                        "RICOH GXR",
                        white,
                        0.18,
                        Anchor::C,
                    )?;
                    let sub_txt = t.base_text(
                        mid,
                        f(y + hdr_h + g + cap2),
                        px2,
                        "inter",
                        &sub,
                        soft,
                        0.04,
                        Anchor::C,
                    )?;
                    over.push(with(&hdr_txt, "auto_ink", pair(white, [28, 28, 32])));
                    over.push(with(&sub_txt, "auto_ink", pair(soft, [52, 52, 58])));
                    total = y + hdr_h + g + cap2;
                }
                _ => {
                    let sub_txt =
                        t.base_text(mid, f(y + cap2), px2, "inter", &sub, soft, 0.04, Anchor::C)?;
                    over.push(with(&sub_txt, "auto_ink", pair(soft, [52, 52, 58])));
                    total = y + cap2;
                }
            }
        }
        let mut u = make_unit(w, total, (0, 0), w, h, under, over, [24, 24, 28]);
        u.bgfx = Some(obj(vec![
            ("kind", text("blur")),
            ("sigma", int(90.max(round(0.16 * f(w.max(h)))?))),
            ("veil", float(0.9)),
            ("sat", float(1.15)),
        ]));
        u.bgfx_always = true;
        Ok(u)
    };
    let s = Solve {
        natural: (m0, m0, round(0.065 * f(short))?),
        f_side: 0.06,
        f_top: 0.06,
        f_bot: 0.06,
        split: 0.45,
    };
    solve(&unit, ratio, w, &s)
}
