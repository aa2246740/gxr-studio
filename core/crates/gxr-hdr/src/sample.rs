//! `ndimage.map_coordinates` for `order=1`, `mode="nearest"`, `prefilter=False`.
//!
//! Order 1 does not prefilter (`prefilter and order > 1` is false). Coordinates are
//! interpolated in float64 after the float32 values Python built (casting float32
//! to float64 is exact). Each sample index is clamped into the array. The output
//! dtype matches the input: the weights are applied in float64 and then cast.

use gxr_raw::CoreError;

use crate::error::container;
use crate::extend::nearest;
use crate::filter::Plane;

fn shape_of(h: usize, w: usize, len: usize) -> Result<(), CoreError> {
    Plane::shape_ok(h, w, len)
}

fn floor_toward_neg(v: f64) -> i64 {
    let f = v.floor();
    if !f.is_finite() || f < i64::MIN as f64 || f > i64::MAX as f64 {
        if v.is_sign_negative() {
            i64::MIN
        } else {
            i64::MAX
        }
    } else {
        f as i64
    }
}

fn bilinear_at(src_f64: &[f64], src_f32: &[f32], h: usize, w: usize, y: f64, x: f64) -> f64 {
    let y0 = floor_toward_neg(y);
    let x0 = floor_toward_neg(x);
    let fy = y - y0 as f64;
    let fx = x - x0 as f64;
    let sample = |yy: i64, xx: i64| -> f64 {
        let rr = nearest(yy, h as i64) as usize;
        let cc = nearest(xx, w as i64) as usize;
        let i = rr * w + cc;
        if src_f64.is_empty() { f64::from(src_f32[i]) } else { src_f64[i] }
    };
    let v00 = sample(y0, x0);
    let v01 = sample(y0, x0 + 1);
    let v10 = sample(y0 + 1, x0);
    let v11 = sample(y0 + 1, x0 + 1);
    let v0 = (1.0 - fx) * v00 + fx * v01;
    let v1 = (1.0 - fx) * v10 + fx * v11;
    (1.0 - fy) * v0 + fy * v1
}

/// Port of `ndimage.map_coordinates(values, [sy, sx], order=1, mode="nearest")`.
pub fn map_coordinates(
    plane: &Plane,
    h: usize,
    w: usize,
    sy: &[f32],
    sx: &[f32],
    out_h: usize,
    out_w: usize,
) -> Result<Plane, CoreError> {
    shape_of(out_h, out_w, sy.len())?;
    shape_of(out_h, out_w, sx.len())?;
    match plane {
        Plane::F64(v) => {
            shape_of(h, w, v.len())?;
            let mut out = Vec::with_capacity(sy.len());
            for i in 0..sy.len() {
                out.push(bilinear_at(v, &[], h, w, f64::from(sy[i]), f64::from(sx[i])));
            }
            Ok(Plane::F64(out))
        }
        Plane::F32(v) => {
            shape_of(h, w, v.len())?;
            let mut out = Vec::with_capacity(sy.len());
            for i in 0..sy.len() {
                let y = bilinear_at(&[], v, h, w, f64::from(sy[i]), f64::from(sx[i]));
                out.push(y as f32);
            }
            Ok(Plane::F32(out))
        }
        Plane::U8(_) => Err(container("map_coordinates 只接受浮点")),
    }
}
