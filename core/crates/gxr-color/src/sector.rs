//! The hue-ring sector model: kernel weights and the coefficient coupling.
//!
//! Port of gxr_studio/engine/sector_model.py, plus Cephes `ndtr` (what `scipy.special.ndtr`
//! runs), written with unfused `polevl` / `p1evl` and libm `exp`.

use crate::cplx::{C, J};

/// `SIGMA_ZERO`, degrees.
pub const SIGMA_ZERO: f64 = 1e-4;

/// The two kernels the ring JSON names.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kernel {
    Linear,
    Hard,
}

/// `np.remainder` on float64 (`npy_divmod`): `fmod`, then moved into the sign of `b`.
pub fn remainder(a: f64, b: f64) -> f64 {
    let m = a % b;
    if b == 0.0 {
        return m;
    }
    if m != 0.0 {
        if (b < 0.0) != (m < 0.0) { m + b } else { m }
    } else {
        0.0f64.copysign(b)
    }
}

/// `np.degrees`: `x * (180 / pi)`.
pub fn degrees(x: f64) -> f64 {
    x * (180.0 / std::f64::consts::PI)
}

/// Port of gxr_studio/engine/sector_model.py:16 hue_deg
pub fn hue_deg(c: C) -> f64 {
    remainder(degrees(c.angle()), 360.0)
}

/// Port of gxr_studio/engine/sector_model.py:24 effective
///
/// `|Z| * exp(1j * k * angle(Z))`. `1j * k` is a Python complex times a float.
pub fn effective(z: C, k: f64) -> C {
    let jk = C::new(J.re * k, J.im * k);
    let arg = jk.mul(C::real(z.angle()));
    C::real(z.abs()).mul(arg.exp())
}

/// Port of gxr_studio/engine/sector_model.py:30 _phi
fn phi(t: f64) -> f64 {
    let inv = 1.0 / (2.0 * std::f64::consts::PI).sqrt();
    gxr_math::exp(-0.5 * t * t) * inv
}

/// Port of gxr_studio/engine/sector_model.py:34 _ramp
fn ramp(d: f64, sigma: f64) -> f64 {
    let t = d / sigma;
    d * ndtr(t) + sigma * phi(t)
}

/// Port of gxr_studio/engine/sector_model.py:40 _step
fn step(d: f64, sigma: f64) -> f64 {
    ndtr(d / sigma)
}

const SHIFTS: [f64; 3] = [-360.0, 0.0, 360.0];

/// `np.maximum(a, b)` for a non-NaN `b`: NaN in `a` propagates.
pub fn maximum(a: f64, b: f64) -> f64 {
    if a.is_nan() || a > b { a } else { b }
}

/// Port of gxr_studio/engine/sector_model.py:47 weights
///
/// One row of 24 weights per hue. `sigma` has one entry per hue and is already floored at
/// `SIGMA_ZERO` by the caller; the floor is applied again, as Python does.
pub fn weights(h: &[f64], n: &[f64; 24], kernel: Kernel, sigma: &[f64]) -> Vec<[f64; 24]> {
    let mut a = [0.0; 24];
    let mut c = [0.0; 24];
    for s in 0..24 {
        a[s] = if s == 0 { n[23] - 360.0 } else { n[s - 1] };
        c[s] = if s == 23 { n[0] + 360.0 } else { n[s + 1] };
    }
    let mut out = Vec::with_capacity(h.len());
    for (p, &hp) in h.iter().enumerate() {
        let folded = remainder(hp - n[0], 360.0) + n[0];
        let sig = maximum(sigma[p], SIGMA_ZERO);
        let mut row = [0.0; 24];
        for s in 0..24 {
            let mut acc = 0.0;
            for shift in SHIFTS {
                let x = folded + shift;
                let term = match kernel {
                    Kernel::Linear => {
                        let ba = n[s] - a[s];
                        let cb = c[s] - n[s];
                        let r0 = ramp(x - a[s], sig);
                        let r1 = ramp(x - n[s], sig);
                        let r2 = ramp(x - c[s], sig);
                        r0 / ba - r1 * (1.0 / ba + 1.0 / cb) + r2 / cb
                    }
                    Kernel::Hard => step(x - n[s], sig) - step(x - c[s], sig),
                };
                acc += term;
            }
            row[s] = maximum(acc, 0.0);
        }
        out.push(row);
    }
    out
}

// Cephes ndtr.c / scipy xsf: erf (T, U), erfc (P, Q for x < 8, R, S beyond).
const T: [f64; 5] = [
    9.604_973_739_870_516_387_49e0,
    9.002_601_972_038_426_892_17e1,
    2.232_005_345_946_843_192_26e3,
    7.003_325_141_128_050_754_73e3,
    5.559_230_130_103_949_627_68e4,
];
const U: [f64; 5] = [
    3.356_171_416_475_030_996_47e1,
    5.213_579_497_801_526_797_95e2,
    4.594_323_829_709_801_279_87e3,
    2.262_900_006_138_909_342_46e4,
    4.926_739_426_086_359_210_86e4,
];
const P: [f64; 9] = [
    2.461_969_814_735_305_125_24e-10,
    5.641_895_648_310_688_219_77e-1,
    7.463_210_564_422_699_126_87e0,
    4.863_719_709_856_813_666_14e1,
    1.965_208_329_560_770_982_42e2,
    5.264_451_949_954_773_586_31e2,
    9.345_285_271_719_576_075_40e2,
    1.027_551_886_895_157_102_72e3,
    5.575_353_353_693_993_275_26e2,
];
const Q: [f64; 8] = [
    1.322_819_511_547_449_925_08e1,
    8.670_721_408_859_897_423_29e1,
    3.549_377_788_878_198_910_62e2,
    9.757_085_017_432_054_897_53e2,
    1.823_909_166_879_097_362_89e3,
    2.246_337_608_187_109_817_92e3,
    1.656_663_091_941_613_501_82e3,
    5.575_353_408_177_276_755_46e2,
];
const R: [f64; 6] = [
    5.641_895_835_477_550_739_84e-1,
    1.275_366_707_599_781_044_16e0,
    5.019_050_422_511_804_774_14e0,
    6.160_210_979_930_535_851_95e0,
    7.409_742_699_504_489_391_60e0,
    2.978_866_653_721_002_406_70e0,
];
const S: [f64; 6] = [
    2.260_528_632_201_172_765_90e0,
    9.396_035_249_380_014_346_73e0,
    1.204_895_398_080_966_566_05e1,
    1.708_144_507_475_658_972_22e1,
    9.608_968_090_632_858_781_98e0,
    3.369_076_451_000_815_160_50e0,
];
const MAXLOG: f64 = 7.097_827_128_933_839_968_43e2;
const SQRTH: f64 = std::f64::consts::FRAC_1_SQRT_2;

fn polevl(x: f64, coef: &[f64]) -> f64 {
    let mut ans = coef[0];
    for &c in &coef[1..] {
        ans = ans * x + c;
    }
    ans
}

fn p1evl(x: f64, coef: &[f64]) -> f64 {
    let mut ans = x + coef[0];
    for &c in &coef[1..] {
        ans = ans * x + c;
    }
    ans
}

fn erf(x: f64) -> f64 {
    if x.abs() > 1.0 {
        return 1.0 - erfc(x);
    }
    let z = x * x;
    x * polevl(z, &T) / p1evl(z, &U)
}

fn erfc(a: f64) -> f64 {
    let x = a.abs();
    if x < 1.0 {
        return 1.0 - erf(a);
    }
    let z = -a * a;
    if z < -MAXLOG {
        return if a < 0.0 { 2.0 } else { 0.0 };
    }
    let z = gxr_math::exp(z);
    let (p, q) = if x < 8.0 {
        (polevl(x, &P), p1evl(x, &Q))
    } else {
        (polevl(x, &R), p1evl(x, &S))
    };
    let y = (z * p) / q;
    let y = if a < 0.0 { 2.0 - y } else { y };
    if y == 0.0 {
        return if a < 0.0 { 2.0 } else { 0.0 };
    }
    y
}

/// `scipy.special.ndtr` (Cephes).
pub fn ndtr(a: f64) -> f64 {
    if a.is_nan() {
        return f64::NAN;
    }
    let x = a * SQRTH;
    let z = x.abs();
    if z < SQRTH {
        return 0.5 + 0.5 * erf(x);
    }
    let y = 0.5 * erfc(z);
    if x > 0.0 { 1.0 - y } else { y }
}

#[cfg(test)]
mod tests {
    use super::{hue_deg, ndtr, remainder};
    use crate::cplx::C;

    #[test]
    fn ndtr_has_the_known_values() {
        assert_eq!(ndtr(0.0), 0.5);
        assert!((ndtr(1.0) - 0.841_344_746_068_542_9).abs() < 1e-15);
        assert!((ndtr(-3.0) - 0.001_349_898_031_630_094_6).abs() < 1e-17);
        assert_eq!(ndtr(-40.0), 0.0);
        assert_eq!(ndtr(40.0), 1.0);
    }

    #[test]
    fn remainder_follows_numpy() {
        assert_eq!(remainder(-30.0, 360.0), 330.0);
        assert_eq!(remainder(720.0, 360.0).to_bits(), 0.0f64.to_bits());
        assert!(remainder(f64::NAN, 360.0).is_nan());
        assert!((hue_deg(C::new(0.0, -1.0)) - 270.0).abs() < 1e-12);
    }
}
