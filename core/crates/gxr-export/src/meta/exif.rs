//! Port of `gxr_studio/export.py` source EXIF extraction (`write_with_exif` lines 295–317,
//! `_clean_text` line 192, `_DROP_EXIF_TAGS`).

use std::collections::BTreeMap;

use gxr_jpeg::{ExifSpec, ExifVal};
use gxr_raw::Json;

use super::tiff_exif::{ExifValue, load_image_exif, open_image};

const DROP_EXIF_TAGS: &[u16] = &[37500];

/// Source photo EXIF tags extracted for export (`base` = IFD0 subset, `sub` = EXIF IFD `0x8769`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SourceExif {
    pub base: BTreeMap<u16, ExifValue>,
    pub sub: BTreeMap<u16, ExifValue>,
}

impl SourceExif {
    /// Render as `export-meta-exif` JSON (`bytes` as `{"hex": ...}`, rationals as `{"num": ..., "den": ...}`).
    pub fn to_json(&self) -> Json {
        Json::object([
            ("base", tag_map_json(&self.base, false)),
            ("sub", tag_map_json(&self.sub, false)),
        ])
    }

    /// Adapter producing `gxr-jpeg`-compatible JSON (`bytes` as `{"bytes": [...]}`).
    pub fn to_jpeg_exif_json(&self) -> Json {
        Json::object([
            ("base", tag_map_json(&self.base, true)),
            ("sub", tag_map_json(&self.sub, true)),
        ])
    }

    /// Adapter converting directly to `gxr_jpeg::ExifSpec` for `gxr_jpeg::build_exif`.
    pub fn to_jpeg_exif_spec(&self) -> ExifSpec {
        let mut spec = ExifSpec::default();
        for (&tag, val) in &self.base {
            spec.base.insert(tag, exif_value_to_jpeg(val));
        }
        for (&tag, val) in &self.sub {
            spec.exif.insert(tag, exif_value_to_jpeg(val));
        }
        spec
    }
}

fn tag_map_json(map: &BTreeMap<u16, ExifValue>, for_jpeg: bool) -> Json {
    Json::object(map.iter().map(|(&tag, val)| {
        let v = if for_jpeg {
            val.to_jpeg_json()
        } else {
            val.to_exif_json()
        };
        (tag.to_string(), v)
    }))
}

fn exif_value_to_jpeg(val: &ExifValue) -> ExifVal {
    match val {
        ExifValue::Str(s) => ExifVal::Str(s.clone()),
        ExifValue::Bytes(b) => ExifVal::Bytes(b.clone()),
        ExifValue::Int(i) => ExifVal::Ints(vec![*i]),
        ExifValue::Ints(v) => ExifVal::Ints(v.clone()),
        ExifValue::Float(f) => ExifVal::Floats(vec![*f]),
        ExifValue::Floats(v) => ExifVal::Floats(v.clone()),
        ExifValue::Rational(n, d) => ExifVal::Rationals(vec![(*n, *d)]),
        ExifValue::Rationals(v) => ExifVal::Rationals(v.clone()),
    }
}

/// Port of `gxr_studio/export.py:192 _clean_text`.
pub fn clean_text(value: Option<&ExifValue>) -> Option<String> {
    let val = value?;
    let raw = match val {
        ExifValue::Bytes(b) => b
            .iter()
            .filter(|&&byte| byte < 0x80)
            .map(|&byte| char::from(byte))
            .collect::<String>(),
        other => other.to_python_str(),
    };
    let cleaned = raw.replace('\0', "");
    let trimmed = cleaned.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Port of `gxr_studio/export.py:299-316` source EXIF extraction (`write_with_exif`).
///
/// Matches Python's `try: with Image.open(source) as src: ... except Exception: pass`, returning
/// empty `base` and `sub` maps if the bytes cannot be identified as JPEG or TIFF/DNG.
pub fn read_source_exif(bytes: &[u8]) -> SourceExif {
    let Ok(doc) = open_image(bytes) else {
        return SourceExif::default();
    };
    let loaded = load_image_exif(bytes, &doc);

    let mut base = BTreeMap::new();
    if let Some(copy) = loaded.base.get(&33432) {
        base.insert(33432, copy.clone());
    }
    for tag in [271u16, 272, 306] {
        if let Some(text) = clean_text(loaded.base.get(&tag)) {
            base.insert(tag, ExifValue::Str(text));
        }
    }

    let mut sub = BTreeMap::new();
    for (&tag, val) in &loaded.sub {
        if !DROP_EXIF_TAGS.contains(&tag) {
            sub.insert(tag, val.clone());
        }
    }
    if clean_text(sub.get(&42036)).is_none()
        && let Some(lens) = clean_text(loaded.base.get(&50708))
    {
        let stripped = lens.strip_prefix("RICOH ").unwrap_or(&lens).trim();
        sub.insert(42036, ExifValue::Str(stripped.to_string()));
    }

    SourceExif { base, sub }
}

#[cfg(test)]
mod tests {
    use gxr_jpeg::{build_exif, parse_exif_json};

    use super::*;

    #[test]
    fn clean_text_strips_nul_whitespace_and_non_ascii_bytes() {
        assert_eq!(
            clean_text(Some(&ExifValue::Str(" RICOH \0 ".to_string()))).as_deref(),
            Some("RICOH")
        );
        assert_eq!(
            clean_text(Some(&ExifValue::Bytes(vec![b'A', 0xff, 0, b'B', b' ']))).as_deref(),
            Some("AB")
        );
        assert_eq!(clean_text(Some(&ExifValue::Str(" \0 ".to_string()))), None);
        assert_eq!(clean_text(None), None);
    }

    #[test]
    fn source_exif_adapters_feed_gxr_jpeg_roundtrip() {
        let mut src = SourceExif::default();
        src.base.insert(271, ExifValue::Str("RICOH".to_string()));
        src.base.insert(272, ExifValue::Str("GXR".to_string()));
        src.base
            .insert(306, ExifValue::Str("2024:05:06 07:08:09".to_string()));
        src.base
            .insert(33432, ExifValue::Str("Studio Copyright".to_string()));
        src.sub.insert(34855, ExifValue::Int(400));
        src.sub.insert(33434, ExifValue::Rational(1, 250));
        src.sub.insert(33437, ExifValue::Rational(28, 10));
        src.sub
            .insert(42036, ExifValue::Str("GR LENS A12 50mm".to_string()));
        src.sub.insert(36864, ExifValue::Bytes(b"0230".to_vec()));

        // Adapter 1: direct ExifSpec -> build_exif
        let spec = src.to_jpeg_exif_spec();
        assert_eq!(spec.base.len(), 4);
        assert_eq!(spec.exif.len(), 5);
        let built = build_exif(&spec, 16, 12).expect("build_exif from spec");
        assert!(built.starts_with(b"Exif\x00\x00"));

        // Adapter 2: JSON -> parse_exif_json -> build_exif
        let json_str = src.to_jpeg_exif_json().render();
        let parsed = parse_exif_json(&json_str).expect("parse_exif_json");
        assert_eq!(parsed.base, spec.base);
        assert_eq!(parsed.exif, spec.exif);

        // Read back the built APP1 segment with read_source_exif (wrapping in minimal JPEG)
        let mut jpg = Vec::new();
        jpg.extend_from_slice(b"\xff\xd8\xff\xe1");
        let seg_len = u16::try_from(built.len() + 2).expect("seg len");
        jpg.extend_from_slice(&seg_len.to_be_bytes());
        jpg.extend_from_slice(&built);
        jpg.extend_from_slice(b"\xff\xc0\x00\x0b\x08\x00\x0c\x00\x10\x01\x01\x11\x00");
        jpg.extend_from_slice(b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xff\xd9");

        let reread = read_source_exif(&jpg);
        assert_eq!(
            reread.base.get(&271),
            Some(&ExifValue::Str("RICOH".to_string()))
        );
        assert_eq!(
            reread.base.get(&272),
            Some(&ExifValue::Str("GXR".to_string()))
        );
        assert_eq!(
            reread.base.get(&33432),
            Some(&ExifValue::Str("Studio Copyright".to_string()))
        );
        assert_eq!(reread.sub.get(&34855), Some(&ExifValue::Int(400)));
        assert_eq!(reread.sub.get(&33434), Some(&ExifValue::Rational(1, 250)));
        assert_eq!(
            reread.sub.get(&42036),
            Some(&ExifValue::Str("GR LENS A12 50mm".to_string()))
        );
    }

    #[test]
    fn unreadable_source_returns_empty_exif() {
        let empty = read_source_exif(b"not an image");
        assert!(empty.base.is_empty());
        assert!(empty.sub.is_empty());
    }
}
