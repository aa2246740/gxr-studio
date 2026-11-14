//! Python `float(str)`, `int(str)`, and `str.isspace()` numeric/whitespace rules (Sol r1 P2).
//! Unicode 16.0.0 (Unicode Character Database, Unicode License v3; see `NOTICE`).

const PY_SPACE: [u32; 29] = [
    0x9, 0xa, 0xb, 0xc, 0xd, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2001,
    0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f,
    0x205f, 0x3000,
];

const DECIMAL_ZERO: [u32; 76] = [
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
    0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
    0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10,
    0x104a0, 0x10d30, 0x10d40, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
    0x11650, 0x116c0, 0x116d0, 0x116da, 0x11730, 0x118e0, 0x11950, 0x11bf0, 0x11c50, 0x11d50,
    0x11da0, 0x11f50, 0x16130, 0x16a60, 0x16ac0, 0x16b50, 0x16d70, 0x1ccf0, 0x1d7ce, 0x1d7d8,
    0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e5f1, 0x1e950, 0x1fbf0,
];

#[inline]
pub(crate) fn py_space(c: char) -> bool {
    PY_SPACE.binary_search(&u32::from(c)).is_ok()
}

fn decimal_digit(c: char) -> Option<u32> {
    let cp = u32::from(c);
    let at = DECIMAL_ZERO.partition_point(|&z| z <= cp);
    let zero = *DECIMAL_ZERO.get(at.checked_sub(1)?)?;
    (cp - zero < 10).then_some(cp - zero)
}

fn normalize_py_number(s: &str) -> Option<String> {
    let mut ascii = String::with_capacity(s.len());
    for c in s.chars() {
        if u32::from(c) < 0x7f {
            ascii.push(c);
        } else if py_space(c) {
            ascii.push(' ');
        } else if let Some(d) = decimal_digit(c) {
            ascii.push(char::from_digit(d, 10)?);
        } else {
            return None;
        }
    }
    let bytes = ascii.as_bytes();
    let mut plain = String::with_capacity(ascii.len());
    let mut prev = b'\0';
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let next_digit = bytes.get(i + 1).is_some_and(u8::is_ascii_digit);
            if !(prev.is_ascii_digit() && next_digit) {
                return None;
            }
        } else {
            plain.push(char::from(b));
        }
        prev = b;
    }
    Some(plain)
}

pub(crate) fn parse_py_float(s: &str) -> Option<f64> {
    let plain = normalize_py_number(s)?;
    let t = plain.trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r'));
    let lower = t.to_ascii_lowercase();
    let (neg, body) = match lower.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, lower.strip_prefix('+').unwrap_or(&lower)),
    };
    let special = match body {
        "inf" | "infinity" => Some(f64::INFINITY),
        "nan" => Some(f64::NAN),
        _ => None,
    };
    if let Some(v) = special {
        return Some(if neg { -v } else { v });
    }
    let (mant, exp) = match body.find('e') {
        Some(at) => (&body[..at], Some(&body[at + 1..])),
        None => (body, None),
    };
    let digits = mant.bytes().filter(u8::is_ascii_digit).count();
    let points = mant.bytes().filter(|&b| b == b'.').count();
    if digits == 0 || points > 1 || digits + points != mant.len() {
        return None;
    }
    if let Some(e) = exp {
        let e = e.strip_prefix(['+', '-']).unwrap_or(e);
        if e.is_empty() || !e.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
    }
    t.parse::<f64>().ok()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PySizeError {
    InvalidLiteral,
    OutOfRange,
}

pub(crate) fn parse_py_size(s: &str, max_n: usize) -> Result<usize, PySizeError> {
    let plain = normalize_py_number(s).ok_or(PySizeError::InvalidLiteral)?;
    let t = plain.trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r'));
    let (neg, body) = match t.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if body.is_empty() || !body.bytes().all(|b| b.is_ascii_digit()) {
        return Err(PySizeError::InvalidLiteral);
    }
    if neg {
        return Err(PySizeError::OutOfRange);
    }
    let cap = max_n + 1;
    let mut val = 0usize;
    for b in body.bytes() {
        let d = usize::from(b - b'0');
        val = (val * 10 + d).min(cap);
    }
    if !(2..=max_n).contains(&val) {
        return Err(PySizeError::OutOfRange);
    }
    Ok(val)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_python_underscores_and_unicode_digits() {
        assert_eq!(parse_py_size("0_2", 129), Ok(2));
        assert_eq!(parse_py_size("+0_2", 129), Ok(2));
        assert_eq!(parse_py_size("２", 129), Ok(2));
        assert_eq!(parse_py_float("0_0"), Some(0.0));
        assert_eq!(parse_py_float("１_２e-１"), Some(1.2));
        assert_eq!(parse_py_float("1_.0"), None);
        assert_eq!(parse_py_float("_1"), None);
    }

    #[test]
    fn huge_and_negative_integers_report_out_of_range() {
        assert_eq!(
            parse_py_size("18446744073709551615", 129),
            Err(PySizeError::OutOfRange)
        );
        let huge = "9".repeat(200);
        assert_eq!(parse_py_size(&huge, 129), Err(PySizeError::OutOfRange));
        assert_eq!(parse_py_size("-5", 129), Err(PySizeError::OutOfRange));
        assert_eq!(parse_py_size("1.5", 129), Err(PySizeError::InvalidLiteral));
    }
}
