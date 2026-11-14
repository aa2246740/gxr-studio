//! The bits of Python semantics the layout code leans on: `round`, `math.floor/ceil`, `int(str)`,
//! `str.isspace`, `\d` and `\s` in `re`, and `datetime.strptime(s, "%Y-%m-%d")`.
//!
//! The Unicode tables come from Python 3.14.7's `unicodedata` (Unicode 16.0.0).

use gxr_raw::CoreError;

/// Code points whose `unicodedata.decimal` is 0; each starts a run of ten `Nd` digits.
const DIGIT_ZEROS: [u32; 76] = [
    0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66,
    0xDE6, 0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90,
    0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
    0x104A0, 0x10D30, 0x10D40, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0,
    0x11650, 0x116C0, 0x116D0, 0x116DA, 0x11730, 0x118E0, 0x11950, 0x11BF0, 0x11C50, 0x11D50,
    0x11DA0, 0x11F50, 0x16130, 0x16A60, 0x16AC0, 0x16B50, 0x16D70, 0x1CCF0, 0x1D7CE, 0x1D7D8,
    0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0, 0x1E5F1, 0x1E950, 0x1FBF0,
];

/// `unicodedata.decimal(ch, None)`: the value of a `Nd` digit (what `\d` and `int()` accept).
pub fn decimal(ch: char) -> Option<u32> {
    let c = u32::from(ch);
    DIGIT_ZEROS
        .iter()
        .find(|&&zero| c >= zero && c < zero + 10)
        .map(|&zero| c - zero)
}

/// `str.isspace()` for one character (also `\s` in a `str` pattern).
pub fn is_space(ch: char) -> bool {
    matches!(
        u32::from(ch),
        0x9..=0xD | 0x1C..=0x20 | 0x85 | 0xA0 | 0x1680 | 0x2000..=0x200A | 0x2028 | 0x2029 | 0x202F | 0x205F | 0x3000
    )
}

/// `str.rstrip()` with no argument.
pub fn rstrip(text: &str) -> &str {
    text.trim_end_matches(is_space)
}

fn overflow() -> CoreError {
    CoreError::new("overflow_error", "cannot convert float infinity to integer")
}

/// A float that is an integer already, as `i64` (the caller rounded it).
fn whole(v: f64) -> Result<i64, CoreError> {
    if v.is_nan() {
        return Err(CoreError::new(
            "value_error",
            "cannot convert float NaN to integer",
        ));
    }
    // 2**63 is exactly representable; anything at or past it does not fit.
    if !v.is_finite() || !(-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&v) {
        return Err(overflow());
    }
    let text = format!("{v:.0}");
    text.parse::<i64>().map_err(|_| overflow())
}

/// Python `round(x)` for a float: half to even, returns an int.
pub fn round(x: f64) -> Result<i64, CoreError> {
    whole(x.round_ties_even())
}

/// `math.floor(x)`.
pub fn floor(x: f64) -> Result<i64, CoreError> {
    whole(x.floor())
}

/// `math.ceil(x)`.
pub fn ceil(x: f64) -> Result<i64, CoreError> {
    whole(x.ceil())
}

/// `int(x)` for a float: toward zero.
pub fn trunc(x: f64) -> Result<i64, CoreError> {
    whole(x.trunc())
}

/// An int used in float arithmetic (`float(n)`); exact for the layout's sizes.
pub fn f(n: i64) -> f64 {
    // Split into halves that convert losslessly; exact for |n| < 2**53, which layout sizes are.
    let hi = f64::from(i32::try_from(n >> 32).unwrap_or(i32::MAX));
    let lo = f64::from(u32::try_from(n & 0xFFFF_FFFF).unwrap_or(u32::MAX));
    hi * 4_294_967_296.0 + lo
}

/// `sep.join(p for p in parts if p)` (`frame_core.join`).
pub fn join(parts: &[&str], sep: &str) -> String {
    let kept: Vec<&str> = parts.iter().copied().filter(|p| !p.is_empty()).collect();
    kept.join(sep)
}

/// `int(text)` reduced to what `frame_core.frame_index` needs: `None` for a `ValueError`, else
/// whether the value is zero and its residue modulo 36 (Python's floor modulo).
///
/// Follows `int()` on a `str`: surrounding `str.isspace()` whitespace, one optional sign, `Nd`
/// digits with single underscores between them, and the 4300-digit limit (a `ValueError`).
pub fn int_mod36(text: &str) -> Option<(bool, i64)> {
    let body = text.trim_matches(is_space);
    let (negative, digits) = match body.chars().next() {
        Some('-') => (true, &body[1..]),
        Some('+') => (false, &body[1..]),
        _ => (false, body),
    };
    let mut residue: i64 = 0;
    let mut zero = true;
    let mut count = 0usize;
    let mut prev_underscore = true;
    for ch in digits.chars() {
        if ch == '_' {
            if prev_underscore {
                return None;
            }
            prev_underscore = true;
            continue;
        }
        let d = decimal(ch)?;
        prev_underscore = false;
        count += 1;
        residue = (residue * 10 + i64::from(d)) % 36;
        if d != 0 {
            zero = false;
        }
    }
    if count == 0 || prev_underscore || count > 4300 {
        return None;
    }
    let residue = if negative {
        (-residue).rem_euclid(36)
    } else {
        residue
    };
    Some((zero, residue))
}

/// `datetime.strptime(text, "%Y-%m-%d")` succeeds: `(year, month, day)`, else `None`.
///
/// The regex `_strptime` builds: `%Y` is `\d\d\d\d`, `%m` is `1[0-2]|0[1-9]|[1-9]`, `%d` is
/// `3[01]|[12]\d|0[1-9]|[1-9]| [1-9]`, matched from the start with no backtracking past the
/// last alternative, then "unconverted data remains" unless the whole string was used. The
/// date must exist (`year` 1–9999).
pub fn strptime_ymd(text: &str) -> Option<(i64, i64, i64)> {
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    let mut year = 0i64;
    for _ in 0..4 {
        year = year * 10 + i64::from(decimal(*chars.get(i)?)?);
        i += 1;
    }
    if chars.get(i) != Some(&'-') {
        return None;
    }
    i += 1;
    // %m alternatives in order; the literal '-' after it lets the regex backtrack between them.
    let month_options: Vec<(i64, usize)> = {
        let a = chars.get(i).copied();
        let b = chars.get(i + 1).copied();
        let mut out = Vec::new();
        if let (Some('1'), Some(c @ '0'..='2')) = (a, b) {
            out.push((10 + i64::from(u32::from(c) - 0x30), 2));
        }
        if let (Some('0'), Some(c @ '1'..='9')) = (a, b) {
            out.push((i64::from(u32::from(c) - 0x30), 2));
        }
        if let Some(c @ '1'..='9') = a {
            out.push((i64::from(u32::from(c) - 0x30), 1));
        }
        out
    };
    for (month, used) in month_options {
        let at = i + used;
        if chars.get(at) != Some(&'-') {
            continue;
        }
        let d0 = at + 1;
        if let Some((day, used_d)) = day_field(&chars, d0) {
            if d0 + used_d != chars.len() {
                return None;
            }
            return valid_date(year, month, day).then_some((year, month, day));
        }
    }
    None
}

/// The first `%d` alternative that matches at `at` (no anchor follows it in the regex).
fn day_field(chars: &[char], at: usize) -> Option<(i64, usize)> {
    let a = chars.get(at).copied();
    let b = chars.get(at + 1).copied();
    if let (Some('3'), Some(c @ ('0' | '1'))) = (a, b) {
        return Some((30 + i64::from(u32::from(c) - 0x30), 2));
    }
    if let (Some(c @ ('1' | '2')), Some(d)) = (a, b)
        && let Some(v) = decimal(d)
    {
        return Some((i64::from(u32::from(c) - 0x30) * 10 + i64::from(v), 2));
    }
    if let (Some('0'), Some(c @ '1'..='9')) = (a, b) {
        return Some((i64::from(u32::from(c) - 0x30), 2));
    }
    if let Some(c @ '1'..='9') = a {
        return Some((i64::from(u32::from(c) - 0x30), 1));
    }
    if let (Some(' '), Some(c @ '1'..='9')) = (a, b) {
        return Some((i64::from(u32::from(c) - 0x30), 2));
    }
    None
}

fn valid_date(year: i64, month: i64, day: i64) -> bool {
    if !(1..=9999).contains(&year) {
        return false;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    (1..=max_day).contains(&day)
}

/// `int(part)` of one `date_override` field, already known to be digits (or a space and a digit).
pub fn small_int(part: &str) -> i64 {
    let mut v = 0i64;
    for ch in part.chars() {
        if let Some(d) = decimal(ch) {
            v = v * 10 + i64::from(d);
        }
    }
    v
}

/// `re.search(r"(\d+(?:\.\d+)?)\s*mm", text)` → group 1.
pub fn focal_mm(text: &str) -> Option<String> {
    let chars: Vec<char> = text.chars().collect();
    let digits_from = |mut k: usize| {
        while k < chars.len() && decimal(chars[k]).is_some() {
            k += 1;
        }
        k
    };
    for start in 0..chars.len() {
        if decimal(chars[start]).is_none() {
            continue;
        }
        let int_end = digits_from(start);
        let mut ends = Vec::new();
        if chars.get(int_end) == Some(&'.')
            && chars.get(int_end + 1).and_then(|&c| decimal(c)).is_some()
        {
            ends.push(digits_from(int_end + 1));
        }
        ends.push(int_end);
        for end in ends {
            let mut k = end;
            while k < chars.len() && is_space(chars[k]) {
                k += 1;
            }
            if chars.get(k) == Some(&'m') && chars.get(k + 1) == Some(&'m') {
                return Some(chars[start..end].iter().collect());
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_follows_python() {
        assert_eq!(round(2.5).unwrap(), 2);
        assert_eq!(round(3.5).unwrap(), 4);
        assert_eq!(round(-0.5).unwrap(), 0);
        assert_eq!(floor(-0.5).unwrap(), -1);
        assert_eq!(ceil(1.0000001).unwrap(), 2);
        assert!(round(f64::INFINITY).is_err());
    }

    #[test]
    fn strptime_accepts_what_python_accepts() {
        assert_eq!(strptime_ymd("2024-02-29"), Some((2024, 2, 29)));
        assert_eq!(strptime_ymd("2026-1-5"), Some((2026, 1, 5)));
        assert_eq!(strptime_ymd("2026-01- 5"), Some((2026, 1, 5)));
        assert_eq!(strptime_ymd("２０２６-01-05"), Some((2026, 1, 5)));
        assert_eq!(strptime_ymd("2026-02-29"), None);
        assert_eq!(strptime_ymd("2026-01-32"), None);
        assert_eq!(strptime_ymd("0000-01-01"), None);
    }

    #[test]
    fn int_and_focal_follow_python() {
        assert_eq!(int_mod36(" 0102 "), Some((false, 102 % 36)));
        assert_eq!(int_mod36("1_0"), Some((false, 10)));
        assert_eq!(int_mod36("1__0"), None);
        assert_eq!(int_mod36("-1"), Some((false, 35)));
        assert_eq!(int_mod36("000"), Some((true, 0)));
        assert_eq!(focal_mm("A12 50mm"), Some("50".into()));
        assert_eq!(focal_mm("x 2.5 mm"), Some("2.5".into()));
        assert_eq!(focal_mm("1.5.5mm"), Some("5.5".into()));
        assert_eq!(focal_mm("no"), None);
    }
}
