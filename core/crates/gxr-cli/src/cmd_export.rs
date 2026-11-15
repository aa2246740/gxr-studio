//! `export-*` commands. Owner: wave-2 export (export-meta: `export-meta-*`; export-pipeline:
//! `export-run-*`).

use gxr_export::pipeline::{ExportFail, State, complete_sdr, optimize_huffman};
use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    if command.starts_with("export-run-") {
        return run_pipeline(command, args);
    }
    Err(Failure::Usage(format!("unknown command {command:?}")))
}

fn run_pipeline(command: &str, args: &[String]) -> Result<Output, Failure> {
    let result = match command {
        "export-run-complete" => cmd_complete(args)?,
        "export-run-huffopt" => cmd_huffopt(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(match result {
        Ok(json) => Output {
            text: json.render(),
            ok: true,
        },
        Err(fail) => Output {
            text: fail.to_json().render(),
            ok: false,
        },
    })
}

fn opt<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    if args.iter().any(|a| a == name) {
        Ok(Some(common::option(args, name)?))
    } else {
        Ok(None)
    }
}

fn write(path: &str, data: &[u8]) -> Result<(), Failure> {
    std::fs::write(path, data).map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))
}

/// A C-order uint8 `.npy` of shape `(H, W, 3)` → `(w, h, bytes)`. Shape problems are core
/// errors with the same text WASM gives for a bad length.
/// `(w, h, bytes)` of an RGB `.npy`.
type RgbNpy = (u64, u64, Vec<u8>);

fn read_rgb_npy(path: &str) -> Result<Result<RgbNpy, ExportFail>, Failure> {
    let bytes = common::read(path)?;
    let bad = |m: &str| {
        Ok(Err(ExportFail::Core(CoreError::new(
            "bad_image",
            m.to_string(),
        ))))
    };
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return bad("不是 .npy 文件");
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
        _ => return bad("不支持的 .npy 版本"),
    };
    let Some(header) = bytes.get(at..at.saturating_add(len)) else {
        return bad(".npy 头不完整");
    };
    let header = String::from_utf8_lossy(header);
    if !(header.contains("'|u1'") || header.contains("'<u1'") || header.contains("'u1'"))
        || header.contains("'fortran_order': True")
    {
        return bad("要是 C 顺序的 uint8 数组");
    }
    let Some(open) = header.find("'shape': (") else {
        return bad(".npy 没有 shape");
    };
    let rest = &header[open + 10..];
    let Some(close) = rest.find(')') else {
        return bad(".npy 没有 shape");
    };
    let dims: Vec<u64> = rest[..close]
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();
    let [h, w, 3] = dims.as_slice() else {
        return bad("要是 (H, W, 3) 数组");
    };
    Ok(Ok((*w, *h, bytes[at + len..].to_vec())))
}

fn cmd_complete(args: &[String]) -> Result<Result<Json, ExportFail>, Failure> {
    let canvas_path = common::option(args, "--canvas")?;
    let state = common::read(common::option(args, "--state")?)?;
    let out = common::option(args, "--out")?;
    let exif_text = match opt(args, "--exif")? {
        Some(p) => String::from_utf8(common::read(p)?)
            .map_err(|_| Failure::Usage("--exif is not UTF-8".into()))?,
        None => String::new(),
    };
    let (w, h, canvas) = match read_rgb_npy(canvas_path)? {
        Ok(v) => v,
        Err(e) => return Ok(Err(e)),
    };
    let state = match State::parse(&state) {
        Ok(s) => s,
        Err(e) => return Ok(Err(e)),
    };
    let source = match gxr_jpeg::parse_exif_json(&exif_text) {
        Ok(s) => s,
        Err(e) => return Ok(Err(e.into())),
    };
    match complete_sdr(&canvas, w, h, &state, &source) {
        Ok((done, _rgb)) => {
            write(out, &done.file)?;
            Ok(Ok(Json::object([
                ("result", done.result),
                ("sha256", Json::str(sha256_hex(&done.file))),
            ])))
        }
        Err(e) => Ok(Err(e)),
    }
}

/// `optimize_huffman` on a JPEG file (a test hook for the transcode alone).
fn cmd_huffopt(args: &[String]) -> Result<Result<Json, ExportFail>, Failure> {
    let data = common::read(common::option(args, "--in")?)?;
    let out = common::option(args, "--out")?;
    match optimize_huffman(&data) {
        Ok(file) => {
            write(out, &file)?;
            Ok(Ok(Json::object([
                ("bytes", Json::Int(file.len() as i64)),
                ("sha256", Json::str(sha256_hex(&file))),
            ])))
        }
        Err(e) => Ok(Err(e.into())),
    }
}
