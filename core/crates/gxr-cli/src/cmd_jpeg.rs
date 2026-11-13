//! `jpeg-*` commands. Owner: wave-1 unit jpeg.

use gxr_jpeg::{
    build_exif, encode_jpeg, parse_exif_json, segments_json, set_meta, sof_dimensions, srgb_icc,
};
use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "jpeg-encode" => cmd_encode(args)?,
        "jpeg-segments" => cmd_segments(args)?,
        "jpeg-set-meta" => cmd_set_meta(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn has_flag(args: &[String], name: &str) -> bool {
    args.iter().any(|a| a == name)
}

fn opt_option<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    args.get(at + 1)
        .map(|s| Some(s.as_str()))
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

fn read_text(path: &str) -> Result<String, Failure> {
    std::fs::read_to_string(path).map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")))
}

fn write_bytes(path: &str, data: &[u8]) -> Result<(), Failure> {
    std::fs::write(path, data).map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))
}

fn cmd_encode(args: &[String]) -> Result<Json, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let q_str = common::option(args, "--quality")?;
    let quality: i32 = q_str
        .parse()
        .map_err(|_| Failure::Usage(format!("bad --quality {q_str:?}")))?;
    let exif_json = match opt_option(args, "--exif")? {
        Some(path) => read_text(path)?,
        None => String::new(),
    };

    let npy_bytes = common::read(in_path)?;
    let (h, w, rgb) = parse_rgb_u8_npy(&npy_bytes)?;
    let encoded = encode_jpeg(&rgb, w, h, quality, &exif_json)?;
    write_bytes(out_path, &encoded)?;

    let len = i64::try_from(encoded.len())
        .map_err(|_| Failure::Core(CoreError::new("encode_failed", "output too large")))?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("width", Json::Int(i64::from(w))),
        ("height", Json::Int(i64::from(h))),
        ("quality", Json::Int(i64::from(quality))),
        ("bytes", Json::Int(len)),
        ("sha256", Json::str(sha256_hex(&encoded))),
    ]))
}

fn cmd_segments(args: &[String]) -> Result<Json, Failure> {
    let in_path = match opt_option(args, "--in")? {
        Some(p) => p,
        None => args
            .first()
            .filter(|a| !a.starts_with("--"))
            .map(String::as_str)
            .ok_or_else(|| Failure::Usage("missing --in".into()))?,
    };
    let data = common::read(in_path)?;
    Ok(segments_json(&data)?)
}

fn cmd_set_meta(args: &[String]) -> Result<Json, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let exif_path = opt_option(args, "--exif")?;
    let with_icc = has_flag(args, "--icc");

    let data = common::read(in_path)?;
    let exif_payload = match exif_path {
        Some(path) => {
            let text = read_text(path)?;
            let spec = parse_exif_json(&text)?;
            let (w, h) = match (spec.width, spec.height) {
                (Some(w), Some(h)) => (w, h),
                _ => {
                    let (sw, sh) = sof_dimensions(&data)?;
                    (spec.width.unwrap_or(sw), spec.height.unwrap_or(sh))
                }
            };
            Some(build_exif(&spec, w, h)?)
        }
        None => None,
    };
    let icc_payload = if with_icc { Some(srgb_icc()) } else { None };
    let out_bytes = set_meta(&data, exif_payload.as_deref(), icc_payload)?;
    write_bytes(out_path, &out_bytes)?;

    let len = i64::try_from(out_bytes.len())
        .map_err(|_| Failure::Core(CoreError::new("container", "output too large")))?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("bytes", Json::Int(len)),
        ("sha256", Json::str(sha256_hex(&out_bytes))),
    ]))
}

/// Minimal `.npy` reader for C-contiguous `(H, W, 3)` `uint8` (`|u1` / `u1` / `<u1`) arrays.
fn parse_rgb_u8_npy(bytes: &[u8]) -> Result<(u32, u32, Vec<u8>), Failure> {
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(Failure::Core(CoreError::new(
            "bad_image",
            "input is not an .npy file",
        )));
    }
    let major = bytes[6];
    let (hdr_at, hdr_len) = match major {
        1 => (
            10usize,
            usize::from(u16::from_le_bytes([bytes[8], bytes[9]])),
        ),
        2 | 3 => {
            if bytes.len() < 12 {
                return Err(Failure::Core(CoreError::new(
                    "bad_image",
                    "truncated .npy header",
                )));
            }
            let len = usize::try_from(u32::from_le_bytes([
                bytes[8], bytes[9], bytes[10], bytes[11],
            ]))
            .map_err(|_| Failure::Core(CoreError::new("bad_image", "npy header overflow")))?;
            (12usize, len)
        }
        _ => {
            return Err(Failure::Core(CoreError::new(
                "bad_image",
                format!("unsupported .npy version {major}"),
            )));
        }
    };
    let data_at = hdr_at
        .checked_add(hdr_len)
        .ok_or_else(|| Failure::Core(CoreError::new("bad_image", "npy header overflow")))?;
    let hdr_bytes = bytes
        .get(hdr_at..data_at)
        .ok_or_else(|| Failure::Core(CoreError::new("bad_image", "truncated .npy header")))?;
    let hdr = std::str::from_utf8(hdr_bytes)
        .map_err(|_| Failure::Core(CoreError::new("bad_image", "non-UTF8 .npy header")))?;

    if !(hdr.contains("'|u1'")
        || hdr.contains("'u1'")
        || hdr.contains("'<u1'")
        || hdr.contains("\"|u1\""))
    {
        return Err(Failure::Core(CoreError::new(
            "bad_image",
            "expected uint8 (|u1) .npy array",
        )));
    }
    if !hdr.contains("'fortran_order': False") && !hdr.contains("\"fortran_order\": False") {
        return Err(Failure::Core(CoreError::new(
            "bad_image",
            "expected C-order (fortran_order: False) .npy array",
        )));
    }
    let (h, w, c) = parse_npy_shape(hdr)?;
    if c != 3 {
        return Err(Failure::Core(CoreError::new(
            "bad_image",
            format!("expected 3 RGB channels, got {c}"),
        )));
    }
    let expected = usize::try_from(h)
        .ok()
        .and_then(|hh| usize::try_from(w).ok().and_then(|ww| hh.checked_mul(ww)))
        .and_then(|px| px.checked_mul(3))
        .ok_or_else(|| Failure::Core(CoreError::new("bad_image", "npy image size overflow")))?;
    let payload = bytes
        .get(data_at..)
        .ok_or_else(|| Failure::Core(CoreError::new("bad_image", "missing .npy payload")))?;
    if payload.len() < expected {
        return Err(Failure::Core(CoreError::new(
            "bad_image",
            format!(
                "truncated .npy payload: expected {expected} bytes, got {}",
                payload.len()
            ),
        )));
    }
    Ok((h, w, payload[..expected].to_vec()))
}

fn parse_npy_shape(hdr: &str) -> Result<(u32, u32, u32), Failure> {
    let key_pos = hdr
        .find("'shape'")
        .or_else(|| hdr.find("\"shape\""))
        .ok_or_else(|| {
            Failure::Core(CoreError::new("bad_image", "missing shape in .npy header"))
        })?;
    let after = &hdr[key_pos..];
    let open = after.find('(').ok_or_else(|| {
        Failure::Core(CoreError::new(
            "bad_image",
            "malformed shape in .npy header",
        ))
    })?;
    let close = after[open + 1..].find(')').ok_or_else(|| {
        Failure::Core(CoreError::new(
            "bad_image",
            "malformed shape in .npy header",
        ))
    })?;
    let inside = &after[open + 1..open + 1 + close];
    let dims: Vec<u32> = inside
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| s.parse::<u32>())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| {
            Failure::Core(CoreError::new(
                "bad_image",
                "invalid shape dimension in .npy",
            ))
        })?;
    if dims.len() != 3 {
        return Err(Failure::Core(CoreError::new(
            "bad_image",
            format!("expected 3D (H, W, 3) array, got {}D", dims.len()),
        )));
    }
    Ok((dims[0], dims[1], dims[2]))
}
