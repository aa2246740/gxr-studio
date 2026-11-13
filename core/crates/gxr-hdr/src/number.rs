//! Python number formatting and fractions used by the gain metadata.
//!
//! - `round(x, n)`: CPython rounds the exact binary value to `n` decimals, half to even,
//!   then parses that text. Rust's `{:.n}` formats the exact value with the same tie rule.
//! - `format(v, ".7g")`: seven significant digits, the `%g` switch between fixed and
//!   exponent notation, trailing zeros dropped.
//! - `Fraction(v).limit_denominator(1 << 16)`: CPython 3.14 `fractions.py`, line for line.

use gxr_raw::CoreError;

use crate::error::container;

/// Python `round(x, ndigits)` for `0 <= ndigits <= 17`.
pub fn py_round(x: f64, ndigits: usize) -> Result<f64, CoreError> {
    if !x.is_finite() {
        return Ok(x);
    }
    if ndigits > 17 {
        return Err(CoreError::new("bad_input", "round 的位数超出范围"));
    }
    let text = format!("{x:.ndigits$}");
    text.parse::<f64>()
        .map_err(|_| CoreError::new("bad_input", "round 的结果读不回来"))
}

/// Python `format(v, ".7g")`.
pub fn fmt_g7(v: f64) -> String {
    if v.is_nan() {
        return "nan".to_string();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let sci = format!("{v:.6e}");
    let (mant, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let neg = mant.starts_with('-');
    let digits: String = mant.chars().filter(char::is_ascii_digit).collect();
    let body = if (-4..7).contains(&exp) {
        let text = if exp >= 0 {
            let split = usize::try_from(exp).unwrap_or(0) + 1;
            format!("{}.{}", &digits[..split], &digits[split..])
        } else {
            let zeros = "0".repeat(usize::try_from(-exp - 1).unwrap_or(0));
            format!("0.{zeros}{digits}")
        };
        strip(&text)
    } else {
        let m = strip(&format!("{}.{}", &digits[..1], &digits[1..]));
        let sign = if exp < 0 { '-' } else { '+' };
        format!("{m}e{sign}{:02}", exp.abs())
    };
    if neg { format!("-{body}") } else { body }
}

fn strip(text: &str) -> String {
    if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.').to_string()
    } else {
        text.to_string()
    }
}

fn floor_div(n: i128, d: i128) -> i128 {
    n.div_euclid(d)
}

fn overflow() -> CoreError {
    container("分数太大")
}

/// `Fraction(v).limit_denominator(1 << 16)` → `(numerator, denominator)`.
///
/// `v` must be finite. `|v| < 2**-40` always limits to `0/1`; `|v| >= 2**62` keeps its
/// integer value (`denominator 1`), which no 32-bit field can hold.
pub fn limit_65536(v: f64) -> Result<(i128, i128), CoreError> {
    if v.is_nan() {
        return Err(CoreError::new("value_error", "cannot convert NaN to integer ratio"));
    }
    if v.is_infinite() {
        return Err(CoreError::new("overflow_error", "cannot convert Infinity to integer ratio"));
    }
    if v.abs() < 1.0 / (1u64 << 40) as f64 {
        return Ok((0, 1));
    }
    if v.abs() >= (1u64 << 62) as f64 {
        // Integral already; saturate past i64, the caller rejects it for 32-bit packing.
        #[allow(clippy::cast_possible_truncation)]
        let n = v as i128;
        return Ok((n, 1));
    }
    let (num, den) = as_integer_ratio(v);
    let max_den: i128 = 1 << 16;
    if den <= max_den {
        return Ok((num, den));
    }
    let (mut p0, mut q0, mut p1, mut q1) = (0i128, 1i128, 1i128, 0i128);
    let (mut n, mut d) = (num, den);
    loop {
        let a = floor_div(n, d);
        let q2 = a.checked_mul(q1).and_then(|x| x.checked_add(q0)).ok_or_else(overflow)?;
        if q2 > max_den {
            break;
        }
        let p2 = a.checked_mul(p1).and_then(|x| x.checked_add(p0)).ok_or_else(overflow)?;
        (p0, q0, p1, q1) = (p1, q1, p2, q2);
        let r = n - a * d;
        (n, d) = (d, r);
    }
    let k = floor_div(max_den - q0, q1);
    let lhs = 2i128
        .checked_mul(d)
        .and_then(|x| x.checked_mul(q0 + k * q1))
        .ok_or_else(overflow)?;
    if lhs <= den {
        Ok((p1, q1))
    } else {
        Ok((p0 + k * p1, q0 + k * q1))
    }
}

/// `float.as_integer_ratio` for `2**-40 <= |v| < 2**62` (fits in i128).
fn as_integer_ratio(v: f64) -> (i128, i128) {
    let bits = v.to_bits();
    let neg = bits >> 63 != 0;
    let biased = i32::try_from((bits >> 52) & 0x7ff).unwrap_or(0);
    let frac = bits & ((1u64 << 52) - 1);
    let (mant, exp) = if biased == 0 {
        (frac, -1074)
    } else {
        (frac | (1u64 << 52), biased - 1075)
    };
    let mut num = i128::from(mant);
    let mut den = 1i128;
    if exp >= 0 {
        num <<= exp;
    } else {
        den <<= -exp;
    }
    let tz = num.trailing_zeros().min(den.trailing_zeros());
    num >>= tz;
    den >>= tz;
    (if neg { -num } else { num }, den)
}

/// `struct.pack(">i", n)` range check.
pub fn signed32(n: i128) -> Result<i32, CoreError> {
    i32::try_from(n)
        .map_err(|_| CoreError::new("struct_error", "'i' format requires -2147483648 <= number <= 2147483647"))
}

/// `struct.pack(">I", n)` range check.
pub fn unsigned32(n: i128) -> Result<u32, CoreError> {
    u32::try_from(n).map_err(|_| CoreError::new("struct_error", "'I' format requires 0 <= number <= 4294967295"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounds_like_python() {
        assert_eq!(py_round(2.5, 0).unwrap(), 2.0);
        assert_eq!(py_round(3.5, 0).unwrap(), 4.0);
        assert_eq!(py_round(0.00025, 4).unwrap(), 0.0003);
        assert_eq!(py_round(2.675, 2).unwrap(), 2.67);
        assert_eq!(format!("{:.1}", 0.25), "0.2");
    }

    #[test]
    fn g7_like_python() {
        assert_eq!(fmt_g7(1.25), "1.25");
        assert_eq!(fmt_g7(1.0 / 64.0), "0.015625");
        assert_eq!(fmt_g7(0.0), "0");
        assert_eq!(fmt_g7(-0.0), "-0");
        assert_eq!(fmt_g7(1e-5), "1e-05");
        assert_eq!(fmt_g7(12345678.0), "1.234568e+07");
        assert_eq!(fmt_g7(0.0001), "0.0001");
    }

    #[test]
    fn limits_like_fractions() {
        assert_eq!(limit_65536(1.0 / 64.0).unwrap(), (1, 64));
        assert_eq!(limit_65536(2.5).unwrap(), (5, 2));
        assert_eq!(limit_65536(0.1).unwrap(), (1, 10));
        assert_eq!(limit_65536(1.23456789).unwrap(), (100, 81));
    }
}
