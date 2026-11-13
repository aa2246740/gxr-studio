//! Port of `gxr_studio/finish/frame_styles.py`: gallery, leica, hasselblad.

use gxr_raw::CoreError;

use crate::Meta;
use crate::frame_core::{
    Anchor, LOGO_INK_Y, LOGO_SIZE, Solve, Type, Unit, color, join, make_unit, meta_get, solve,
};
use crate::item::{Num, int, obj, rgb, text};
use crate::layout::{FrameLayout, LogoKw};
use crate::logo::{LogoInfo, clogo_item, fit_logo};
use crate::metrics::Fonts;
use crate::py::{f, round};
use crate::spec::FrameSpec;

/// `_palette(color)`.
pub struct Palette {
    pub bg: [i64; 3],
    pub ink: [i64; 3],
    pub muted: [i64; 3],
    pub rule: [i64; 3],
}

/// Port of gxr_studio/finish/frame_styles.py:15 _palette
pub fn palette(name: &str) -> Palette {
    let dark = name == "black";
    let cream = name == "cream";
    Palette {
        bg: color(name).unwrap_or([255, 255, 255]),
        ink: if dark { [244, 244, 246] } else { [22, 22, 26] },
        muted: if dark {
            [160, 160, 168]
        } else if cream {
            [112, 106, 96]
        } else {
            [112, 112, 120]
        },
        rule: if dark { [82, 82, 90] } else { [204, 202, 196] },
    }
}

/// Port of gxr_studio/finish/frame_styles.py:26 _bg_lum
pub fn bg_lum(name: &str) -> f64 {
    if name == "black" { 18.0 } else { 245.0 }
}

/// `GALLERY_WEIGHTS`.
fn gallery_weight(weight: &str) -> Result<(f64, f64, f64), CoreError> {
    match weight {
        "thin" => Ok((0.035, 1.2, 0.06)),
        "medium" => Ok((0.06, 1.25, 0.09)),
        "thick" => Ok((0.1, 1.28, 0.13)),
        _ => Err(CoreError::new("key_error", format!("'{weight}'"))),
    }
}

/// Port of gxr_studio/finish/frame_styles.py:33 gallery
pub fn gallery(
    w: i64,
    h: i64,
    ratio: Option<(i64, i64)>,
    color_name: &str,
    weight: &str,
) -> Result<FrameLayout, CoreError> {
    let short = w.min(h);
    let (r_side, bot_mult, fr) = gallery_weight(weight)?;
    let margin = 14.max(round(r_side * f(short))?);
    let mb = 18.max(round(f(margin) * bot_mult)?);
    let bg = color(color_name)?;
    let unit = move |_wref: i64| -> Result<Unit, CoreError> {
        Ok(make_unit(w, h, (0, 0), w, h, vec![], vec![], bg))
    };
    let s = Solve {
        natural: (margin, margin, mb),
        f_side: fr,
        f_top: fr,
        f_bot: fr,
        split: 0.45,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_styles.py:48 _leica_left
#[allow(clippy::too_many_arguments)]
fn leica_left(
    t: &Type,
    w: i64,
    type_bar: i64,
    gxr_px: i64,
    gxr_cap: i64,
    kind: &str,
    custom: Option<&LogoInfo>,
    logo_size: &str,
    model: bool,
) -> Result<(i64, i64, &'static str, i64, f64), CoreError> {
    let (lw, lh) = match (kind, custom) {
        ("custom", Some(c)) => fit_logo(
            c,
            gxr_cap,
            round(0.60 * f(type_bar))?,
            round(0.34 * f(w))?,
            logo_size,
        )?,
        _ => {
            let lh = 14.max(round(f(gxr_cap) * 1.04 * 433.0 / 412.0)?);
            (round(f(LOGO_SIZE.0 * lh) / f(LOGO_SIZE.1))?, lh)
        }
    };
    let model_txt = if model { "GXR" } else { "" };
    let gap_logo = 10.max(round(0.55 * f(gxr_cap))?);
    let mw = if model_txt.is_empty() {
        0.0
    } else {
        t.advance("inter_sb", gxr_px, model_txt, 0.02)?
    };
    // `lw + (gap_logo + mw if model_txt else 0)`: an int when there is no model text.
    let left_w = if kind == "none" {
        0.0
    } else if model_txt.is_empty() {
        f(lw)
    } else {
        f(lw) + (f(gap_logo) + mw)
    };
    Ok((lw, lh, model_txt, gap_logo, left_w))
}

/// Port of gxr_studio/finish/frame_styles.py:71 leica
#[allow(clippy::too_many_arguments)]
pub fn leica(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    spec: &FrameSpec,
    show_logo: bool,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let pal = palette(&spec.color);
    let m0 = 18.max(round(0.038 * f(short))?);
    let base_bar = 84.max(round(0.125 * f(short))?).max(round(0.075 * f(w))?);
    let kind: &str = if !show_logo {
        "none"
    } else if kw.kind == "custom" && kw.custom.is_some() {
        "custom"
    } else {
        kw.kind
    };
    let (show_params, show_date, caption, model) = (
        spec.show_params,
        spec.show_date,
        spec.text.as_str(),
        spec.model,
    );
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let type_bar = base_bar.max(round(0.082 * f(wref))?);
        let px1 = sz.p1("inter_sb")?.max(round(0.17 * f(base_bar))?);
        let px2 = sz.p2("inter")?.max(round(0.13 * f(base_bar))?);
        let gxr_px = sz.cap("inter_sb", 0.0155)?.max(round(0.16 * f(base_bar))?);
        let (cap1, cap2) = (t.cap_h("inter_sb", px1)?, t.cap_h("inter", px2)?);
        let gap_y = 7.max(round(0.72 * f(cap2))?);
        let mut l1 = if show_params {
            join(
                &["lens_short", "aperture", "shutter", "iso"].map(|k| meta_get(meta, k)),
                "  ",
            )
        } else {
            String::new()
        };
        let l2_main = if show_date {
            meta_get(meta, "date")
        } else if show_params && meta.get("lens") != meta.get("lens_short") {
            meta_get(meta, "lens")
        } else {
            ""
        };
        let mut l2 = join(&[l2_main, caption], "  ·  ");
        if l1.is_empty() && !l2.is_empty() {
            l1 = std::mem::take(&mut l2);
        }
        let gxr_cap = t.cap_h("inter_sb", gxr_px)?;
        let (lw, lh, model_txt, gap_logo, left_w) = leica_left(
            &t, w, type_bar, gxr_px, gxr_cap, kind, kw.custom, kw.size, model,
        )?;
        let rule_gap = 12.max(round(0.9 * f(cap1))?);
        let widths = |a: &str, b: &str| -> Result<f64, CoreError> {
            let first = t.advance("inter_sb", px1, a, 0.01)?;
            let second = if b.is_empty() {
                0.0
            } else {
                t.advance("inter", px2, b, 0.02)?
            };
            Ok(if second > first { second } else { first })
        };
        let avail = f(w) - left_w - f(rule_gap) - f(round(1.2 * f(cap1))?) - 8.0;
        let lens_short = meta_get(meta, "lens_short");
        if !l1.is_empty()
            && widths(&l1, &l2)? > avail
            && !lens_short.is_empty()
            && show_params
            && l1.starts_with(lens_short)
        {
            l1 = join(
                &["aperture", "shutter", "iso"].map(|k| meta_get(meta, k)),
                "  ",
            );
            l2 = join(&[lens_short, &l2], "  ·  ");
        }
        let stack_h = if l2.is_empty() {
            cap1
        } else {
            cap1 + gap_y + cap2
        };
        let need = if l1.is_empty() {
            left_w
        } else {
            left_w + f(rule_gap) + f(round(widths(&l1, &l2)?)?) + f(round(1.2 * f(cap1))?) + 8.0
        };
        let uw = w.max(round(need)?);
        let logo_bar = if kind != "none" {
            round(f(lh) * 1.9)?
        } else {
            0
        };
        let bar = type_bar
            .max(round(f(stack_h) * 2.3)?)
            .max(round(f(gxr_cap) * 3.2)?)
            .max(logo_bar);
        let y_mid = f(h) + 0.49 * f(bar);
        let mut items = Vec::new();
        if kind != "none" {
            match (kind, kw.custom) {
                ("custom", Some(c)) => {
                    items.push(clogo_item(
                        c,
                        0,
                        round(y_mid - f(lh) / 2.0)?,
                        lw,
                        lh,
                        kw.mode,
                        pal.ink,
                        bg_lum(&spec.color),
                    ));
                }
                _ => {
                    let logo_mid = f(lh * (LOGO_INK_Y.0 + LOGO_INK_Y.1)) / (2.0 * f(LOGO_SIZE.1));
                    items.push(obj(vec![
                        ("k", text("logo")),
                        ("x", int(0)),
                        ("y", int(round(y_mid - logo_mid)?)),
                        ("w", int(lw)),
                        ("h", int(lh)),
                    ]));
                }
            }
            if !model_txt.is_empty() {
                items.push(t.cap_text(
                    Num::I(lw + gap_logo),
                    y_mid,
                    gxr_px,
                    "inter_sb",
                    model_txt,
                    pal.ink,
                    0.02,
                    Anchor::L,
                )?);
            }
        }
        if !l1.is_empty() {
            let wtxt = widths(&l1, &l2)?;
            let x_text = f(uw) - wtxt;
            let top = y_mid - f(stack_h) / 2.0;
            items.push(t.base_text(
                Num::F(x_text),
                top + f(cap1),
                px1,
                "inter_sb",
                &l1,
                pal.ink,
                0.01,
                Anchor::L,
            )?);
            if !l2.is_empty() {
                items.push(t.base_text(
                    Num::F(x_text),
                    top + f(cap1) + f(gap_y) + f(cap2),
                    px2,
                    "inter",
                    &l2,
                    pal.muted,
                    0.02,
                    Anchor::L,
                )?);
            }
            let rx = round(x_text - f(rule_gap))?;
            if f(rx) > left_w + 8.0 {
                items.push(obj(vec![
                    ("k", text("line")),
                    ("x1", int(rx)),
                    ("y1", int(round(top)?)),
                    ("x2", int(rx)),
                    ("y2", int(round(top + f(stack_h))?)),
                    ("w", int(1.max(round(0.016 * f(type_bar))?))),
                    ("fill", rgb(pal.rule)),
                ]));
            }
        }
        Ok(make_unit(
            uw,
            h + bar,
            ((uw - w).div_euclid(2), 0),
            w,
            h,
            vec![],
            items,
            pal.bg,
        ))
    };
    let s = Solve {
        natural: (m0, m0, 0),
        f_side: 0.06,
        f_top: 0.06,
        f_bot: 0.04,
        split: 0.46,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_styles.py:161 hasselblad
#[allow(clippy::too_many_arguments)]
pub fn hasselblad(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    spec: &FrameSpec,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let pal = palette(&spec.color);
    let accent = if spec.color == "black" {
        [202, 154, 88]
    } else {
        [188, 138, 72]
    };
    let m0 = 18.max(round(0.036 * f(short))?);
    let base_bar = 96.max(round(0.145 * f(short))?).max(round(0.085 * f(w))?);
    let kind = match (kw.kind, kw.custom) {
        ("custom", Some(_)) => "custom",
        ("none", _) => "none",
        _ => "ricoh",
    };
    let (show_params, show_date, caption) = (spec.show_params, spec.show_date, spec.text.as_str());
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let type_bar = base_bar.max(round(0.095 * f(wref))?);
        let px1 = sz.p1("inter_sb")?.max(round(0.15 * f(base_bar))?);
        let px2 = sz.p2("inter")?.max(round(0.115 * f(base_bar))?);
        let (cap1, cap2) = (t.cap_h("inter_sb", px1)?, t.cap_h("inter", px2)?);
        let mut bits: Vec<&str> = Vec::new();
        if show_params {
            bits.extend(["lens_short", "aperture", "shutter", "iso"].map(|k| meta_get(meta, k)));
        }
        let tail = [
            if show_date {
                meta_get(meta, "date")
            } else {
                ""
            },
            caption,
        ];
        let all: Vec<&str> = bits.iter().copied().chain(tail).collect();
        let one = join(&all, "  ·  ");
        let mut lines: Vec<String> = if one.is_empty() {
            vec![]
        } else {
            vec![one.clone()]
        };
        if !one.is_empty() && t.advance("inter", px2, &one, 0.03)? > f(w - round(0.08 * f(short))?)
        {
            lines = [join(&bits, "  ·  "), join(&tail, "  ·  ")]
                .into_iter()
                .filter(|x| !x.is_empty())
                .collect();
        }
        let g = 8.max(round(0.7 * f(cap1))?);
        let rule_h = 2.max(round(0.012 * f(type_bar))?);
        let lgap = 6.max(round(0.55 * f(cap2))?);
        let n = i64::try_from(lines.len())
            .map_err(|_| CoreError::new("overflow_error", "too many lines"))?;
        let sub_h = n * cap2 + 0.max(n - 1) * lgap;
        let tail_h = if lines.is_empty() {
            0
        } else {
            g + rule_h + g + sub_h
        };
        let (lw, hdr_h, hdr_w, stack) = match (kind, kw.custom) {
            ("custom", Some(c)) => {
                let (lw, hdr_h) = fit_logo(
                    c,
                    cap1,
                    round(0.50 * f(type_bar))?,
                    round(0.42 * f(w))?,
                    kw.size,
                )?;
                (lw, hdr_h, f(lw), hdr_h + tail_h)
            }
            ("none", _) => (0, 0, 0.0, sub_h),
            _ => (
                0,
                cap1,
                t.advance("inter_sb", px1, "RICOH GXR", 0.26)?,
                cap1 + tail_h,
            ),
        };
        let bar = type_bar.max(round(f(stack) * 2.1)?);
        let top = f(h) + 0.47 * f(bar) - f(stack) / 2.0;
        let mut widest = f64::NEG_INFINITY;
        for ln in &lines {
            widest = widest.max(t.advance("inter", px2, ln, 0.03)?);
        }
        widest = widest.max(hdr_w);
        let uw = w.max(round(widest)? + round(0.08 * f(short))?);
        let cx = f(uw) / 2.0;
        let mut items = Vec::new();
        match (kind, kw.custom) {
            ("custom", Some(c)) => {
                items.push(clogo_item(
                    c,
                    round(cx - f(lw) / 2.0)?,
                    round(top)?,
                    lw,
                    hdr_h,
                    kw.mode,
                    pal.ink,
                    bg_lum(&spec.color),
                ));
            }
            ("ricoh", _) => {
                items.push(t.base_text(
                    Num::F(cx),
                    top + f(cap1),
                    px1,
                    "inter_sb",
                    "RICOH GXR",
                    pal.ink,
                    0.26,
                    Anchor::C,
                )?);
            }
            _ => {}
        }
        if !lines.is_empty() {
            let mut y = if kind != "none" {
                let rw = 28.max(round(0.28 * f(type_bar))?);
                let ry = round(top + f(hdr_h) + f(g))?;
                items.push(obj(vec![
                    ("k", text("line")),
                    ("x1", int(round(cx - f(rw) / 2.0)?)),
                    ("y1", int(ry)),
                    ("x2", int(round(cx + f(rw) / 2.0)?)),
                    ("y2", int(ry)),
                    ("w", int(rule_h)),
                    ("fill", rgb(accent)),
                ]));
                top + f(hdr_h) + f(g) + f(rule_h) + f(g)
            } else {
                top
            };
            for ln in &lines {
                items.push(t.base_text(
                    Num::F(cx),
                    y + f(cap2),
                    px2,
                    "inter",
                    ln,
                    pal.muted,
                    0.03,
                    Anchor::C,
                )?);
                // `y += cap2 + lgap`: the int sum first.
                y += f(cap2 + lgap);
            }
        }
        Ok(make_unit(
            uw,
            h + bar,
            ((uw - w).div_euclid(2), 0),
            w,
            h,
            vec![],
            items,
            pal.bg,
        ))
    };
    let s = Solve {
        natural: (m0, m0, 0),
        f_side: 0.06,
        f_top: 0.06,
        f_bot: 0.04,
        split: 0.46,
    };
    solve(&unit, ratio, w, &s)
}
