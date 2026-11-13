//! Python's `repr`/`str` for the values that appear inside error messages, and the few Python
//! conversions (`int()`, `float()`, `round()`) whose failures become message text.

use crate::error::{PyErr, PyResult};

/// One element of a TIFF tag value as Python sees it: an `int`, a `float`, or a one-character
/// `str` (iterating an ASCII value yields characters).
#[derive(Clone, Debug, PartialEq)]
pub enum Scalar {
    Int(i64),
    Float(f64),
    Char(char),
}

impl Scalar {
    /// Python `x == n` for an integer `n`.
    pub(crate) fn eq_int(&self, n: i64) -> bool {
        match *self {
            Self::Int(v) => v == n,
            #[allow(clippy::cast_precision_loss)]
            Self::Float(v) => v == n as f64,
            Self::Char(_) => false,
        }
    }
}

/// Python `repr(float)`: shortest round-trip digits; scientific below 1e-4 and from 1e16 up.
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    let sign = if x < 0.0 { "-" } else { "" };
    let sci = format!("{:e}", x.abs());
    // `format!("{:e}")` of a finite float always contains `e`. These digits are not
    // a file length; if the debug form is unexpected, return it unchanged.
    let Some((mantissa, exponent)) = sci.split_once('e') else {
        return format!("{sign}{sci}");
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let Ok(exp10) = exponent.parse::<i64>() else {
        return format!("{sign}{sci}");
    };
    let decpt = exp10 + 1;
    let Ok(n) = i64::try_from(digits.len()) else {
        return format!("{sign}{sci}");
    };
    let zeros = |count: i64| usize::try_from(count).ok().map(|n| "0".repeat(n));
    let body = if decpt <= -4 || decpt > 16 {
        let (head, tail) = digits.split_at(1);
        let frac = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        let esign = if exp10 < 0 { '-' } else { '+' };
        format!("{head}{frac}e{esign}{:02}", exp10.abs())
    } else if decpt <= 0 {
        let Some(pad) = zeros(-decpt) else {
            return format!("{sign}{sci}");
        };
        format!("0.{pad}{digits}")
    } else if decpt < n {
        let Some(at) = usize::try_from(decpt).ok() else {
            return format!("{sign}{sci}");
        };
        let (head, tail) = digits.split_at(at);
        format!("{head}.{tail}")
    } else {
        let Some(pad) = zeros(decpt - n) else {
            return format!("{sign}{sci}");
        };
        format!("{digits}{pad}.0")
    };
    format!("{sign}{body}")
}

/// Python `repr` of one scalar.
pub fn py_scalar_repr(v: &Scalar) -> String {
    match v {
        Scalar::Int(i) => i.to_string(),
        Scalar::Float(f) => py_float_repr(*f),
        Scalar::Char(c) => py_str_repr(&c.to_string()),
    }
}

/// Python `repr(str)` (single quotes unless the text has one and no double quote).
pub fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python `repr(bytes)`.
pub fn py_bytes_repr(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = String::from("b");
    out.push(char::from(quote));
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(char::from(c));
            }
            0x20..=0x7e => out.push(char::from(c)),
            c => out.push_str(&format!("\\x{c:02x}")),
        }
    }
    out.push(char::from(quote));
    out
}

/// Python `repr(list)` of scalars.
pub fn py_list_repr(items: &[Scalar]) -> String {
    let parts: Vec<String> = items.iter().map(py_scalar_repr).collect();
    format!("[{}]", parts.join(", "))
}

/// Python `repr(list)` of floats.
pub fn py_float_list_repr(items: &[f64]) -> String {
    let parts: Vec<String> = items.iter().map(|v| py_float_repr(*v)).collect();
    format!("[{}]", parts.join(", "))
}

/// Python `repr(list)` of ints.
pub fn py_int_list_repr(items: &[i64]) -> String {
    let parts: Vec<String> = items.iter().map(ToString::to_string).collect();
    format!("[{}]", parts.join(", "))
}

/// Python `repr(tuple)` of ints (`(4,)` for one element).
pub fn py_int_tuple_repr(items: &[i64]) -> String {
    let parts: Vec<String> = items.iter().map(ToString::to_string).collect();
    if parts.len() == 1 {
        format!("({},)", parts[0])
    } else {
        format!("({})", parts.join(", "))
    }
}

/// numpy's shape text inside reshape errors: `(3,3)`, `(4,)`.
pub(crate) fn np_shape_text(dims: &[i64]) -> String {
    let parts: Vec<String> = dims.iter().map(ToString::to_string).collect();
    if parts.len() == 1 {
        format!("({},)", parts[0])
    } else {
        format!("({})", parts.join(","))
    }
}

fn float_to_int(v: f64) -> PyResult<i64> {
    if v.is_nan() {
        return Err(PyErr::Value("cannot convert float NaN to integer".into()));
    }
    if v.is_infinite() {
        return Err(PyErr::Overflow(
            "cannot convert float infinity to integer".into(),
        ));
    }
    #[allow(clippy::cast_possible_truncation)]
    Ok(v.trunc() as i64)
}

/// Python `int(x)`.
pub(crate) fn py_int(v: &Scalar) -> PyResult<i64> {
    match *v {
        Scalar::Int(i) => Ok(i),
        Scalar::Float(f) => float_to_int(f),
        Scalar::Char(c) => c.to_digit(10).map(i64::from).ok_or_else(|| {
            PyErr::Value(format!(
                "invalid literal for int() with base 10: {}",
                py_str_repr(&c.to_string())
            ))
        }),
    }
}

/// Python `float(x)`.
pub(crate) fn py_float(v: &Scalar) -> PyResult<f64> {
    match *v {
        #[allow(clippy::cast_precision_loss)]
        Scalar::Int(i) => Ok(i as f64),
        Scalar::Float(f) => Ok(f),
        Scalar::Char(c) => c.to_digit(10).map(f64::from).ok_or_else(|| {
            PyErr::Value(format!(
                "could not convert string to float: {}",
                py_str_repr(&c.to_string())
            ))
        }),
    }
}

/// Python `round(float)` (half to even), returning an int.
pub(crate) fn py_round_float(v: f64) -> PyResult<i64> {
    float_to_int(v.round_ties_even())
}

/// Python `round(x)` for an int or a float.
pub(crate) fn py_round(v: &Scalar) -> PyResult<i64> {
    match *v {
        Scalar::Int(i) => Ok(i),
        Scalar::Float(f) => py_round_float(f),
        Scalar::Char(_) => Err(PyErr::Type(
            "type str doesn't define __round__ method".into(),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python() {
        let cases = [
            (1e-5, "1e-05"),
            (4095.0, "4095.0"),
            (1e16, "1e+16"),
            (1.5e-7, "1.5e-07"),
            (123_456_789_012_345_680.0, "1.2345678901234568e+17"),
            (0.0001, "0.0001"),
            (1_234_567_890_123_456.0, "1234567890123456.0"),
            (0.1, "0.1"),
            (-2.5, "-2.5"),
            (-0.0, "-0.0"),
            (f64::NAN, "nan"),
            (f64::NEG_INFINITY, "-inf"),
            (16383.0, "16383.0"),
            (12.75, "12.75"),
        ];
        for (x, want) in cases {
            assert_eq!(py_float_repr(x), want, "{x:e}");
        }
    }

    #[test]
    fn container_reprs_match_python() {
        assert_eq!(py_int_tuple_repr(&[0, 0, 10, 20]), "(0, 0, 10, 20)");
        assert_eq!(py_int_tuple_repr(&[4]), "(4,)");
        assert_eq!(np_shape_text(&[3, 3]), "(3,3)");
        assert_eq!(py_float_list_repr(&[0.5, 0.0]), "[0.5, 0.0]");
        assert_eq!(
            py_list_repr(&[Scalar::Int(0), Scalar::Float(1.0), Scalar::Char('a')]),
            "[0, 1.0, 'a']"
        );
        assert_eq!(py_bytes_repr(&[1, b'a', b'\'']), "b\"\\x01a'\"");
    }
}
