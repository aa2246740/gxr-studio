//! scipy.ndimage `gaussian_filter`, `uniform_filter` and `minimum_filter` on 2-D planes.
//!
//! All three are separable: axis 0, then axis 1, with the first pass written to the output
//! dtype before the second pass reads it. `mode="reflect"`, `origin=0`, so an even window
//! covers `[i - size/2, i - size/2 + size - 1]` and leans toward the lower indices.
//!
//! Each line is copied into a float64 buffer (`NI_LineBuffer`), filtered in float64 and cast
//! back to the array dtype, as `ni_filters.c` does:
//!
//! - Gaussian: the kernel is `_gaussian_kernel1d` (`exp(-0.5 / sigma**2 * x**2)` over a
//!   numpy-summed total). The kernel is symmetric, so `NI_Correlate1D` computes
//!   `t = in[0] * w[0]`, then `t += (in[-j] + in[j]) * w[-j]` from the outside in. scipy's
//!   arm64 build fuses that update for short kernels. This port does not (CONVENTIONS §4),
//!   so float64 results may differ in the last bit; float32 results only when that bit
//!   crosses a float32 rounding boundary.
//! - Uniform: a running window sum `s += in[i + size - 1] - in[i - 1]`, written as `s / size`.
//! - Minimum: the smallest value in the window; exact.

use gxr_math::exp;
use gxr_raw::CoreError;

use crate::error::unsupported;
use crate::extend::reflect;
use crate::np::{Real, count_f64, sum};

/// Largest Gaussian radius and window this port accepts (scipy has no limit; a larger
/// window only repeats the reflected line).
const MAX_RADIUS: i64 = 4096;

fn bad(message: &str) -> CoreError {
    unsupported("bad_input", message)
}

/// Check that `values` holds an `h × w` plane.
pub fn check_shape(h: usize, w: usize, len: usize) -> Result<(), CoreError> {
    if crate::size::count(&[crate::size::dim(h), crate::size::dim(w)])? == len {
        Ok(())
    } else {
        Err(bad("数组长度和形状对不上"))
    }
}

/// Run `line` over every line of axis `axis`, reading float64 and writing `T`.
fn per_line<T: Real>(
    src: &[T],
    h: usize,
    w: usize,
    axis: usize,
    mut line: impl FnMut(&[f64], &mut [f64]),
) -> Vec<T> {
    let (n, lines) = if axis == 0 { (h, w) } else { (w, h) };
    let mut out = src.to_vec();
    // `n` is one axis of a plane already in memory.
    let mut buf = vec![0.0; n];
    let mut res = vec![0.0; n];
    for l in 0..lines {
        for (i, slot) in buf.iter_mut().enumerate() {
            let at = if axis == 0 { i * w + l } else { l * w + i };
            *slot = src[at].wide();
        }
        line(&buf, &mut res);
        for (i, &v) in res.iter().enumerate() {
            let at = if axis == 0 { i * w + l } else { l * w + i };
            out[at] = T::narrow(v);
        }
    }
    out
}

fn to_i64(v: usize) -> i64 {
    i64::try_from(v).unwrap_or(i64::MAX)
}

fn ext(line: &[f64], index: i64) -> f64 {
    // `reflect` stays inside `0 .. n`, so the index is valid.
    let i = usize::try_from(reflect(index, to_i64(line.len()))).unwrap_or(0);
    line[i]
}

/// Port of scipy `_gaussian_kernel1d(sigma, 0, radius)` with
/// `radius = int(truncate * sigma + 0.5)` and `truncate = 4`.
pub fn gaussian_kernel(sigma: f64) -> Result<Vec<f64>, CoreError> {
    let raw = 4.0 * sigma + 0.5;
    if !raw.is_finite() {
        return Err(CoreError::new(
            "overflow_error",
            "cannot convert float infinity to integer",
        ));
    }
    if raw >= (MAX_RADIUS + 1) as f64 {
        return Err(bad("高斯半径太大"));
    }
    // `int()` truncates toward zero; `raw` is positive here because sigma > 1e-15.
    #[allow(clippy::cast_possible_truncation)]
    let radius = raw.trunc() as i64;
    let sigma2 = sigma * sigma;
    let factor = -0.5 / sigma2;
    let phi: Vec<f64> = (-radius..=radius)
        .map(|x| {
            #[allow(clippy::cast_precision_loss)]
            let xx = (x * x) as f64;
            exp(factor * xx)
        })
        .collect();
    let total = sum(&phi);
    Ok(phi.iter().map(|v| v / total).collect())
}

/// Port of `ndimage.gaussian_filter(plane, sigma)` (all other arguments default).
///
/// scipy skips an axis whose sigma is not above `1e-15`; then the plane is copied.
pub fn gaussian_filter<T: Real>(
    src: &[T],
    h: usize,
    w: usize,
    sigma: f64,
) -> Result<Vec<T>, CoreError> {
    check_shape(h, w, src.len())?;
    if sigma.is_nan() || sigma <= 1e-15 {
        return Ok(src.to_vec());
    }
    let k = gaussian_kernel(sigma)?;
    let r = k.len() / 2;
    let run = |line: &[f64], out: &mut [f64]| {
        for (i, slot) in out.iter_mut().enumerate() {
            let c = to_i64(i);
            let mut t = line[i] * k[r];
            for j in (1..=r).rev() {
                let jj = to_i64(j);
                t += (ext(line, c - jj) + ext(line, c + jj)) * k[r - j];
            }
            *slot = t;
        }
    };
    let first = per_line(src, h, w, 0, run);
    Ok(per_line(&first, h, w, 1, run))
}

/// The 2-D wrappers skip an axis whose size is not above 1, so `size <= 1` copies.
fn check_size(size: i64) -> Result<usize, CoreError> {
    if size <= 1 {
        return Ok(1);
    }
    if size > MAX_RADIUS {
        return Err(bad("滤波器尺寸太大"));
    }
    usize::try_from(size).map_err(|_| bad("滤波器尺寸太大"))
}

/// Port of `ndimage.uniform_filter(plane, size, mode="reflect")`. Size <= 1 copies.
pub fn uniform_filter<T: Real>(
    src: &[T],
    h: usize,
    w: usize,
    size: i64,
) -> Result<Vec<T>, CoreError> {
    check_shape(h, w, src.len())?;
    let size_u = check_size(size)?;
    if size_u == 1 {
        return Ok(src.to_vec());
    }
    let start = -(size / 2);
    let denom = count_f64(size_u);
    let run = |line: &[f64], out: &mut [f64]| {
        let mut s = 0.0;
        for k in 0..size {
            s += ext(line, start + k);
        }
        out[0] = s / denom;
        for (i, slot) in out.iter_mut().enumerate().skip(1) {
            let c = to_i64(i);
            s += ext(line, start + c + size - 1) - ext(line, start + c - 1);
            *slot = s / denom;
        }
    };
    let first = per_line(src, h, w, 0, run);
    Ok(per_line(&first, h, w, 1, run))
}

fn min_axis<T: Copy + PartialOrd>(src: &[T], h: usize, w: usize, axis: usize, size: i64) -> Vec<T> {
    let (n, lines) = if axis == 0 { (h, w) } else { (w, h) };
    let start = -(size / 2);
    let nn = to_i64(n);
    let mut out = src.to_vec();
    for l in 0..lines {
        let at = |i: i64| {
            let i = usize::try_from(reflect(i, nn)).unwrap_or(0);
            if axis == 0 { i * w + l } else { l * w + i }
        };
        for i in 0..n {
            let c = to_i64(i);
            let mut best = src[at(c + start)];
            for k in 1..size {
                let v = src[at(c + start + k)];
                if v < best {
                    best = v;
                }
            }
            out[if axis == 0 { i * w + l } else { l * w + i }] = best;
        }
    }
    out
}

/// Port of `ndimage.minimum_filter(plane, size)`, `mode="reflect"`. Size <= 1 copies.
pub fn minimum_filter<T: Copy + PartialOrd>(
    src: &[T],
    h: usize,
    w: usize,
    size: i64,
) -> Result<Vec<T>, CoreError> {
    check_shape(h, w, src.len())?;
    if check_size(size)? == 1 || src.is_empty() {
        return Ok(src.to_vec());
    }
    let first = min_axis(src, h, w, 0, size);
    Ok(min_axis(&first, h, w, 1, size))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn even_uniform_window_leans_low() {
        let a: Vec<f64> = (0..16).map(f64::from).collect();
        let u = uniform_filter(&a, 4, 4, 4).unwrap();
        assert_eq!(u[0], 2.5);
        let m = minimum_filter(&a, 4, 4, 2).unwrap();
        assert_eq!(&m[..4], &[0.0, 0.0, 1.0, 2.0]);
    }

    #[test]
    fn gaussian_radius_and_centre() {
        let k = gaussian_kernel(0.6).unwrap();
        assert_eq!(k.len(), 5);
        assert_eq!(k[2], 0.6638183293849666);
        assert_eq!(gaussian_kernel(8.0).unwrap().len(), 65);
    }
}
