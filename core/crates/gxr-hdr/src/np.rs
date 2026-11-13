//! The numpy behaviour `hdr/gain.py` depends on, measured against numpy 2.4.3 on this Mac.
//!
//! - `add.reduce` of a C-contiguous array starts from the identity `0` and adds one
//!   `pairwise_sum` (`loops_utils.h.src`) over every element. There is no 8192 chunking.
//! - A strided 2-D view is copied into a buffer of `max(1, 8192 / w)` whole rows. Each buffer
//!   is one pairwise sum, and the buffer sums are added in order, starting from `0`.
//! - `mean` and `var` divide by `np.intp(n)`, so a float32 mean is `f32(f64(sum) / f64(n))`.
//! - A Python float next to a float32 array is cast to float32 first (NEP 50), comparisons
//!   included. Call sites spell that cast out.

use std::ops::{Add, Div, Mul, Sub};

use gxr_raw::CoreError;

use crate::error::unsupported;

/// numpy's reduction buffer size (`NPY_BUFSIZE`).
const BUFSIZE: usize = 8192;
/// `PW_BLOCKSIZE` in `loops_utils.h.src`.
const PW_BLOCKSIZE: usize = 128;

/// float32 or float64, with numpy's conversions.
pub trait Real:
    Copy
    + PartialOrd
    + Add<Output = Self>
    + Sub<Output = Self>
    + Mul<Output = Self>
    + Div<Output = Self>
{
    const ZERO: Self;
    const NEG_ZERO: Self;
    fn wide(self) -> f64;
    /// Round a float64 to this dtype (`astype`, or an unsafe-cast `out=`).
    fn narrow(v: f64) -> Self;
    fn nan(self) -> bool;
    fn root(self) -> Self;
}

impl Real for f32 {
    const ZERO: Self = 0.0;
    const NEG_ZERO: Self = -0.0;
    fn wide(self) -> f64 {
        f64::from(self)
    }
    #[allow(clippy::cast_possible_truncation)]
    fn narrow(v: f64) -> Self {
        // IEEE round to nearest even, as numpy's float64 → float32 cast.
        v as f32
    }
    fn nan(self) -> bool {
        self.is_nan()
    }
    fn root(self) -> Self {
        self.sqrt()
    }
}

impl Real for f64 {
    const ZERO: Self = 0.0;
    const NEG_ZERO: Self = -0.0;
    fn wide(self) -> f64 {
        self
    }
    fn narrow(v: f64) -> Self {
        v
    }
    fn nan(self) -> bool {
        self.is_nan()
    }
    fn root(self) -> Self {
        self.sqrt()
    }
}

/// An element count as numpy's `np.intp` → float64 conversion sees it.
#[allow(clippy::cast_precision_loss)]
pub fn count_f64(n: usize) -> f64 {
    // Exact below 2**53; every array here is far smaller.
    n as f64
}

/// numpy `@TYPE@_pairwise_sum`.
pub fn pairwise<T: Real>(a: &[T]) -> T {
    let n = a.len();
    if n < 8 {
        let mut res = T::NEG_ZERO;
        for &v in a {
            res = res + v;
        }
        res
    } else if n <= PW_BLOCKSIZE {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - n % 8 {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot = *slot + a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res = res + a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise(&a[..n2]) + pairwise(&a[n2..])
    }
}

/// `add.reduce` of a C-contiguous array (any shape).
pub fn sum<T: Real>(a: &[T]) -> T {
    T::ZERO + pairwise(a)
}

/// A rectangle `[y0, y0+h) × [x0, x0+w)` inside a row-major plane of width `stride`.
#[derive(Clone, Copy, Debug)]
pub struct View {
    pub y0: usize,
    pub x0: usize,
    pub h: usize,
    pub w: usize,
    pub stride: usize,
}

impl View {
    pub fn len(&self) -> usize {
        self.h * self.w
    }

    /// The view copied out in C order.
    pub fn copy<T: Copy>(&self, src: &[T]) -> Vec<T> {
        let mut out = Vec::with_capacity(self.len());
        for r in 0..self.h {
            let at = (self.y0 + r) * self.stride + self.x0;
            out.extend_from_slice(&src[at..at + self.w]);
        }
        out
    }
}

/// `add.reduce` over a strided 2-D view: buffered whole rows, see the module comment.
pub fn sum_view<T: Real>(src: &[T], v: View) -> T {
    if v.h == 0 || v.w == 0 {
        return T::ZERO;
    }
    if v.w == v.stride || v.h == 1 {
        // The iterator coalesces a view whose rows are adjacent into one inner loop.
        let at = v.y0 * v.stride + v.x0;
        return sum(&src[at..at + (v.h - 1) * v.stride + v.w]);
    }
    let rows = (BUFSIZE / v.w).max(1);
    let mut acc = T::ZERO;
    let mut buf = Vec::with_capacity(rows * v.w);
    let mut r = 0;
    while r < v.h {
        buf.clear();
        for k in r..(r + rows).min(v.h) {
            let at = (v.y0 + k) * v.stride + v.x0;
            buf.extend_from_slice(&src[at..at + v.w]);
        }
        acc = acc + pairwise(&buf);
        r += rows;
    }
    acc
}

/// `np.mean` from its sum: `dtype(f64(sum) / intp(n))`.
pub fn mean_of<T: Real>(total: T, n: usize) -> T {
    T::narrow(total.wide() / count_f64(n))
}

/// `a.mean()` of a contiguous array.
pub fn mean<T: Real>(a: &[T]) -> T {
    mean_of(sum(a), a.len())
}

/// `a.mean()` of a view.
pub fn mean_view<T: Real>(src: &[T], v: View) -> T {
    mean_of(sum_view(src, v), v.len())
}

/// `a.std()` (`ddof=0`) given the view's mean; the squared deviations are a new C array.
fn std_from<T: Real>(values: &[T], m: T) -> T {
    let sq: Vec<T> = values
        .iter()
        .map(|&x| {
            let d = x - m;
            d * d
        })
        .collect();
    mean_of(sum(&sq), values.len()).root()
}

/// `a.std()` of a contiguous array.
pub fn std<T: Real>(a: &[T]) -> T {
    std_from(a, mean(a))
}

/// `a.std()` of a view.
pub fn std_view<T: Real>(src: &[T], v: View) -> T {
    std_from(&v.copy(src), mean_view(src, v))
}

/// `np.maximum(a, s)` for one element: NaN wins.
pub fn maximum<T: Real>(a: T, s: T) -> T {
    if a.nan() || s.nan() {
        if a.nan() { a } else { s }
    } else if a >= s {
        a
    } else {
        s
    }
}

/// `np.minimum(a, s)` for one element: NaN wins.
pub fn minimum<T: Real>(a: T, s: T) -> T {
    if a.nan() || s.nan() {
        if a.nan() { a } else { s }
    } else if a <= s {
        a
    } else {
        s
    }
}

/// `np.clip(a, lo, hi)` for one element (`minimum(maximum(a, lo), hi)`).
pub fn clip<T: Real>(a: T, lo: T, hi: T) -> T {
    minimum(maximum(a, lo), hi)
}

/// `np.max` of a non-empty array (NaN wins).
pub fn max_of<T: Real>(a: &[T]) -> Option<T> {
    let mut it = a.iter();
    let mut best = *it.next()?;
    for &v in it {
        if best.nan() {
            break;
        }
        best = maximum(best, v);
    }
    Some(best)
}

fn sorted<T: Real>(a: &[T]) -> Vec<T> {
    let mut s = a.to_vec();
    s.sort_by(|x, y| x.partial_cmp(y).unwrap_or(std::cmp::Ordering::Equal));
    s
}

/// `np.percentile(a, q)` with the default linear method and a Python-float `q` (weak).
///
/// `gamma` is a Python float, so `diff * gamma` casts it to the array dtype.
pub fn percentile<T: Real>(a: &[T], q: f64) -> T {
    if a.iter().any(|v| v.nan()) {
        return a.iter().copied().find(|v| v.nan()).unwrap_or(T::ZERO);
    }
    let s = sorted(a);
    let n = s.len();
    let virtual_index = count_f64(n - 1) * (q / 100.0);
    let (prev, next) = if virtual_index >= count_f64(n - 1) {
        (n - 1, n - 1)
    } else if virtual_index < 0.0 {
        (0, 0)
    } else {
        let p = floor_index(virtual_index);
        (p, p + 1)
    };
    let gamma = virtual_index - count_f64(prev);
    let (lo, hi) = (s[prev], s[next]);
    let diff = hi - lo;
    if gamma >= 0.5 {
        hi - diff * T::narrow(1.0 - gamma)
    } else {
        lo + diff * T::narrow(gamma)
    }
}

#[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
fn floor_index(v: f64) -> usize {
    // `v` is a finite, non-negative index below the array length.
    v.floor() as usize
}

/// `np.median(a)`: the middle value, or the mean of the two middle values.
pub fn median<T: Real>(a: &[T]) -> T {
    if let Some(&bad) = a.iter().find(|v| v.nan()) {
        return bad;
    }
    let s = sorted(a);
    let n = s.len();
    if n == 0 {
        return T::narrow(f64::NAN);
    }
    if n % 2 == 1 {
        mean_of(T::ZERO + s[n / 2], 1)
    } else {
        mean_of(T::ZERO + ((T::NEG_ZERO + s[n / 2 - 1]) + s[n / 2]), 2)
    }
}

/// `np.interp(x, xp, fp, left, right)` without the compiler's fused multiply-add.
pub fn interp(x: f64, xp: &[f64], fp: &[f64], left: f64, right: f64) -> f64 {
    let n = xp.len();
    if x.is_nan() {
        return x;
    }
    if n == 1 {
        return if x < xp[0] {
            left
        } else if x > xp[0] {
            right
        } else {
            fp[0]
        };
    }
    if x < xp[0] {
        return left;
    }
    if x > xp[n - 1] {
        return right;
    }
    let j = xp.partition_point(|&e| e <= x) - 1;
    if j == n - 1 || xp[j] == x {
        return fp[j];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut res = slope * (x - xp[j]) + fp[j];
    if res.is_nan() {
        res = slope * (x - xp[j + 1]) + fp[j + 1];
        if res.is_nan() && fp[j] == fp[j + 1] {
            res = fp[j];
        }
    }
    res
}

/// `np.arange(lo, stop, step)` with float32 `lo` and `stop` and a Python-float `step`.
///
/// The length is `ceil(f32(stop - lo) / f32(step))`. The array is float64. Its second
/// element is the float32 sum `lo + step`, and the rest are `start + i * delta`.
pub fn arange_f32(lo: f32, stop: f32, step: f64) -> Result<Vec<f64>, CoreError> {
    let step32 = f32::narrow(step);
    let length = f64::from((stop - lo) / step32).ceil();
    if length.is_nan() || length > 10_000_000.0 {
        return Err(unsupported("no_curve", "亮度范围太窄，拟合不出曲线"));
    }
    if length <= 0.0 {
        return Ok(Vec::new());
    }
    let n = floor_index(length);
    let start = f64::from(lo);
    let second = f64::from(lo + step32);
    let delta = second - start;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        out.push(match i {
            0 => start,
            1 => second,
            _ => start + count_f64(i) * delta,
        });
    }
    Ok(out)
}

/// `np.digitize(x, edges)` for increasing edges: the number of edges `<= x`.
pub fn digitize(x: f64, edges: &[f64]) -> usize {
    if x.is_nan() {
        return edges.len();
    }
    edges.partition_point(|&e| e <= x)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pairwise_matches_numpy_layout() {
        let a: Vec<f32> = (0..300).map(|i| (i as f32) * 0.1).collect();
        // Split 144 / 156 at the top level; just check it is a sum.
        assert!((sum(&a) - 4485.0).abs() < 0.01);
        assert_eq!(sum::<f64>(&[]), 0.0);
    }

    #[test]
    fn percentile_lerp_and_bounds() {
        let a = [1.0f64, 2.0, 3.0, 4.0];
        assert_eq!(percentile(&a, 0.5), 1.015);
        assert_eq!(percentile(&a, 100.0), 4.0);
    }

    #[test]
    fn interp_edges() {
        let xp = [0.0, 1.0, 2.0];
        let fp = [0.0, 10.0, 30.0];
        assert_eq!(interp(-1.0, &xp, &fp, -5.0, 99.0), -5.0);
        assert_eq!(interp(2.0, &xp, &fp, -5.0, 99.0), 30.0);
        assert_eq!(interp(1.5, &xp, &fp, -5.0, 99.0), 20.0);
    }
}
