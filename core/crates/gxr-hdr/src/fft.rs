//! The FFT behind gxr_studio/hdr/gain.py:177 `_phase_shift`.
//!
//! numpy runs PocketFFT in complex64 on the float32 planes. Only the location of the
//! phase-correlation peak leaves `_phase_shift`, so this port computes the same transform in
//! float64: radix 2 for powers of two, Bluestein's chirp (over a radix-2 transform) for any
//! other length. Twiddles are `cos`/`sin` of the exact angle (no recurrence). Complex
//! products are `(ar*br - ai*bi, ar*bi + ai*br)`, never fused (CONVENTIONS §4). If the
//! peak ever lands on a different bin than numpy's, that is a stop condition
//! (PORT-WAVE-1 A, LUT/HDR 3), not something to tune here.

use gxr_math::{cos, hypot, sin};
use gxr_raw::CoreError;

use crate::error::unsupported;
use crate::np::count_f64;

#[derive(Clone, Copy, Debug)]
struct Cx {
    re: f64,
    im: f64,
}

const ZERO: Cx = Cx { re: 0.0, im: 0.0 };

fn mul(a: Cx, b: Cx) -> Cx {
    Cx {
        re: a.re * b.re - a.im * b.im,
        im: a.re * b.im + a.im * b.re,
    }
}

fn conj(a: Cx) -> Cx {
    Cx {
        re: a.re,
        im: -a.im,
    }
}

/// `exp(-2πi k / n)`.
fn twiddle(k: usize, n: usize) -> Cx {
    let theta = -2.0 * std::f64::consts::PI * count_f64(k) / count_f64(n);
    Cx {
        re: cos(theta),
        im: sin(theta),
    }
}

/// Radix-2 plan for a power of two.
struct Pow2 {
    n: usize,
    tw: Vec<Cx>,
}

impl Pow2 {
    fn new(n: usize) -> Self {
        Self {
            n,
            tw: (0..n / 2).map(|k| twiddle(k, n)).collect(),
        }
    }

    /// Forward transform in place (`inverse` uses the conjugate twiddles, unscaled).
    fn run(&self, a: &mut [Cx], inverse: bool) {
        let n = self.n;
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
        let mut len = 2;
        while len <= n {
            let step = n / len;
            let half = len / 2;
            for start in (0..n).step_by(len) {
                for k in 0..half {
                    let t = self.tw[k * step];
                    let t = if inverse { conj(t) } else { t };
                    let u = a[start + k];
                    let v = mul(a[start + k + half], t);
                    a[start + k] = Cx {
                        re: u.re + v.re,
                        im: u.im + v.im,
                    };
                    a[start + k + half] = Cx {
                        re: u.re - v.re,
                        im: u.im - v.im,
                    };
                }
            }
            len *= 2;
        }
    }
}

/// A plan for one length: radix 2, or Bluestein over a radix-2 transform of size `m`.
struct Plan {
    n: usize,
    pow2: Pow2,
    /// Bluestein only: `exp(-iπ k² / n)` and the transformed conjugate chirp.
    chirp: Vec<Cx>,
    kernel: Vec<Cx>,
}

impl Plan {
    fn new(n: usize) -> Result<Self, CoreError> {
        if n.is_power_of_two() {
            return Ok(Self {
                n,
                pow2: Pow2::new(n),
                chirp: Vec::new(),
                kernel: Vec::new(),
            });
        }
        let m = n
            .checked_mul(2)
            .and_then(|v| v.checked_next_power_of_two())
            .ok_or_else(|| unsupported("bad_input", "FFT 长度太大"))?;
        let pow2 = Pow2::new(m);
        let period = 2 * n;
        let chirp: Vec<Cx> = (0..n)
            .map(|k| {
                // k² mod 2n keeps the angle exact for large k.
                let kk = ((k as u128 * k as u128) % period as u128) as usize;
                let theta = -std::f64::consts::PI * count_f64(kk) / count_f64(n);
                Cx {
                    re: cos(theta),
                    im: sin(theta),
                }
            })
            .collect();
        let mut kernel = vec![ZERO; m];
        for k in 0..n {
            kernel[k] = conj(chirp[k]);
            if k > 0 {
                kernel[m - k] = conj(chirp[k]);
            }
        }
        pow2.run(&mut kernel, false);
        Ok(Self {
            n,
            pow2,
            chirp,
            kernel,
        })
    }

    /// Unscaled transform of `a` (length `n`) in place.
    fn run(&self, a: &mut [Cx], inverse: bool) {
        if self.chirp.is_empty() {
            self.pow2.run(a, inverse);
            return;
        }
        // The inverse is conj(forward(conj(x))).
        let m = self.pow2.n;
        let mut buf = vec![ZERO; m];
        for k in 0..self.n {
            let x = if inverse { conj(a[k]) } else { a[k] };
            buf[k] = mul(x, self.chirp[k]);
        }
        self.pow2.run(&mut buf, false);
        for (v, &k) in buf.iter_mut().zip(&self.kernel) {
            *v = mul(*v, k);
        }
        self.pow2.run(&mut buf, true);
        let scale = 1.0 / count_f64(m);
        for k in 0..self.n {
            let y = mul(
                Cx {
                    re: buf[k].re * scale,
                    im: buf[k].im * scale,
                },
                self.chirp[k],
            );
            a[k] = if inverse { conj(y) } else { y };
        }
    }
}

/// 2-D transform of a row-major `h × w` plane, rows first, then columns.
fn fft2(a: &mut [Cx], h: usize, w: usize, inverse: bool) -> Result<(), CoreError> {
    let rows = Plan::new(w)?;
    for r in 0..h {
        rows.run(&mut a[r * w..(r + 1) * w], inverse);
    }
    let cols = Plan::new(h)?;
    let mut col = vec![ZERO; h];
    for c in 0..w {
        for r in 0..h {
            col[r] = a[r * w + c];
        }
        cols.run(&mut col, inverse);
        for r in 0..h {
            a[r * w + c] = col[r];
        }
    }
    Ok(())
}

fn complex(values: &[f32]) -> Vec<Cx> {
    values
        .iter()
        .map(|&v| Cx {
            re: f64::from(v),
            im: 0.0,
        })
        .collect()
}

/// Port of gxr_studio/hdr/gain.py:177 `_phase_shift`: the peak of
/// `real(ifft2(F / (|F| + 1e-9)))` with `F = fft2(a) * conj(fft2(b))`, wrapped into
/// `[-n/2, n/2]` (`i <= n // 2` stays positive). `argmax` keeps the first index in C order.
pub fn phase_shift(a: &[f32], b: &[f32], h: usize, w: usize) -> Result<(i64, i64), CoreError> {
    if h == 0 || w == 0 || h.checked_mul(w) != Some(a.len()) || a.len() != b.len() {
        return Err(unsupported("bad_input", "FFT 形状对不上"));
    }
    let mut fa = complex(a);
    let mut fb = complex(b);
    fft2(&mut fa, h, w, false)?;
    fft2(&mut fb, h, w, false)?;
    for (x, &y) in fa.iter_mut().zip(&fb) {
        let f = mul(*x, conj(y));
        let mag = hypot(f.re, f.im) + 1e-9;
        *x = Cx {
            re: f.re / mag,
            im: f.im / mag,
        };
    }
    fft2(&mut fa, h, w, true)?;
    // The 1/(h*w) scale of `ifft2` is positive and does not move the peak.
    let mut best = 0usize;
    let mut best_v = f64::NAN;
    for (i, v) in fa.iter().enumerate() {
        // numpy's argmax returns the first NaN if there is one.
        if v.re.is_nan() {
            best = i;
            break;
        }
        if best_v.is_nan() || v.re > best_v {
            best_v = v.re;
            best = i;
        }
    }
    let (iy, ix) = (best / w, best % w);
    let wrap = |i: usize, n: usize| -> i64 {
        let i = i64::try_from(i).unwrap_or(i64::MAX);
        let n = i64::try_from(n).unwrap_or(i64::MAX);
        if i <= n / 2 { i } else { i - n }
    };
    Ok((wrap(iy, h), wrap(ix, w)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dft(x: &[Cx]) -> Vec<Cx> {
        let n = x.len();
        (0..n)
            .map(|k| {
                let mut s = ZERO;
                for (j, &v) in x.iter().enumerate() {
                    let t = mul(v, twiddle((j * k) % n, n));
                    s = Cx {
                        re: s.re + t.re,
                        im: s.im + t.im,
                    };
                }
                s
            })
            .collect()
    }

    #[test]
    fn bluestein_and_radix2_match_a_direct_dft() {
        for n in [1usize, 2, 3, 5, 8, 12, 17, 64, 67] {
            let x: Vec<Cx> = (0..n)
                .map(|i| Cx {
                    re: sin(i as f64 * 0.37),
                    im: cos(i as f64 * 0.11),
                })
                .collect();
            let mut got = x.clone();
            Plan::new(n).unwrap().run(&mut got, false);
            for (g, e) in got.iter().zip(dft(&x)) {
                assert!(
                    (g.re - e.re).abs() < 1e-9 && (g.im - e.im).abs() < 1e-9,
                    "n={n}"
                );
            }
        }
    }

    #[test]
    fn a_shifted_copy_peaks_at_the_shift() {
        let (h, w) = (24usize, 30usize);
        let base: Vec<f32> = (0..h * w)
            .map(|i| ((i * 7919) % 101) as f32 / 101.0)
            .collect();
        let mut moved = vec![0.0f32; h * w];
        for y in 0..h {
            for x in 0..w {
                moved[((y + 2) % h) * w + (x + w - 3) % w] = base[y * w + x];
            }
        }
        assert_eq!(phase_shift(&moved, &base, h, w).unwrap(), (2, -3));
    }
}
