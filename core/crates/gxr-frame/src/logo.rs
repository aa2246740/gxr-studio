//! Port of the layout half of `gxr_studio/finish/logo_math.py`: custom-logo sizing and the
//! `clogo` item. Painting the user's image stays in the browser.

use gxr_raw::CoreError;

use crate::item::{int, obj, rgb, text};
use crate::py;
use crate::value::Val;

/// Port of gxr_studio/finish/logo_math.py:24 LogoInfo
#[derive(Clone, Debug, PartialEq)]
pub struct LogoInfo {
    pub id: String,
    pub name: String,
    pub w: i64,
    pub h: i64,
    pub coverage: f64,
    pub opaque: bool,
    pub lum: f64,
}

impl LogoInfo {
    /// Port of gxr_studio/finish/logo_math.py:36 LogoInfo.aspect
    pub fn aspect(&self) -> f64 {
        py::f(self.w) / py::f(1.max(self.h))
    }
}

/// `logo_math.SIZE_MULT.get(size, 1.0)`.
fn size_mult(size: &str) -> f64 {
    match size {
        "small" => 0.80,
        "large" => 1.25,
        _ => 1.00,
    }
}

const MULT: [(f64, f64); 7] = [
    (0.5, 2.6),
    (1.0, 2.5),
    (1.5, 2.5),
    (2.5, 2.3),
    (4.0, 1.5),
    (6.0, 1.04),
    (9.0, 0.88),
];

/// Port of gxr_studio/finish/logo_math.py:40 aspect_mult
pub fn aspect_mult(aspect: f64) -> f64 {
    if aspect <= MULT[0].0 {
        return MULT[0].1;
    }
    for pair in MULT.windows(2) {
        let ((a0, m0), (a1, m1)) = (pair[0], pair[1]);
        if aspect <= a1 {
            return m0 + (m1 - m0) * (aspect - a0) / (a1 - a0);
        }
    }
    MULT[6].1
}

/// Port of gxr_studio/finish/logo_math.py:50 coverage_comp
pub fn coverage_comp(coverage: f64) -> f64 {
    let base = 0.35 / coverage.max(0.02);
    1.25f64.min(0.90f64.max(gxr_math::powf(base, 0.25)))
}

/// Port of gxr_studio/finish/logo_math.py:55 fit_logo
pub fn fit_logo(
    logo: &LogoInfo,
    cap: i64,
    max_h: i64,
    max_w: i64,
    size: &str,
) -> Result<(i64, i64), CoreError> {
    let aspect = logo.aspect();
    if aspect <= 0.0 || max_w < 1 || max_h < 1 {
        let side = 0.max(max_w.min(max_h).min(8));
        return Ok((side, side));
    }
    let mult = size_mult(size);
    let target = py::f(cap) * aspect_mult(aspect) * coverage_comp(logo.coverage) * mult;
    let target = target.min(py::f(max_h)).min(py::f(max_w) / aspect);
    let ih = 8.max(py::round(target)?);
    let iw = 8.max(py::round(py::f(ih) * aspect)?);
    if iw <= max_w && ih <= max_h {
        return Ok((iw, ih));
    }
    fit_inside(aspect, target, max_h, max_w)
}

/// Port of gxr_studio/finish/logo_math.py:75 _fit_inside
fn fit_inside(aspect: f64, target_h: f64, max_h: i64, max_w: i64) -> Result<(i64, i64), CoreError> {
    let mut ih = max_h.min(1.max(py::round(target_h.min(py::f(max_h)))?));
    let mut iw = py::round(py::f(ih) * aspect)?;
    while (iw > max_w || ih > max_h) && ih > 1 {
        ih -= 1;
        iw = py::round(py::f(ih) * aspect)?;
    }
    if iw <= max_w && ih <= max_h && iw >= 1 && ih >= 1 {
        return Ok((iw, ih));
    }
    let limited = if aspect != 0.0 {
        py::trunc(py::f(max_w) / aspect)?
    } else {
        max_h
    };
    ih = max_h.min(1.max(limited));
    iw = py::round(py::f(ih) * aspect)?;
    while (iw > max_w || ih > max_h) && ih > 1 {
        ih -= 1;
        iw = py::round(py::f(ih) * aspect)?;
    }
    Ok((max_w.min(1.max(iw)), max_h.min(1.max(ih))))
}

/// Port of gxr_studio/finish/logo_math.py:93 pick_logo_mode
pub fn pick_logo_mode(logo: &LogoInfo, mode: &str, bg_lum: f64) -> String {
    if logo.opaque {
        return "orig".into();
    }
    if mode == "mono" || mode == "orig" {
        return mode.into();
    }
    if (logo.lum - bg_lum).abs() < 70.0 {
        "mono"
    } else {
        "orig"
    }
    .into()
}

/// Port of gxr_studio/finish/logo_math.py:102 clogo_item
#[allow(clippy::too_many_arguments)]
pub fn clogo_item(
    logo: &LogoInfo,
    x: i64,
    y: i64,
    w: i64,
    h: i64,
    mode: &str,
    ink: [i64; 3],
    bg_lum: f64,
) -> Val {
    obj(vec![
        ("k", text("clogo")),
        ("id", text(&logo.id)),
        ("x", int(x)),
        ("y", int(y)),
        ("w", int(w)),
        ("h", int(h)),
        ("mode", text(&pick_logo_mode(logo, mode, bg_lum))),
        ("ink", rgb(ink)),
        ("auto", Val::Bool(mode == "auto" && !logo.opaque)),
    ])
}
