//! `export-run-ink` CLI handler delegating to `gxr_export::pipeline::export_ink`.

use gxr_export::pipeline::export_ink;
use gxr_raw::Json;

use super::{Ran, num_arg, opt, read_rgb_npy, write, write_rgb_npy};
use crate::common::{self, Failure};

fn pack_fonts_raw(args: &[String]) -> Result<(String, Vec<u8>), Failure> {
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

fn read_rgba_npy(path: &str) -> Result<(usize, usize, Vec<u8>), Failure> {
    let bytes = common::read(path)?;
    let bad = |m: &str| Failure::Usage(format!("{path}: {m}"));
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(bad("not an .npy file"));
    }
    let (at, len) = match bytes[6] {
        1 => (
            10usize,
            usize::from(u16::from_le_bytes([bytes[8], bytes[9]])),
        ),
        2 | 3 if bytes.len() >= 12 => (
            12usize,
            u32::from_le_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]) as usize,
        ),
        _ => return Err(bad("unsupported .npy version")),
    };
    let header = bytes
        .get(at..at.saturating_add(len))
        .ok_or_else(|| bad("short header"))?;
    let header = String::from_utf8_lossy(header);
    if !(header.contains("'|u1'") || header.contains("'<u1'") || header.contains("'u1'"))
        || header.contains("'fortran_order': True")
    {
        return Err(bad("want a C-order uint8 array"));
    }
    let open = header.find("'shape': (").ok_or_else(|| bad("no shape"))?;
    let rest = &header[open + 10..];
    let close = rest.find(')').ok_or_else(|| bad("no shape"))?;
    let dims: Vec<usize> = rest[..close]
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();
    let [h, w, c] = dims.as_slice() else {
        return Err(bad("want shape (H, W, 4)"));
    };
    if *c != 4 {
        return Err(bad("want shape (H, W, 4)"));
    }
    let payload = &bytes[at + len..];
    let expected = h
        .checked_mul(*w)
        .and_then(|p| p.checked_mul(4))
        .ok_or_else(|| bad("shape overflow"))?;
    if payload.len() != expected {
        return Err(bad("payload size mismatch"));
    }
    Ok((*w, *h, payload.to_vec()))
}

fn pack_logos(args: &[String]) -> Result<(String, Vec<u8>), Failure> {
    if let Some(idx_path) = opt(args, "--logo-index")? {
        let idx = std::fs::read_to_string(idx_path)
            .map_err(|e| Failure::Usage(format!("cannot read {idx_path}: {e}")))?;
        let blob = match opt(args, "--logo-blob")? {
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
            let (w, h, pixels) = read_rgba_npy(path)?;
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

fn write_mask_npy(path: &str, width: usize, height: usize, data: &[u8]) -> Result<(), Failure> {
    let mut header =
        format!("{{'descr': '|u1', 'fortran_order': False, 'shape': ({height}, {width}), }}");
    while (10 + header.len() + 1) % 64 != 0 {
        header.push(' ');
    }
    header.push('\n');
    let mut out = Vec::with_capacity(10 + header.len() + data.len());
    out.extend_from_slice(b"\x93NUMPY\x01\x00");
    out.extend_from_slice(&(header.len() as u16).to_le_bytes());
    out.extend_from_slice(header.as_bytes());
    out.extend_from_slice(data);
    write(path, &out)
}

/// `export-run-ink --canvas RGB.npy --prepared P.json [--w W --h H]
/// [--font NAME=PATH...] [--logo ID=PATH.npy... | --logo-index F --logo-blob F]
/// [--stamp-mask-out MASK.npy] --out OUT.npy`
pub(super) fn cmd_ink(args: &[String]) -> Ran {
    let (w, h, canvas) = read_rgb_npy(common::option(args, "--canvas")?)?;
    let w = num_arg(args, "--w")?.unwrap_or(w);
    let h = num_arg(args, "--h")?.unwrap_or(h);
    let prepared = common::read(common::option(args, "--prepared")?)?;
    let out = common::option(args, "--out")?;
    let (font_index, font_blob) = pack_fonts_raw(args)?;
    let (logo_index, logo_blob) = pack_logos(args)?;
    match export_ink(
        canvas,
        w,
        h,
        &prepared,
        &font_index,
        &font_blob,
        &logo_index,
        &logo_blob,
    ) {
        Ok(inked) => {
            write_rgb_npy(out, inked.width, inked.height, &inked.canvas)?;
            if let Some(mask_out) = opt(args, "--stamp-mask-out")? {
                write_mask_npy(
                    mask_out,
                    inked.mask_width,
                    inked.mask_height,
                    &inked.stamp_mask,
                )?;
            }
            Ok(Ok(Json::object([
                ("width", Json::Int(inked.width as i64)),
                ("height", Json::Int(inked.height as i64)),
                ("mask_width", Json::Int(inked.mask_width as i64)),
                ("mask_height", Json::Int(inked.mask_height as i64)),
                ("exact", Json::Bool(inked.exact)),
            ])))
        }
        Err(e) => Ok(Err(e)),
    }
}
