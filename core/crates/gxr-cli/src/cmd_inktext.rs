//! `inktext-*` subcommands for unit `ink-text`.

use std::io::Write;

use gxr_finish::{
    FontPack, gxr_mark_json, inktext_batch_masks, inktext_draw_u64, inktext_frame_u64,
    inktext_run_u64, inktext_stamp_u64, stamp_hdr_mask,
};
use gxr_frame::parse_json;
use gxr_raw::Json;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let text = match command {
        "inktext-draw" => cmd_draw(args)?,
        "inktext-batch" => cmd_batch(args)?,
        "inktext-run" => cmd_run(args)?,
        "inktext-mark" => cmd_mark(args)?,
        "inktext-stamp" => cmd_stamp(args)?,
        "inktext-frame" => cmd_frame(args)?,
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
    raw.parse()
        .map_err(|_| Failure::Usage(format!("bad integer for {name}: {raw:?}")))
}

fn pack_fonts(args: &[String]) -> Result<(String, Vec<u8>), Failure> {
    let mut index = Vec::new();
    let mut blob = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--font" {
            let spec = args
                .get(i + 1)
                .ok_or_else(|| Failure::Usage("--font needs a value".into()))?;
            let (name, path) = spec
                .split_once('=')
                .ok_or_else(|| Failure::Usage(format!("--font wants NAME=PATH, got {spec:?}")))?;
            let bytes = common::read(path)?;
            index.push(Json::Array(vec![
                Json::str(name),
                Json::Int(bytes.len() as i64),
            ]));
            blob.extend_from_slice(&bytes);
            i += 2;
        } else {
            i += 1;
        }
    }
    Ok((Json::Array(index).render(), blob))
}

fn cmd_draw(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let ops_json = read_text_flag(args, "--ops", "--ops-json", Some("[]"))?;
    let (font_index_json, fonts_blob) = pack_fonts(args)?;
    let (h, w, ch, pixels) = read_u8_image_npy(in_path)?;
    let mode_str = match opt_str(args, "--mode")? {
        Some(m) => m.to_string(),
        None => match ch {
            1 => "L".to_string(),
            3 => "RGB".to_string(),
            4 => "RGBA".to_string(),
            _ => {
                return Err(Failure::Usage(format!(
                    "cannot infer mode from {ch} channels"
                )));
            }
        },
    };
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
    let res = inktext_draw_u64(
        &pixels,
        w_arg,
        h_arg,
        &mode_str,
        &ops_json,
        &font_index_json,
        &fonts_blob,
    )?;
    if res.channels == 1 {
        write_npy_bytes(out_path, "|u1", &res.pixels, &[res.height, res.width])?;
    } else {
        write_npy_bytes(
            out_path,
            "|u1",
            &res.pixels,
            &[res.height, res.width, res.channels],
        )?;
    }
    Ok(res.report_json().render())
}

fn cmd_batch(args: &[String]) -> Result<String, Failure> {
    let font_path = common::option(args, "--font")?;
    let out_bin = common::option(args, "--out-bin")?;
    let cases_json = read_text_flag(args, "--cases", "--cases-json", None)?;
    let font_bytes = common::read(font_path)?;
    let (meta, blob) = inktext_batch_masks(&font_bytes, &cases_json)?;
    std::fs::write(out_bin, &blob)
        .map_err(|e| Failure::Usage(format!("cannot write {out_bin}: {e}")))?;
    Ok(meta.render())
}

fn pack_logos(args: &[String]) -> Result<(String, Vec<u8>), Failure> {
    if let Some(idx_path) = opt_str(args, "--logo-index")? {
        let idx = std::fs::read_to_string(idx_path)
            .map_err(|e| Failure::Usage(format!("cannot read {idx_path}: {e}")))?;
        let blob = match opt_str(args, "--logo-blob")? {
            Some(p) => common::read(p)?,
            None => Vec::new(),
        };
        return Ok((idx, blob));
    }
    let mut index = Vec::new();
    let mut blob = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--logo" {
            let spec = args
                .get(i + 1)
                .ok_or_else(|| Failure::Usage("--logo needs a value".into()))?;
            let (id, path) = spec
                .split_once('=')
                .ok_or_else(|| Failure::Usage(format!("--logo wants ID=PATH, got {spec:?}")))?;
            let (h, w, ch, pixels) = read_u8_image_npy(path)?;
            if ch != 4 {
                return Err(Failure::Usage(format!(
                    "--logo {id} wants RGBA npy (4 channels), got {ch}"
                )));
            }
            index.push(Json::Array(vec![
                Json::str(id),
                Json::Int(w as i64),
                Json::Int(h as i64),
            ]));
            blob.extend_from_slice(&pixels);
            i += 2;
        } else {
            i += 1;
        }
    }
    Ok((Json::Array(index).render(), blob))
}

fn infer_mode(args: &[String], ch: usize) -> Result<String, Failure> {
    match opt_str(args, "--mode")? {
        Some(m) => Ok(m.to_string()),
        None => match ch {
            1 => Ok("L".to_string()),
            3 => Ok("RGB".to_string()),
            4 => Ok("RGBA".to_string()),
            _ => Err(Failure::Usage(format!(
                "cannot infer mode from {ch} channels"
            ))),
        },
    }
}

fn write_ink_image(path: &str, img: &gxr_finish::ink::InkImage) -> Result<(), Failure> {
    let ch = img.mode().channels();
    if ch == 1 {
        write_npy_bytes(path, "|u1", img.as_slice(), &[img.height(), img.width()])
    } else {
        write_npy_bytes(
            path,
            "|u1",
            img.as_slice(),
            &[img.height(), img.width(), ch],
        )
    }
}

fn cmd_run(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let run_json = read_text_flag(args, "--run", "--run-json", None)?;
    let (font_index_json, fonts_blob) = pack_fonts(args)?;
    let (h, w, ch, pixels) = read_u8_image_npy(in_path)?;
    let mode_str = infer_mode(args, ch)?;
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
    let (img, exact) = inktext_run_u64(
        &pixels,
        w_arg,
        h_arg,
        &mode_str,
        &run_json,
        &font_index_json,
        &fonts_blob,
    )?;
    write_ink_image(out_path, &img)?;
    Ok(Json::object([
        ("width", Json::Int(img.width() as i64)),
        ("height", Json::Int(img.height() as i64)),
        ("channels", Json::Int(img.mode().channels() as i64)),
        ("exact", Json::Bool(exact)),
    ])
    .render())
}

fn cmd_mark(args: &[String]) -> Result<String, Failure> {
    let (font_index_json, fonts_blob) = pack_fonts(args)?;
    let json = gxr_mark_json(&font_index_json, &fonts_blob)?;
    Ok(json.render())
}

fn cmd_stamp(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let layout_json = read_text_flag(args, "--layout", "--layout-json", None)?;
    let (font_index_json, fonts_blob) = pack_fonts(args)?;
    let (h, w, _ch, pixels) = read_u8_image_npy(in_path)?;
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
    let (img, exact) = inktext_stamp_u64(
        &pixels,
        w_arg,
        h_arg,
        &layout_json,
        &font_index_json,
        &fonts_blob,
    )?;
    write_ink_image(out_path, &img)?;
    if let Some(mask_out) = opt_str(args, "--hdr-mask-out")? {
        let fonts = FontPack::from_pack(&font_index_json, &fonts_blob)?;
        let layout = parse_json(layout_json.as_bytes())?;
        let mask = stamp_hdr_mask(w_arg, h_arg, &fonts, &layout)?;
        write_npy_bytes(mask_out, "|u1", &mask, &[img.height(), img.width()])?;
    }
    Ok(Json::object([
        ("width", Json::Int(img.width() as i64)),
        ("height", Json::Int(img.height() as i64)),
        ("channels", Json::Int(img.mode().channels() as i64)),
        ("exact", Json::Bool(exact)),
    ])
    .render())
}

fn cmd_frame(args: &[String]) -> Result<String, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let layout_json = read_text_flag(args, "--layout", "--layout-json", None)?;
    let (font_index_json, fonts_blob) = pack_fonts(args)?;
    let (logo_index_json, logo_blob) = pack_logos(args)?;
    let (h, w, _ch, pixels) = read_u8_image_npy(in_path)?;
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
    let (img, exact) = inktext_frame_u64(
        &pixels,
        w_arg,
        h_arg,
        &layout_json,
        &font_index_json,
        &fonts_blob,
        &logo_index_json,
        &logo_blob,
    )?;
    write_ink_image(out_path, &img)?;
    Ok(Json::object([
        ("width", Json::Int(img.width() as i64)),
        ("height", Json::Int(img.height() as i64)),
        ("channels", Json::Int(img.mode().channels() as i64)),
        ("exact", Json::Bool(exact)),
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

fn read_u8_image_npy(path: &str) -> Result<(usize, usize, usize, Vec<u8>), Failure> {
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
    if !["'descr': '|u1'", "'descr': '<u1'", "'descr': 'u1'"]
        .iter()
        .any(|d| header.contains(d))
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
    let (h, w, ch) = match dims.as_slice() {
        [h, w] => (*h, *w, 1),
        [h, w, ch] => (*h, *w, *ch),
        _ => {
            return Err(Failure::Usage(format!(
                "{path}: expected 2D or 3D shape, got {dims:?}"
            )));
        }
    };
    let expected = h
        .checked_mul(w)
        .and_then(|p| p.checked_mul(ch))
        .ok_or_else(|| Failure::Usage(format!("{path}: shape overflow")))?;
    let payload = &bytes[end..];
    if payload.len() != expected {
        return Err(Failure::Usage(format!(
            "{path}: payload len {} != expected {expected}",
            payload.len()
        )));
    }
    Ok((h, w, ch, payload.to_vec()))
}
