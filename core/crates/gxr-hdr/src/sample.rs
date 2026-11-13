//! `ndimage.map_coordinates(plane, [sy, sx], order=1, mode="nearest")`.
//!
//! Order 1 skips the spline prefilter (`prefilter and order > 1`). For each output point
//! (`NI_GeometricTransform`): clamp each coordinate into `[0, n - 1]`, take
//! `start = floor(c)` and the weights `[1 - t, t]` with `t = c - start`, then add the four
//! neighbours in C order, each as `(value * wy) * wx`, starting from `0.0`. Neighbour indices
//! past the edge are clamped. The sum is float64 and is cast to the plane's dtype.
//! This order was checked against scipy 1.17.1 on random planes.

use gxr_raw::CoreError;

use crate::error::unsupported;
use crate::extend::nearest;
use crate::filter::check_shape;
use crate::np::Real;

fn axis_weights(c: f64, n: usize) -> (i64, [f64; 2]) {
    let top = i64::try_from(n).unwrap_or(i64::MAX) - 1;
    #[allow(clippy::cast_precision_loss)]
    let hi = top as f64;
    let c = if c < 0.0 {
        0.0
    } else if c > hi {
        hi
    } else {
        c
    };
    let f = c.floor();
    #[allow(clippy::cast_possible_truncation)]
    let start = if f.is_finite() { f as i64 } else { 0 };
    #[allow(clippy::cast_precision_loss)]
    let t = c - start as f64;
    (start, [1.0 - t, t])
}

/// Port of the `map_coordinates` call in gxr_studio/hdr/gain.py:174 `warp`.
pub fn map_coordinates<T: Real>(
    src: &[T],
    h: usize,
    w: usize,
    sy: &[f32],
    sx: &[f32],
) -> Result<Vec<T>, CoreError> {
    check_shape(h, w, src.len())?;
    if sy.len() != sx.len() {
        return Err(unsupported("bad_input", "坐标数组长度不一致"));
    }
    if h == 0 || w == 0 {
        return Err(unsupported("bad_input", "无法在空图上取样"));
    }
    let (hh, ww) = (i64::try_from(h).unwrap_or(i64::MAX), i64::try_from(w).unwrap_or(i64::MAX));
    let mut out = Vec::with_capacity(sy.len());
    for (&y, &x) in sy.iter().zip(sx) {
        let (y0, wy) = axis_weights(f64::from(y), h);
        let (x0, wx) = axis_weights(f64::from(x), w);
        let mut acc = 0.0f64;
        for (i, &fy) in wy.iter().enumerate() {
            let row = usize::try_from(nearest(y0 + i64::try_from(i).unwrap_or(0), hh)).unwrap_or(0);
            for (j, &fx) in wx.iter().enumerate() {
                let col = usize::try_from(nearest(x0 + i64::try_from(j).unwrap_or(0), ww)).unwrap_or(0);
                let mut coeff = src[row * w + col].wide();
                coeff *= fy;
                coeff *= fx;
                acc += coeff;
            }
        }
        out.push(T::narrow(acc));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn half_way_is_the_mean() {
        let a = [1.0f32, 2.0, 3.0, 4.0];
        let got = map_coordinates(&a, 2, 2, &[0.0, 0.5], &[0.0, 0.5]).unwrap();
        assert_eq!(got, vec![1.0, 2.5]);
    }
}
