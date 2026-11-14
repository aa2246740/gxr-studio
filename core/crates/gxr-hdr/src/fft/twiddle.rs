//! pocketfft's `sincos_2pibyn<T>`: `exp(2πi k / n)` for `k < n`, as `T` (float or double).
//!
//! Two double tables, `v1[k & mask]` and `v2[k >> shift]`, hold `cos`/`sin` of multiples of
//! `ang = (π/4) / n` folded into the first octant (`calc`). An entry is the double complex
//! product of one value from each table, rounded once to `T`. clang fused that product as
//! `fma(x1.r, x2.r, -(x1.i*x2.i))` and `fma(x1.r, x2.i, x1.i*x2.r)` in double (the wheel's
//! `comp_twiddle` shows `fnmsub`/`fmadd` on `d` registers), so this does the same with
//! `libm::fma`. Counts are `u64`: Bluestein asks for a table of length `2n`.

use gxr_raw::CoreError;

use super::alloc;
use super::cx::{Cx, Real};
use crate::size;

/// The double `cos` and `sin` the tables are built from.
#[derive(Clone, Copy)]
pub struct Trig {
    pub cos: fn(f64) -> f64,
    pub sin: fn(f64) -> f64,
}

impl Trig {
    /// Correctly rounded (the PORT-WAVE-1 oracle); the default everywhere.
    pub const CORRECT: Trig = Trig {
        cos: super::trig::cos,
        sin: super::trig::sin,
    };
}

/// One table entry and the angle its `cos`/`sin` were taken of (0 for the exact `(1, 0)`).
#[derive(Clone, Copy)]
struct C64 {
    r: f64,
    i: f64,
    a: f64,
}

pub struct SinCos {
    n: u64,
    mask: u64,
    shift: u32,
    v1: Vec<C64>,
    v2: Vec<C64>,
}

/// `calc`'s octant folding of `x << 3`: the multiple `m` of `ang` passed to `cos`/`sin`,
/// whether the real part is the `sin` (and the imaginary part the `cos`), and both signs.
fn fold(x: u64, n: u64) -> (u64, bool, bool, bool) {
    if x < 4 * n {
        // first half
        if x < 2 * n {
            // first quadrant
            if x < n {
                return (x, false, false, false);
            }
            return (2 * n - x, true, false, false);
        }
        // second quadrant
        let x = x - 2 * n;
        if x < n {
            return (x, true, true, false);
        }
        return (2 * n - x, false, true, false);
    }
    let x = 8 * n - x;
    if x < 2 * n {
        // third quadrant
        if x < n {
            return (x, false, false, true);
        }
        return (2 * n - x, true, false, true);
    }
    // fourth quadrant
    let x = x - 2 * n;
    if x < n {
        return (x, true, true, true);
    }
    (2 * n - x, false, true, true)
}

/// `sincos_2pibyn::calc(x, n, ang)`: `cos` and `sin` of `Thigh(m)*ang`, placed and signed.
fn calc(x: u64, n: u64, ang: f64, t: Trig) -> C64 {
    let (m, swap, neg_r, neg_i) = fold(x << 3, n);
    // Exact: m <= 2n < 2**53.
    let a = (m as f64) * ang;
    let (c, s) = ((t.cos)(a), (t.sin)(a));
    let (r, i) = if swap { (s, c) } else { (c, s) };
    C64 {
        r: if neg_r { -r } else { r },
        i: if neg_i { -i } else { i },
        a,
    }
}

impl SinCos {
    /// `sincos_2pibyn(n)`. `n` is at most `2 * (2**32 - 1)`, so `8 * n` fits in `u64`.
    pub fn new(n: u64, t: Trig) -> Result<Self, CoreError> {
        // `Thigh(0.25L*pi/n)`: long double is double on arm64 macOS.
        let ang = 0.25 * core::f64::consts::PI / (n as f64);
        let nval = n / 2 + 1;
        let mut shift = 1u32;
        while (1u64 << shift) * (1u64 << shift) < nval {
            shift += 1;
        }
        let mask = (1u64 << shift) - 1;
        let one = C64 {
            r: 1.0,
            i: 0.0,
            a: 0.0,
        };
        let len1 = size::count(&[mask + 1])?;
        let mut v1 = alloc::with_capacity(len1)?;
        v1.push(one);
        v1.extend((1..=mask).map(|i| calc(i, n, ang, t)));
        let len2 = size::count(&[nval.div_ceil(mask + 1)])?;
        let mut v2 = alloc::with_capacity(len2)?;
        v2.push(one);
        v2.extend((1..len2 as u64).map(|i| calc(i * (mask + 1), n, ang, t)));
        Ok(Self {
            n,
            mask,
            shift,
            v1,
            v2,
        })
    }

    /// The raw `v1` then `v2` tables as `(re, im, angle)`, and the length of `v1`.
    pub fn tables(&self) -> Result<(Vec<[f64; 3]>, usize), CoreError> {
        let mut rows = alloc::with_capacity(self.v1.len() + self.v2.len())?;
        rows.extend(self.v1.iter().chain(&self.v2).map(|c| [c.r, c.i, c.a]));
        Ok((rows, self.v1.len()))
    }

    /// `operator[](idx)` for `idx < n`.
    pub fn get<T: Real>(&self, idx: u64) -> Cx<T> {
        let (k, flip) = if 2 * idx <= self.n {
            (idx, false)
        } else {
            (self.n - idx, true)
        };
        let x1 = self.v1[(k & self.mask) as usize];
        let x2 = self.v2[(k >> self.shift) as usize];
        let r = T::of(libm::fma(x1.r, x2.r, -(x1.i * x2.i)));
        let i = T::of(libm::fma(x1.r, x2.i, x1.i * x2.r));
        Cx::new(r, if flip { -i } else { i })
    }
}
