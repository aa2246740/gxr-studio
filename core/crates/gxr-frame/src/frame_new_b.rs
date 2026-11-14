//! Port of `gxr_studio/finish/frame_new.py`, second half: cinema, slide_mount, darkroom,
//! postage_stamp.

use gxr_raw::CoreError;

use crate::Meta;
use crate::frame_core::{Anchor, Solve, Type, Unit, frame_index, join, make_unit, meta_get, solve};
use crate::frame_new_a::{drop_last, kind_of};
use crate::item::{Num, float, int, obj, rgb, text};
use crate::layout::{FrameLayout, LogoKw};
use crate::logo::{clogo_item, fit_logo};
use crate::metrics::Fonts;
use crate::py::{self, f, round};
use crate::value::Val;

/// Port of gxr_studio/finish/frame_new.py:234 cinema
pub fn cinema(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    caption: &str,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let (black, yellow, dim) = ([8, 8, 10], [244, 198, 68], [150, 152, 160]);
    let gate = 20.max(round(0.03 * f(short))?);
    let kind = kind_of(&kw);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let px_m = sz.p2("inter_sb")?.max(round(0.015 * f(short))?);
        let px_s = sz.p1("inter_sb")?.max(round(0.022 * f(short))?);
        let px_e = sz
            .cap("courier", if caption.is_empty() { 0.015 } else { 0.011 })?
            .max(round(0.016 * f(short))?);
        let (cap_m, cap_s, cap_e) = (
            t.cap_h("inter_sb", px_m)?,
            t.cap_h("inter_sb", px_s)?,
            t.cap_h("courier", px_e)?,
        );
        let uw = w + 2 * gate;
        let bt = round(0.1 * f(short))?.max(round(3.4 * f(cap_m))?);
        let (lw, lh) = match (kind, kw.custom) {
            ("custom", Some(c)) => {
                fit_logo(c, cap_m, round(0.70 * f(bt))?, round(0.30 * f(w))?, kw.size)?
            }
            _ => (0, 0),
        };
        let bb = if caption.is_empty() {
            bt
        } else {
            let mix = 1.3 * f(cap_s) + 1.4 * f(cap_e) + 2.2 * f(cap_s);
            bt.max(round(0.15 * f(short))?).max(round(mix)?)
        };
        let mut items = vec![obj(vec![
            ("k", text("rect")),
            ("x", int(gate - 1)),
            ("y", int(bt - 1)),
            ("w", int(w + 2)),
            ("h", int(h + 2)),
            ("fill", Val::Null),
            ("outline", rgb([38, 40, 46])),
            ("stroke_w", int(1)),
        ])];
        let ym = f(bt) / 2.0;
        let lens_short = meta_get(meta, "lens_short");
        match (kind, kw.custom) {
            ("custom", Some(c)) => {
                items.push(clogo_item(
                    c,
                    gate,
                    round(ym - f(lh) / 2.0)?,
                    lw,
                    lh,
                    kw.mode,
                    [236, 236, 240],
                    8.0,
                ));
                if !lens_short.is_empty() {
                    let x = gate + lw + round(1.4 * f(cap_m))?;
                    items.push(t.cap_text(
                        Num::I(x),
                        ym,
                        px_m,
                        "inter_sb",
                        lens_short,
                        dim,
                        0.16,
                        Anchor::L,
                    )?);
                }
            }
            ("ricoh", _) => {
                let label = join(&["RICOH GXR", lens_short], "  ·  ");
                items.push(t.cap_text(
                    Num::I(gate),
                    ym,
                    px_m,
                    "inter_sb",
                    &label,
                    dim,
                    0.16,
                    Anchor::L,
                )?);
            }
            _ => {
                if !lens_short.is_empty() {
                    items.push(t.cap_text(
                        Num::I(gate),
                        ym,
                        px_m,
                        "inter_sb",
                        lens_short,
                        dim,
                        0.16,
                        Anchor::L,
                    )?);
                }
            }
        }
        let date = meta_get(meta, "date");
        if !date.is_empty() {
            items.push(t.cap_text(
                Num::I(gate + w),
                ym,
                px_m,
                "inter_sb",
                date,
                dim,
                0.1,
                Anchor::R,
            )?);
        }
        let exp = join(
            &["aperture", "shutter", "iso"].map(|k| meta_get(meta, k)),
            "   ·   ",
        );
        let y0 = bt + h;
        let mid = Num::F(f(uw) / 2.0);
        if !caption.is_empty() {
            let sub = t.clip_text(caption, "inter_sb", px_s, f(w - 2 * gate), 0.02)?;
            items.push(t.cap_text(
                mid,
                f(y0) + f(bb) * 0.36,
                px_s,
                "inter_sb",
                &sub,
                yellow,
                0.02,
                Anchor::C,
            )?);
            items.push(t.cap_text(
                mid,
                f(y0) + f(bb) * 0.74,
                px_e,
                "courier",
                &exp,
                dim,
                0.08,
                Anchor::C,
            )?);
        } else {
            items.push(t.cap_text(
                mid,
                f(y0) + f(bb) / 2.0,
                px_e,
                "courier",
                &exp,
                dim,
                0.08,
                Anchor::C,
            )?);
        }
        Ok(make_unit(
            uw,
            bt + h + bb,
            (gate, bt),
            w,
            h,
            vec![],
            items,
            black,
        ))
    };
    let s = Solve {
        natural: (0, 0, 0),
        f_side: 0.05,
        f_top: 0.05,
        f_bot: 0.03,
        split: 0.48,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_new.py:290 slide_mount
pub fn slide_mount(
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
    let dark = color == "black";
    let mount = if dark { [30, 31, 35] } else { [242, 238, 229] };
    let ink = if dark { [232, 230, 224] } else { [38, 36, 34] };
    let red = if dark { [224, 62, 52] } else { [198, 44, 38] };
    let rule = if dark { [64, 66, 72] } else { [206, 200, 188] };
    let table = if dark { [226, 222, 214] } else { [22, 22, 26] };
    let kind = kind_of(&kw);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let px1 = sz.p1("inter_sb")?.max(round(0.017 * f(short))?);
        let px2 = sz.p2("courier")?.max(round(0.016 * f(short))?);
        let cap1 = t.cap_h("inter_sb", px1)?;
        let ry = round(0.145 * f(short))?.max(round(3.6 * f(cap1))?);
        let rx = round(0.115 * f(short))?.max(round(0.11 * f(w))?);
        let (w0, h0) = (w + 2 * rx, h + 2 * ry);
        let bz = 6.max(round(0.012 * f(short))?);
        let rad = 8.max(round(0.018 * f(short))?);
        let rule_y = round(f(ry) * 0.22)?;
        let under = vec![
            obj(vec![
                ("k", text("shadow")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(w0)),
                ("h", int(h0)),
                ("blur", int(8.max(round(0.022 * f(short))?))),
                ("dy", int(4.max(round(0.012 * f(short))?))),
                ("alpha", float(if dark { 0.3 } else { 0.45 })),
                ("radius", int(rad)),
                ("fx_only", Val::Bool(true)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(w0)),
                ("h", int(h0)),
                ("fill", rgb(mount)),
                ("outline", rgb(rule)),
                ("stroke_w", int(1)),
                ("radius", int(rad)),
                ("fx_only", Val::Bool(true)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(rx - bz)),
                ("y", int(ry - bz)),
                ("w", int(w + 2 * bz)),
                ("h", int(h + 2 * bz)),
                (
                    "fill",
                    rgb(if dark { [18, 19, 22] } else { [222, 216, 204] }),
                ),
                ("outline", rgb(rule)),
                ("stroke_w", int(2)),
                ("radius", int(4.max(round(0.01 * f(short))?))),
            ]),
            obj(vec![
                ("k", text("line")),
                ("x1", int(rx)),
                ("y1", int(rule_y)),
                ("x2", int(w0 - rx)),
                ("y2", int(rule_y)),
                ("w", int(1)),
                ("fill", rgb(rule)),
            ]),
            obj(vec![
                ("k", text("line")),
                ("x1", int(rx)),
                ("y1", int(h0 - rule_y)),
                ("x2", int(w0 - rx)),
                ("y2", int(h0 - rule_y)),
                ("w", int(1)),
                ("fill", rgb(rule)),
            ]),
        ];
        let (ym_t, ym_b) = (f(ry) * 0.6, f(h0) - f(ry) * 0.58);
        let date = meta_get(meta, "date");
        let mut over = vec![
            t.cap_text(
                Num::I(rx),
                ym_t,
                px1,
                "inter_sb",
                &frame_index(meta).to_string(),
                red,
                0.06,
                Anchor::L,
            )?,
            t.cap_text(Num::I(rx), ym_b, px2, "courier", date, red, 0.06, Anchor::L)?,
        ];
        match (kind, kw.custom) {
            ("custom", Some(c)) => {
                let (lw, lh) =
                    fit_logo(c, cap1, round(0.60 * f(ry))?, round(0.40 * f(w))?, kw.size)?;
                let x = round(f(w0) / 2.0 - f(lw) / 2.0)?;
                over.push(clogo_item(
                    c,
                    x,
                    round(ym_t - f(lh) / 2.0)?,
                    lw,
                    lh,
                    kw.mode,
                    ink,
                    if dark { 30.0 } else { 240.0 },
                ));
            }
            ("ricoh", _) => {
                let label = join(&["RICOH GXR", meta_get(meta, "lens_short")], "  ·  ");
                over.push(t.cap_text(
                    Num::F(f(w0) / 2.0),
                    ym_t,
                    px1,
                    "inter_sb",
                    &label,
                    ink,
                    0.14,
                    Anchor::C,
                )?);
            }
            _ => {}
        }
        let mut spec = join(
            &["aperture", "shutter", "iso"].map(|k| meta_get(meta, k)),
            "  ",
        );
        while !spec.is_empty()
            && t.advance("courier", px2, &spec, 0.03)?
                + t.advance("courier", px2, date, 0.06)?
                + 40.0
                > f(w)
        {
            let parts: Vec<&str> = spec.split("  ").collect();
            spec = parts[..parts.len() - 1].join("  ");
        }
        if !spec.is_empty() {
            over.push(t.cap_text(
                Num::I(w0 - rx),
                ym_b,
                px2,
                "courier",
                &spec,
                ink,
                0.03,
                Anchor::R,
            )?);
        }
        let mut u = make_unit(w0, h0, (rx, ry), w, h, under, over, mount);
        u.table = Some(table);
        Ok(u)
    };
    let s = Solve {
        natural: (0, 0, 0),
        f_side: 0.06,
        f_top: 0.06,
        f_bot: 0.06,
        split: 0.48,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_new.py:370 darkroom
pub fn darkroom(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    caption: &str,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let (paper, black, pencil) = ([246, 242, 233], [14, 14, 15], [104, 102, 98]);
    let c = 10.max(round(0.017 * f(short))?);
    let jit = 4.max(round(0.5 * f(c))?);
    let b = c + jit + 2;
    let ms = 38.max(round(0.082 * f(short))?);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let under = vec![obj(vec![
            ("k", text("carrier")),
            ("x", int(b)),
            ("y", int(b)),
            ("w", int(w)),
            ("h", int(h)),
            ("c", int(c)),
            ("jit", int(jit)),
            ("seed", int(19_790_324)),
            ("fill", rgb(black)),
            ("paper", rgb(paper)),
        ])];
        let sz = t.sizes(wref);
        let px1 = sz.cap("patrick", 0.019)?.max(round(0.032 * f(short))?);
        let px2 = sz.cap("patrick", 0.016)?.max(round(0.028 * f(short))?);
        let note_y = b + h + b + 10.max(round(0.014 * f(short))?);
        let capn = t.cap_h("patrick", px1)?;
        let left = if caption.is_empty() {
            meta_get(meta, "date")
        } else {
            caption
        };
        let right = join(
            &["lens_short", "aperture", "shutter"].map(|k| meta_get(meta, k)),
            "  ",
        );
        let mut over = Vec::new();
        if !left.is_empty() {
            over.push(t.base_text(
                Num::I(b),
                f(note_y + capn),
                px1,
                "patrick",
                left,
                pencil,
                0.01,
                Anchor::L,
            )?);
        }
        if !right.is_empty()
            && t.advance("patrick", px2, &right, 0.01)?
                + t.advance("patrick", px1, left, 0.01)?
                + 40.0
                < f(w)
        {
            over.push(t.base_text(
                Num::I(b + w),
                f(note_y + capn),
                px2,
                "patrick",
                &right,
                pencil,
                0.01,
                Anchor::R,
            )?);
        }
        let (uw, uh) = (w + 2 * b, note_y + capn + round(0.35 * f(capn))?);
        let mut u = make_unit(uw, uh, (b, b), w, h, under, over, paper);
        u.grain = 0.016;
        Ok(u)
    };
    let side = if ms > b { ms - b } else { 0 };
    let s = Solve {
        natural: (side, side, round(0.05 * f(short))?),
        f_side: 0.07,
        f_top: 0.07,
        f_bot: 0.06,
        split: 0.44,
    };
    solve(&unit, ratio, w, &s)
}

/// Port of gxr_studio/finish/frame_new.py:407 postage_stamp
pub fn postage_stamp(
    w: i64,
    h: i64,
    meta: &Meta,
    ratio: Option<(i64, i64)>,
    m: &Fonts,
    kw: LogoKw,
) -> Result<FrameLayout, CoreError> {
    let t = Type { m };
    let short = w.min(h);
    let (stock, paper, ink, muted, eng) = (
        [22, 23, 27],
        [250, 247, 238],
        [32, 32, 36],
        [96, 92, 86],
        [78, 74, 68],
    );
    let pad = 32.max(round(0.068 * f(short))?);
    let (hr, pitch) = (
        6.max(round(0.013 * f(short))?),
        18.max(round(0.042 * f(short))?),
    );
    let eg = 6.max(round(0.011 * f(short))?);
    let focal = py::focal_mm(meta_get(meta, "lens_short"));
    let inscription = join(&["RICOH GXR", focal.as_deref().unwrap_or("")], "  ·  ");
    let kind = kind_of(&kw);
    let unit = |wref: i64| -> Result<Unit, CoreError> {
        let sz = t.sizes(wref);
        let px1 = sz.p1("inter_sb")?.max(round(0.018 * f(short))?);
        let px2 = sz.p2("inter")?.max(round(0.016 * f(short))?);
        let cap1 = t.cap_h("inter_sb", px1)?;
        let (lw, lh) = match (kind, kw.custom) {
            ("custom", Some(c)) => {
                fit_logo(c, cap1, round(3.2 * f(cap1))?, round(0.30 * f(w))?, kw.size)?
            }
            _ => (0, 0),
        };
        let pad_bot = round(0.105 * f(short))?
            .max(round(3.4 * f(cap1))? + eg)
            .max(lh + 3 * eg + hr);
        let (sw, sh) = (w + 2 * pad, h + pad + pad_bot);
        let mut under = vec![
            obj(vec![
                ("k", text("rect")),
                ("x", int(0)),
                ("y", int(0)),
                ("w", int(sw)),
                ("h", int(sh)),
                ("fill", rgb(paper)),
            ]),
            obj(vec![
                ("k", text("rect")),
                ("x", int(pad - eg)),
                ("y", int(pad - eg)),
                ("w", int(w + 2 * eg)),
                ("h", int(h + 2 * eg)),
                ("fill", Val::Null),
                ("outline", rgb(eng)),
                ("stroke_w", int(1)),
            ]),
        ];
        let (nx, ny) = (
            4.max(round(f(sw) / f(pitch))?),
            4.max(round(f(sh) / f(pitch))?),
        );
        for i in 0..nx {
            let cx = (f(i) + 0.5) * f(sw) / f(nx);
            for cy in [0, sh] {
                under.push(obj(vec![
                    ("k", text("ellipse")),
                    ("cx", int(round(cx)?)),
                    ("cy", int(cy)),
                    ("r", int(hr)),
                    ("fill", rgb(stock)),
                ]));
            }
        }
        for j in 0..ny {
            let center_y = (f(j) + 0.5) * f(sh) / f(ny);
            for cx in [0, sw] {
                under.push(obj(vec![
                    ("k", text("ellipse")),
                    ("cx", int(cx)),
                    ("cy", int(round(center_y)?)),
                    ("r", int(hr)),
                    ("fill", rgb(stock)),
                ]));
            }
        }
        let yb = f(pad + h + eg) + f(pad_bot - eg - hr) * 0.52 + f(cap1) / 2.0;
        let (mut over, left_w): (Vec<Val>, f64) = match (kind, kw.custom) {
            ("custom", Some(c)) => {
                let y = round(yb - f(cap1) / 2.0 - f(lh) / 2.0)?;
                (
                    vec![clogo_item(c, pad, y, lw, lh, kw.mode, ink, 250.0)],
                    f(lw),
                )
            }
            ("none", _) => (vec![], 0.0),
            _ => (
                vec![t.base_text(
                    Num::I(pad),
                    yb,
                    px1,
                    "inter_sb",
                    &inscription,
                    ink,
                    0.1,
                    Anchor::L,
                )?],
                t.advance("inter_sb", px1, &inscription, 0.1)?,
            ),
        };
        let mut right = join(
            &["lens_short", "aperture", "shutter", "date"].map(|k| meta_get(meta, k)),
            "  ·  ",
        );
        let pad_left = if left_w != 0.0 { left_w + 30.0 } else { 0.0 };
        while !right.is_empty() && t.advance("inter", px2, &right, 0.02)? + pad_left > f(w) {
            right = drop_last(&right, "  ·  ");
        }
        if !right.is_empty() {
            over.push(t.base_text(
                Num::I(pad + w),
                yb,
                px2,
                "inter",
                &right,
                muted,
                0.02,
                Anchor::R,
            )?);
        }
        Ok(make_unit(sw, sh, (pad, pad), w, h, under, over, stock))
    };
    let margin = 34.max(round(0.072 * f(short))?);
    let s = Solve {
        natural: (margin, margin, margin),
        f_side: 0.07,
        f_top: 0.07,
        f_bot: 0.07,
        split: 0.47,
    };
    solve(&unit, ratio, w, &s)
}
