//! Port of `gxr_studio/finish/layout.py`: text metrics, the stamp layout and the frame dispatch.
//! Numbers only; the browser paints.

use std::collections::BTreeMap;

use gxr_raw::{CoreError, Json};

use crate::item::{Num, int, ints, obj, rgb, text};
use crate::logo::LogoInfo;
use crate::metrics::Fonts;
use crate::py;
use crate::spec::{FrameSpec, StampSpec};
use crate::value::Val;
use crate::{frame_new_a, frame_new_b, frame_objects, frame_styles};

/// `layout.STAMP_COLORS`.
pub fn stamp_color(name: &str) -> Result<[i64; 3], CoreError> {
    match name {
        "orange" => Ok([255, 106, 0]),
        "red" => Ok([220, 32, 32]),
        "yellow" => Ok([255, 196, 40]),
        "white" => Ok([255, 255, 255]),
        _ => Err(CoreError::new("key_error", format!("'{name}'"))),
    }
}

/// `layout.STAMP_SIZE`.
fn stamp_size(name: &str) -> Result<f64, CoreError> {
    match name {
        "small" => Ok(0.038),
        "medium" => Ok(0.055),
        "large" => Ok(0.078),
        _ => Err(CoreError::new("key_error", format!("'{name}'"))),
    }
}

/// `layout.STAMP_FONT`.
fn stamp_font(name: &str) -> Result<&'static str, CoreError> {
    match name {
        "seg7" => Ok("dseg"),
        "typewriter" => Ok("courier"),
        "sans" => Ok("atkinson"),
        _ => Err(CoreError::new("key_error", format!("'{name}'"))),
    }
}

/// `spec.OUTPUT_RATIOS`.
pub fn output_ratio(name: &str) -> Option<(i64, i64)> {
    match name {
        "1:1" => Some((1, 1)),
        "4:5" => Some((4, 5)),
        "3:4" => Some((3, 4)),
        "2:3" => Some((2, 3)),
        "9:16" => Some((9, 16)),
        "16:9" => Some((16, 9)),
        "3:2" => Some((3, 2)),
        _ => None,
    }
}

/// Port of gxr_studio/finish/layout.py:43 needs_cjk
pub fn needs_cjk(text: &str) -> bool {
    text.chars().any(|ch| u32::from(ch) > 127)
}

/// Port of gxr_studio/finish/layout.py:47 ascent_px
pub fn ascent_px(ascender: i64, upm: i64, px: i64) -> Result<i64, CoreError> {
    let over = || CoreError::new("overflow_error", "ascent 超出范围");
    let num = 2i64
        .checked_mul(ascender)
        .and_then(|v| v.checked_mul(px))
        .and_then(|v| v.checked_mul(64))
        .and_then(|v| v.checked_add(upm))
        .ok_or_else(over)?;
    let den = upm.checked_mul(2).ok_or_else(over)?;
    let q = num.div_euclid(den);
    Ok(-((-q).div_euclid(64)))
}

/// Port of gxr_studio/finish/layout.py:52 _pens
fn pens(m: &Fonts, font: &str, text: &str, cjk: bool) -> Result<Vec<i64>, CoreError> {
    let mut out = vec![0i64];
    for ch in text.chars() {
        let last = *out.last().unwrap_or(&0);
        out.push(last + m.glyph(font, ch, cjk)?.0);
    }
    Ok(out)
}

/// Port of gxr_studio/finish/layout.py:60 advance
pub fn advance(m: &Fonts, font: &str, px: i64, text: &str, cjk: bool) -> Result<f64, CoreError> {
    let p = pens(m, font, text, cjk)?;
    let last = *p.last().unwrap_or(&0);
    // int * int, then true division (correctly rounded, as Python's int / int).
    Ok(py::f(last * px) / py::f(m.upm(font, cjk)?))
}

/// Port of gxr_studio/finish/layout.py:65 glyph_xs
pub fn glyph_xs(
    m: &Fonts,
    font: &str,
    px: i64,
    text: &str,
    x: Num,
    cjk: bool,
) -> Result<Vec<i64>, CoreError> {
    let upm = m.upm(font, cjk)?;
    let p = pens(m, font, text, cjk)?;
    let mut out = Vec::with_capacity(p.len().saturating_sub(1));
    for &pen in &p[..p.len() - 1] {
        out.push(py::floor(x.f() + py::f(pen * px) / py::f(upm) + 0.5)?);
    }
    Ok(out)
}

/// Port of gxr_studio/finish/layout.py:71 ink_box
pub fn ink_box(
    m: &Fonts,
    font: &str,
    px: i64,
    text: &str,
    cjk: bool,
) -> Result<[i64; 4], CoreError> {
    let upm = py::f(m.upm(font, cjk)?);
    let asc = m.ascent(font, px, cjk)?;
    let p = pens(m, font, text, cjk)?;
    let mut left = 0.0f64;
    let mut right = py::f(*p.last().unwrap_or(&0) * px) / upm;
    let mut up = 0.0f64;
    let mut down = 0.0f64;
    for (ch, &pen) in text.chars().zip(p.iter()) {
        let Some(b) = m.glyph(font, ch, cjk)?.1 else {
            continue;
        };
        left = left.min(py::f((pen + b[0]) * px) / upm);
        right = right.max(py::f((pen + b[2]) * px) / upm);
        up = up.max(py::f(b[3] * px) / upm);
        down = down.max(py::f(-b[1] * px) / upm);
    }
    Ok([
        py::floor(left)?,
        py::floor(py::f(asc) - up)?,
        py::ceil(right)?,
        py::ceil(py::f(asc) + down)?,
    ])
}

/// Port of gxr_studio/finish/layout.py:148 text_item
#[allow(clippy::too_many_arguments)]
pub fn text_item(
    m: &Fonts,
    x: Num,
    y: f64,
    px: i64,
    font: &str,
    s: &str,
    fill: [i64; 3],
    cjk: bool,
) -> Result<Val, CoreError> {
    Ok(obj(vec![
        ("k", text("text")),
        ("x", x.val()),
        ("y", int(py::floor(y + 0.5)?)),
        ("px", int(px)),
        ("font", text(font)),
        ("text", text(s)),
        ("fill", rgb(fill)),
        ("cjk", Val::Bool(cjk)),
        ("xs", ints(&glyph_xs(m, font, px, s, x, cjk)?)),
    ]))
}

/// Port of gxr_studio/finish/layout.py:171 ratio_size
pub fn ratio_size(w: i64, h: i64, rw: i64, rh: i64) -> Result<(i64, i64), CoreError> {
    if rw == 0 || rh == 0 {
        return Err(CoreError::new("zero_division_error", "division by zero"));
    }
    let multiple = (-((-w).div_euclid(rw))).max(-((-h).div_euclid(rh)));
    Ok((multiple * rw, multiple * rh))
}

/// Port of gxr_studio/finish/layout.py:177 fit_size
pub fn fit_size(w: i64, h: i64, max_edge: Option<i64>) -> Result<(i64, i64), CoreError> {
    let edge = match max_edge {
        Some(e) if e != 0 && w.max(h) > e => e,
        _ => return Ok((w, h)),
    };
    let scale = py::f(edge) / py::f(w.max(h));
    Ok((
        1.max(py::round(py::f(w) * scale)?),
        1.max(py::round(py::f(h) * scale)?),
    ))
}

/// The custom logos a layout may use, by id (`logos` in `frame_layout`).
pub type Logos = BTreeMap<String, LogoInfo>;

/// `logo_styles` in `spec.py`.
const LOGO_STYLES: [&str; 8] = [
    "leica",
    "hasselblad",
    "thin_keyline",
    "exhibition_label",
    "cinema",
    "slide_mount",
    "postage_stamp",
    "blur_fill",
];

/// Port of gxr_studio/finish/layout.py:185 _resolve_logo
fn resolve_logo<'a>(spec: &FrameSpec, logos: &'a Logos) -> (&'static str, Option<&'a LogoInfo>) {
    if !LOGO_STYLES.contains(&spec.style.as_str()) || spec.logo == "none" || !spec.show_logo {
        return ("none", None);
    }
    if spec.logo == "ricoh" {
        return ("ricoh", None);
    }
    match logos.get(&spec.logo) {
        Some(custom) => ("custom", Some(custom)),
        None => ("ricoh", None),
    }
}

/// The custom-logo arguments every logo style takes (`logo_kw`).
#[derive(Clone, Copy)]
pub struct LogoKw<'a> {
    pub kind: &'static str,
    pub custom: Option<&'a LogoInfo>,
    pub mode: &'a str,
    pub size: &'a str,
}

/// Port of gxr_studio/finish/layout.py:92 FrameLayout
#[derive(Clone, Debug)]
pub struct FrameLayout {
    pub width: i64,
    pub height: i64,
    pub photo: (i64, i64),
    pub bg: Val,
    pub items: Vec<Val>,
    pub bgfx: Val,
    pub unit_box: [i64; 4],
    pub fixed: bool,
    pub grain: f64,
}

impl FrameLayout {
    /// Port of gxr_studio/finish/layout.py:105 FrameLayout.to_json
    pub fn to_json(&self) -> Json {
        Json::object([
            ("width", Json::Int(self.width)),
            ("height", Json::Int(self.height)),
            ("photo", Json::ints(&[self.photo.0, self.photo.1])),
            ("bg", crate::item::to_json(&self.bg)),
            (
                "items",
                Json::Array(self.items.iter().map(crate::item::to_json).collect()),
            ),
            ("bgfx", crate::item::to_json(&self.bgfx)),
            ("unit_box", Json::ints(&self.unit_box)),
            ("fixed", Json::Bool(self.fixed)),
            ("grain", Json::Float(self.grain)),
        ])
    }
}

/// Port of gxr_studio/finish/layout.py:200 frame_layout
pub fn frame_layout(
    w: i64,
    h: i64,
    spec: &FrameSpec,
    meta: &crate::Meta,
    m: &Fonts,
    logos: &Logos,
) -> Result<Option<FrameLayout>, CoreError> {
    let ratio = output_ratio(&spec.output_ratio);
    if spec.style == "none" {
        return Ok(None);
    }
    let (kind, custom) = resolve_logo(spec, logos);
    let kw = LogoKw {
        kind,
        custom,
        mode: &spec.logo_mode,
        size: &spec.logo_size,
    };
    let raw = match spec.style.as_str() {
        "gallery" => frame_styles::gallery(w, h, ratio, &spec.color, &spec.weight)?,
        "leica" => frame_styles::leica(w, h, meta, ratio, m, spec, kind != "none", kw)?,
        "hasselblad" => frame_styles::hasselblad(w, h, meta, ratio, m, spec, kw)?,
        "film" => frame_objects::strip(w, h, meta, ratio, &spec.fmt, &spec.table, m)?,
        "instant" => {
            frame_objects::instant(w, h, meta, ratio, m, &spec.text, spec.show_date, &spec.fmt)?
        }
        "thin_keyline" => frame_new_a::thin_keyline(w, h, meta, ratio, m, &spec.color, kw)?,
        "exhibition_label" => {
            frame_new_a::exhibition_label(w, h, meta, ratio, m, &spec.color, &spec.text, kw)?
        }
        "blur_fill" => frame_new_a::blur_fill(w, h, meta, ratio, m, spec.show_params, kw)?,
        "cinema" => frame_new_b::cinema(w, h, meta, ratio, m, &spec.text, kw)?,
        "slide_mount" => frame_new_b::slide_mount(w, h, meta, ratio, m, &spec.color, kw)?,
        "darkroom" => frame_new_b::darkroom(w, h, meta, ratio, m, &spec.text)?,
        _ => frame_new_b::postage_stamp(w, h, meta, ratio, m, kw)?,
    };
    Ok(Some(raw))
}

// ---- 时间戳 -------------------------------------------------------------------

/// Port of gxr_studio/finish/layout.py:265 seg7_text
pub fn seg7_text(text: &str) -> String {
    text.replace(' ', "!")
}

/// `int(px * k)` for a positive layout size.
fn int_times(px: i64, k: f64) -> Result<i64, CoreError> {
    py::trunc(py::f(px) * k)
}

/// Port of gxr_studio/finish/layout.py:270 tick_item
pub fn tick_item(x: i64, y: i64, px: i64, fill: [i64; 3]) -> Result<Val, CoreError> {
    let w = 2.max(int_times(px, 0.07)?);
    let h = 6.max(int_times(px, 0.42)?);
    let x0 = x + w;
    let y0 = y + 0.max(int_times(px, 0.04)?);
    let pts = Val::Arr(vec![
        ints(&[x0 + 2 * w, y0]),
        ints(&[x0 + 3 * w, y0]),
        ints(&[x0 + w, y0 + h]),
        ints(&[x0, y0 + h]),
    ]);
    Ok(obj(vec![
        ("k", text("poly")),
        ("pts", pts),
        ("fill", rgb(fill)),
    ]))
}

/// Port of gxr_studio/finish/layout.py:279 _stamp_ops
fn stamp_ops(
    origin: (i64, i64),
    s: &str,
    px: i64,
    font: &str,
    cjk: bool,
    ink: [i64; 3],
    m: &Fonts,
) -> Result<Vec<Val>, CoreError> {
    let (mut x, y) = origin;
    let yf = py::f(y);
    if font != "dseg" || cjk {
        return Ok(vec![text_item(m, Num::I(x), yf, px, font, s, ink, cjk)?]);
    }
    let drawn: Vec<char> = seg7_text(s).chars().collect();
    let mut ops = Vec::new();
    let mut start = 0;
    while start < drawn.len() {
        let Some(tick) = drawn[start..]
            .iter()
            .position(|&c| c == '\'')
            .map(|k| k + start)
        else {
            let rest: String = drawn[start..].iter().collect();
            ops.push(text_item(m, Num::I(x), yf, px, font, &rest, ink, false)?);
            break;
        };
        let chunk: String = drawn[start..tick].iter().collect();
        if !chunk.is_empty() {
            ops.push(text_item(m, Num::I(x), yf, px, font, &chunk, ink, false)?);
            x += py::round(advance(m, font, px, &chunk, false)?)?;
        }
        ops.push(tick_item(x, y, px, ink)?);
        x += py::round(advance(m, font, px, "'", false)?)?;
        start = tick + 1;
    }
    Ok(ops)
}

/// Port of gxr_studio/finish/layout.py:127 StampLayout
#[derive(Clone, Debug)]
pub struct StampLayout {
    pub px: i64,
    pub font: String,
    pub rect: [i64; 4],
    pub ink: [i64; 3],
    pub glow: Option<f64>,
    pub ops: Vec<Val>,
}

impl StampLayout {
    /// Port of gxr_studio/finish/layout.py:137 StampLayout.to_json
    pub fn to_json(&self) -> Json {
        Json::object([
            ("px", Json::Int(self.px)),
            ("font", Json::str(&self.font)),
            ("rect", Json::ints(&self.rect)),
            ("ink", Json::ints(&self.ink)),
            ("glow", self.glow.map_or(Json::Null, Json::Float)),
            (
                "ops",
                Json::Array(self.ops.iter().map(crate::item::to_json).collect()),
            ),
        ])
    }
}

/// `corner[i]` of a stamp corner such as `"br"`.
fn corner_at(spec: &StampSpec, i: usize) -> char {
    spec.corner.chars().nth(i).unwrap_or(' ')
}

/// Port of gxr_studio/finish/layout.py:301 stamp_layout
pub fn stamp_layout(
    w: i64,
    h: i64,
    spec: &StampSpec,
    s: &str,
    m: &Fonts,
    frame: Option<&FrameLayout>,
) -> Result<Option<StampLayout>, CoreError> {
    if s.is_empty() {
        return Ok(None);
    }
    if let Some(frame) = frame {
        return border_stamp_layout(w, h, spec, s, m, frame);
    }
    let font = stamp_font(&spec.font)?;
    let cjk = needs_cjk(s);
    let shown = if cjk || spec.font != "seg7" {
        s.to_string()
    } else {
        seg7_text(s)
    };
    let short = py::f(w.min(h));
    let mut px = 12.max(py::round(short * stamp_size(&spec.size)?)?);
    let mut b = ink_box(m, font, 1.max(px), &shown, cjk)?;
    let margin = 1.max(py::round(short * 0.035)?);
    let (limit_w, limit_h) = (1.max(w - 2 * margin), 1.max(h - 2 * margin));
    while (b[2] - b[0] > limit_w || b[3] - b[1] > limit_h) && px > 1 {
        let nxt = int_times(px, 0.85)?;
        px = if nxt < px { nxt } else { px - 1 };
        b = ink_box(m, font, 1.max(px), &shown, cjk)?;
    }
    let (tw, th) = (b[2] - b[0], b[3] - b[1]);
    let x = if corner_at(spec, 1) == 'l' {
        margin
    } else {
        margin.max(w - margin - tw)
    };
    let y = if corner_at(spec, 0) == 't' {
        margin
    } else {
        margin.max(h - margin - th)
    };
    let ink = stamp_color(&spec.color)?;
    let face_px = 1.max(px);
    let ops = stamp_ops((x - b[0], y - b[1]), s, face_px, font, cjk, ink, m)?;
    let glow = if spec.glow && spec.font == "seg7" {
        Some(1.2f64.max(py::f(px) * 0.16))
    } else {
        None
    };
    Ok(Some(StampLayout {
        px: face_px,
        font: font.to_string(),
        rect: [x, y, tw, th],
        ink,
        glow,
        ops,
    }))
}

/// Port of gxr_studio/finish/layout.py:328 _border_stamp_layout
fn border_stamp_layout(
    w: i64,
    h: i64,
    spec: &StampSpec,
    s: &str,
    m: &Fonts,
    frame: &FrameLayout,
) -> Result<Option<StampLayout>, CoreError> {
    let Some(base) = stamp_layout(w, h, spec, s, m, None)? else {
        return Ok(None);
    };
    let mut top = frame.photo.1 + h;
    for item in &frame.items {
        if crate::item::str_of(item, "k")? == "text" {
            let cjk = matches!(item.get("cjk"), Some(Val::Bool(true)));
            let b = ink_box(
                m,
                crate::item::str_of(item, "font")?,
                crate::item::int_of(item, "px")?,
                crate::item::str_of(item, "text")?,
                cjk,
            )?;
            top = top.max(crate::item::int_of(item, "y")? + b[3] + 1);
        }
    }
    let bottom = frame.unit_box[1] + frame.unit_box[3];
    let margin = 2.max(py::round(py::f(w.min(h)) * 0.035)?.min((bottom - top).div_euclid(8)));
    let cjk = needs_cjk(s);
    let shown = if spec.font == "seg7" && !cjk {
        seg7_text(s)
    } else {
        s.to_string()
    };
    let mut px = base.px;
    loop {
        let b = ink_box(m, &base.font, px, &shown, cjk)?;
        let glow = base.glow.map(|_| 1.2f64.max(py::f(px) * 0.16));
        let pad = match glow {
            Some(g) => margin.max(py::ceil(3.0 * g)? + 2),
            None => margin,
        };
        let (tw, th) = (b[2] - b[0], b[3] - b[1]);
        if tw <= w - 2 * pad && th <= bottom - top - 2 * pad {
            let x = frame.photo.0
                + if corner_at(spec, 1) == 'l' {
                    pad
                } else {
                    w - pad - tw
                };
            let y = if corner_at(spec, 0) == 't' {
                top + pad
            } else {
                bottom - pad - th
            };
            let ops = stamp_ops((x - b[0], y - b[1]), s, px, &base.font, cjk, base.ink, m)?;
            return Ok(Some(StampLayout {
                px,
                font: base.font.clone(),
                rect: [x, y, tw, th],
                ink: base.ink,
                glow,
                ops,
            }));
        }
        if px == 1 {
            return Ok(None);
        }
        px = 1.max(int_times(px, 0.85)?);
    }
}

/// A moment as `format_stamp` reads it: year, month, day, hour, minute.
pub type Moment = (i64, i64, i64, i64, i64);

/// Port of gxr_studio/finish/stamp.py:16 format_stamp
pub fn format_stamp(spec: &StampSpec, taken: Option<Moment>, file_time: Option<Moment>) -> String {
    let moment = match &spec.date_override {
        Some(text) if !text.is_empty() => {
            let parts: Vec<i64> = text.split('-').map(py::small_int).collect();
            match parts.as_slice() {
                [y, mo, d] => Some((*y, *mo, *d, 0, 0)),
                _ => None,
            }
        }
        _ => taken.or(file_time),
    };
    let mut parts: Vec<String> = Vec::new();
    if let Some((year, month, day, hour, minute)) = moment
        && (spec.content == "date" || spec.content == "date+text")
    {
        {
            let yy = format!("{:02}", year.rem_euclid(100));
            let (mm, dd) = (format!("{month:02}"), format!("{day:02}"));
            let mut s = match spec.date_format.as_str() {
                "yy mm dd" => format!("{yy} {mm} {dd}"),
                "yyyy.mm.dd" => format!("{year:04}.{mm}.{dd}"),
                "mm dd 'yy" => format!("{mm} {dd} '{yy}"),
                _ => format!("'{yy} {mm} {dd}"),
            };
            if spec.show_time {
                s = format!("{s} {hour:02}:{minute:02}");
            }
            parts.push(s);
        }
    }
    if (spec.content == "text" || spec.content == "date+text") && !spec.text.is_empty() {
        parts.push(
            spec.text
                .chars()
                .take(crate::spec::STAMP_TEXT_MAX)
                .collect(),
        );
    }
    parts.join(" ")
}
