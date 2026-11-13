//! `geom-*` subcommands. Owner: wave-1 unit geometry.

use std::io::Write;

use gxr_finish::crop::crop_box_json;
use gxr_finish::geometry::fit::{
    clamp_norm_rect, fit_ratio_in_valid, fitted_crop, header_value, normalized_valid_rect,
};
use gxr_finish::geometry::json::{JsonValue, parse_json};
use gxr_finish::{final_box_json, parse_geometry, warp, warp_valid};
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "geom-crop-box" => cmd_crop_box(args)?,
        "geom-final-box" => cmd_final_box(args)?,
        "geom-warp" => cmd_warp(args)?,
        "geom-warp-valid" => cmd_warp_valid(args)?,
        "geom-header" => cmd_header(args)?,
        "geom-fitted-crop" => cmd_fitted_crop(args)?,
        "geom-clamp-rect" => cmd_clamp_rect(args)?,
        "geom-fit-ratio" => cmd_fit_ratio(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn parse_dim(args: &[String], name: &str) -> Result<usize, Failure> {
    let raw = common::option(args, name)?;
    let v: usize = raw
        .parse()
        .map_err(|_| Failure::Usage(format!("bad integer for {name}: {raw:?}")))?;
    if v == 0 {
        return Err(Failure::Usage(format!("{name} must be > 0")));
    }
    Ok(v)
}

fn opt_dim(args: &[String], name: &str, default: usize) -> Result<usize, Failure> {
    if args.iter().any(|a| a == name) {
        parse_dim(args, name)
    } else {
        Ok(default)
    }
}

fn has_flag(args: &[String], name: &str) -> bool {
    args.iter().any(|a| a == name)
}

fn opt_str<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    if args.iter().any(|a| a == name) {
        Ok(Some(common::option(args, name)?))
    } else {
        Ok(None)
    }
}

fn cmd_crop_box(args: &[String]) -> Result<Json, Failure> {
    let w = parse_dim(args, "--w")? as i64;
    let h = parse_dim(args, "--h")? as i64;
    let crop = common::option(args, "--crop")?;
    Ok(crop_box_json(w, h, crop)?)
}

fn cmd_final_box(args: &[String]) -> Result<Json, Failure> {
    let w = parse_dim(args, "--w")?;
    let h = parse_dim(args, "--h")?;
    let geometry = opt_str(args, "--geometry")?.unwrap_or("{}");
    let crop = opt_str(args, "--crop")?.unwrap_or("null");
    Ok(final_box_json(w, h, geometry, crop)?)
}

fn cmd_header(args: &[String]) -> Result<Json, Failure> {
    if let Some(rect_str) = opt_str(args, "--rect")? {
        let parts: Vec<&str> = rect_str.split(',').collect();
        if parts.len() != 4 {
            return Err(Failure::Usage("bad --rect, expected X,Y,W,H".into()));
        }
        let mut rect = [0.0_f64; 4];
        for (i, p) in parts.iter().enumerate() {
            rect[i] = p
                .trim()
                .parse()
                .map_err(|_| Failure::Usage(format!("bad float in --rect: {p:?}")))?;
        }
        return Ok(Json::object([("header", Json::str(header_value(rect)))]));
    }
    let geom_str = common::option(args, "--geometry")?;
    let geom = parse_geometry(geom_str)?;
    let w = opt_dim(args, "--w", 120)?;
    let h = opt_dim(args, "--h", 90)?;
    let valid_rect = if geom.is_identity() {
        [0.0, 0.0, 1.0, 1.0]
    } else {
        let (vw, vh, mask) = warp_valid(w, h, &geom)?;
        normalized_valid_rect(&mask, vw, vh)
    };
    Ok(Json::object([(
        "header",
        Json::str(header_value(valid_rect)),
    )]))
}

fn cmd_warp(args: &[String]) -> Result<Json, Failure> {
    let in_path = common::option(args, "--in")?;
    let geom_str = common::option(args, "--geometry")?;
    let out_path = common::option(args, "--out")?;
    let mask_path = common::option(args, "--mask")?;
    let geom = parse_geometry(geom_str)?;
    let (h, w, rgb) = read_rgb_npy(in_path)?;
    let warped = warp(&rgb, w, h, &geom)?;
    write_npy_bytes(
        out_path,
        "|u1",
        &warped.rgb,
        &[warped.height, warped.width, 3],
    )?;
    write_npy_bytes(
        mask_path,
        "|b1",
        &warped.valid,
        &[warped.height, warped.width],
    )?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("mask", Json::str(mask_path)),
        ("dtype", Json::str("uint8")),
        (
            "shape",
            Json::ints(&[warped.height as i64, warped.width as i64, 3]),
        ),
    ]))
}

fn cmd_warp_valid(args: &[String]) -> Result<Json, Failure> {
    let w = parse_dim(args, "--w")?;
    let h = parse_dim(args, "--h")?;
    let geom_str = common::option(args, "--geometry")?;
    let out_path = common::option(args, "--out")?;
    let geom = parse_geometry(geom_str)?;
    let (vw, vh, mask) = warp_valid(w, h, &geom)?;
    write_npy_bytes(out_path, "|b1", &mask, &[vh, vw])?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("dtype", Json::str("bool")),
        ("shape", Json::ints(&[vh as i64, vw as i64])),
    ]))
}

fn parse_rect4(val: &JsonValue) -> Result<[f64; 4], CoreError> {
    let arr = val
        .as_array()
        .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?;
    if arr.len() != 4 {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let mut out = [0.0; 4];
    for (i, item) in arr.iter().enumerate() {
        out[i] = item
            .as_f64()
            .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?;
    }
    Ok(out)
}

fn cmd_fitted_crop(args: &[String]) -> Result<Json, Failure> {
    let w = opt_dim(args, "--w", 0)?;
    let h = opt_dim(args, "--h", 0)?;
    let geom_str = common::option(args, "--geometry")?;
    let crop_str = opt_str(args, "--crop")?.unwrap_or("{}");
    let force = has_flag(args, "--force");
    let geom = parse_geometry(geom_str)?;
    let val = parse_json(crop_str, "bad_rect", "裁剪框超出画面范围")?;
    let rect = match val.get("rect") {
        None | Some(JsonValue::Null) => None,
        Some(v) => Some(parse_rect4(v)?),
    };
    let preset = match val.get("preset") {
        None | Some(JsonValue::Null) => None,
        Some(v) => v.as_str(),
    };
    let res = fitted_crop(
        &geom,
        rect,
        preset,
        if w > 0 { Some(w) } else { None },
        if h > 0 { Some(h) } else { None },
        force,
    )?;
    Ok(Json::object([(
        "rect",
        match res {
            Some(r) => Json::floats(&r),
            None => Json::Null,
        },
    )]))
}

fn cmd_clamp_rect(args: &[String]) -> Result<Json, Failure> {
    let w = parse_dim(args, "--w")?;
    let h = parse_dim(args, "--h")?;
    let geom_str = common::option(args, "--geometry")?;
    let rect_str = common::option(args, "--rect")?;
    let aspect = opt_str(args, "--aspect")?
        .map(|s| {
            s.parse::<f64>()
                .map_err(|_| Failure::Usage(format!("bad --aspect: {s:?}")))
        })
        .transpose()?;
    let geom = parse_geometry(geom_str)?;
    let val = parse_json(rect_str, "bad_rect", "裁剪框超出画面范围")?;
    let rect = parse_rect4(&val)?;
    let (vw, vh, mask) = warp_valid(w, h, &geom)?;
    let out = clamp_norm_rect(&mask, vw, vh, rect, aspect);
    Ok(Json::object([("rect", Json::floats(&out))]))
}

fn cmd_fit_ratio(args: &[String]) -> Result<Json, Failure> {
    let w = parse_dim(args, "--w")?;
    let h = parse_dim(args, "--h")?;
    let geom_str = common::option(args, "--geometry")?;
    let rect_str = common::option(args, "--rect")?;
    let numer = parse_dim(args, "--numer")?;
    let denom = parse_dim(args, "--denom")?;
    let geom = parse_geometry(geom_str)?;
    let val = parse_json(rect_str, "bad_rect", "裁剪框超出画面范围")?;
    let rect = parse_rect4(&val)?;
    let (vw, vh, mask) = warp_valid(w, h, &geom)?;
    let out = fit_ratio_in_valid(&mask, vw, vh, rect, numer, denom);
    Ok(Json::object([(
        "box",
        Json::ints(&[out[0] as i64, out[1] as i64, out[2] as i64, out[3] as i64]),
    )]))
}

fn npy_header(descr: &str, shape: &[usize]) -> Vec<u8> {
    let dims: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        format!("({})", dims.join(", "))
    };
    let mut dict =
        format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape_text}, }}");
    let fixed = 10;
    let total = fixed + dict.len() + 1;
    dict.push_str(&" ".repeat((64 - total % 64) % 64));
    dict.push('\n');
    let mut out = b"\x93NUMPY\x01\x00".to_vec();
    let len = u16::try_from(dict.len()).unwrap_or(u16::MAX);
    out.extend_from_slice(&len.to_le_bytes());
    out.extend_from_slice(dict.as_bytes());
    out
}

fn write_npy_bytes(path: &str, descr: &str, data: &[u8], shape: &[usize]) -> Result<(), Failure> {
    let mut file = std::fs::File::create(path)
        .map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))?;
    file.write_all(&npy_header(descr, shape))
        .and_then(|()| file.write_all(data))
        .and_then(|()| file.flush())
        .map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))
}

fn read_rgb_npy(path: &str) -> Result<(usize, usize, Vec<u8>), Failure> {
    let bytes =
        std::fs::read(path).map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")))?;
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(Failure::Usage(format!("{path} is not an npy file")));
    }
    let (header_len, header_at) = match bytes[6] {
        1 => (
            usize::from(u16::from_le_bytes([bytes[8], bytes[9]])),
            10_usize,
        ),
        2 if bytes.len() >= 12 => (
            u32::from_le_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]) as usize,
            12_usize,
        ),
        _ => return Err(Failure::Usage(format!("{path}: unsupported npy version"))),
    };
    let end = header_at
        .checked_add(header_len)
        .filter(|&e| e <= bytes.len())
        .ok_or_else(|| Failure::Usage(format!("{path}: truncated npy header")))?;
    let header = std::str::from_utf8(&bytes[header_at..end])
        .map_err(|_| Failure::Usage(format!("{path}: invalid npy header")))?;
    if !header.contains("'descr': '|u1'")
        && !header.contains("'descr': '<u1'")
        && !header.contains("'descr': 'u1'")
    {
        return Err(Failure::Usage(format!("{path}: expected uint8 npy")));
    }
    if !header.contains("'fortran_order': False") {
        return Err(Failure::Usage(format!("{path}: expected C-order npy")));
    }
    let shape_start = header
        .find("'shape': (")
        .ok_or_else(|| Failure::Usage(format!("{path}: missing shape in npy")))?
        + "'shape': (".len();
    let shape_end = header[shape_start..]
        .find(')')
        .ok_or_else(|| Failure::Usage(format!("{path}: malformed shape in npy")))?
        + shape_start;
    let dims: Vec<usize> = header[shape_start..shape_end]
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            s.parse::<usize>()
                .map_err(|_| Failure::Usage(format!("{path}: bad dim {s:?}")))
        })
        .collect::<Result<_, _>>()?;
    if dims.len() != 3 || dims[2] != 3 || dims[0] == 0 || dims[1] == 0 {
        return Err(Failure::Usage(format!(
            "{path}: expected (H, W, 3) shape, got {dims:?}"
        )));
    }
    let h = dims[0];
    let w = dims[1];
    let expected = h
        .checked_mul(w)
        .and_then(|p| p.checked_mul(3))
        .ok_or_else(|| Failure::Usage(format!("{path}: shape overflow")))?;
    let payload = &bytes[end..];
    if payload.len() != expected {
        return Err(Failure::Usage(format!(
            "{path}: payload len {} != expected {expected}",
            payload.len()
        )));
    }
    Ok((h, w, payload.to_vec()))
}
