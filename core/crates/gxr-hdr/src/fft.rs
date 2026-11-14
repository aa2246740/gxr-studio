//! The FFT behind gxr_studio/hdr/gain.py:177 `_phase_shift`, bit for bit.
//!
//! `a` and `b` are float32. numpy 2.4.3 then runs:
//!
//! - `fft2`: per axis (last first) the gufunc `fft(a, fct=1)`. The Python int `1` makes
//!   the type resolver pick the complex128 loop (`pfu.fft.resolve_dtypes((complex64, int,
//!   None))` is complex128), so pocketfft's `c2c` runs in **double** on the exactly widened
//!   input and the result is rounded to complex64 (the preallocated output);
//! - `*`, `abs`, `+ 1e-9`, `/` in complex64 / float32;
//! - `ifft2`: per axis `ifft(a, fct=reciprocal(n, float32))`, the complex64 loop, so
//!   pocketfft runs in **float** and applies the float32 `1/n` itself.
//!
//! The submodules port pocketfft for both widths line for line, including the FMAs the macOS
//! arm64 wheel was compiled with (clang `-ffp-contract=on`, see `fft/cx.rs`):
//!
//! - `cx`: `cmplx<T>`, `special_mul`, `PM`, `ROTX90`;
//! - `twiddle`, `trig`: `sincos_2pibyn<T>` with correctly rounded double `cos`/`sin`
//!   (the PORT-WAVE-1 libm rule; Apple's are not correctly rounded on every angle);
//! - `cfftp`, `passes`, `passg`: the mixed-radix plan and its passes;
//! - `blue`: Bluestein, `good_size_cmplx`, `cost_guess` and `pocketfft_c`'s choice.
//!
//! The elementwise steps below follow numpy's arm64 loops: the complex multiply is
//! `simd_cmul_f32` (`fma(ar, br, -(ai*bi))`, `fma(ar, bi, ai*br)`); `abs` is
//! `simd_cabsolute_f32` (not `hypotf`: the array is contiguous); the divide is
//! `CFLOAT_divide` (Smith's form, fused) with the float32 divisor promoted to `(d, +0)`.
//! pocketfft code is BSD-3-Clause (see `fft/NOTICE`).

mod blue;
mod cfftp;
mod cx;
mod passes;
mod passg;
mod trig;
mod twiddle;

use gxr_raw::CoreError;

use crate::error::unsupported;
use crate::gain::Plane32;
use crate::size;
use cx::{Cx, Real, fma};
use twiddle::Trig;

/// A `(cos, sin)` pair of host functions for the native-only `--trig host` diagnostic.
pub type HostTrig = (fn(f64) -> f64, fn(f64) -> f64);

fn trig_of(host: Option<HostTrig>) -> Trig {
    match host {
        Some((cos, sin)) => Trig { cos, sin },
        None => Trig::CORRECT,
    }
}

enum Kind<T> {
    Pack(cfftp::Cfftp<T>),
    Blue(blue::Blue<T>),
}

/// `pocketfft_c<T>` for one length, with its work buffers.
struct Plan<T> {
    kind: Kind<T>,
    ch: Vec<Cx<T>>,
    akf: Vec<Cx<T>>,
}

impl<T: Real> Plan<T> {
    fn new(len: usize, trig: Trig) -> Result<Self, CoreError> {
        let n = size::count(&[size::dim(len)])?;
        if blue::use_bluestein(size::dim(n)) {
            let b = blue::Blue::new(n, trig)?;
            let n2 = b.n2();
            return Ok(Self {
                kind: Kind::Blue(b),
                ch: size::filled(n2, Cx::ZERO)?,
                akf: size::filled(n2, Cx::ZERO)?,
            });
        }
        let p = cfftp::Cfftp::new(n, trig)?;
        let len = p.len();
        Ok(Self {
            kind: Kind::Pack(p),
            ch: size::filled(len, Cx::ZERO)?,
            akf: Vec::new(),
        })
    }

    /// `plan->exec(c, fct, fwd)`.
    fn exec(&mut self, c: &mut [Cx<T>], fct: T, fwd: bool) {
        match &self.kind {
            Kind::Pack(p) => p.exec(c, &mut self.ch, fct, fwd),
            Kind::Blue(b) => b.exec(c, &mut self.akf, &mut self.ch, fct, fwd),
        }
    }
}

/// One axis of numpy's `fft` (complex128 loop) or `ifft` (complex64 loop) on complex64 data.
enum Line {
    Forward(Plan<f64>, Vec<Cx<f64>>),
    /// The plan and numpy's `fct = reciprocal(n, dtype=float32)`.
    Inverse(Plan<f32>, f32),
}

impl Line {
    fn new(n: usize, inverse: bool, trig: Trig) -> Result<Self, CoreError> {
        if inverse {
            Ok(Line::Inverse(Plan::new(n, trig)?, f32::recip_count(n)))
        } else {
            Ok(Line::Forward(
                Plan::new(n, trig)?,
                size::filled(n, Cx::ZERO)?,
            ))
        }
    }

    fn run(&mut self, line: &mut [Cx<f32>]) {
        match self {
            Line::Forward(plan, buf) => {
                for (b, v) in buf.iter_mut().zip(line.iter()) {
                    *b = Cx::new(f64::from(v.r), f64::from(v.i));
                }
                plan.exec(buf, 1.0, true);
                // complex128 -> complex64: each part rounded to nearest.
                for (v, b) in line.iter_mut().zip(buf.iter()) {
                    *v = Cx::new(b.r as f32, b.i as f32);
                }
            }
            Line::Inverse(plan, fct) => plan.exec(line, *fct, false),
        }
    }
}

/// `fft` (or `ifft`) along the last axis of a row-major `rows × n` complex64 array.
fn along_rows(a: &mut [Cx<f32>], n: usize, inverse: bool, trig: Trig) -> Result<(), CoreError> {
    let mut line = Line::new(n, inverse, trig)?;
    for row in a.chunks_exact_mut(n) {
        line.run(row);
    }
    Ok(())
}

/// `fft` (or `ifft`) along axis 0 of a row-major `h × w` array.
fn along_columns(
    a: &mut [Cx<f32>],
    h: usize,
    w: usize,
    inverse: bool,
    trig: Trig,
) -> Result<(), CoreError> {
    let mut line = Line::new(h, inverse, trig)?;
    let mut col = size::filled(h, Cx::ZERO)?;
    for c in 0..w {
        for (r, v) in col.iter_mut().enumerate() {
            *v = a[r * w + c];
        }
        line.run(&mut col);
        for (r, v) in col.iter().enumerate() {
            a[r * w + c] = *v;
        }
    }
    Ok(())
}

/// `fft2` / `ifft2`: `_raw_fftnd` transforms the last axis first.
fn fft2(a: &mut [Cx<f32>], h: usize, w: usize, inverse: bool, trig: Trig) -> Result<(), CoreError> {
    along_rows(a, w, inverse, trig)?;
    along_columns(a, h, w, inverse, trig)
}

/// numpy complex64 `a * b` (`simd_cmul_f32`; the scalar loop fuses the same way).
fn cmul(a: Cx<f32>, b: Cx<f32>) -> Cx<f32> {
    Cx::new(fma(a.r, b.r, -(a.i * b.i)), fma(a.r, b.i, a.i * b.r))
}

/// numpy complex64 `abs` on a contiguous array: `simd_cabsolute_f32`.
fn cabs(z: Cx<f32>) -> f32 {
    let inf = f32::INFINITY;
    let (mut re, mut im) = (z.r.abs(), z.i.abs());
    let (re_inf, im_inf) = (re == inf, im == inf);
    if re_inf {
        im = inf;
    }
    if im_inf {
        re = inf;
    }
    let (re_ok, im_ok) = (!re.is_nan(), !im.is_nan());
    if !re_ok {
        im = f32::NAN;
    }
    if !im_ok {
        re = f32::NAN;
    }
    // vmaxq/vminq: both are NaN or neither is, by now.
    let larger = if re.is_nan() || re >= im { re } else { im };
    let smaller = if im.is_nan() || im <= re { im } else { re };
    let divide = !(larger == 0.0 || smaller == inf);
    let ratio = if divide { smaller / larger } else { 0.0 };
    libm::sqrtf(fma(ratio, ratio, 1.0)) * larger
}

/// numpy `CFLOAT_divide(in1, (in2r, in2i))`: Smith's algorithm, compiled with FMA.
fn cdiv(in1: Cx<f32>, in2r: f32, in2i: f32) -> Cx<f32> {
    let (in2r_abs, in2i_abs) = (in2r.abs(), in2i.abs());
    if in2r_abs >= in2i_abs {
        if in2r_abs == 0.0 && in2i_abs == 0.0 {
            // divide by zero should yield a complex inf or nan
            return Cx::new(in1.r / in2r_abs, in1.i / in2i_abs);
        }
        let rat = in2i / in2r;
        let scl = 1.0 / fma(in2i, rat, in2r);
        Cx::new(fma(in1.i, rat, in1.r) * scl, fma(-in1.r, rat, in1.i) * scl)
    } else {
        let rat = in2r / in2i;
        let scl = 1.0 / fma(in2r, rat, in2i);
        Cx::new(fma(in1.r, rat, in1.i) * scl, fma(in1.i, rat, -in1.r) * scl)
    }
}

fn complex(values: &[f32]) -> Result<Vec<Cx<f32>>, CoreError> {
    let mut out = size::with_capacity(values.len())?;
    out.extend(values.iter().map(|&v| Cx::new(v, 0.0)));
    Ok(out)
}

fn check_shape(a: &[f32], b: &[f32], h: usize, w: usize) -> Result<(), CoreError> {
    if h == 0
        || w == 0
        || size::count(&[size::dim(h), size::dim(w)])? != a.len()
        || a.len() != b.len()
    {
        return Err(unsupported("bad_input", "FFT 形状对不上"));
    }
    Ok(())
}

/// The float32 plane `np.real(np.fft.ifft2(f / (np.abs(f) + 1e-9)))` with
/// `f = np.fft.fft2(a) * np.conj(np.fft.fft2(b))`.
fn plane(a: &[f32], b: &[f32], h: usize, w: usize, trig: Trig) -> Result<Vec<f32>, CoreError> {
    check_shape(a, b, h, w)?;
    let mut fa = complex(a)?;
    let mut fb = complex(b)?;
    fft2(&mut fa, h, w, false, trig)?;
    fft2(&mut fb, h, w, false, trig)?;
    // NEP 50: the Python float 1e-9 is cast to float32 and added in float32.
    let eps = 1e-9_f64 as f32;
    for (x, &y) in fa.iter_mut().zip(&fb) {
        let f = cmul(*x, Cx::new(y.r, -y.i));
        *x = cdiv(f, cabs(f) + eps, 0.0);
    }
    fft2(&mut fa, h, w, true, trig)?;
    let mut out = size::with_capacity(fa.len())?;
    out.extend(fa.iter().map(|v| v.r));
    Ok(out)
}

/// Port of gxr_studio/hdr/gain.py:177 `_phase_shift`: the peak of the phase-correlation
/// plane, wrapped into `[-n/2, n/2]` (`i <= n // 2` stays positive). `argmax` keeps the first
/// index in C order and returns the first NaN if there is one.
pub fn phase_shift(a: &[f32], b: &[f32], h: usize, w: usize) -> Result<(i64, i64), CoreError> {
    let pc = plane(a, b, h, w, Trig::CORRECT)?;
    Ok(peak(&pc, h, w))
}

fn peak(pc: &[f32], h: usize, w: usize) -> (i64, i64) {
    let mut best = 0usize;
    let mut best_v = f32::NAN;
    for (i, &v) in pc.iter().enumerate() {
        if v.is_nan() {
            best = i;
            break;
        }
        if best_v.is_nan() || v > best_v {
            best_v = v;
            best = i;
        }
    }
    let (iy, ix) = (best / w, best % w);
    let wrap = |i: usize, n: usize| -> i64 {
        let i = i64::try_from(i).unwrap_or(i64::MAX);
        let n = i64::try_from(n).unwrap_or(i64::MAX);
        if i <= n / 2 { i } else { i - n }
    };
    (wrap(iy, h), wrap(ix, w))
}

/// NaN payloads differ between hosts; the planes the CLI and wasm write carry one NaN.
fn canonical(v: f32) -> f32 {
    if v.is_nan() { f32::NAN } else { v }
}

/// Entry points for the `hdr-fft*` / `hdr-phase-plane` commands. They hang off `Plane32`
/// because `lib.rs` belongs to the hdr unit; complex data is `(re, im)` pairs along `w`.
impl Plane32 {
    /// numpy `fft` / `ifft` along the last axis of complex64 data (`w = 2n`), or `fft2` /
    /// `ifft2` over the whole `h × n` plane when `two_d`.
    pub fn fft_complex(
        &self,
        inverse: bool,
        two_d: bool,
        host: Option<HostTrig>,
    ) -> Result<Plane32, CoreError> {
        if !self.w.is_multiple_of(2) || self.v.len() != self.h * self.w {
            return Err(unsupported("bad_input", "FFT 形状对不上"));
        }
        let n = self.w / 2;
        // numpy checks the length of the axis it transforms, never the batch: `_raw_fft`
        // refuses `n < 1`; `fft2` transforms the last axis, then axis 0 (length `h`).
        let points = |p: usize| {
            CoreError::new(
                "value_error",
                format!("Invalid number of FFT data points ({p}) specified."),
            )
        };
        if n == 0 {
            return Err(points(0));
        }
        if self.h == 0 {
            if two_d {
                return Err(points(0));
            }
            // An empty batch: an empty array of the same shape.
            return Ok(Plane32 {
                h: 0,
                w: self.w,
                v: Vec::new(),
            });
        }
        let mut data = size::with_capacity(self.h * n)?;
        data.extend(self.v.chunks_exact(2).map(|p| Cx::new(p[0], p[1])));
        let trig = trig_of(host);
        if two_d {
            fft2(&mut data, self.h, n, inverse, trig)?;
        } else {
            along_rows(&mut data, n, inverse, trig)?;
        }
        let mut v = size::with_capacity(self.v.len())?;
        for c in &data {
            v.push(canonical(c.r));
            v.push(canonical(c.i));
        }
        Ok(Plane32 {
            h: self.h,
            w: self.w,
            v,
        })
    }

    /// The float32 phase-correlation plane of `_phase_shift(self, other)` and its peak.
    pub fn phase_plane(
        &self,
        other: &Plane32,
        host: Option<HostTrig>,
    ) -> Result<(Plane32, (i64, i64)), CoreError> {
        if other.h != self.h || other.w != self.w {
            return Err(unsupported("bad_input", "FFT 形状对不上"));
        }
        let pc = plane(&self.v, &other.v, self.h, self.w, trig_of(host))?;
        let shift = peak(&pc, self.h, self.w);
        let v = pc.into_iter().map(canonical).collect();
        Ok((
            Plane32 {
                h: self.h,
                w: self.w,
                v,
            },
            shift,
        ))
    }

    /// `sincos_2pibyn(n)` as `(re, im)` pairs, and the length of `v1`. `kind` 0: every
    /// `operator[](idx)` of the float table (widened exactly); 1: of the double table; 2: the
    /// raw double tables `v1` then `v2` as `(re, im, angle)` triples: the `cos`/`sin` values
    /// and the argument they were taken of.
    pub fn fft_twiddles(
        n: u64,
        kind: u8,
        host: Option<HostTrig>,
    ) -> Result<(Vec<f64>, usize), CoreError> {
        let count = size::count(&[n, 2])?;
        if n == 0 {
            return Err(unsupported("bad_input", "FFT 形状对不上"));
        }
        let t = twiddle::SinCos::new(n, trig_of(host))?;
        let (rows, v1_len) = t.tables();
        if kind == 2 {
            let mut v = size::with_capacity(3 * rows.len())?;
            for row in &rows {
                v.extend_from_slice(row);
            }
            return Ok((v, v1_len));
        }
        let mut v = size::with_capacity(count)?;
        for idx in 0..n {
            let (r, i) = if kind == 0 {
                let c: Cx<f32> = t.get(idx);
                (f64::from(c.r), f64::from(c.i))
            } else {
                let c: Cx<f64> = t.get(idx);
                (c.r, c.i)
            };
            v.push(r);
            v.push(i);
        }
        Ok((v, v1_len))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[test]
    fn bluestein_padding_past_the_count_limit_is_refused() {
        // 2**31 - 1 is prime: Bluestein, padded to good_size(2**32 - 3) > 2**32 - 1.
        let n = 2_147_483_647usize;
        assert!(blue::use_bluestein(n as u64));
        assert!(blue::good_size_cmplx(2 * n as u64 - 1) > u64::from(u32::MAX));
        for err in [
            Plan::<f32>::new(n, Trig::CORRECT).err(),
            Plan::<f64>::new(n, Trig::CORRECT).err(),
        ] {
            assert_eq!(
                err.map(|e| e.message),
                Some(size::ARRAY_TOO_BIG.to_string())
            );
        }
        assert!(Plane32::fft_twiddles(u64::from(u32::MAX), 0, None).is_err());
    }

    #[test]
    fn abs_and_divide_edge_cases() {
        assert_eq!(cabs(Cx::new(3.0, -4.0)), 5.0);
        assert_eq!(cabs(Cx::new(0.0, 0.0)), 0.0);
        assert_eq!(cabs(Cx::new(f32::INFINITY, f32::NAN)), f32::INFINITY);
        assert!(cabs(Cx::new(1.0, f32::NAN)).is_nan());
        let q = cdiv(Cx::new(1.0, 2.0), 4.0, 0.0);
        assert_eq!((q.r, q.i), (0.25, 0.5));
        assert!(cdiv(Cx::new(1.0, 0.0), 0.0, 0.0).r.is_infinite());
    }
}
