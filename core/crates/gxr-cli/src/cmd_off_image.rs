//! `off-image-*` 子命令：关机图生成、重封装与严格校验的对拍入口。

use gxr_export::off_image;
use gxr_raw::CoreError;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "off-image-make" => cmd_make(args),
        "off-image-check" => cmd_check(args),
        "off-image-remux" => cmd_remux(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

/// 读取 `(H, W, 3)` C-order uint8 `.npy`。
fn read_rgb_npy(path: &str) -> Result<(u32, u32, Vec<u8>), Failure> {
    let bytes = common::read(path)?;
    let bad = |m: &str| Failure::Usage(format!("{path}: {m}"));
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(bad("not an .npy file"));
    }
    let (at, len) = match bytes[6] {
        1 => (
            10_usize,
            usize::from(u16::from_le_bytes([bytes[8], bytes[9]])),
        ),
        2 | 3 if bytes.len() >= 12 => (
            12_usize,
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
    let dims: Vec<u32> = rest[..close]
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();
    let [h, w, c] = dims.as_slice() else {
        return Err(bad("want shape (H, W, 3)"));
    };
    if *c != 3 {
        return Err(bad("want shape (H, W, 3)"));
    }
    Ok((*w, *h, bytes[at + len..].to_vec()))
}

fn cmd_make(args: &[String]) -> Result<Output, Failure> {
    let inp = common::option(args, "--in")?;
    let out = common::option(args, "--out")?;
    let (w, h, rgb) = read_rgb_npy(inp)?;
    let made = off_image::make_off_image(&rgb, w, h)?;
    std::fs::write(out, &made.data)
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    Ok(Output {
        text: made.meta_json().render(),
        ok: true,
    })
}

fn cmd_check(args: &[String]) -> Result<Output, Failure> {
    let inp = common::option(args, "--in")?;
    let bytes = common::read(inp)?;
    let chk = off_image::check_off_image(&bytes);
    Ok(Output {
        text: chk.to_json().render(),
        ok: true,
    })
}

fn cmd_remux(args: &[String]) -> Result<Output, Failure> {
    let inp = common::option(args, "--in")?;
    let out = common::option(args, "--out")?;
    let bytes = common::read(inp)?;
    let remuxed = off_image::remux_off_image(&bytes).map_err(|e: CoreError| Failure::Core(e))?;
    std::fs::write(out, &remuxed)
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    let chk = off_image::check_off_image(&remuxed);
    Ok(Output {
        text: chk.to_json().render(),
        ok: true,
    })
}
