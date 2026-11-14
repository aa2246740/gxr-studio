//! Port of `gxr_studio/hdr/capture.py` `_capture` and `differs`.
//!
//! `from_dng` needs `gxr-raw`'s TIFF reader, whose constructor is crate-private, and
//! `from_jpeg` is Pillow's EXIF reader. So this module starts from the tag dictionary those
//! readers hand to `_capture`.

use gxr_raw::Json;
use gxr_raw::pyrepr::py_float_repr;

use crate::unicode_tables::{DECIMAL_ZERO, FOLD1_KEY, FOLD1_VAL, FOLDN_KEY, FOLDN_VAL, PY_SPACE};

/// A tag value as `_capture` sees it.
#[derive(Clone, Debug, PartialEq)]
pub enum TagValue {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    /// A tuple or list. Only number tags accept it (one element is unwrapped).
    List(Vec<TagValue>),
}

/// A captured field: text (casefolded) or a positive finite number.
#[derive(Clone, Debug, PartialEq)]
pub enum Field {
    Text(String),
    Number(f64),
}

const TEXT_TAGS: [(u32, &str); 6] = [
    (271, "Make"),
    (272, "Model"),
    (50708, "UniqueCameraModel"),
    (36867, "DateTimeOriginal"),
    (42036, "LensModel"),
    (42016, "ImageUniqueID"),
];
const NUMBER_TAGS: [(u32, &str); 4] = [
    (33434, "ExposureTime"),
    (33437, "FNumber"),
    (34855, "ISO"),
    (37386, "FocalLength"),
];

/// `capture.MISMATCH`.
pub const MISMATCH: &str = "RAW 与 JPG 不是同一次拍摄";

/// Python `str.isspace` for one character.
fn py_space(c: char) -> bool {
    PY_SPACE.binary_search(&u32::from(c)).is_ok()
}

/// `unicodedata.decimal(c)` for a Unicode decimal digit (category Nd), else `None`.
fn decimal_digit(c: char) -> Option<u32> {
    let cp = u32::from(c);
    let at = DECIMAL_ZERO.partition_point(|&z| z <= cp);
    let zero = *DECIMAL_ZERO.get(at.checked_sub(1)?)?;
    (cp - zero < 10).then_some(cp - zero)
}

/// `str(value)` for the scalar types a text tag can hold. Lists are refused by the caller.
fn py_str(v: &TagValue) -> Option<String> {
    match v {
        TagValue::None => Some(String::new()),
        TagValue::Bool(b) => Some(if *b { "True" } else { "False" }.to_string()),
        TagValue::Int(n) => Some(n.to_string()),
        TagValue::Float(f) => Some(py_float_repr(*f)),
        TagValue::Text(s) => Some(s.clone()),
        TagValue::List(_) => None,
    }
}

/// Port of gxr_studio/hdr/capture.py:26 `_text` (`None` → empty).
fn text(v: Option<&TagValue>) -> Option<String> {
    let raw = match v {
        None | Some(TagValue::None) => return Some(String::new()),
        Some(v) => py_str(v)?,
    };
    let cleaned = raw.replace('\u{0}', "");
    Some(
        cleaned
            .split(py_space)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" "),
    )
}

/// `str.casefold`: Python's full case folding, code point by code point, from the generated
/// Unicode 16.0 table (Sol r1 P3-06).
fn casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        let cp = u32::from(c);
        if let Ok(i) = FOLD1_KEY.binary_search(&cp) {
            out.extend(char::from_u32(FOLD1_VAL[i]));
        } else if let Ok(i) = FOLDN_KEY.binary_search(&cp) {
            out.extend(
                FOLDN_VAL[3 * i..3 * i + 3]
                    .iter()
                    .filter(|&&v| v != 0)
                    .filter_map(|&v| char::from_u32(v)),
            );
        } else {
            out.push(c);
        }
    }
    out
}

/// Python `float(str)` (`PyFloat_FromString`), for the decimal text a tag can hold (Sol r1
/// P2-05). Non-ASCII whitespace becomes a space and each Unicode decimal digit its ASCII digit
/// (`_PyUnicode_TransformDecimalAndSpaceToASCII`); any other character from U+007F up is invalid.
/// Then PEP 515 underscores (only between digits), surrounding whitespace, an optional sign,
/// `inf`/`infinity`/`nan`, or digits with an optional point and exponent.
fn py_float_text(s: &str) -> Option<f64> {
    let mut ascii = String::with_capacity(s.len());
    for c in s.chars() {
        // Below U+007F a character passes through unchanged (so U+001C..U+001F stay and make
        // the text invalid); above it, whitespace becomes a space and a decimal digit its
        // ASCII digit; anything else is invalid.
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
    // Digits, at most one point, at least one digit, then an optional exponent.
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

/// `float(raw_value)` after the one-element unwrap; `None` when Python skips the tag.
fn number(v: Option<&TagValue>) -> Option<f64> {
    let v = match v? {
        TagValue::List(items) if items.len() == 1 => &items[0],
        TagValue::List(_) | TagValue::None => return None,
        other => other,
    };
    match v {
        TagValue::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        #[allow(clippy::cast_precision_loss)]
        TagValue::Int(n) => Some(*n as f64),
        TagValue::Float(f) => Some(*f),
        TagValue::Text(s) => py_float_text(s),
        TagValue::None | TagValue::List(_) => None,
    }
}

/// Port of gxr_studio/hdr/capture.py:32 `_capture`, in Python's key order.
///
/// `None` when a text tag holds a list (Python would print its repr; not ported).
pub fn capture_of(tags: &[(u32, TagValue)]) -> Option<Vec<(String, Field)>> {
    let get = |tag: u32| tags.iter().rev().find(|(t, _)| *t == tag).map(|(_, v)| v);
    let mut result = Vec::new();
    for (tag, name) in TEXT_TAGS {
        let value = text(get(tag))?;
        if !value.is_empty() && (tag != 36867 || !value.starts_with("0000")) {
            result.push((name.to_string(), Field::Text(casefold(&value))));
        }
    }
    let subsec = text(get(37521))?;
    if !subsec.is_empty() && subsec.bytes().all(|b| b.is_ascii_digit()) {
        let kept = subsec.trim_end_matches('0');
        result.push((
            "SubSecTimeOriginal".to_string(),
            Field::Text(if kept.is_empty() { "0" } else { kept }.to_string()),
        ));
    }
    for (tag, name) in NUMBER_TAGS {
        if let Some(n) = number(get(tag))
            && n.is_finite()
            && n > 0.0
        {
            result.push((name.to_string(), Field::Number(n)));
        }
    }
    Some(result)
}

/// `math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)`.
fn isclose(a: f64, b: f64) -> bool {
    if a == b {
        return true;
    }
    if a.is_infinite() || b.is_infinite() {
        return false;
    }
    let diff = (b - a).abs();
    diff <= (1e-6 * b).abs() || diff <= (1e-6 * a).abs() || diff <= 1e-9
}

/// Port of gxr_studio/hdr/capture.py:71 differs: only keys present on both sides count.
pub fn differs(raw: &[(String, Field)], jpeg: &[(String, Field)]) -> bool {
    raw.iter().any(|(name, a)| {
        jpeg.iter()
            .find(|(k, _)| k == name)
            .is_some_and(|(_, b)| match (a, b) {
                (Field::Number(x), Field::Number(y)) => !isclose(*x, *y),
                _ => a != b,
            })
    })
}

/// The captured fields as a JSON object, in order.
pub fn field_json(fields: &[(String, Field)]) -> Json {
    Json::object(fields.iter().map(|(k, v)| {
        (
            k.clone(),
            match v {
                Field::Text(s) => Json::str(s.clone()),
                Field::Number(n) => Json::Float(*n),
            },
        )
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_is_cleaned_and_casefolded() {
        let tags = vec![
            (271, TagValue::Text(" RICOH\u{0}  ".into())),
            (36867, TagValue::Text("0000:00:00 00:00:00".into())),
            (37521, TagValue::Text("120".into())),
            (33434, TagValue::List(vec![TagValue::Float(0.004)])),
            (34855, TagValue::Int(0)),
        ];
        let got = capture_of(&tags).unwrap();
        assert_eq!(
            got,
            vec![
                ("Make".to_string(), Field::Text("ricoh".into())),
                ("SubSecTimeOriginal".to_string(), Field::Text("12".into())),
                ("ExposureTime".to_string(), Field::Number(0.004)),
            ]
        );
    }

    #[test]
    fn only_shared_keys_count() {
        let a = vec![("ISO".to_string(), Field::Number(200.0))];
        let b = vec![("Make".to_string(), Field::Text("x".into()))];
        assert!(!differs(&a, &b));
        let c = vec![("ISO".to_string(), Field::Number(200.0001))];
        assert!(!differs(&a, &c));
        let d = vec![("ISO".to_string(), Field::Number(201.0))];
        assert!(differs(&a, &d));
    }
}
