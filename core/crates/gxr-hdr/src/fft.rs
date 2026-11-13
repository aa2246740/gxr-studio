//! Complex FFT used by the phase-correlation shift in `gain.py`.
//!
//! Forward transform uses the numpy sign, `exp(-2πi kn / N)`. A power of two
//! goes through radix-2. Any other length uses Bluestein's chirp, convolved
//! with that radix-2 transform. Products are written out as
//! `(ar*br - ai*bi, ar*bi + ai*br)` with no fused multiply-add.
//!
//! The shift search only needs the peak location and, after the ±1 check, the
//! correlation of the registered planes. This transform is still compared
//! against `np.fft` on the sizes the alignment actually builds.

use gxr_math::{cos, sin};
use gxr_raw::CoreError;

use crate::error::container;

#[derive(Clone, Copy)]
struct Cx {
    re: f64,
    im: f64,
}

fn mul(a: Cx, b: Cx) -> Cx {
    Cx {
        re: a.re * b.re - a.im * b.im,
        im: a.re * b.im + a.im * b.re,
    }
}

fn scale(a: Cx, s: f64) -> Cx {
    Cx {
        re: a.re * s,
        im: a.im * s,
    }
}

const PI: f64 = std::f64::consts::PI;

fn cis(theta: f64) -> Cx {
    Cx {
        re: cos(theta),
        im: sin(theta),
    }
}

fn bit_reverse(a: &mut [Cx]) {
    let n = a.len();
    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            a.swap(i, j);
        }
    }
}

/// In-place radix-2. `inverse` flips the sign and divides by `n`.
fn fft_pow2(a: &mut [Cx], inverse: bool) -> Result<(), CoreError> {
    let n = a.len();
    if n == 0 || !n.is_power_of_two() {
        return Err(container("FFT 长度不是 2 的幂"));
    }
    bit_reverse(a);
    let mut len = 2usize;
    while len <= n {
        let ang = if inverse { 2.0 * PI / len as f64 } else { -2.0 * PI / len as f64 };
        let wlen = cis(ang);
        let mut i = 0usize;
        while i < n {
            let mut w = Cx { re: 1.0, im: 0.0 };
            for j in 0..(len / 2) {
                let u = a[i + j];
                let v = mul(a[i + j + len / 2], w);
                a[i + j] = Cx {
                    re: u.re + v.re,
                    im: u.im + v.im,
                };
                a[i + j + len / 2] = Cx {
                    re: u.re - v.re,
                    im: u.im - v.im,
                };
                w = mul(w, wlen);
            }
            i += len;
        }
        len *= 2;
    }
    if inverse {
        let s = 1.0 / n as f64;
        for v in a.iter_mut() {
            *v = scale(*v, s);
        }
    }
    Ok(())
}

fn next_pow2(n: usize) -> Result<usize, CoreError> {
    if n == 0 {
        return Ok(1);
    }
    if n > (usize::MAX / 2) {
        return Err(container("FFT 长度太大"));
    }
    Ok(n.next_power_of_two())
}

fn chirp_theta(k: i64, n: i64, inverse: bool) -> f64 {
    let kk = i128::from(k) * i128::from(k);
    let period = i128::from(n) * 2;
    let mut r = kk % period;
    if r < 0 {
        r += period;
    }
    let sign = if inverse { 1.0 } else { -1.0 };
    sign * PI * (r as f64) / (n as f64)
}

fn fft_bluestein(input: &[Cx], inverse: bool) -> Result<Vec<Cx>, CoreError> {
    let n = input.len();
    if n == 0 {
        return Ok(vec![]);
    }
    let n_i = i64::try_from(n).map_err(|_| container("FFT 长度太大"))?;
    let m = next_pow2(n.checked_mul(2).and_then(|v| v.checked_sub(1)).ok_or_else(|| container("FFT 长度太大"))?)?;
    let mut chirp = vec![Cx { re: 0.0, im: 0.0 }; n];
    for (k, slot) in chirp.iter_mut().enumerate() {
        *slot = cis(chirp_theta(k as i64, n_i, inverse));
    }
    let mut a = vec![Cx { re: 0.0, im: 0.0 }; m];
    let mut b = vec![Cx { re: 0.0, im: 0.0 }; m];
    for k in 0..n {
        a[k] = mul(input[k], chirp[k]);
        let conj = Cx {
            re: chirp[k].re,
            im: -chirp[k].im,
        };
        b[k] = conj;
        if k > 0 {
            b[m - k] = conj;
        }
    }
    fft_pow2(&mut a, false)?;
    fft_pow2(&mut b, false)?;
    for i in 0..m {
        a[i] = mul(a[i], b[i]);
    }
    fft_pow2(&mut a, true)?;
    let mut out = Vec::with_capacity(n);
    for k in 0..n {
        out.push(mul(a[k], chirp[k]));
    }
    Ok(out)
}

fn fft_1d(input: &[Cx], inverse: bool) -> Result<Vec<Cx>, CoreError> {
    if input.is_empty() {
        return Ok(vec![]);
    }
    if input.len().is_power_of_two() {
        let mut a = input.to_vec();
        fft_pow2(&mut a, inverse)?;
        return Ok(a);
    }
    if input.len() == 1 {
        return Ok(input.to_vec());
    }
    fft_bluestein(input, inverse)
}

/// `np.fft.fft2` on a real plane, row-major. Returns complex values, row-major.
fn fft2_real(values: &[f64], h: usize, w: usize) -> Result<Vec<Cx>, CoreError> {
    if h.checked_mul(w) != Some(values.len()) {
        return Err(container("FFT 形状对不上"));
    }
    let mut rows = Vec::with_capacity(values.len());
    for r in 0..h {
        let row: Vec<Cx> = values[r * w..(r + 1) * w]
            .iter()
            .map(|&re| Cx { re, im: 0.0 })
            .collect();
        rows.extend(fft_1d(&row, false)?);
    }
    let mut out = vec![Cx { re: 0.0, im: 0.0 }; values.len()];
    for c in 0..w {
        let col: Vec<Cx> = (0..h).map(|r| rows[r * w + c]).collect();
        let transformed = fft_1d(&col, false)?;
        for r in 0..h {
            out[r * w + c] = transformed[r];
        }
    }
    Ok(out)
}

fn ifft2(values: &[Cx], h: usize, w: usize) -> Result<Vec<Cx>, CoreError> {
    if h.checked_mul(w) != Some(values.len()) {
        return Err(container("FFT 形状对不上"));
    }
    let mut rows = Vec::with_capacity(values.len());
    for r in 0..h {
        rows.extend(fft_1d(&values[r * w..(r + 1) * w], true)?);
    }
    let mut out = vec![Cx { re: 0.0, im: 0.0 }; values.len()];
    for c in 0..w {
        let col: Vec<Cx> = (0..h).map(|r| rows[r * w + c]).collect();
        let transformed = fft_1d(&col, true)?;
        for r in 0..h {
            out[r * w + c] = transformed[r];
        }
    }
    Ok(out)
}

/// Port of gxr_studio/hdr/gain.py:177 `_phase_shift`.
///
/// Returns `(dy, dx)` in `-n/2 .. n/2`, with the Nyquist bin kept non-negative
/// (`iy <= h // 2`). Ties in `argmax` keep the first C-order index.
pub fn phase_shift(a: &[f64], b: &[f64], h: usize, w: usize) -> Result<(i64, i64), CoreError> {
    use gxr_math::hypot;
    let fa = fft2_real(a, h, w)?;
    let fb = fft2_real(b, h, w)?;
    let mut prod = Vec::with_capacity(fa.len());
    for i in 0..fa.len() {
        let conj_b = Cx {
            re: fb[i].re,
            im: -fb[i].im,
        };
        let f = mul(fa[i], conj_b);
        let mag = hypot(f.re, f.im) + 1e-9;
        prod.push(scale(f, 1.0 / mag));
    }
    let pc = ifft2(&prod, h, w)?;
    let mut best_i = 0usize;
    let mut best = f64::NEG_INFINITY;
    for (i, v) in pc.iter().enumerate() {
        if v.re > best {
            best = v.re;
            best_i = i;
        }
    }
    if !best.is_finite() {
        return Ok((0, 0));
    }
    let iy = (best_i / w) as i64;
    let ix = (best_i % w) as i64;
    let dy = if iy <= h as i64 / 2 { iy } else { iy - h as i64 };
    let dx = if ix <= w as i64 / 2 { ix } else { ix - w as i64 };
    Ok((dy, dx))
}
