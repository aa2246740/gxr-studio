//! `finish-*` subcommands. Owner: wave-2 unit raster.

use std::io::Write;

use gxr_finish::raster::logo::KeyMode;
use gxr_finish::{
    draw_frame_canvas_u64, finish_array_u64, fit_logo_json, key_white, linear_to_srgb,
    prepare_logo_key, srgb_to_linear,
};
use gxr_raw::Json;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let text = match command {
        "finish-array" => cmd_array(args)?,
        "finish-frame-canvas" => cmd_frame_canvas(args)?,
        "finish-logo-key" => cmd_logo_key(args)?,
        "finish-logo-fit" => cmd_logo_fit(args)?,
        "finish-srgb-to-linear" => cmd_srgb_to_linear(args)?,
        "finish-linear-to-srgb" => cmd_linear_to_srgb(args)?,
        "finish-key-white" => cmd_key_white(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output { text, ok: true })
}

fn opt_str<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    if args.iter().any(|a| a == name) {
        Ok(Some(common::option(args, name)?))
    } else {
        Ok(None)
    }
}

fn read_text_flag(
    args: &[String],
    file_flag: &str,
    inline_flag: &str,
    default: Option<&str>,
) -> Result<String, Failure> {
    if let Some(path) = opt_str(args, file_flag)? {
        return std::fs::read_to_string(path)
            .map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")));
    }
    if let Some(inline) = opt_str(args, inline_flag)? {
        return Ok(inline.to_string());
    }
    match default {
        Some(d) => Ok(d.to_string()),
        None => Err(Failure::Usage(format!(
            "missing {file_flag} or {inline_flag}"
        ))),
    }
}

fn parse_dim_u64(args: &[String], name: &str) -> Result<u64, Failure> {
    let raw = common::option(args, name)?;
    let v: u64 = raw
        .parse()
        .map_err(|_| Failure::Usage(format!("bad integer for {name}: {raw:?}")))?;
    if v == 0 {
        return Err(Failure::Usage(format!("{name} must be > 0")));
    }
    Ok(v)
}

fn cmd_array(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let edit_json = read_text_flag(args, "--edit", "--edit-json", Some("{}"))?;
    let options_json = read_text_flag(args, "--options", "--options-json", Some("{}"))?;
    let (h, w, rgb) = read_u8_npy(in_path, 3)?;
    let w_arg = if args.iter().any(|a| a == "--w") {
        parse_dim_u64(args, "--w")?
    } else {
        w as u64
    };
    let h_arg = if args.iter().any(|a| a == "--h") {
        parse_dim_u64(args, "--h")?
    } else {
        h as u64
    };
    let res = finish_array_u64(&rgb, w_arg, h_arg, &edit_json, &options_json)?;
    write_npy_bytes(out_path, "|u1", &res.rgb, &[res.height, res.width, 3])?;
    Ok(res.report_json().render())
}

fn cmd_frame_canvas(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let layout_json = read_text_flag(args, "--layout", "--layout-json", None)?;
    let (h, w, rgb) = read_u8_npy(in_path, 3)?;
    let w_arg = if args.iter().any(|a| a == "--w") {
        parse_dim_u64(args, "--w")?
    } else {
        w as u64
    };
    let h_arg = if args.iter().any(|a| a == "--h") {
        parse_dim_u64(args, "--h")?
    } else {
        h as u64
    };
    let res = draw_frame_canvas_u64(&rgb, w_arg, h_arg, &layout_json)?;
    write_npy_bytes(out_path, "|u1", &res.rgb, &[res.height, res.width, 3])?;
    Ok(res.report_json().render())
}

fn cmd_logo_key(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let (h, w, rgba) = read_u8_npy(in_path, 4)?;
    let w_arg = if args.iter().any(|a| a == "--w") {
        parse_dim_u64(args, "--w")?
    } else {
        w as u64
    };
    let h_arg = if args.iter().any(|a| a == "--h") {
        parse_dim_u64(args, "--h")?
    } else {
        h as u64
    };
    let key_mode = match opt_str(args, "--key-mode")?.unwrap_or("auto") {
        "auto" => KeyMode::Auto,
        "true" | "1" | "yes" | "on" => KeyMode::Bool(true),
        "false" | "0" | "no" | "off" => KeyMode::Bool(false),
        other => return Err(Failure::Usage(format!("bad --key-mode: {other:?}"))),
    };
    let tol: i64 = match opt_str(args, "--tol")? {
        None => 30,
        Some(s) => s
            .parse()
            .map_err(|_| Failure::Usage(format!("bad --tol: {s:?}")))?,
    };
    let id = opt_str(args, "--id")?.unwrap_or("");
    let name = opt_str(args, "--name")?.unwrap_or("");
    let res = prepare_logo_key(&rgba, w_arg, h_arg, key_mode, tol, id, name)?;
    write_npy_bytes(out_path, "|u1", &res.rgba, &[res.height, res.width, 4])?;
    Ok(res.report_json().render())
}

fn cmd_logo_fit(args: &[String]) -> Result<String, Failure> {
    let info_json = read_text_flag(args, "--info", "--info-json", None)?;
    let request_json = read_text_flag(args, "--request", "--request-json", None)?;
    Ok(fit_logo_json(&info_json, &request_json)?)
}

fn cmd_srgb_to_linear(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let (h, w, rgb) = read_u8_npy(in_path, 3)?;
    let lin = srgb_to_linear(&rgb, w as u64, h as u64)?;
    Ok(common::write_npy(out_path, &lin, &[h, w, 3], "float32")?.render())
}

fn cmd_linear_to_srgb(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let (h, w, lin) = read_f32_npy(in_path, 3)?;
    let rgb = linear_to_srgb(&lin, w as u64, h as u64)?;
    write_npy_bytes(out_path, "|u1", &rgb, &[h, w, 3])?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("dtype", Json::str("uint8")),
        ("shape", Json::ints(&[h as i64, w as i64, 3])),
    ])
    .render())
}

fn cmd_key_white(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let tol: i64 = match opt_str(args, "--tol")? {
        None => 30,
        Some(s) => s
            .parse()
            .map_err(|_| Failure::Usage(format!("bad --tol: {s:?}")))?,
    };
    let (h, w, rgba) = read_u8_npy(in_path, 4)?;
    let out = key_white(&rgba, w as u64, h as u64, tol)?;
    write_npy_bytes(out_path, "|u1", &out, &[h, w, 4])?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("dtype", Json::str("uint8")),
        ("shape", Json::ints(&[h as i64, w as i64, 4])),
    ])
    .render())
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

fn parse_npy_header(
    path: &str,
    expected_descr: &[&str],
    channels: usize,
) -> Result<(usize, usize, usize, Vec<u8>), Failure> {
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
    if !expected_descr.iter().any(|d| header.contains(d)) {
        return Err(Failure::Usage(format!("{path}: unexpected npy descr")));
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
    if dims.len() != 3 || dims[2] != channels || dims[0] == 0 || dims[1] == 0 {
        return Err(Failure::Usage(format!(
            "{path}: expected (H, W, {channels}) shape, got {dims:?}"
        )));
    }
    Ok((dims[0], dims[1], end, bytes))
}

fn read_u8_npy(path: &str, channels: usize) -> Result<(usize, usize, Vec<u8>), Failure> {
    let (h, w, end, bytes) = parse_npy_header(
        path,
        &["'descr': '|u1'", "'descr': '<u1'", "'descr': 'u1'"],
        channels,
    )?;
    let expected = h
        .checked_mul(w)
        .and_then(|p| p.checked_mul(channels))
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

fn read_f32_npy(path: &str, channels: usize) -> Result<(usize, usize, Vec<f32>), Failure> {
    let (h, w, end, bytes) = parse_npy_header(path, &["'descr': '<f4'"], channels)?;
    let elems = h
        .checked_mul(w)
        .and_then(|p| p.checked_mul(channels))
        .ok_or_else(|| Failure::Usage(format!("{path}: shape overflow")))?;
    let expected = elems
        .checked_mul(4)
        .ok_or_else(|| Failure::Usage(format!("{path}: byte len overflow")))?;
    let payload = &bytes[end..];
    if payload.len() != expected {
        return Err(Failure::Usage(format!(
            "{path}: payload len {} != expected {expected}",
            payload.len()
        )));
    }
    let mut out = Vec::with_capacity(elems);
    for chunk in payload.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Ok((h, w, out))
}
