//! Port of `gxr_studio/finish/frame_core.py`: the integer canvas solver and cap-aligned text.

use gxr_raw::CoreError;

use crate::Meta;
use crate::item::{Num, int, ints, obj, rgb, set, text};
use crate::layout::FrameLayout;
use crate::metrics::Fonts;
use crate::py;
use crate::value::Val;

/// `frame_core.COLORS`.
pub fn color(name: &str) -> Result<[i64; 3], CoreError> {
    match name {
        "white" => Ok([255, 255, 255]),
        "black" => Ok([14, 14, 16]),
        "cream" => Ok([244, 239, 230]),
        _ => Err(CoreError::new("key_error", format!("'{name}'"))),
    }
}

/// `frame_core.LOGO_SIZE`: the pixel size of the built-in wordmark Python paints. The layout
/// only uses the ratio; this crate never opens or ships that image.
pub const LOGO_SIZE: (i64, i64) = (2400, 433);
/// `frame_core.LOGO_INK_Y`.
pub const LOGO_INK_Y: (i64, i64) = (9, 420);

/// `meta.get(key, "")`.
pub fn meta_get<'a>(meta: &'a Meta, key: &str) -> &'a str {
    meta.get(key).map_or("", String::as_str)
}

/// The text anchor of `cap_text` / `base_text`.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Anchor {
    L,
    C,
    R,
}

fn is_cjk(ch: char) -> bool {
    u32::from(ch) >= 0x2E80
}

/// Port of gxr_studio/finish/frame_core.py:22 Type
pub struct Type<'a> {
    pub m: &'a Fonts,
}

impl Type<'_> {
    /// Port of gxr_studio/finish/frame_core.py:26 Type.pens
    pub fn pens(&self, font: &str, s: &str, track: Num) -> Result<Vec<f64>, CoreError> {
        let upm = self.m.upm(font, false)?;
        let cjk = s.chars().any(is_cjk);
        let extra = if cjk {
            0
        } else {
            py::round(track.f() * py::f(upm))?
        };
        let n = s.chars().count();
        let mut out = vec![0.0f64];
        for (i, ch) in s.chars().enumerate() {
            let c = is_cjk(ch);
            let adv = py::f(self.m.glyph(font, ch, c)?.0 * upm) / py::f(self.m.upm(font, c)?);
            let last = *out.last().unwrap_or(&0.0);
            out.push(last + adv + py::f(if i + 1 < n { extra } else { 0 }));
        }
        Ok(out)
    }

    /// Port of gxr_studio/finish/frame_core.py:37 Type.advance
    pub fn advance(&self, font: &str, px: i64, s: &str, track: f64) -> Result<f64, CoreError> {
        self.advance_n(font, px, s, Num::F(track))
    }

    pub fn advance_n(&self, font: &str, px: i64, s: &str, track: Num) -> Result<f64, CoreError> {
        let p = self.pens(font, s, track)?;
        Ok(*p.last().unwrap_or(&0.0) * py::f(px) / py::f(self.m.upm(font, false)?))
    }

    /// Port of gxr_studio/finish/frame_core.py:40 Type.cap_box
    pub fn cap_box(&self, font: &str, px: i64) -> Result<(i64, i64), CoreError> {
        let Some(b) = self.m.glyph(font, 'H', false)?.1 else {
            return Err(CoreError::new(
                "value_error",
                format!("Frame font {font} has no H outline"),
            ));
        };
        let scale = py::f(px) / py::f(self.m.upm(font, false)?);
        let asc = py::f(self.m.ascent(font, px, false)?);
        Ok((
            py::floor(asc - py::f(b[3]) * scale)?,
            py::ceil(asc - py::f(b[1]) * scale)?,
        ))
    }

    /// Port of gxr_studio/finish/frame_core.py:48 Type.cap_h
    pub fn cap_h(&self, font: &str, px: i64) -> Result<i64, CoreError> {
        let (top, bottom) = self.cap_box(font, px)?;
        Ok(bottom - top)
    }

    /// Port of gxr_studio/finish/frame_core.py:52 Type.px_for_cap
    pub fn px_for_cap(&self, font: &str, target: f64) -> Result<i64, CoreError> {
        let mut px = 8i64;
        while py::f(self.cap_h(font, px)?) < target {
            px += 1;
            if px > 1 << 24 {
                return Err(CoreError::new(
                    "value_error",
                    format!("Frame font {font} never reaches the cap height"),
                ));
            }
        }
        Ok(px)
    }

    /// Port of gxr_studio/finish/frame_core.py:58 Type.sizes
    pub fn sizes(&self, wref: i64) -> Sizes<'_, '_> {
        Sizes { t: self, wref }
    }

    /// Port of gxr_studio/finish/frame_core.py:61 Type.text
    #[allow(clippy::too_many_arguments)]
    pub fn text(
        &self,
        x: Num,
        y: f64,
        px: i64,
        font: &str,
        s: &str,
        fill: [i64; 3],
        track: Num,
    ) -> Result<Val, CoreError> {
        let track = if s.chars().any(is_cjk) {
            Num::I(0)
        } else {
            track
        };
        let upm = py::f(self.m.upm(font, false)?);
        let p = self.pens(font, s, track)?;
        let mut xs = Vec::with_capacity(p.len().saturating_sub(1));
        for &pen in &p[..p.len() - 1] {
            xs.push(py::floor(x.f() + pen * py::f(px) / upm + 0.5)?);
        }
        Ok(obj(vec![
            ("k", text("text")),
            ("x", x.val()),
            ("y", int(py::floor(y + 0.5)?)),
            ("px", int(px)),
            ("font", text(font)),
            ("text", text(s)),
            ("fill", rgb(fill)),
            ("track", track.val()),
            ("cjk", Val::Bool(false)),
            ("xs", ints(&xs)),
        ]))
    }

    fn start(
        &self,
        x: Num,
        px: i64,
        font: &str,
        s: &str,
        track: f64,
        anchor: Anchor,
    ) -> Result<Num, CoreError> {
        let width = self.advance(font, px, s, track)?;
        Ok(match anchor {
            Anchor::L => x,
            Anchor::C => Num::F(x.f() - width / 2.0),
            Anchor::R => Num::F(x.f() - width),
        })
    }

    /// Port of gxr_studio/finish/frame_core.py:77 Type.cap_text
    #[allow(clippy::too_many_arguments)]
    pub fn cap_text(
        &self,
        x: Num,
        y_mid: f64,
        px: i64,
        font: &str,
        s: &str,
        fill: [i64; 3],
        track: f64,
        anchor: Anchor,
    ) -> Result<Val, CoreError> {
        let x0 = self.start(x, px, font, s, track, anchor)?;
        let (top, bottom) = self.cap_box(font, px)?;
        self.text(
            x0,
            y_mid - py::f(top + bottom) / 2.0,
            px,
            font,
            s,
            fill,
            Num::F(track),
        )
    }

    /// Port of gxr_studio/finish/frame_core.py:85 Type.base_text
    #[allow(clippy::too_many_arguments)]
    pub fn base_text(
        &self,
        x: Num,
        baseline: f64,
        px: i64,
        font: &str,
        s: &str,
        fill: [i64; 3],
        track: f64,
        anchor: Anchor,
    ) -> Result<Val, CoreError> {
        let x0 = self.start(x, px, font, s, track, anchor)?;
        let asc = self.m.ascent(font, px, false)?;
        self.text(x0, baseline - py::f(asc), px, font, s, fill, Num::F(track))
    }

    /// Port of gxr_studio/finish/frame_core.py:92 Type.clip_text
    pub fn clip_text(
        &self,
        s: &str,
        font: &str,
        px: i64,
        max_w: f64,
        track: f64,
    ) -> Result<String, CoreError> {
        if self.advance(font, px, s, track)? <= max_w {
            return Ok(s.to_string());
        }
        let mut chars: Vec<char> = s.chars().collect();
        loop {
            if chars.len() <= 1 {
                break;
            }
            let probe: String = chars.iter().collect::<String>() + "...";
            if self.advance(font, px, &probe, track)? <= max_w {
                break;
            }
            chars.pop();
        }
        let kept: String = chars.iter().collect();
        Ok(format!("{}...", py::rstrip(&kept)))
    }
}

/// Port of gxr_studio/finish/frame_core.py:100 Sizes
pub struct Sizes<'t, 'a> {
    t: &'t Type<'a>,
    wref: i64,
}

impl Sizes<'_, '_> {
    /// Port of gxr_studio/finish/frame_core.py:104 Sizes.p1
    pub fn p1(&self, font: &str) -> Result<i64, CoreError> {
        self.t.px_for_cap(font, 0.015 * py::f(self.wref))
    }

    /// Port of gxr_studio/finish/frame_core.py:107 Sizes.p2
    pub fn p2(&self, font: &str) -> Result<i64, CoreError> {
        self.t.px_for_cap(font, 0.011 * py::f(self.wref))
    }

    /// Port of gxr_studio/finish/frame_core.py:110 Sizes.cap
    pub fn cap(&self, font: &str, fraction: f64) -> Result<i64, CoreError> {
        self.t.px_for_cap(font, fraction * py::f(self.wref))
    }
}

/// Port of gxr_studio/finish/frame_core.py:114 join
pub fn join(parts: &[&str], sep: &str) -> String {
    py::join(parts, sep)
}

/// Port of gxr_studio/finish/frame_core.py:118 frame_index
pub fn frame_index(meta: &Meta) -> i64 {
    let raw = meta_get(meta, "frame_no");
    let raw = if raw.is_empty() { "0" } else { raw };
    match py::int_mod36(raw) {
        None | Some((true, _)) => 1,
        Some((false, r)) => (r - 1).rem_euclid(36) + 1,
    }
}

const POINT_X: [&str; 5] = ["x", "x1", "x2", "cx", "gx"];
const POINT_Y: [&str; 5] = ["y", "y1", "y2", "cy", "gy"];

fn shifted(v: &Val, d: i64) -> Result<Val, CoreError> {
    match v {
        Val::Int(n) => Ok(Val::Int(n + d)),
        Val::Float(f) => Ok(Val::Float(f + py::f(d))),
        _ => Err(CoreError::new("type_error", "坐标不是数字")),
    }
}

/// Port of gxr_studio/finish/frame_core.py:126 shift_items
pub fn shift_items(items: &[Val], dx: i64, dy: i64) -> Result<Vec<Val>, CoreError> {
    let mut out = Vec::with_capacity(items.len());
    for orig in items {
        let mut it = orig.clone();
        if let Val::Obj(pairs) = &mut it {
            for (k, v) in pairs.iter_mut() {
                if POINT_X.contains(&k.as_str()) {
                    *v = shifted(v, dx)?;
                } else if POINT_Y.contains(&k.as_str()) {
                    *v = shifted(v, dy)?;
                } else if k == "xs" {
                    if let Val::Arr(xs) = v {
                        for x in xs.iter_mut() {
                            *x = shifted(x, dx)?;
                        }
                    }
                } else if (k == "pts" || k == "hole_pts")
                    && let Val::Arr(pts) = v
                {
                    for p in pts.iter_mut() {
                        if let Val::Arr(xy) = p
                            && xy.len() >= 2
                        {
                            let nx = shifted(&xy[0], dx)?;
                            let ny = shifted(&xy[1], dy)?;
                            *p = Val::Arr(vec![nx, ny]);
                        }
                    }
                }
            }
        }
        out.push(it);
    }
    Ok(out)
}

/// Port of gxr_studio/finish/frame_core.py:145 make_unit (the dict, plus the keys styles add).
#[derive(Clone, Debug)]
pub struct Unit {
    pub w: i64,
    pub h: i64,
    pub px: i64,
    pub py: i64,
    pub pw: i64,
    pub ph: i64,
    pub under: Vec<Val>,
    pub over: Vec<Val>,
    pub bg: [i64; 3],
    pub table: Option<[i64; 3]>,
    pub bgfx: Option<Val>,
    pub grain: f64,
    pub bgfx_always: bool,
}

/// Port of gxr_studio/finish/frame_core.py:145 make_unit
#[allow(clippy::too_many_arguments)]
pub fn make_unit(
    w: i64,
    h: i64,
    photo_xy: (i64, i64),
    pw: i64,
    ph: i64,
    under: Vec<Val>,
    over: Vec<Val>,
    bg: [i64; 3],
) -> Unit {
    Unit {
        w,
        h,
        px: photo_xy.0,
        py: photo_xy.1,
        pw,
        ph,
        under,
        over,
        bg,
        table: None,
        bgfx: None,
        grain: 0.0,
        bgfx_always: false,
    }
}

/// Port of gxr_studio/finish/frame_core.py:175 assemble
pub fn assemble(
    u: &Unit,
    width: i64,
    height: i64,
    x: i64,
    y: i64,
    fixed: bool,
) -> Result<FrameLayout, CoreError> {
    let keep = |items: &[Val]| -> Vec<Val> {
        items
            .iter()
            .filter(|i| fixed || !crate::item::truthy(i, "fx_only"))
            .cloned()
            .collect()
    };
    let under = keep(&u.under);
    let over = keep(&u.over);
    let photo = obj(vec![
        ("k", text("photo")),
        ("x", int(u.px + x)),
        ("y", int(u.py + y)),
        ("w", int(u.pw)),
        ("h", int(u.ph)),
    ]);
    let mut items = shift_items(&under, x, y)?;
    items.push(photo);
    items.extend(shift_items(&over, x, y)?);
    let bg = match u.table {
        Some(t) if fixed => t,
        _ => u.bg,
    };
    let bgfx = if fixed || u.bgfx_always {
        u.bgfx.clone().unwrap_or(Val::Null)
    } else {
        Val::Null
    };
    Ok(FrameLayout {
        width,
        height,
        photo: (u.px + x, u.py + y),
        bg: rgb(bg),
        items,
        bgfx,
        unit_box: [x, y, u.w, u.h],
        fixed,
        grain: u.grain,
    })
}

/// `solve`'s keyword arguments.
pub struct Solve {
    pub natural: (i64, i64, i64),
    pub f_side: f64,
    pub f_top: f64,
    pub f_bot: f64,
    pub split: f64,
}

/// `-(-a // b)`.
fn ceil_div(a: i64, b: i64) -> Result<i64, CoreError> {
    if b == 0 {
        return Err(CoreError::new("zero_division_error", "division by zero"));
    }
    Ok(-((-a).div_euclid(b)))
}

/// Port of gxr_studio/finish/frame_core.py:192 solve
pub fn solve(
    unit_fn: &dyn Fn(i64) -> Result<Unit, CoreError>,
    ratio: Option<(i64, i64)>,
    w_hint: i64,
    s: &Solve,
) -> Result<FrameLayout, CoreError> {
    let (ms, mt, mb) = s.natural;
    let cap_ref = py::round(1.9 * py::f(w_hint))?;
    let Some((rw, rh)) = ratio else {
        let mut u = unit_fn(w_hint + 2 * ms)?;
        for _ in 0..2 {
            u = unit_fn((u.w + 2 * ms).min(cap_ref))?;
        }
        return assemble(&u, u.w + 2 * ms, u.h + mt + mb, ms, mt, false);
    };
    let u = unit_fn(w_hint)?;
    let side = py::round((1.0 - 2.0 * s.f_side) * 100.0)?;
    let tall = py::round((1.0 - s.f_top - s.f_bot) * 100.0)?;
    let mut k = ceil_div(u.w * 100, side * rw)?
        .max(ceil_div(u.h * 100, tall * rh)?)
        .max(1);
    loop {
        let (width, height) = (k * rw, k * rh);
        let u = unit_fn(width.max(py::round(0.62 * py::f(height))?).min(cap_ref))?;
        let sm = py::ceil(s.f_side * py::f(width))?;
        let tm = py::ceil(s.f_top * py::f(height))?;
        let bm = py::ceil(s.f_bot * py::f(height))?;
        if width - u.w >= 2 * sm && height - u.h >= tm + bm {
            let free = height - u.h;
            let y = tm.max((free - bm).min(py::round(s.split * py::f(free))?));
            return assemble(&u, width, height, (width - u.w).div_euclid(2), y, true);
        }
        k += 1;
        if k > 1 << 30 {
            return Err(CoreError::new("value_error", "边框画布找不到合适的尺寸"));
        }
    }
}

fn rot_val(v: &Val, wl: i64) -> Result<Val, CoreError> {
    match v {
        Val::Int(n) => Ok(Val::Int(wl - n)),
        Val::Float(f) => Ok(Val::Float(py::f(wl) - f)),
        _ => Err(CoreError::new("type_error", "坐标不是数字")),
    }
}

/// `wl - a - b` with Python's int/float result.
fn rot_sub2(a: &Val, b: &Val, wl: i64) -> Result<Val, CoreError> {
    let first = rot_val(a, wl)?;
    match (first, b) {
        (Val::Int(x), Val::Int(y)) => Ok(Val::Int(x - y)),
        (Val::Int(x), Val::Float(y)) => Ok(Val::Float(py::f(x) - y)),
        (Val::Float(x), Val::Int(y)) => Ok(Val::Float(x - py::f(*y))),
        (Val::Float(x), Val::Float(y)) => Ok(Val::Float(x - y)),
        _ => Err(CoreError::new("type_error", "坐标不是数字")),
    }
}

fn field(it: &Val, key: &str) -> Result<Val, CoreError> {
    it.get(key)
        .cloned()
        .ok_or_else(|| CoreError::new("key_error", format!("'{key}'")))
}

/// Port of gxr_studio/finish/frame_core.py:225 rotate_unit_ccw
pub fn rotate_unit_ccw(unit: &Unit) -> Result<Unit, CoreError> {
    let wl = unit.w;
    let rot = |it: &Val| -> Result<Val, CoreError> {
        let mut n = it.clone();
        let kind = crate::item::str_of(it, "k")?;
        match kind {
            "rect" | "hole" | "shadow" => {
                set(&mut n, "x", field(it, "y")?);
                set(
                    &mut n,
                    "y",
                    rot_sub2(&field(it, "x")?, &field(it, "w")?, wl)?,
                );
                set(&mut n, "w", field(it, "h")?);
                set(&mut n, "h", field(it, "w")?);
                if it.get("dy").is_some() {
                    set(&mut n, "dx", field(it, "dy")?);
                    set(&mut n, "dy", Val::Int(0));
                }
            }
            "poly" => {
                let mut pts = Vec::new();
                if let Some(Val::Arr(list)) = it.get("pts") {
                    for p in list {
                        let Val::Arr(xy) = p else {
                            return Err(CoreError::new("type_error", "pts 不是点列表"));
                        };
                        let (Some(px), Some(py_)) = (xy.first(), xy.get(1)) else {
                            return Err(CoreError::new("index_error", "list index out of range"));
                        };
                        pts.push(Val::Arr(vec![py_.clone(), rot_val(px, wl)?]));
                    }
                }
                set(&mut n, "pts", Val::Arr(pts));
            }
            "line" => {
                set(&mut n, "x1", field(it, "y1")?);
                set(&mut n, "y1", rot_val(&field(it, "x1")?, wl)?);
                set(&mut n, "x2", field(it, "y2")?);
                set(&mut n, "y2", rot_val(&field(it, "x2")?, wl)?);
            }
            "text" => {
                let width = crate::item::int_of(it, "tw")?;
                let x0 = crate::item::num(it, "x")?.round()?;
                let gx = match crate::item::num(it, "y")? {
                    Num::I(v) => v,
                    Num::F(v) => py::trunc(v)?,
                };
                let mut xs0 = Vec::new();
                if let Some(Val::Arr(xs)) = it.get("xs") {
                    for x in xs {
                        let Val::Int(x) = x else {
                            return Err(CoreError::new("type_error", "xs 不是整数"));
                        };
                        xs0.push(x - x0);
                    }
                }
                set(&mut n, "rot", Val::Bool(true));
                set(&mut n, "gx", int(gx));
                set(&mut n, "gy", int(wl - x0 - width));
                set(&mut n, "xs0", ints(&xs0));
            }
            _ => {}
        }
        Ok(n)
    };
    let mut out = unit.clone();
    out.w = unit.h;
    out.h = wl;
    out.px = unit.py;
    out.py = wl - unit.px - unit.pw;
    out.pw = unit.ph;
    out.ph = unit.pw;
    out.under = unit.under.iter().map(&rot).collect::<Result<_, _>>()?;
    out.over = unit.over.iter().map(&rot).collect::<Result<_, _>>()?;
    Ok(out)
}
