//! Separable filters matching scipy.ndimage on the HDR path.
//!
//! `gaussian_filter`, `uniform_filter` and `minimum_filter` all use `mode="reflect"`
//! and `origin=0`. An even window is not centred: it starts at `i - size // 2`
//! and looks toward the lower indices (PORT-NOTES §3). Axes run in order 0, then 1.
//! A float64 kernel is built the way `_gaussian_kernel1d` does (`arange` then `exp`),
//! then cast to the array dtype. The correlate accumulates in that dtype.

use gxr_math::exp;
use gxr_raw::CoreError;

use crate::error::container;
use crate::extend::reflect;

/// One plane, row-major. `f32` is stored as bits so NaN survives equality checks in tests.
#[derive(Clone, Debug)]
pub enum Plane {
    F64(Vec<f64>),
    F32(Vec<f32>),
    U8(Vec<u8>),
}

impl Plane {
    pub fn shape_ok(h: usize, w: usize, len: usize) -> Result<(), CoreError> {
        let n = h
            .checked_mul(w)
            .ok_or_else(|| container("数组太大"))?;
        if n != len {
            return Err(container("数组长度和形状对不上"));
        }
        Ok(())
    }
}

fn radius_of(sigma: f64) -> Result<i64, CoreError> {
    if !(sigma.is_finite() && sigma > 0.0) {
        return Err(container("sigma 必须是正的有限数"));
    }
    // `int(truncate * float(sigma) + 0.5)` with truncate 4. Truncation is toward zero.
    let raw = 4.0 * sigma + 0.5;
    if !raw.is_finite() || raw < 0.0 || raw >= i64::MAX as f64 {
        return Err(container("sigma 太大"));
    }
    Ok(raw.floor() as i64)
}

fn gaussian_kernel(sigma: f64) -> Result<Vec<f64>, CoreError> {
    let radius = radius_of(sigma)?;
    let sigma2 = sigma * sigma;
    let mut phi = Vec::new();
    let mut sum = 0.0;
    let mut x = -radius;
    while x <= radius {
        let xx = (x as f64) * (x as f64);
        let w = exp(-0.5 / sigma2 * xx);
        phi.push(w);
        sum += w;
        x += 1;
    }
    if sum == 0.0 || !sum.is_finite() {
        return Err(container("高斯核不是有限数"));
    }
    for w in &mut phi {
        *w /= sum;
    }
    Ok(phi)
}

fn idx(x: usize, y: usize, w: usize) -> usize {
    x * w + y
}

/// Correlate one axis with `kernel` (already in the accumulation dtype's wider form).
fn correlate_f64(src: &[f64], h: usize, w: usize, axis: usize, kernel: &[f64]) -> Result<Vec<f64>, CoreError> {
    let n = if axis == 0 { h } else { w };
    let radius = (kernel.len() / 2) as i64;
    let mut out = vec![0.0; src.len()];
    for r in 0..h {
        for c in 0..w {
            let mut acc = 0.0;
            for (k, &weight) in kernel.iter().enumerate() {
                let at = (k as i64) - radius;
                let coord = if axis == 0 { r as i64 } else { c as i64 };
                let src_i = reflect(coord + at, n as i64)?;
                let (rr, cc) = if axis == 0 {
                    (src_i as usize, c)
                } else {
                    (r, src_i as usize)
                };
                acc += src[idx(rr, cc, w)] * weight;
            }
            out[idx(r, c, w)] = acc;
        }
    }
    Ok(out)
}

fn correlate_f32(src: &[f32], h: usize, w: usize, axis: usize, kernel: &[f64]) -> Result<Vec<f32>, CoreError> {
    let n = if axis == 0 { h } else { w };
    let radius = (kernel.len() / 2) as i64;
    let mut out = vec![0.0; src.len()];
    for r in 0..h {
        for c in 0..w {
            let mut acc = 0.0f32;
            for (k, &weight) in kernel.iter().enumerate() {
                let at = (k as i64) - radius;
                let coord = if axis == 0 { r as i64 } else { c as i64 };
                let src_i = reflect(coord + at, n as i64)?;
                let (rr, cc) = if axis == 0 {
                    (src_i as usize, c)
                } else {
                    (r, src_i as usize)
                };
                acc += src[idx(rr, cc, w)] * (weight as f32);
            }
            out[idx(r, c, w)] = acc;
        }
    }
    Ok(out)
}

/// Port of the `ndimage.gaussian_filter` calls in `gxr_studio/hdr/gain.py`.
///
/// `order=0`, `mode="reflect"`, `truncate=4`. Output dtype follows the input.
pub fn gaussian_filter(plane: &Plane, h: usize, w: usize, sigma: f64) -> Result<Plane, CoreError> {
    Plane::shape_ok(h, w, match plane {
        Plane::F64(v) => v.len(),
        Plane::F32(v) => v.len(),
        Plane::U8(_) => return Err(container("gaussian 只接受浮点")),
    })?;
    let kernel = gaussian_kernel(sigma)?;
    match plane {
        Plane::F64(v) => {
            let a0 = correlate_f64(v, h, w, 0, &kernel)?;
            Ok(Plane::F64(correlate_f64(&a0, h, w, 1, &kernel)?))
        }
        Plane::F32(v) => {
            let a0 = correlate_f32(v, h, w, 0, &kernel)?;
            Ok(Plane::F32(correlate_f32(&a0, h, w, 1, &kernel)?))
        }
        Plane::U8(_) => Err(container("gaussian 只接受浮点")),
    }
}

fn window_start(index: i64, size: i64) -> i64 {
    // origin 0: the window begins `size // 2` samples before the output.
    index - size / 2
}

fn filter_uniform_f64(src: &[f64], h: usize, w: usize, axis: usize, size: i64) -> Result<Vec<f64>, CoreError> {
    let n = if axis == 0 { h } else { w };
    let mut out = vec![0.0; src.len()];
    let denom = size as f64;
    for r in 0..h {
        for c in 0..w {
            let origin = if axis == 0 { r as i64 } else { c as i64 };
            let start = window_start(origin, size);
            let mut acc = 0.0;
            for k in 0..size {
                let src_i = reflect(start + k, n as i64)?;
                let (rr, cc) = if axis == 0 {
                    (src_i as usize, c)
                } else {
                    (r, src_i as usize)
                };
                acc += src[idx(rr, cc, w)];
            }
            out[idx(r, c, w)] = acc / denom;
        }
    }
    Ok(out)
}

fn less_f64(a: f64, b: f64) -> bool {
    a < b
}

fn filter_minimum<T, F>(src: &[T], h: usize, w: usize, axis: usize, size: i64, less: F) -> Result<Vec<T>, CoreError>
where
    T: Copy,
    F: Fn(T, T) -> bool,
{
    let n = if axis == 0 { h } else { w };
    let mut out = vec![src[0]; src.len()];
    if src.is_empty() {
        return Ok(out);
    }
    for r in 0..h {
        for c in 0..w {
            let origin = if axis == 0 { r as i64 } else { c as i64 };
            let start = window_start(origin, size);
            let mut acc = {
                let src_i = reflect(start, n as i64)?;
                let (rr, cc) = if axis == 0 {
                    (src_i as usize, c)
                } else {
                    (r, src_i as usize)
                };
                src[idx(rr, cc, w)]
            };
            for k in 1..size {
                let src_i = reflect(start + k, n as i64)?;
                let (rr, cc) = if axis == 0 {
                    (src_i as usize, c)
                } else {
                    (r, src_i as usize)
                };
                let v = src[idx(rr, cc, w)];
                if less(v, acc) {
                    acc = v;
                }
            }
            out[idx(r, c, w)] = acc;
        }
    }
    Ok(out)
}

fn check_size(size: i64) -> Result<(), CoreError> {
    if size < 1 {
        return Err(container("滤波器尺寸至少是 1"));
    }
    if size > 4096 {
        return Err(container("滤波器尺寸太大"));
    }
    Ok(())
}

/// Port of `ndimage.uniform_filter(..., mode="reflect", origin=0)`.
pub fn uniform_filter(plane: &Plane, h: usize, w: usize, size: i64) -> Result<Plane, CoreError> {
    check_size(size)?;
    match plane {
        Plane::F64(v) => {
            Plane::shape_ok(h, w, v.len())?;
            let a0 = filter_uniform_f64(v, h, w, 0, size)?;
            Ok(Plane::F64(filter_uniform_f64(&a0, h, w, 1, size)?))
        }
        Plane::F32(v) => {
            Plane::shape_ok(h, w, v.len())?;
            let wide: Vec<f64> = v.iter().map(|x| f64::from(*x)).collect();
            let a0 = filter_uniform_f64(&wide, h, w, 0, size)?;
            let a1 = filter_uniform_f64(&a0, h, w, 1, size)?;
            // scipy keeps the input dtype. float32 division is not this path's call
            // (`_local_residual` filters float64). Cast back only when asked.
            Ok(Plane::F32(a1.iter().map(|x| *x as f32).collect()))
        }
        Plane::U8(_) => Err(container("uniform 只接受浮点")),
    }
}

/// Port of `ndimage.minimum_filter(..., mode="reflect", origin=0)`.
pub fn minimum_filter(plane: &Plane, h: usize, w: usize, size: i64) -> Result<Plane, CoreError> {
    check_size(size)?;
    match plane {
        Plane::F64(v) => {
            Plane::shape_ok(h, w, v.len())?;
            if v.is_empty() {
                return Ok(Plane::F64(vec![]));
            }
            let a0 = filter_minimum(v, h, w, 0, size, less_f64)?;
            Ok(Plane::F64(filter_minimum(&a0, h, w, 1, size, less_f64)?))
        }
        Plane::U8(v) => {
            Plane::shape_ok(h, w, v.len())?;
            if v.is_empty() {
                return Ok(Plane::U8(vec![]));
            }
            let a0 = filter_minimum(v, h, w, 0, size, |a, b| a < b)?;
            Ok(Plane::U8(filter_minimum(&a0, h, w, 1, size, |a, b| a < b)?))
        }
        Plane::F32(v) => {
            Plane::shape_ok(h, w, v.len())?;
            if v.is_empty() {
                return Ok(Plane::F32(vec![]));
            }
            let less = |a: f32, b: f32| a < b;
            let a0 = filter_minimum(v, h, w, 0, size, less)?;
            Ok(Plane::F32(filter_minimum(&a0, h, w, 1, size, less)?))
        }
    }
}
