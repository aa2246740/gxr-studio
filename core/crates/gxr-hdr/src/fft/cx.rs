//! pocketfft's `cmplx<T>` and its helpers, with the fusions the numpy 2.4.3 arm64 wheel
//! was compiled with, for `T = float` and `T = double`.
//!
//! The wheel was built by clang with `-ffp-contract=on`: the front end fuses `a*b + c` and
//! `a*b - c` into one FMA only inside a single source expression, never across a function
//! call such as `cmplx::operator*` followed by `cmplx::operator+`. For `x*y ± z*w` it fuses
//! the left product (`fma(x, y, ±(z*w))`); for `c ± x*y` with only the right one a product,
//! `fma(±x, y, c)`. The disassembly of `_pocketfft_umath.so` agrees for both widths
//! (`fmadd`/`fnmsub` in `special_mul`, plain `fmul` + `fadd` in `t0 + t1*twr`). Every fused
//! step calls `libm::fmaf` / `libm::fma`, which are exact on native and wasm32 alike.

use core::ops::{Add, Mul, Neg, Sub};

/// `float` or `double`.
pub trait Real:
    Copy + PartialEq + Add<Output = Self> + Sub<Output = Self> + Mul<Output = Self> + Neg<Output = Self>
{
    const ZERO: Self;
    const ONE: Self;
    /// `a*b + c`, rounded once.
    fn fma(a: Self, b: Self, c: Self) -> Self;
    /// `T(x)` for a double: a `long double` literal (double on arm64 macOS), or a twiddle
    /// product computed in double.
    fn of(v: f64) -> Self;
    /// `T0(1)/T0(n)`, `n` already below the count limit.
    fn recip_count(n: usize) -> Self;
}

impl Real for f32 {
    const ZERO: Self = 0.0;
    const ONE: Self = 1.0;
    fn fma(a: Self, b: Self, c: Self) -> Self {
        libm::fmaf(a, b, c)
    }
    fn of(v: f64) -> Self {
        v as f32
    }
    fn recip_count(n: usize) -> Self {
        1.0 / (n as f32)
    }
}

impl Real for f64 {
    const ZERO: Self = 0.0;
    const ONE: Self = 1.0;
    fn fma(a: Self, b: Self, c: Self) -> Self {
        libm::fma(a, b, c)
    }
    fn of(v: f64) -> Self {
        v
    }
    fn recip_count(n: usize) -> Self {
        1.0 / (n as f64)
    }
}

/// `cmplx<T>`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Cx<T> {
    pub r: T,
    pub i: T,
}

impl<T: Real> Cx<T> {
    pub const ZERO: Cx<T> = Cx {
        r: T::ZERO,
        i: T::ZERO,
    };

    pub const fn new(r: T, i: T) -> Self {
        Self { r, i }
    }

    /// `operator+`.
    pub fn add(self, o: Cx<T>) -> Cx<T> {
        Cx::new(self.r + o.r, self.i + o.i)
    }

    /// `operator-`.
    pub fn sub(self, o: Cx<T>) -> Cx<T> {
        Cx::new(self.r - o.r, self.i - o.i)
    }

    /// `operator*(T0)`: both parts times a real factor, each rounded.
    pub fn scale(self, f: T) -> Cx<T> {
        Cx::new(self.r * f, self.i * f)
    }
}

/// One fused multiply-add, `a*b + c` rounded once.
pub fn fma<T: Real>(a: T, b: T, c: T) -> T {
    T::fma(a, b, c)
}

/// `special_mul<fwd>(v1, v2)`: `v1 * conj(v2)` forward, `v1 * v2` backward.
///
/// Forward: `(v1.r*v2.r + v1.i*v2.i, v1.i*v2.r - v1.r*v2.i)`.
/// Backward: `(v1.r*v2.r - v1.i*v2.i, v1.r*v2.i + v1.i*v2.r)`.
pub fn special_mul<const FWD: bool, T: Real>(v1: Cx<T>, v2: Cx<T>) -> Cx<T> {
    if FWD {
        Cx::new(
            fma(v1.r, v2.r, v1.i * v2.i),
            fma(v1.i, v2.r, -(v1.r * v2.i)),
        )
    } else {
        Cx::new(
            fma(v1.r, v2.r, -(v1.i * v2.i)),
            fma(v1.r, v2.i, v1.i * v2.r),
        )
    }
}

/// `PM(a, b, c, d)`: `(c + d, c - d)`.
pub fn pm<T: Real>(c: Cx<T>, d: Cx<T>) -> (Cx<T>, Cx<T>) {
    (c.add(d), c.sub(d))
}

/// `PMINPLACE(a, b)`: `t = a; a += b; b = t - b`.
pub fn pm_in_place<T: Real>(a: &mut Cx<T>, b: &mut Cx<T>) {
    let t = *a;
    *a = a.add(*b);
    *b = t.sub(*b);
}

/// `ROTX90<fwd>`: times `-i` forward, `+i` backward.
pub fn rotx90<const FWD: bool, T: Real>(a: Cx<T>) -> Cx<T> {
    if FWD {
        Cx::new(a.i, -a.r)
    } else {
        Cx::new(-a.i, a.r)
    }
}

/// The real constants of pass3/5/7/8/11: `T0(…L)`.
pub fn constant<T: Real>(v: f64) -> T {
    T::of(v)
}

/// The sign pocketfft applies to the imaginary constants: `(fwd ? -1 : 1) * T0(…)`.
pub fn signed<const FWD: bool, T: Real>(v: f64) -> T {
    if FWD { -T::of(v) } else { T::of(v) }
}
