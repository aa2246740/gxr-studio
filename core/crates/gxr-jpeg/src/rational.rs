//! EXIF rationals as Pillow 12.2.0 writes them (`TiffImagePlugin.py`): the type inference in
//! `ImageFileDirectory_v2._setitem`, and `write_rational` / `write_signed_rational` with
//! `_limit_rational` and `_limit_signed_rational`. Used by `build_exif` in Pillow mode, which
//! `encode_jpeg_with(.., optimize = true)` selects.
//!
//! Python's ints are unbounded; here every value fits in `i128`. Inputs are `i64` pairs, and the
//! only larger numbers come from `Fraction(float)`, whose denominator is at most `2**1074`. Those
//! that don't fit in `i128` are floats with `|x| <= 2**-73`, whose limited value is exactly 0 for
//! every `max_val` used here, so they are answered without the fraction (`limit_rational`). The
//! arithmetic that can grow beyond `i128` in `limit_denominator` is checked and resolved exactly
//! (see there).

use gxr_raw::CoreError;

const U32_MAX: i128 = (1_i128 << 32) - 1;
const I32_MAX: i128 = (1_i128 << 31) - 1;
const I32_MIN: i128 = -(1_i128 << 31);
/// `2**-73`: at or below it, `Fraction(x).limit_denominator(m)` is 0 for any `m <= 2**32 - 1`.
const TINY: f64 = 1.058_791_184_067_875_4e-22;

fn bad(what: &str) -> CoreError {
    CoreError::new("encode_failed", format!("EXIF 有理数：{what}"))
}

/// One value handed to Pillow's rational writers.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum RatVal {
    /// `IFDRational(n, d)`, stored as given (a zero denominator is NaN).
    Ifd(i64, i64),
    /// A Python float (`float(int)` for ints in a RATIONAL tag, or a float value).
    Float(f64),
}

/// Pillow's choice between RATIONAL (5) and SRATIONAL (10) for a tag whose values are all
/// `IFDRational`: SRATIONAL when any value is `< 0`. A zero denominator is NaN, never `< 0`.
pub(crate) fn infer_signed(rs: &[(i64, i64)]) -> bool {
    rs.iter()
        .any(|&(n, d)| d != 0 && n != 0 && ((n < 0) != (d < 0)))
}

fn gcd(mut a: i128, mut b: i128) -> i128 {
    a = a.abs();
    b = b.abs();
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}

/// `Fraction(n, d)`: reduced, denominator positive (`d != 0`).
fn fraction(n: i128, d: i128) -> (i128, i128) {
    let g = gcd(n, d).max(1);
    let (n, d) = (n / g, d / g);
    if d < 0 { (-n, -d) } else { (n, d) }
}

/// `Fraction(x)` for a finite float: exact, reduced.
fn fraction_from_f64(x: f64) -> Result<(i128, i128), CoreError> {
    if !x.is_finite() {
        return Err(bad("不是有限数"));
    }
    if x == 0.0 {
        return Ok((0, 1));
    }
    let bits = x.to_bits();
    let neg = bits >> 63 == 1;
    let exp = i32::try_from((bits >> 52) & 0x7FF).map_err(|_| bad("浮点"))?;
    let frac = bits & ((1_u64 << 52) - 1);
    let (mant, e) = if exp == 0 {
        (frac, -1074)
    } else {
        (frac | (1_u64 << 52), exp - 1075)
    };
    let tz = mant.trailing_zeros();
    let mant = i128::from(mant >> tz);
    let e = e + i32::try_from(tz).map_err(|_| bad("浮点"))?;
    let (n, d) = if e >= 0 {
        let shift = u32::try_from(e).map_err(|_| bad("浮点"))?;
        if shift >= 126 - 53 {
            return Err(bad("数值太大"));
        }
        (mant << shift, 1)
    } else {
        let shift = u32::try_from(-e).map_err(|_| bad("浮点"))?;
        if shift > 125 {
            return Err(bad("数值太小"));
        }
        (mant, 1_i128 << shift)
    };
    Ok(if neg { (-n, d) } else { (n, d) })
}

/// Port of CPython 3.14 `fractions.Fraction.limit_denominator` on a reduced fraction `n0/d0`
/// (`d0 > 0`). Every intermediate the algorithm keeps is at most `max_d` or `|n0|`; the two
/// products that can exceed `i128` only decide a comparison, and overflow there means "larger".
fn limit_denominator(n0: i128, d0: i128, max_d: i128) -> (i128, i128) {
    if d0 <= max_d {
        return (n0, d0);
    }
    let (mut p0, mut q0, mut p1, mut q1) = (0_i128, 1_i128, 1_i128, 0_i128);
    let (mut n, mut d) = (n0, d0);
    loop {
        let a = n.div_euclid(d);
        // `q2 = q0 + a*q1`; past `max_d` (or past i128) the loop ends.
        let q2 = a.checked_mul(q1).and_then(|x| x.checked_add(q0));
        match q2 {
            Some(q2) if q2 <= max_d => {
                (p0, q0, p1, q1) = (p1, q1, p0 + a * p1, q2);
                (n, d) = (d, n - a * d);
            }
            _ => break,
        }
    }
    let k = (max_d - q0).div_euclid(q1);
    let q = q0 + k * q1;
    // `2*d*q <= d0`, with every factor non-negative.
    let closer_p1 = 2_i128
        .checked_mul(d)
        .and_then(|x| x.checked_mul(q))
        .is_some_and(|lhs| lhs <= d0);
    if closer_p1 {
        (p1, q1)
    } else {
        (p0 + k * p1, q)
    }
}

/// Port of Pillow `TiffImagePlugin.py:294 _limit_rational(val, max_val)`.
fn limit_rational(val: RatVal, max_val: i128) -> Result<(i128, i128), CoreError> {
    match val {
        RatVal::Ifd(n, d) => {
            // `IFDRational.limit_rational`: a zero denominator is returned as it is
            // (`abs(nan) > 1` is false, so never inverted).
            if d == 0 {
                return Ok((i128::from(n), 0));
            }
            let (fn_, fd) = fraction(i128::from(n), i128::from(d));
            if fn_.abs() > fd {
                // `1 / val` is `Fraction(d, n)`, then limited and swapped back.
                let (inv_n, inv_d) = fraction(fd, fn_);
                let (p, q) = limit_denominator(inv_n, inv_d, max_val);
                Ok((q, p))
            } else {
                Ok(limit_denominator(fn_, fd, max_val))
            }
        }
        RatVal::Float(f) => {
            // `IFDRational(1 / f if inv else f)` is `Fraction` of that float, limited.
            if f.is_nan() {
                return Err(bad("不是有限数"));
            }
            let inv = f.abs() > 1.0;
            let x = if inv { 1.0 / f } else { f };
            // `Fraction(x).limit_denominator(max_val)` is 0 whenever `|x| <= 2**-73` and
            // `max_val <= 2**32 - 1` (0 is closer than any p/q with q <= max_val), and those are
            // the floats whose exact fraction needs more than i128 (Sol r1). Pillow writes `0/1`,
            // swapped to `1/0` on the reciprocal branch; never the reduced EXIF.
            if max_val <= U32_MAX && x.abs() <= TINY {
                return Ok(if inv { (1, 0) } else { (0, 1) });
            }
            let (n, d) = fraction_from_f64(x)?;
            let (p, q) = limit_denominator(n, d, max_val);
            Ok(if inv { (q, p) } else { (p, q) })
        }
    }
}

/// Port of Pillow `TiffImagePlugin.py:302 _limit_signed_rational(val, 2**31 - 1, -2**31)` for an
/// `IFDRational(n, d)`.
fn limit_signed_rational(n: i64, d: i64) -> Result<(i128, i128), CoreError> {
    // `Fraction(val)` keeps the IFDRational's own numerator and denominator.
    let mut n_d = (i128::from(n), i128::from(d));
    if n_d.0.min(n_d.1) < I32_MIN {
        n_d = limit_rational(RatVal::Ifd(n, d), -I32_MIN)?;
    }
    if n_d.0.max(n_d.1) > I32_MAX {
        // Floats from here on: `n_d_float[0] / n_d_float[1]`.
        #[allow(clippy::cast_precision_loss)]
        let (a, b) = (n_d.0 as f64, n_d.1 as f64);
        if b == 0.0 {
            return Err(bad("除以零"));
        }
        n_d = limit_rational(RatVal::Float(a / b), I32_MAX)?;
    }
    Ok(n_d)
}

/// `write_rational`: each value through `_limit_rational(v, 2**32 - 1)`, packed as two `L`.
pub(crate) fn pack_rational(val: RatVal) -> Result<[u8; 8], CoreError> {
    let (n, d) = limit_rational(val, U32_MAX)?;
    let n = u32::try_from(n).map_err(|_| bad("RATIONAL 分子超出范围"))?;
    let d = u32::try_from(d).map_err(|_| bad("RATIONAL 分母超出范围"))?;
    let mut out = [0_u8; 8];
    out[..4].copy_from_slice(&n.to_be_bytes());
    out[4..].copy_from_slice(&d.to_be_bytes());
    Ok(out)
}

/// `write_signed_rational`: each value through `_limit_signed_rational`, packed as two `l`.
pub(crate) fn pack_signed_rational(n: i64, d: i64) -> Result<[u8; 8], CoreError> {
    let (n, d) = limit_signed_rational(n, d)?;
    let n = i32::try_from(n).map_err(|_| bad("SRATIONAL 分子超出范围"))?;
    let d = i32::try_from(d).map_err(|_| bad("SRATIONAL 分母超出范围"))?;
    let mut out = [0_u8; 8];
    out[..4].copy_from_slice(&n.to_be_bytes());
    out[4..].copy_from_slice(&d.to_be_bytes());
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unsigned(n: i64, d: i64) -> Option<(u32, u32)> {
        pack_rational(RatVal::Ifd(n, d)).ok().map(|b| {
            (
                u32::from_be_bytes([b[0], b[1], b[2], b[3]]),
                u32::from_be_bytes([b[4], b[5], b[6], b[7]]),
            )
        })
    }

    fn signed(n: i64, d: i64) -> Option<(i32, i32)> {
        pack_signed_rational(n, d).ok().map(|b| {
            (
                i32::from_be_bytes([b[0], b[1], b[2], b[3]]),
                i32::from_be_bytes([b[4], b[5], b[6], b[7]]),
            )
        })
    }

    #[test]
    fn unsigned_values_from_pillow_12_2_0() {
        assert_eq!(unsigned(28, 10), Some((14, 5)));
        assert_eq!(unsigned(10, 2500), Some((1, 250)));
        assert_eq!(unsigned(0, 7), Some((0, 1)));
        assert_eq!(unsigned(0, -3), Some((0, 1)));
        assert_eq!(unsigned(5, 0), Some((5, 0)));
        assert_eq!(unsigned(0, 0), Some((0, 0)));
        assert_eq!(unsigned(-6, -4), Some((3, 2)));
        assert_eq!(unsigned(-5, 0), None);
        assert_eq!(unsigned(1 << 40, 3), Some((1, 0)));
        assert_eq!(unsigned(3, 1 << 40), Some((0, 1)));
        assert_eq!(
            unsigned((1 << 40) + 1, (1 << 33) + 7),
            Some((3685513853, 28793077))
        );
    }

    #[test]
    fn signed_values_from_pillow_12_2_0() {
        assert_eq!(signed(-1, 2), Some((-1, 2)));
        assert_eq!(signed(-28, 10), Some((-28, 10)));
        assert_eq!(signed(6, -4), Some((6, -4)));
        assert_eq!(signed(-5, 0), Some((-5, 0)));
        assert_eq!(signed(1, 0), Some((1, 0)));
        assert_eq!(signed(-(1 << 40), 3), Some((1, 0)));
        assert_eq!(signed(-1, 1 << 40), Some((0, 1)));
        assert_eq!(signed(3, -(1 << 33)), Some((-1, 2147483647)));
        assert_eq!(signed(1 << 40, 0), None);
    }

    #[test]
    fn inference_matches_pillow() {
        assert!(!infer_signed(&[(0, 0)]));
        assert!(!infer_signed(&[(5, 0)]));
        assert!(!infer_signed(&[(-5, 0)]));
        assert!(!infer_signed(&[(0, -3)]));
        assert!(!infer_signed(&[(-6, -4)]));
        assert!(infer_signed(&[(-1, 2)]));
        assert!(infer_signed(&[(1, 0), (-1, 2)]));
        assert!(infer_signed(&[(6, -4)]));
    }

    #[test]
    fn floats_are_exact() {
        assert_eq!(fraction_from_f64(0.5).unwrap(), (1, 2));
        assert_eq!(fraction_from_f64(-3.0).unwrap(), (-3, 1));
        assert_eq!(
            fraction_from_f64(0.1).unwrap(),
            (3602879701896397, 36028797018963968)
        );
        assert!(fraction_from_f64(f64::NAN).is_err());
        assert!(fraction_from_f64(f64::INFINITY).is_err());
        assert!(fraction_from_f64(1e-300).is_err());
        assert_eq!(
            limit_rational(RatVal::Float(300.0), U32_MAX).unwrap(),
            (300, 1)
        );
        assert_eq!(
            limit_rational(RatVal::Float(0.1), U32_MAX).unwrap(),
            (1, 10)
        );
    }

    #[test]
    fn tiny_and_huge_floats_like_pillow() {
        // Sol r1: Pillow writes 0/1 (1/0 through the reciprocal); these used to be errors,
        // which dropped the whole EXIF to the reduced set.
        assert_eq!(TINY.to_bits(), (1023 - 73) << 52);
        for f in [1e-23, -1e-23, 1e-300, -1e-300, 5e-324, -5e-324, TINY, -TINY] {
            assert_eq!(
                limit_rational(RatVal::Float(f), U32_MAX).unwrap(),
                (0, 1),
                "{f}"
            );
            assert_eq!(
                pack_rational(RatVal::Float(f.abs())).unwrap(),
                [0, 0, 0, 0, 0, 0, 0, 1]
            );
        }
        for f in [1e23, -1e23, 1e300, -1e300, f64::MAX, f64::INFINITY] {
            assert_eq!(
                limit_rational(RatVal::Float(f), U32_MAX).unwrap(),
                (1, 0),
                "{f}"
            );
            assert_eq!(
                pack_rational(RatVal::Float(f)).unwrap(),
                [0, 0, 0, 1, 0, 0, 0, 0]
            );
        }
        // Just above 2**-73 still takes the exact path, with Python's answer.
        let above = f64::from_bits(TINY.to_bits() + 1);
        assert_eq!(
            limit_rational(RatVal::Float(above), U32_MAX).unwrap(),
            (0, 1)
        );
        assert_eq!(
            limit_rational(RatVal::Float(1e-20), U32_MAX).unwrap(),
            (0, 1)
        );
        assert_eq!(
            limit_rational(RatVal::Float(1e22), U32_MAX).unwrap(),
            (1, 0)
        );
    }
}
