//! Port of `gxr_studio/export.py` EXIF tag building (`build_exif`, `write_with_exif`).

use std::collections::BTreeMap;

use gxr_raw::CoreError;

use crate::exif_tiff::serialize_ifd0_with_exif;
use crate::json_parse::{self, Value as JsonVal};
use crate::segments::{EXIF_SIG, MAX_MARKER_PAYLOAD};

pub const SOFTWARE: &str = "GXR Studio";

const DROP_BASE_TAGS: &[u16] = &[274, 34665, 34853, 330, 513, 514];
const DROP_EXIF_TAGS: &[u16] = &[37500];
const REDUCED_BASE_TAGS: &[u16] = &[270, 271, 272, 306, 33432];
const REDUCED_EXIF_TAGS: &[u16] = &[36867, 36868, 34855, 42036];

/// Typed EXIF tag value matching Pillow's `Image.Exif` value representation.
#[derive(Clone, Debug, PartialEq)]
pub enum ExifVal {
    Str(String),
    Bytes(Vec<u8>),
    Ints(Vec<i64>),
    Floats(Vec<f64>),
    Rationals(Vec<(i64, i64)>),
}

/// Parsed EXIF specification from `EXIF.json`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ExifSpec {
    pub base: BTreeMap<u16, ExifVal>,
    pub exif: BTreeMap<u16, ExifVal>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub full: Option<bool>,
}

/// Parse `EXIF.json` into an `ExifSpec`. Empty / whitespace input produces an empty spec.
pub fn parse_exif_json(text: &str) -> Result<ExifSpec, CoreError> {
    if text.trim().is_empty() {
        return Ok(ExifSpec::default());
    }
    let root = json_parse::parse(text)?;
    let JsonVal::Object(pairs) = root else {
        return Err(CoreError::new("bad_exif", "EXIF JSON must be an object"));
    };
    let mut spec = ExifSpec::default();
    let mut convenience: Vec<(String, JsonVal)> = Vec::new();
    for (key, val) in pairs {
        match key.as_str() {
            "base" => parse_tag_map(val, &mut spec.base)?,
            "exif" | "sub" => parse_tag_map(val, &mut spec.exif)?,
            "width" => spec.width = Some(parse_dim(val, "width")?),
            "height" => spec.height = Some(parse_dim(val, "height")?),
            "full" => match val {
                JsonVal::Bool(b) => spec.full = Some(b),
                JsonVal::Null => {}
                _ => return Err(CoreError::new("bad_exif", "EXIF 'full' must be a boolean")),
            },
            _ => convenience.push((key, val)),
        }
    }
    for (key, val) in convenience {
        if matches!(val, JsonVal::Null) {
            continue;
        }
        match key.as_str() {
            "description" | "note" => {
                spec.base.entry(270).or_insert(parse_exif_val(val)?);
            }
            "make" => {
                spec.base.entry(271).or_insert(parse_exif_val(val)?);
            }
            "model" => {
                spec.base.entry(272).or_insert(parse_exif_val(val)?);
            }
            "datetime" | "date_time" => {
                spec.base.entry(306).or_insert(parse_exif_val(val)?);
            }
            "copyright" => {
                spec.base.entry(33432).or_insert(parse_exif_val(val)?);
            }
            "date_time_original" | "datetime_original" => {
                spec.exif.entry(36867).or_insert(parse_exif_val(val)?);
            }
            "date_time_digitized" | "datetime_digitized" => {
                spec.exif.entry(36868).or_insert(parse_exif_val(val)?);
            }
            "iso" => {
                spec.exif.entry(34855).or_insert(parse_exif_val(val)?);
            }
            "lens" | "lens_model" => {
                spec.exif.entry(42036).or_insert(parse_exif_val(val)?);
            }
            _ => {}
        }
    }
    Ok(spec)
}

fn parse_dim(val: JsonVal, name: &str) -> Result<u32, CoreError> {
    let JsonVal::Int(i) = val else {
        return Err(CoreError::new(
            "bad_exif",
            format!("EXIF '{name}' must be an integer"),
        ));
    };
    u32::try_from(i).map_err(|_| CoreError::new("bad_exif", format!("EXIF '{name}' out of range")))
}

fn parse_tag_map(val: JsonVal, out: &mut BTreeMap<u16, ExifVal>) -> Result<(), CoreError> {
    if matches!(val, JsonVal::Null) {
        return Ok(());
    }
    let JsonVal::Object(pairs) = val else {
        return Err(CoreError::new("bad_exif", "EXIF tag map must be an object"));
    };
    for (k, v) in pairs {
        let tag: u16 = k
            .parse()
            .map_err(|_| CoreError::new("bad_exif", format!("invalid EXIF tag id {k:?}")))?;
        if matches!(v, JsonVal::Null) {
            continue;
        }
        out.insert(tag, parse_exif_val(v)?);
    }
    Ok(())
}

fn parse_rational_pair(val: &JsonVal) -> Result<(i64, i64), CoreError> {
    match val {
        JsonVal::Array(arr) if arr.len() == 2 => {
            let (JsonVal::Int(n), JsonVal::Int(d)) = (&arr[0], &arr[1]) else {
                return Err(CoreError::new(
                    "bad_exif",
                    "rational elements must be integers",
                ));
            };
            Ok((*n, *d))
        }
        JsonVal::Object(pairs) => {
            let mut n = None;
            let mut d = None;
            for (k, v) in pairs {
                if k == "rational" {
                    return parse_rational_pair(v);
                }
                if k == "num" {
                    if let JsonVal::Int(i) = v {
                        n = Some(*i);
                    }
                } else if k == "den"
                    && let JsonVal::Int(i) = v
                {
                    d = Some(*i);
                }
            }
            match (n, d) {
                (Some(n), Some(d)) => Ok((n, d)),
                _ => Err(CoreError::new("bad_exif", "invalid rational object")),
            }
        }
        _ => Err(CoreError::new("bad_exif", "expected rational pair")),
    }
}

fn parse_exif_val(val: JsonVal) -> Result<ExifVal, CoreError> {
    match val {
        JsonVal::Str(s) => Ok(ExifVal::Str(s)),
        JsonVal::Int(i) => Ok(ExifVal::Ints(vec![i])),
        JsonVal::Float(f) => Ok(ExifVal::Floats(vec![f])),
        JsonVal::Object(pairs) => parse_exif_object(pairs),
        JsonVal::Array(items) => parse_exif_array(items),
        JsonVal::Bool(_) | JsonVal::Null => Err(CoreError::new(
            "bad_exif",
            "unsupported EXIF boolean or null value",
        )),
    }
}

fn parse_exif_object(pairs: Vec<(String, JsonVal)>) -> Result<ExifVal, CoreError> {
    if pairs.len() == 1 {
        let (k, v) = &pairs[0];
        match k.as_str() {
            "rational" => return Ok(ExifVal::Rationals(vec![parse_rational_pair(v)?])),
            "rationals" => {
                let JsonVal::Array(items) = v else {
                    return Err(CoreError::new("bad_exif", "'rationals' must be an array"));
                };
                let mut rats = Vec::with_capacity(items.len());
                for item in items {
                    rats.push(parse_rational_pair(item)?);
                }
                return Ok(ExifVal::Rationals(rats));
            }
            "bytes" => {
                let JsonVal::Array(items) = v else {
                    return Err(CoreError::new("bad_exif", "'bytes' must be an array"));
                };
                let mut bytes = Vec::with_capacity(items.len());
                for item in items {
                    let JsonVal::Int(i) = item else {
                        return Err(CoreError::new("bad_exif", "'bytes' items must be integers"));
                    };
                    let b = u8::try_from(*i)
                        .map_err(|_| CoreError::new("bad_exif", "byte value out of 0..=255"))?;
                    bytes.push(b);
                }
                return Ok(ExifVal::Bytes(bytes));
            }
            "latin1" => {
                let JsonVal::Str(s) = v else {
                    return Err(CoreError::new("bad_exif", "'latin1' must be a string"));
                };
                return Ok(ExifVal::Bytes(encode_latin1(s)?));
            }
            _ => {}
        }
    }
    if pairs.iter().any(|(k, _)| k == "num") {
        return Ok(ExifVal::Rationals(vec![parse_rational_pair(
            &JsonVal::Object(pairs),
        )?]));
    }
    Err(CoreError::new("bad_exif", "unsupported EXIF value object"))
}

fn parse_exif_array(items: Vec<JsonVal>) -> Result<ExifVal, CoreError> {
    if items.is_empty() {
        return Err(CoreError::new("bad_exif", "EXIF array cannot be empty"));
    }
    if items.iter().all(|v| matches!(v, JsonVal::Int(_))) {
        let ints = items
            .into_iter()
            .map(|v| match v {
                JsonVal::Int(i) => i,
                _ => 0,
            })
            .collect();
        return Ok(ExifVal::Ints(ints));
    }
    if items
        .iter()
        .all(|v| matches!(v, JsonVal::Int(_) | JsonVal::Float(_)))
    {
        let floats = items
            .into_iter()
            .map(|v| match v {
                #[allow(clippy::cast_precision_loss)]
                JsonVal::Int(i) => i as f64,
                JsonVal::Float(f) => f,
                _ => 0.0,
            })
            .collect();
        return Ok(ExifVal::Floats(floats));
    }
    if items
        .iter()
        .all(|v| matches!(v, JsonVal::Object(_) | JsonVal::Array(_)))
    {
        let mut rats = Vec::with_capacity(items.len());
        for item in &items {
            rats.push(parse_rational_pair(item)?);
        }
        return Ok(ExifVal::Rationals(rats));
    }
    Err(CoreError::new(
        "bad_exif",
        "unsupported EXIF array element types",
    ))
}

fn encode_latin1(s: &str) -> Result<Vec<u8>, CoreError> {
    let mut out = Vec::with_capacity(s.len());
    for ch in s.chars() {
        let code = u32::from(ch);
        let byte = u8::try_from(code).map_err(|_| {
            CoreError::new(
                "encode_failed",
                format!("character U+{code:04X} cannot be encoded as latin-1"),
            )
        })?;
        out.push(byte);
    }
    Ok(out)
}

/// Port of `gxr_studio/export.py:254 build_exif`.
///
/// Produces the `b"Exif\x00\x00..."` APP1 payload, trying `full=true` first and falling back
/// to `full=false` if the serialized payload exceeds `MAX_MARKER_PAYLOAD` (65533 bytes).
pub fn build_exif(spec: &ExifSpec, width: u32, height: u32) -> Result<Vec<u8>, CoreError> {
    let attempts: &[bool] = match spec.full {
        Some(false) => &[false],
        _ => &[true, false],
    };
    let mut last_err = None;
    for &full in attempts {
        match build_exif_attempt(spec, width, height, full) {
            Ok(bytes) if bytes.len() <= MAX_MARKER_PAYLOAD => return Ok(bytes),
            Ok(_) => {
                last_err = Some(CoreError::new("encode_failed", "EXIF data is too long"));
            }
            Err(e) => last_err = Some(e),
        }
    }
    Err(last_err.unwrap_or_else(|| CoreError::new("encode_failed", "EXIF data is too long")))
}

fn build_exif_attempt(
    spec: &ExifSpec,
    width: u32,
    height: u32,
    full: bool,
) -> Result<Vec<u8>, CoreError> {
    let mut base: BTreeMap<u16, ExifVal> = BTreeMap::new();
    for (&tag, val) in &spec.base {
        if DROP_BASE_TAGS.contains(&tag) {
            continue;
        }
        if !full && !REDUCED_BASE_TAGS.contains(&tag) {
            continue;
        }
        let encoded = match (tag, val) {
            (33432, ExifVal::Str(s)) => ExifVal::Bytes(encode_latin1(s)?),
            (270, ExifVal::Str(s)) => ExifVal::Bytes(s.as_bytes().to_vec()),
            _ => val.clone(),
        };
        base.insert(tag, encoded);
    }
    base.insert(274, ExifVal::Ints(vec![1]));
    base.insert(305, ExifVal::Str(SOFTWARE.to_string()));

    let mut sub: BTreeMap<u16, ExifVal> = BTreeMap::new();
    for (&tag, val) in &spec.exif {
        if DROP_EXIF_TAGS.contains(&tag) {
            continue;
        }
        if !full && !REDUCED_EXIF_TAGS.contains(&tag) {
            continue;
        }
        sub.insert(tag, val.clone());
    }
    sub.insert(40962, ExifVal::Ints(vec![i64::from(width)]));
    sub.insert(40963, ExifVal::Ints(vec![i64::from(height)]));

    let tiff = serialize_ifd0_with_exif(&base, &sub)?;
    let mut out = Vec::with_capacity(EXIF_SIG.len() + 8 + tiff.len());
    out.extend_from_slice(EXIF_SIG);
    out.extend_from_slice(b"MM\x00*\x00\x00\x00\x08");
    out.extend_from_slice(&tiff);
    Ok(out)
}
