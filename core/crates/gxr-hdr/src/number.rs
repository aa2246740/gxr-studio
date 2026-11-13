//! Python `round`, `Fraction.limit_denominator` and `format(v, ".7g")`.

use gxr_math::{log10, powi};
use gxr_raw::CoreError;

use crate::error::container;

fn even_integer(f: f64) -> bool {
    let half = f / 2.0;
    half.floor() == half
}

/// Python `round` of a finite float to an integer, half to even.
pub fn round_half_even(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let base = x.floor();
    let diff = x - base;
    if diff > 0.5 || (diff == 0.5 && !even_integer(base)) {
        base + 1.0
    } else {
        base
    }
}

/// Python `round(x, ndigits)` for a small non-negative `ndigits`.
pub fn py_round(x: f64, ndigits: i32) -> Result<f64, CoreError> {
    if !x.is_finite() {
        return Ok(x);
    }
    if !(0..=15).contains(&ndigits) {
        return Err(container("round 的位数超出范围"));
    }
    let scale = powi(10.0, ndigits);
    Ok(round_half_even(x * scale) / scale)
}

fn gcd(mut a: i128, mut b: i128) -> i128 {
    if a < 0 {
        a = -a;
    }
    if b < 0 {
        b = -b;
    }
    while b != 0 {
        let r = a % b;
        a = b;
        b = r;
    }
    a
}

/// `float.as_integer_ratio` for finite values whose reduced denominator fits `i128`.
pub fn as_integer_ratio(x: f64) -> Result<(i128, i128), CoreError> {
    if !x.is_finite() {
        return Err(container("分数不是有限数"));
    }
    if x == 0.0 {
        return Ok((0, 1));
    }
    let bits = x.to_bits();
    let neg = bits >> 63 != 0;
    let biased = ((bits >> 52) & 0x7ff) as i32;
    let frac = bits & ((1u64 << 52) - 1);
    let (mant, exp) = if biased == 0 {
        (i128::from(frac), 1 - 1023 - 52)
    } else {
        (i128::from(frac | (1u64 << 52)), biased - 1023 - 52)
    };
    let mut numer = mant;
    let mut denom = 1i128;
    if exp >= 0 {
        if exp > 120 {
            return Err(container("分数太大"));
        }
        numer <<= exp;
    } else {
        let shift = -exp;
        if shift > 120 {
            return Err(container("分数太大"));
        }
        denom <<= shift;
    }
    if neg {
        numer = -numer;
    }
    let g = gcd(numer, denom);
    Ok((numer / g, denom / g))
}

fn abs_diff_cross(n: i128, d: i128, on: i128, od: i128, scale_d: i128) -> Option<i128> {
    let left = n.checked_mul(od)?;
    let right = on.checked_mul(d)?;
    let diff = left.checked_sub(right)?.checked_abs()?;
    diff.checked_mul(scale_d.checked_abs()?)
}

/// Port of `Fraction(v).limit_denominator(65536)` after `as_integer_ratio`.
pub fn limit_denominator(numer: i128, denom: i128, max_den: i128) -> Result<(i128, i128), CoreError> {
    if denom == 0 || max_den < 1 {
        return Err(container("分母无效"));
    }
    let (orig_n, orig_d) = if denom < 0 {
        (-numer, -denom)
    } else {
        (numer, denom)
    };
    if orig_d <= max_den {
        return Ok((orig_n, orig_d));
    }
    let (mut n, mut d) = (orig_n, orig_d);
    let (mut p0, mut q0, mut p1, mut q1) = (0i128, 1i128, 1i128, 0i128);
    loop {
        let a = n / d;
        let q2 = q0
            .checked_add(a.checked_mul(q1).ok_or_else(|| container("分数太大"))?)
            .ok_or_else(|| container("分数太大"))?;
        if q2 > max_den {
            break;
        }
        let p2 = p0
            .checked_add(a.checked_mul(p1).ok_or_else(|| container("分数太大"))?)
            .ok_or_else(|| container("分数太大"))?;
        p0 = p1;
        q0 = q1;
        p1 = p2;
        q1 = q2;
        let rem = n - a * d;
        n = d;
        d = rem;
        if d == 0 {
            break;
        }
    }
    let k = (max_den - q0) / q1;
    let n1 = p0
        .checked_add(k.checked_mul(p1).ok_or_else(|| container("分数太大"))?)
        .ok_or_else(|| container("分数太大"))?;
    let d1 = q0
        .checked_add(k.checked_mul(q1).ok_or_else(|| container("分数太大"))?)
        .ok_or_else(|| container("分数太大"))?;
    let (n2, d2) = (p1, q1);
    let left = abs_diff_cross(n2, d2, orig_n, orig_d, d1).ok_or_else(|| container("分数太大"))?;
    let right = abs_diff_cross(n1, d1, orig_n, orig_d, d2).ok_or_else(|| container("分数太大"))?;
    if left <= right { Ok((n2, d2)) } else { Ok((n1, d1)) }
}

/// `Fraction(float).limit_denominator(1 << 16)`, numerator then denominator.
pub fn frac_65536(v: f64) -> Result<(i128, i128), CoreError> {
    let (n, d) = as_integer_ratio(v)?;
    limit_denominator(n, d, 1 << 16)
}

fn trim_zeros(digits: &mut String) {
    while digits.ends_with('0') {
        digits.pop();
    }
    if digits.ends_with('.') {
        digits.pop();
    }
}

/// Python `format(v, ".7g")` for the finite metadata values the container prints.
pub fn fmt_g7(v: f64) -> Result<String, CoreError> {
    if !v.is_finite() {
        return Err(container("元数据不是有限数"));
    }
    if v == 0.0 {
        return Ok("0".to_string());
    }
    let neg = v.is_sign_negative();
    let x = v.abs();
    let mut exp = log10(x).floor() as i32;
    let mut pow = powi(10.0, exp);
    if x / pow >= 10.0 {
        exp += 1;
        pow = powi(10.0, exp);
    } else if x / pow < 1.0 {
        exp -= 1;
        pow = powi(10.0, exp);
    }
    let scaled = x / pow * powi(10.0, 6);
    let mut rounded = round_half_even(scaled);
    if rounded >= 10_000_000.0 {
        rounded /= 10.0;
        exp += 1;
    }
    let mut digits = format!("{rounded:.0}");
    while digits.len() < 7 {
        digits.push('0');
    }
    let body = if (-4..7).contains(&exp) {
        if exp >= 0 {
            let split = (exp as usize) + 1;
            let mut text = digits[..split].to_string();
            let rest = digits[split..].trim_end_matches('0');
            if !rest.is_empty() {
                text.push('.');
                text.push_str(rest);
            }
            text
        } else {
            let mut text = "0.".to_string();
            for _ in 0..(-exp - 1) {
                text.push('0');
            }
            let rest = digits.trim_end_matches('0');
            text.push_str(rest);
            text
        }
    } else {
        let mut mant = digits[..1].to_string();
        let rest = digits[1..].trim_end_matches('0');
        if !rest.is_empty() {
            mant.push('.');
            mant.push_str(rest);
        }
        format!("{mant}e{exp:+03}")
    };
    let _ = trim_zeros;
    Ok(if neg { format!("-{body}") } else { body })
}
