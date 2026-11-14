//! Typed EXIF / TIFF tag value representation and byte-order helpers matching Pillow 12.2.0.

use gxr_raw::Json;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Endian {
    Little,
    Big,
}

impl Endian {
    pub fn prefix(self) -> &'static [u8; 2] {
        match self {
            Self::Little => b"II",
            Self::Big => b"MM",
        }
    }

    pub fn u16(self, b: [u8; 2]) -> u16 {
        match self {
            Self::Little => u16::from_le_bytes(b),
            Self::Big => u16::from_be_bytes(b),
        }
    }

    pub fn i16(self, b: [u8; 2]) -> i16 {
        match self {
            Self::Little => i16::from_le_bytes(b),
            Self::Big => i16::from_be_bytes(b),
        }
    }

    pub fn u32(self, b: [u8; 4]) -> u32 {
        match self {
            Self::Little => u32::from_le_bytes(b),
            Self::Big => u32::from_be_bytes(b),
        }
    }

    pub fn i32(self, b: [u8; 4]) -> i32 {
        match self {
            Self::Little => i32::from_le_bytes(b),
            Self::Big => i32::from_be_bytes(b),
        }
    }

    pub fn u64(self, b: [u8; 8]) -> u64 {
        match self {
            Self::Little => u64::from_le_bytes(b),
            Self::Big => u64::from_be_bytes(b),
        }
    }

    pub fn f32(self, b: [u8; 4]) -> f32 {
        match self {
            Self::Little => f32::from_le_bytes(b),
            Self::Big => f32::from_be_bytes(b),
        }
    }

    pub fn f64(self, b: [u8; 8]) -> f64 {
        match self {
            Self::Little => f64::from_le_bytes(b),
            Self::Big => f64::from_be_bytes(b),
        }
    }
}

/// Typed EXIF / TIFF tag value matching Pillow's `ImageFileDirectory_v2` / `Image.Exif`.
#[derive(Clone, Debug, PartialEq)]
pub enum ExifValue {
    Str(String),
    Bytes(Vec<u8>),
    Int(i64),
    Ints(Vec<i64>),
    Float(f64),
    Floats(Vec<f64>),
    Rational(i64, i64),
    Rationals(Vec<(i64, i64)>),
}

/// Canonical signed arbitrary-precision Python integer (`int(v)`), stored as decimal ASCII.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyInt(String);

impl PyInt {
    pub fn from_i64(v: i64) -> Self {
        Self(v.to_string())
    }

    pub fn from_i128(v: i128) -> Self {
        Self(v.to_string())
    }

    pub fn from_f64_trunc(f: f64) -> Result<Self, &'static str> {
        if f.is_nan() {
            return Err("ValueError: cannot convert float NaN to integer");
        }
        if f.is_infinite() {
            return Err("OverflowError: cannot convert float infinity to integer");
        }
        let t = f.trunc();
        if t == 0.0 {
            return Ok(Self("0".to_string()));
        }
        Ok(Self(format!("{t:.0}")))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn is_zero(&self) -> bool {
        self.0 == "0"
    }

    pub fn as_i64(&self) -> Option<i64> {
        self.0.parse::<i64>().ok()
    }

    pub fn as_i128(&self) -> Option<i128> {
        self.0.parse::<i128>().ok()
    }
}

impl std::fmt::Display for PyInt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl ExifValue {
    /// Encode as JSON for `export-meta-exif`: rational -> `{num, den}`, bytes -> `{hex}`.
    pub fn to_exif_json(&self) -> Json {
        match self {
            Self::Str(s) => Json::str(s),
            Self::Bytes(b) => Json::object([("hex", Json::str(bytes_to_hex(b)))]),
            Self::Int(i) => Json::Int(*i),
            Self::Ints(items) => Json::ints(items),
            Self::Float(f) => Json::Float(*f),
            Self::Floats(items) => Json::floats(items),
            Self::Rational(n, d) => rational_json(*n, *d),
            Self::Rationals(items) => {
                Json::Array(items.iter().map(|&(n, d)| rational_json(n, d)).collect())
            }
        }
    }

    /// Encode as `gxr-jpeg`-compatible JSON (`bytes` as integer list `{bytes: [...]}`).
    pub fn to_jpeg_json(&self) -> Json {
        match self {
            Self::Bytes(b) => Json::object([(
                "bytes",
                Json::Array(b.iter().map(|&v| Json::Int(i64::from(v))).collect()),
            )]),
            other => other.to_exif_json(),
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(i) => Some(*i),
            _ => None,
        }
    }

    pub fn as_ints(&self) -> Option<Vec<i64>> {
        match self {
            Self::Int(i) => Some(vec![*i]),
            Self::Ints(v) => Some(v.clone()),
            _ => None,
        }
    }

    pub fn as_rational_f64(&self) -> Option<f64> {
        match self {
            Self::Rational(_, 0) => None,
            Self::Rational(n, d) => Some((*n as f64) / (*d as f64)),
            Self::Int(i) => Some(*i as f64),
            Self::Float(f) => Some(*f),
            Self::Str(s) => parse_py_float_str(s.trim()),
            Self::Bytes(b) => std::str::from_utf8(b)
                .ok()
                .map(trim_py_ascii_bytes_ws)
                .and_then(parse_py_float_str),
            Self::Ints(_) | Self::Floats(_) | Self::Rationals(_) => None,
        }
    }

    /// `bool(value)` in Python for `ifd.get(tag)`: empty arrays/strings/bytes or numeric 0 are falsy.
    pub fn is_truthy(&self) -> bool {
        match self {
            Self::Str(s) => !s.is_empty(),
            Self::Bytes(b) => !b.is_empty(),
            Self::Int(i) => *i != 0,
            Self::Ints(v) => !v.is_empty(),
            Self::Float(f) => *f != 0.0,
            Self::Floats(v) => !v.is_empty(),
            Self::Rational(n, d) => *d == 0 || *n != 0,
            Self::Rationals(v) => !v.is_empty(),
        }
    }

    /// Convert to arbitrary-precision integer the way Python `int(v)` does.
    pub fn to_python_int(&self) -> Result<Option<PyInt>, &'static str> {
        match self {
            Self::Int(i) => Ok(Some(PyInt::from_i64(*i))),
            Self::Float(f) => PyInt::from_f64_trunc(*f).map(Some),
            Self::Rational(_, 0) => Err("ValueError: cannot convert float NaN to integer"),
            Self::Rational(n, d) => Ok(Some(PyInt::from_i128(i128::from(*n) / i128::from(*d)))),
            Self::Str(s) => match parse_py_int_str(s.trim()) {
                Some(v) => Ok(Some(v)),
                None => Err("ValueError: invalid literal for int() with base 10"),
            },
            Self::Bytes(b) => match std::str::from_utf8(b)
                .ok()
                .map(trim_py_ascii_bytes_ws)
                .and_then(parse_py_int_str)
            {
                Some(v) => Ok(Some(v)),
                None => Err("ValueError: invalid literal for int() with base 10"),
            },
            Self::Ints(_) | Self::Floats(_) | Self::Rationals(_) => Err(
                "TypeError: int() argument must be a string, a bytes-like object or a real number",
            ),
        }
    }

    /// Convert to string the way Python `str(v)` does.
    pub fn to_python_str(&self) -> String {
        match self {
            Self::Str(s) => s.clone(),
            Self::Int(i) => i.to_string(),
            Self::Float(f) => format_py_float(*f),
            Self::Rational(_, 0) => "nan".to_string(),
            Self::Rational(n, d) => format_py_float((*n as f64) / (*d as f64)),
            Self::Bytes(b) => b.iter().map(|&c| char::from(c)).collect(),
            Self::Ints(v) => format_int_tuple(v),
            Self::Floats(v) => format_float_tuple(v),
            Self::Rationals(v) => format_rat_tuple(v),
        }
    }
}

pub fn format_py_float(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let sign = if f.is_sign_negative() { "-" } else { "" };
    if f == 0.0 {
        return format!("{sign}0.0");
    }
    let (raw_digits, exp) = super::float_repr::shortest_decimal_digits(f.abs());
    let digits: String = raw_digits
        .into_iter()
        .map(|d| char::from(b'0' + d))
        .collect();
    if digits.is_empty() {
        return format!("{sign}0.0");
    }
    let mut out = String::from(sign);
    if !(-4..16).contains(&exp) {
        let mut chars = digits.chars();
        if let Some(first) = chars.next() {
            out.push(first);
        }
        let rest: &str = &digits[1..];
        if !rest.is_empty() {
            out.push('.');
            out.push_str(rest);
        }
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", exp.unsigned_abs()));
    } else if exp < 0 {
        out.push_str("0.");
        let zeros = (-exp - 1) as usize;
        for _ in 0..zeros {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        let int_digits = (exp as usize) + 1;
        if digits.len() <= int_digits {
            out.push_str(&digits);
            for _ in digits.len()..int_digits {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..int_digits]);
            out.push('.');
            out.push_str(&digits[int_digits..]);
        }
    }
    out
}

fn format_int_tuple(v: &[i64]) -> String {
    let parts: Vec<String> = v.iter().map(ToString::to_string).collect();
    if v.len() == 1 {
        format!("({},)", parts[0])
    } else {
        format!("({})", parts.join(", "))
    }
}

fn format_float_tuple(v: &[f64]) -> String {
    let parts: Vec<String> = v.iter().map(|&f| format_py_float(f)).collect();
    if v.len() == 1 {
        format!("({},)", parts[0])
    } else {
        format!("({})", parts.join(", "))
    }
}

fn format_rat_tuple(v: &[(i64, i64)]) -> String {
    let parts: Vec<String> = v
        .iter()
        .map(|&(n, d)| {
            if d == 0 {
                "nan".to_string()
            } else {
                format_py_float((n as f64) / (d as f64))
            }
        })
        .collect();
    if v.len() == 1 {
        format!("({},)", parts[0])
    } else {
        format!("({})", parts.join(", "))
    }
}

fn rational_json(num: i64, den: i64) -> Json {
    Json::object([("num", Json::Int(num)), ("den", Json::Int(den))])
}

pub fn is_py_whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
}

pub fn py_strip(s: &str) -> &str {
    s.trim_matches(is_py_whitespace)
}

pub(crate) fn trim_py_ascii_bytes_ws(s: &str) -> &str {
    s.trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c'))
}

fn parse_py_int_str(s: &str) -> Option<PyInt> {
    if s.is_empty() {
        return None;
    }
    let (neg, rest) = match s.as_bytes()[0] {
        b'+' => (false, &s[1..]),
        b'-' => (true, &s[1..]),
        _ => (false, s),
    };
    if rest.is_empty() || rest.starts_with('_') || rest.ends_with('_') || rest.contains("__") {
        return None;
    }
    if !rest.bytes().all(|b| b.is_ascii_digit() || b == b'_') {
        return None;
    }
    let digits: String = rest
        .bytes()
        .filter(|&b| b != b'_')
        .map(char::from)
        .collect();
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        Some(PyInt("0".to_string()))
    } else if neg {
        Some(PyInt(format!("-{trimmed}")))
    } else {
        Some(PyInt(trimmed.to_string()))
    }
}

fn parse_py_float_str(s: &str) -> Option<f64> {
    if s.is_empty() {
        return None;
    }
    let bytes = s.as_bytes();
    let mut clean = String::with_capacity(bytes.len());
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let prev_ok = i > 0 && bytes[i - 1].is_ascii_digit();
            let next_ok = i + 1 < bytes.len() && bytes[i + 1].is_ascii_digit();
            if !prev_ok || !next_ok {
                return None;
            }
            continue;
        }
        clean.push(char::from(b));
    }
    clean.parse::<f64>().ok()
}

pub fn bytes_to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(char::from(HEX[usize::from(b >> 4)]));
        out.push(char::from(HEX[usize::from(b & 0x0F)]));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[allow(clippy::excessive_precision)]
    fn format_py_float_matches_cpython_repr() {
        assert_eq!(format_py_float(0.0), "0.0");
        assert_eq!(format_py_float(-0.0), "-0.0");
        assert_eq!(format_py_float(1e-8), "1e-08");
        assert_eq!(format_py_float(1e-7), "1e-07");
        assert_eq!(
            format_py_float(9.999999999999999e-5),
            "9.999999999999999e-05"
        );
        assert_eq!(format_py_float(1e-4), "0.0001");
        assert_eq!(format_py_float(1.2345), "1.2345");
        assert_eq!(format_py_float(1e15), "1000000000000000.0");
        assert_eq!(format_py_float(1e16), "1e+16");
        assert_eq!(format_py_float(1e20), "1e+20");
        assert_eq!(format_py_float(5e-324), "5e-324");
        assert_eq!(format_py_float(f64::NAN), "nan");
        assert_eq!(format_py_float(f64::INFINITY), "inf");
        assert_eq!(format_py_float(f64::NEG_INFINITY), "-inf");

        // Exact decimal midpoint ties break toward even last digit (CPython dtoa mode=0)
        assert_eq!(
            format_py_float(1_000_000_000_000_000.25),
            "1000000000000000.2"
        );
        assert_eq!(
            format_py_float(1_000_000_000_000_000.75),
            "1000000000000000.8"
        );
        assert_eq!(
            format_py_float(1_000_000_000_000_000.125),
            "1000000000000000.1"
        );
        assert_eq!(
            format_py_float(1_000_000_000_000_000.375),
            "1000000000000000.4"
        );
        assert_eq!(
            format_py_float(1_125_899_906_842_624.25),
            "1125899906842624.2"
        );
        assert_eq!(
            format_py_float(-1_125_899_906_842_624.25),
            "-1125899906842624.2"
        );
        assert_eq!(
            format_py_float(-2_161_885_572_975_909.25),
            "-2161885572975909.2"
        );
        assert_eq!(
            format_py_float(2_161_885_572_975_909.25),
            "2161885572975909.2"
        );
        assert_eq!(
            format_py_float(1_125_899_906_842_624.75),
            "1125899906842624.8"
        );
        assert_eq!(
            format_py_float(-2_161_885_572_975_909.75),
            "-2161885572975909.8"
        );
        assert_eq!(format_py_float(9.999_999_999_999_998), "9.999999999999998");
        assert_eq!(
            format_py_float(2.225_073_858_507_201e-308),
            "2.225073858507201e-308"
        );
        assert_eq!(
            format_py_float(2.225_073_858_507_201_4e-308),
            "2.2250738585072014e-308"
        );
        assert_eq!(format_py_float(f64::MAX), "1.7976931348623157e+308");
        assert_eq!(format_py_float(0.1 + 0.2), "0.30000000000000004");
    }

    #[test]
    fn py_strip_includes_control_separators_1c_to_1f() {
        assert_eq!(py_strip("\x1cA\x1d"), "A");
        assert_eq!(py_strip("\x1e\x1fRICOH \u{85}\u{a0}"), "RICOH");
    }
}
