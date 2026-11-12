//! Deterministic float maths for the GXR core.
//!
//! std's transcendental methods (`f64::exp`, `f64::powf`, …) call the platform's maths library,
//! so the native CLI and the WASM build can differ in the last bit. Every core crate calls these
//! wrappers instead; each one forwards to the pure-Rust `libm` crate, which compiles to the same
//! code on every target. `core/clippy.toml` forbids the std methods.
//!
//! `sqrt` is correctly rounded by IEEE 754 on every target, so the std method stays allowed.
#![forbid(unsafe_code)]

/// `e^x` (`f64::exp`).
#[inline]
pub fn exp(x: f64) -> f64 {
    libm::exp(x)
}

/// `2^x` (`f64::exp2`).
#[inline]
pub fn exp2(x: f64) -> f64 {
    libm::exp2(x)
}

/// Natural logarithm (`f64::ln`, Python `math.log`).
#[inline]
pub fn ln(x: f64) -> f64 {
    libm::log(x)
}

/// Base-2 logarithm (`f64::log2`).
#[inline]
pub fn log2(x: f64) -> f64 {
    libm::log2(x)
}

/// Base-10 logarithm (`f64::log10`).
#[inline]
pub fn log10(x: f64) -> f64 {
    libm::log10(x)
}

/// `x^y` (`f64::powf`, Python `x ** y` on floats).
#[inline]
pub fn powf(x: f64, y: f64) -> f64 {
    libm::pow(x, y)
}

/// `x^n` for an integer `n` (`f64::powi`). CPython and numpy evaluate `x ** n` with C `pow`,
/// not by repeated multiplication, so this does the same.
#[inline]
pub fn powi(x: f64, n: i32) -> f64 {
    libm::pow(x, f64::from(n))
}

/// Sine (`f64::sin`).
#[inline]
pub fn sin(x: f64) -> f64 {
    libm::sin(x)
}

/// Cosine (`f64::cos`).
#[inline]
pub fn cos(x: f64) -> f64 {
    libm::cos(x)
}

/// Tangent (`f64::tan`).
#[inline]
pub fn tan(x: f64) -> f64 {
    libm::tan(x)
}

/// Four-quadrant arctangent of `y / x` (`f64::atan2`).
#[inline]
pub fn atan2(y: f64, x: f64) -> f64 {
    libm::atan2(y, x)
}

/// Cube root (`f64::cbrt`).
#[inline]
pub fn cbrt(x: f64) -> f64 {
    libm::cbrt(x)
}

/// `sqrt(x² + y²)` without overflow (`f64::hypot`).
#[inline]
pub fn hypot(x: f64, y: f64) -> f64 {
    libm::hypot(x, y)
}

/// `e^x` in float32 (`f32::exp`).
#[inline]
pub fn expf(x: f32) -> f32 {
    libm::expf(x)
}

/// `2^x` in float32 (`f32::exp2`).
#[inline]
pub fn exp2f(x: f32) -> f32 {
    libm::exp2f(x)
}

/// Natural logarithm in float32 (`f32::ln`).
#[inline]
pub fn lnf(x: f32) -> f32 {
    libm::logf(x)
}

/// Base-2 logarithm in float32 (`f32::log2`).
#[inline]
pub fn log2f(x: f32) -> f32 {
    libm::log2f(x)
}

/// Base-10 logarithm in float32 (`f32::log10`).
#[inline]
pub fn log10f(x: f32) -> f32 {
    libm::log10f(x)
}

/// `x^y` in float32 (`f32::powf`).
#[inline]
pub fn powff(x: f32, y: f32) -> f32 {
    libm::powf(x, y)
}

/// `x^n` in float32 for an integer `n` (`f32::powi`), through `powf` like numpy.
#[inline]
#[allow(clippy::cast_precision_loss)]
pub fn powif(x: f32, n: i32) -> f32 {
    libm::powf(x, n as f32)
}

/// Sine in float32 (`f32::sin`).
#[inline]
pub fn sinf(x: f32) -> f32 {
    libm::sinf(x)
}

/// Cosine in float32 (`f32::cos`).
#[inline]
pub fn cosf(x: f32) -> f32 {
    libm::cosf(x)
}

/// Tangent in float32 (`f32::tan`).
#[inline]
pub fn tanf(x: f32) -> f32 {
    libm::tanf(x)
}

/// Four-quadrant arctangent in float32 (`f32::atan2`).
#[inline]
pub fn atan2f(y: f32, x: f32) -> f32 {
    libm::atan2f(y, x)
}

/// Cube root in float32 (`f32::cbrt`).
#[inline]
pub fn cbrtf(x: f32) -> f32 {
    libm::cbrtf(x)
}

/// `sqrt(x² + y²)` in float32 (`f32::hypot`).
#[inline]
pub fn hypotf(x: f32, y: f32) -> f32 {
    libm::hypotf(x, y)
}

#[cfg(test)]
mod tests {
    use super::*;

    const F64_INPUTS: [f64; 12] = [
        0.0, -0.0, 1e-300, 0.1, 0.5, 1.0, 2.0, 3.25, 10.0, 123.456, -7.5, 1e10,
    ];
    const F32_INPUTS: [f32; 10] = [0.0, -0.0, 1e-30, 0.1, 0.5, 1.0, 2.0, 3.25, 10.0, -7.5];

    fn same64(a: f64, b: f64) -> bool {
        a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
    }

    fn same32(a: f32, b: f32) -> bool {
        a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
    }

    #[test]
    fn f64_wrappers_equal_libm() {
        for &x in &F64_INPUTS {
            assert!(same64(exp(x), libm::exp(x)));
            assert!(same64(exp2(x), libm::exp2(x)));
            assert!(same64(ln(x), libm::log(x)));
            assert!(same64(log2(x), libm::log2(x)));
            assert!(same64(log10(x), libm::log10(x)));
            assert!(same64(sin(x), libm::sin(x)));
            assert!(same64(cos(x), libm::cos(x)));
            assert!(same64(tan(x), libm::tan(x)));
            assert!(same64(cbrt(x), libm::cbrt(x)));
            for &y in &F64_INPUTS {
                assert!(same64(powf(x, y), libm::pow(x, y)));
                assert!(same64(atan2(x, y), libm::atan2(x, y)));
                assert!(same64(hypot(x, y), libm::hypot(x, y)));
            }
            for n in -3..=3 {
                assert!(same64(powi(x, n), libm::pow(x, f64::from(n))));
            }
        }
    }

    #[test]
    #[allow(clippy::cast_precision_loss)]
    fn f32_wrappers_equal_libm() {
        for &x in &F32_INPUTS {
            assert!(same32(expf(x), libm::expf(x)));
            assert!(same32(exp2f(x), libm::exp2f(x)));
            assert!(same32(lnf(x), libm::logf(x)));
            assert!(same32(log2f(x), libm::log2f(x)));
            assert!(same32(log10f(x), libm::log10f(x)));
            assert!(same32(sinf(x), libm::sinf(x)));
            assert!(same32(cosf(x), libm::cosf(x)));
            assert!(same32(tanf(x), libm::tanf(x)));
            assert!(same32(cbrtf(x), libm::cbrtf(x)));
            for &y in &F32_INPUTS {
                assert!(same32(powff(x, y), libm::powf(x, y)));
                assert!(same32(atan2f(x, y), libm::atan2f(x, y)));
                assert!(same32(hypotf(x, y), libm::hypotf(x, y)));
            }
            for n in -3..=3 {
                assert!(same32(powif(x, n), libm::powf(x, n as f32)));
            }
        }
    }

    #[test]
    fn known_values() {
        assert_eq!(exp(0.0), 1.0);
        assert_eq!(ln(1.0), 0.0);
        assert_eq!(log2(8.0), 3.0);
        assert_eq!(log10(1000.0), 3.0);
        assert_eq!(powf(2.0, 10.0), 1024.0);
        assert_eq!(powi(3.0, 3), 27.0);
        assert_eq!(hypot(3.0, 4.0), 5.0);
        assert_eq!(cbrt(27.0), 3.0);
        assert_eq!(exp2(-1.0), 0.5);
    }
}
