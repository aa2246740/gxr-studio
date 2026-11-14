//! Port of `gxr_studio/finish/frame_objects.py`: the film strip and the instant print.

use gxr_raw::CoreError;

use crate::Meta;
use crate::frame_core::{
    Anchor, Solve, Type, Unit, frame_index, join, make_unit, meta_get, rotate_unit_ccw, solve,
};
use crate::item::{Num, float, int, ints, obj, rgb, set, str_of, text};
use crate::layout::FrameLayout;
use crate::metrics::Fonts;
use crate::py::{ceil, f, round};
use crate::value::Val;

/// Port of gxr_studio/finish/frame_objects.py:15 _tri
fn tri(x: f64, y_mid: f64, size: i64, fill: [i64; 3]) -> Result<Val, CoreError> {
    let half = f(size) / 2.0;
    let pts = Val::Arr(vec![
        ints(&[round(x)?, round(y_mid - half)?]),
        ints(&[round(x)?, round(y_mid + half)?]),
        ints(&[round(x + f(size) * 0.9)?, round(y_mid)?]),
    ]);
    Ok(obj(vec![
        ("k", text("poly")),
        ("pts", pts),
        ("fill", rgb(fill)),
    ]))
}

fn zero_division() -> CoreError {
    CoreError::new("zero_division_error", "division by zero")
}

/// Port of gxr_studio/finish/frame_objects.py:23 _strip
pub fn strip(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    kind: &str,
    table: &str,
    m: &Fonts,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let portrait = h > w;
    let (a, c) = if portrait { (h, w) } else { (w, h) };
    let short = c;
    let light = table != "black";
    let table_col = if light { [243, 241, 236] } else { [24, 24, 26] };
    let strip_col = [13, 12, 12];
    let amber = if kind == "135" {
        [214, 140, 56]
    } else {
        [210, 148, 66]
    };
    let n = frame_index(meta);
    let s = f(short);

    let unit = |_wref: i64| -> Result<Unit, CoreError> {
        let (rebate, e, hw, hh, pitch, hy, mark_mid, px);
        if kind == "135" {
            rebate = round(0.229 * s)?;
            e = round(0.1 * s)?;
            hw = round(0.117 * s)?;
            hh = round(0.0825 * s)?;
            pitch = round(0.198 * s)?;
            hy = round(0.083 * s - f(hh) / 2.0)?;
            mark_mid = f(hy + hh + rebate) / 2.0;
            px = t.px_for_cap("inter_sb", 0.026 * s)?;
        } else {
            e = round(0.07 * s)?;
            px = t.px_for_cap("inter_sb", 0.017 * s)?;
            rebate = round(0.065 * s)?.max(round(3.2 * f(t.cap_h("inter_sb", px)?))?);
            mark_mid = f(rebate) / 2.0;
            (hw, hh, pitch, hy) = (0, 0, 0, 0);
        }
        let (strip_w, strip_h) = (a + 2 * e, c + 2 * rebate);
        let radius = round(0.006 * s)?;
        let mut under = vec![
            obj(vec![
                ("k", text("shadow")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(strip_w)),
                ("h", int(strip_h)),
                ("blur", int(6.max(round(0.024 * s)?))),
                ("dy", int(round(0.012 * s)?)),
                ("alpha", float(if light { 0.4 } else { 0.0 })),
                ("radius", int(radius)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(strip_w)),
                ("h", int(strip_h)),
                ("fill", rgb(strip_col)),
                ("radius", int(radius)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(1)),
                ("y", int(1)),
                ("w", int(strip_w - 2)),
                ("h", int(strip_h - 2)),
                ("fill", Val::Null),
                ("outline", rgb([46, 42, 38])),
                ("stroke_w", int(1)),
                ("radius", int(radius)),
            ]),
        ];
        if kind == "135" {
            if pitch == 0 {
                return Err(zero_division());
            }
            let cnt = (strip_w - 2 * round(0.03 * s)?).div_euclid(pitch);
            let x0 = (strip_w - ((cnt - 1) * pitch + hw)).div_euclid(2);
            let hole_r = round(0.2 * f(hh))?;
            for i in 0..cnt.max(0) {
                for yy in [hy, strip_h - hy - hh] {
                    if light {
                        under.push(obj(vec![
                            ("k", text("hole")),
                            ("x", int(x0 + i * pitch)),
                            ("y", int(yy)),
                            ("w", int(hw)),
                            ("h", int(hh)),
                            ("radius", int(hole_r)),
                        ]));
                    } else {
                        under.push(obj(vec![
                            ("k", text("rect")),
                            ("x", int(x0 + i * pitch)),
                            ("y", int(yy)),
                            ("w", int(hw)),
                            ("h", int(hh)),
                            ("fill", rgb([232, 228, 218])),
                            ("radius", int(hole_r)),
                        ]));
                    }
                }
            }
        } else {
            let edge = round(0.012 * s)?;
            let inset = round(0.03 * s)?;
            for yy in [edge, strip_h - edge] {
                under.push(obj(vec![
                    ("k", text("line")),
                    ("x1", int(inset)),
                    ("y1", int(yy)),
                    ("x2", int(strip_w - inset)),
                    ("y2", int(yy)),
                    ("w", int(1)),
                    ("fill", rgb([40, 37, 34])),
                ]));
            }
        }
        let mut over = vec![obj(vec![
            ("k", text("rect")),
            ("x", int(e - 1)),
            ("y", int(rebate - 1)),
            ("w", int(a + 2)),
            ("h", int(c + 2)),
            ("fill", Val::Null),
            ("outline", rgb([30, 28, 26])),
            ("stroke_w", int(1)),
        ])];
        let side = if kind == "135" { round(0.05 * s)? } else { e };
        let (xl, xr) = (side, strip_w - side);
        let tri_size = 8.max(round(0.62 * f(t.cap_h("inter_sb", px)?))?);
        let (top_mid, bot_mid) = (mark_mid, f(strip_h) - mark_mid);
        let top_l = if kind == "135" {
            "RICOH GXR 135"
        } else {
            "RICOH GXR 120"
        };
        over.push(t.cap_text(
            Num::I(xl),
            top_mid,
            px,
            "inter_sb",
            top_l,
            amber,
            0.08,
            Anchor::L,
        )?);
        let label = if kind == "135" {
            n.to_string()
        } else {
            ((n - 1).rem_euclid(12) + 1).to_string()
        };
        let label_a = format!("{label}A");
        if kind == "135" {
            let cx = f(strip_w) * 0.52;
            over.push(tri(cx - f(tri_size) * 1.4, top_mid, tri_size, amber)?);
            over.push(t.cap_text(
                Num::F(cx),
                top_mid,
                px,
                "inter_sb",
                &label,
                amber,
                0.06,
                Anchor::L,
            )?);
            over.push(t.cap_text(
                Num::I(xr),
                top_mid,
                px,
                "inter_sb",
                &label_a,
                amber,
                0.06,
                Anchor::R,
            )?);
            let left = join(&["RICOH GXR", meta_get(meta, "lens_short")], "  ·  ");
            over.push(t.cap_text(
                Num::I(xl),
                bot_mid,
                px,
                "inter_sb",
                &left,
                amber,
                0.06,
                Anchor::L,
            )?);
            over.push(tri(cx - f(tri_size) * 1.4, bot_mid, tri_size, amber)?);
            over.push(t.cap_text(
                Num::F(cx),
                bot_mid,
                px,
                "inter_sb",
                &label_a,
                amber,
                0.06,
                Anchor::L,
            )?);
            over.push(t.cap_text(
                Num::I(xr),
                bot_mid,
                px,
                "inter_sb",
                meta_get(meta, "date"),
                amber,
                0.06,
                Anchor::R,
            )?);
        } else {
            let tx = f(xr) - t.advance("inter_sb", px, &label, 0.06)? - f(tri_size) * 1.5;
            over.push(tri(tx, top_mid, tri_size, amber)?);
            over.push(t.cap_text(
                Num::I(xr),
                top_mid,
                px,
                "inter_sb",
                &label,
                amber,
                0.06,
                Anchor::R,
            )?);
            let spec = join(
                &["lens_short", "aperture", "shutter", "iso"].map(|k| meta_get(meta, k)),
                "   ",
            );
            over.push(t.cap_text(
                Num::I(xl),
                bot_mid,
                px,
                "inter_sb",
                &spec,
                amber,
                0.05,
                Anchor::L,
            )?);
            over.push(t.cap_text(
                Num::I(xr),
                bot_mid,
                px,
                "inter_sb",
                meta_get(meta, "date"),
                amber,
                0.05,
                Anchor::R,
            )?);
        }
        if portrait {
            for item in over.iter_mut() {
                if str_of(item, "k")? == "text" {
                    let track = crate::item::num(item, "track")?;
                    let tw = ceil(t.advance_n(
                        str_of(item, "font")?,
                        crate::item::int_of(item, "px")?,
                        str_of(item, "text")?,
                        track,
                    )?)? + 2;
                    set(item, "tw", int(tw));
                }
            }
        }
        let mut u = make_unit(strip_w, strip_h, (e, rebate), a, c, under, over, table_col);
        u.bgfx = Some(obj(vec![
            ("kind", text("glow")),
            ("falloff", float(if light { 0.07 } else { 0.0 })),
        ]));
        u.bgfx_always = true;
        if portrait { rotate_unit_ccw(&u) } else { Ok(u) }
    };
    let margin = round(0.07 * s)?;
    let sv = Solve {
        natural: (margin, margin, margin),
        f_side: 0.06,
        f_top: 0.06,
        f_bot: 0.06,
        split: 0.5,
    };
    solve(&unit, ratio, w, &sv)
}

/// `INSTANT_FORMATS`.
fn instant_format(fmt: &str) -> Result<(f64, f64, f64), CoreError> {
    match fmt {
        "polaroid" => Ok((0.057, 0.062, 0.29)),
        "instax_square" => Ok((0.081, 0.081, 0.306)),
        "instax_wide" => Ok((0.073, 0.065, 0.32)),
        "instax_mini" => Ok((0.087, 0.087, 0.43)),
        _ => Err(CoreError::new("key_error", format!("'{fmt}'"))),
    }
}

/// Port of gxr_studio/finish/frame_objects.py:142 instant
#[allow(clippy::too_many_arguments)]
pub fn instant(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    caption: &str,
    show_date: bool,
    fmt: &str,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let (rs, rt, rc) = instant_format(fmt)?;
    let (side, top) = (22.max(round(rs * f(short))?), 22.max(round(rt * f(short))?));
    let chin = (if fmt == "polaroid" {
        4 * side
    } else {
        3 * side
    })
    .max(round(rc * f(short))?);
    let (ivory, stone, ink) = ([250, 248, 243], [232, 228, 220], [43, 43, 43]);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let (card_w, card_h) = (w + 2 * side, h + top + chin);
        let rad = round(0.023 * f(card_w))?;
        let under = vec![
            obj(vec![
                ("k", text("shadow")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(card_w)),
                ("h", int(card_h)),
                ("blur", int(8.max(round(0.02 * f(short))?))),
                ("dy", int(4.max(round(0.012 * f(short))?))),
                ("alpha", float(0.3)),
                ("radius", int(rad)),
                ("fx_only", Val::Bool(true)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(card_w)),
                ("h", int(card_h)),
                ("fill", rgb(ivory)),
                ("outline", rgb([214, 210, 200])),
                ("stroke_w", int(1)),
                ("radius", int(rad)),
                ("fx_only", Val::Bool(true)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(side - 1)),
                ("y", int(top - 1)),
                ("w", int(w + 2)),
                ("h", int(h + 2)),
                ("fill", Val::Null),
                ("outline", rgb([222, 218, 208])),
                ("stroke_w", int(1)),
            ]),
        ];
        let mut over = Vec::new();
        let cy = f(top + h) + 0.46 * f(chin);
        let date = meta_get(meta, "date");
        if !caption.is_empty() {
            let px = round(0.15 * f(chin))?.max(sz.p2("patrick")?);
            let px = 14.max(px.min(99));
            let cap = t.clip_text(caption, "patrick", px, f(w - 2 * side), 0.01)?;
            let lift = if show_date { 0.1 * f(chin) } else { 0.0 };
            over.push(t.cap_text(
                Num::F(f(card_w) / 2.0),
                cy - lift,
                px,
                "patrick",
                &cap,
                ink,
                0.01,
                Anchor::C,
            )?);
        }
        if show_date && !date.is_empty() {
            let dpx = round(0.105 * f(chin))?.max(sz.p2("patrick")?);
            if !caption.is_empty() {
                let y = f(top + h) + 0.8 * f(chin);
                over.push(t.cap_text(
                    Num::I(card_w - side),
                    y,
                    dpx,
                    "patrick",
                    date,
                    [96, 94, 90],
                    0.02,
                    Anchor::R,
                )?);
            } else {
                over.push(t.cap_text(
                    Num::F(f(card_w) / 2.0),
                    cy,
                    dpx,
                    "patrick",
                    date,
                    ink,
                    0.02,
                    Anchor::C,
                )?);
            }
        }
        let mut u = make_unit(card_w, card_h, (side, top), w, h, under, over, ivory);
        u.table = Some(stone);
        Ok(u)
    };
    let sv = Solve {
        natural: (0, 0, 0),
        f_side: 0.06,
        f_top: 0.06,
        f_bot: 0.06,
        split: 0.47,
    };
    solve(&unit, ratio, w, &sv)
}
