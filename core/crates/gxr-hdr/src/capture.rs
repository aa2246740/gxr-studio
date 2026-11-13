//! Port of `gxr_studio/hdr/capture.py` `_capture` and `differs`.
//!
//! `from_dng` / `from_jpeg` stay in Python for this unit: `gxr-raw`'s TIFF reader
//! is crate-private, and JPEG EXIF is Pillow's. The CLI takes the tag map those
//! readers would hand to `_capture`.

use std::collections::BTreeMap;

use gxr_raw::Json;

/// A field after `_capture`: text is already casefolded, numbers are finite and positive.
#[derive(Clone, Debug, PartialEq)]
pub enum Field {
    Text(String),
    Number(f64),
}

const TEXT: [(i64, &str); 6] = [
    (271, "Make"),
    (272, "Model"),
    (50708, "UniqueCameraModel"),
    (36867, "DateTimeOriginal"),
    (42036, "LensModel"),
    (42016, "ImageUniqueID"),
];
const NUMBERS: [(i64, &str); 4] = [
    (33434, "ExposureTime"),
    (33437, "FNumber"),
    (34855, "ISO"),
    (37386, "FocalLength"),
];

fn text_of(value: &str) -> String {
    value.replace('\u{0}', "").split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Port of gxr_studio/hdr/capture.py:32 `_capture`.
///
/// `tags` maps a TIFF tag number to either a string or a finite float. A missing
/// tag is simply absent. One-element lists should already have been unwrapped.
pub fn capture_of(tags: &BTreeMap<i64, Field>) -> BTreeMap<String, Field> {
    let mut result = BTreeMap::new();
    for (tag, name) in TEXT {
        let Some(Field::Text(raw)) = tags.get(&tag) else {
            continue;
        };
        let value = text_of(raw);
        if value.is_empty() || (tag == 36867 && value.starts_with("0000")) {
            continue;
        }
        result.insert((*name).to_string(), Field::Text(value.to_lowercase()));
    }
    if let Some(Field::Text(raw)) = tags.get(&37521) {
        let subsec = text_of(raw);
        if subsec.is_ascii() && !subsec.is_empty() && subsec.bytes().all(|b| b.is_ascii_digit()) {
            let trimmed = subsec.trim_end_matches('0');
            let kept = if trimmed.is_empty() { "0" } else { trimmed };
            result.insert("SubSecTimeOriginal".to_string(), Field::Text(kept.to_string()));
        }
    }
    for (tag, name) in NUMBERS {
        let Some(Field::Number(number)) = tags.get(&tag) else {
            continue;
        };
        if number.is_finite() && *number > 0.0 {
            result.insert((*name).to_string(), Field::Number(*number));
        }
    }
    result
}

/// Port of gxr_studio/hdr/capture.py:71 differs.
pub fn differs(raw: &BTreeMap<String, Field>, jpeg: &BTreeMap<String, Field>) -> bool {
    for (name, a) in raw {
        let Some(b) = jpeg.get(name) else {
            continue;
        };
        match (a, b) {
            (Field::Number(x), Field::Number(y)) => {
                // math.isclose(rel_tol=1e-6, abs_tol=1e-9).
                let limit = (1e-6 * x.abs().max(y.abs())).max(1e-9);
                if !x.is_finite() || !y.is_finite() || (x - y).abs() > limit {
                    return true;
                }
            }
            (Field::Text(x), Field::Text(y)) if x != y => return true,
            (Field::Text(_), Field::Number(_)) | (Field::Number(_), Field::Text(_)) => return true,
            _ => {}
        }
    }
    false
}

pub fn field_json(fields: &BTreeMap<String, Field>) -> Json {
    let mut pairs = Vec::new();
    for (k, v) in fields {
        let value = match v {
            Field::Text(s) => Json::str(s),
            Field::Number(n) => Json::Float(*n),
        };
        pairs.push((k.clone(), value));
    }
    Json::object(pairs)
}

/// `MISMATCH` from capture.py.
pub const MISMATCH: &str = "RAW 与 JPG 不是同一次拍摄";
