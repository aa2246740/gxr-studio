//! Elementwise transcendental functions as numpy calls them on this path.
//!
//! numpy's float32 `log2` and `power` call the platform libm (Apple's on this Mac). Those are
//! not correctly rounded everywhere, and neither is `libm`, and the two differ on different
//! inputs. This port uses the float64 `libm` result rounded once to float32, which is the
//! correctly rounded value except in rare double-rounding cases. `docs/mobile.md` (hdr) records
//! how many inputs disagree with numpy and what that does to the gates.

use gxr_math::{exp2 as exp2_64, log2 as log2_64, powf};

/// numpy `np.log2` on float32.
#[allow(clippy::cast_possible_truncation)]
pub fn log2f(x: f32) -> f32 {
    log2_64(f64::from(x)) as f32
}

/// numpy `np.log2` / `math.log2` on float64.
pub fn log2(x: f64) -> f64 {
    log2_64(x)
}

/// numpy `np.exp2` on float64.
pub fn exp2(x: f64) -> f64 {
    exp2_64(x)
}

/// numpy `np.tanh` on float64.
pub fn tanh(x: f64) -> f64 {
    libm::tanh(x)
}

/// numpy `x ** y` with float32 `x` and a Python-float `y` (cast to float32 first).
#[allow(clippy::cast_possible_truncation)]
pub fn powf32(x: f32, y: f32) -> f32 {
    powf(f64::from(x), f64::from(y)) as f32
}
