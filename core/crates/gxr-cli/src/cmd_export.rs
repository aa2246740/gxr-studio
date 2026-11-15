//! `export-*` commands. Owner: wave-2 export (export-meta: `export-meta-*`; export-pipeline:
//! `export-run-*`, thin wrappers over `gxr_export::pipeline::api`, which `gxr-wasm` calls too).

use gxr_export::pipeline::api::{self, Picture, PrepareArgs, SizesArgs};
use gxr_export::pipeline::{ExportFail, ExportOpts, optimize_huffman};
use gxr_frame::Fonts;
use gxr_raw::Json;
use gxr_raw::sha256::sha256_hex;

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
        "export-run-plan" => cmd_plan(args)?,
        "export-run-prepare" => cmd_prepare(args)?,
        "export-run-sizes" => cmd_sizes(args)?,
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

type Ran = Result<Result<Json, ExportFail>, Failure>;
/// `(w, h, bytes)` of an RGB `.npy`.
type RgbNpy = (f64, f64, Vec<u8>);

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

fn read_or_empty(args: &[String], name: &str) -> Result<Vec<u8>, Failure> {
    match opt(args, name)? {
        Some(p) => common::read(p),
        None => Ok(Vec::new()),
    }
}

fn num_arg(args: &[String], name: &str) -> Result<Option<f64>, Failure> {
    match opt(args, name)? {
        None => Ok(None),
        Some(t) => t
            .parse::<f64>()
            .map(Some)
            .map_err(|_| Failure::Usage(format!("bad number for {name}: {t:?}"))),
    }
}

/// `--font NAME=PATH`, one per face (as `frame-*`).
fn fonts(args: &[String]) -> Result<Fonts, Failure> {
    let mut fonts = Fonts::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--font" {
            let spec = args
                .get(i + 1)
                .ok_or_else(|| Failure::Usage("--font needs a value".into()))?;
            let (name, path) = spec
                .split_once('=')
                .ok_or_else(|| Failure::Usage(format!("--font wants NAME=PATH, got {spec:?}")))?;
            fonts.add(name, &common::read(path)?)?;
            i += 2;
        } else {
            i += 1;
        }
    }
    Ok(fonts)
}

/// A uint8 `.npy` of shape `(H, W, 3)` → `(w, h, bytes)`. A file that isn't one is a usage
/// error; the core checks the size against the bytes, the same way WASM does.
fn read_rgb_npy(path: &str) -> Result<RgbNpy, Failure> {
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
    let dims: Vec<f64> = rest[..close]
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();
    let [h, w, c] = dims.as_slice() else {
        return Err(bad("want shape (H, W, 3)"));
    };
    if *c != 3.0 {
        return Err(bad("want shape (H, W, 3)"));
    }
    Ok((*w, *h, bytes[at + len..].to_vec()))
}

/// `np.save` of a `(H, W, 3)` uint8 array (format 1.0).
fn write_rgb_npy(path: &str, width: usize, height: usize, data: &[u8]) -> Result<(), Failure> {
    let mut header =
        format!("{{'descr': '|u1', 'fortran_order': False, 'shape': ({height}, {width}, 3), }}");
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

fn picture(p: Option<&RgbNpy>) -> Option<Picture<'_>> {
    p.map(|(w, h, d)| Picture {
        data: d,
        width: *w,
        height: *h,
    })
}

/// `export-run-complete --canvas RGB.npy [--w W --h H] --state S [--exif EXIF.json] --out OUT.jpg`
/// (`--w` / `--h` override the array's size, as numbers, for the size-check cases).
fn cmd_complete(args: &[String]) -> Ran {
    let (w, h, canvas) = read_rgb_npy(common::option(args, "--canvas")?)?;
    let (w, h) = (
        num_arg(args, "--w")?.unwrap_or(w),
        num_arg(args, "--h")?.unwrap_or(h),
    );
    let state = common::read(common::option(args, "--state")?)?;
    let out = common::option(args, "--out")?;
    let exif = String::from_utf8(read_or_empty(args, "--exif")?)
        .map_err(|_| Failure::Usage("--exif is not UTF-8".into()))?;
    let picture = Picture {
        data: &canvas,
        width: w,
        height: h,
    };
    match api::complete(picture, &state, &exif) {
        Ok(done) => {
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
fn cmd_huffopt(args: &[String]) -> Ran {
    let data = common::read(common::option(args, "--in")?)?;
    let out = common::option(args, "--out")?;
    match optimize_huffman(&data) {
        Ok(file) => {
            write(out, &file)?;
            Ok(Ok(Json::object([
                (
                    "bytes",
                    Json::Int(i64::try_from(file.len()).unwrap_or(i64::MAX)),
                ),
                ("sha256", Json::str(sha256_hex(&file))),
            ])))
        }
        Err(e) => Ok(Err(e.into())),
    }
}

/// `export-run-prepare --shot S [--side E] [--defaults D] [--layout L] [--base MODE]
/// [--quality Q] [--long-edge N] [--hdr] [--font NAME=PATH...]
/// [--picked RGB.npy --from-raw 0|1 --labels J] [--jpg RGB.npy] --out CANVAS.npy`.
fn cmd_prepare(args: &[String]) -> Ran {
    let shot = common::read(common::option(args, "--shot")?)?;
    let side = read_or_empty(args, "--side")?;
    let defaults = read_or_empty(args, "--defaults")?;
    let layout = read_or_empty(args, "--layout")?;
    let labels = read_or_empty(args, "--labels")?;
    let base = opt(args, "--base")?.unwrap_or("auto").to_string();
    let out = common::option(args, "--out")?;
    let opts = ExportOpts {
        quality: num_arg(args, "--quality")?.unwrap_or(95.0),
        long_edge: num_arg(args, "--long-edge")?,
        hdr: args.iter().any(|a| a == "--hdr"),
    };
    let fonts = fonts(args)?;
    let (ow, oh) = (num_arg(args, "--w")?, num_arg(args, "--h")?);
    let size = |(w, h, d): RgbNpy| (ow.unwrap_or(w), oh.unwrap_or(h), d);
    let picked = opt(args, "--picked")?
        .map(read_rgb_npy)
        .transpose()?
        .map(size);
    let jpg = opt(args, "--jpg")?.map(read_rgb_npy).transpose()?.map(size);
    let pargs = PrepareArgs {
        shot: &shot,
        side: &side,
        defaults: &defaults,
        layout: &layout,
        base: &base,
        labels: &labels,
        from_raw: opt(args, "--from-raw")? == Some("1"),
        opts,
    };
    match api::prepare(
        &pargs,
        picture(picked.as_ref()),
        picture(jpg.as_ref()),
        &fonts,
    ) {
        Ok((canvas, json, original)) => {
            write_rgb_npy(out, canvas.width, canvas.height, &canvas.data)?;
            Ok(Ok(Json::object([
                ("original", Json::Bool(original)),
                ("prepared", json),
            ])))
        }
        Err(e) => Ok(Err(e)),
    }
}

/// `export-run-sizes --width W --height H [--side E] [--defaults D] [--layout L] [--rect JSON]
/// [--preset P] [--font NAME=PATH...]`.
fn cmd_sizes(args: &[String]) -> Ran {
    let width =
        num_arg(args, "--width")?.ok_or_else(|| Failure::Usage("missing --width".into()))?;
    let height =
        num_arg(args, "--height")?.ok_or_else(|| Failure::Usage("missing --height".into()))?;
    let side = read_or_empty(args, "--side")?;
    let defaults = read_or_empty(args, "--defaults")?;
    let layout = read_or_empty(args, "--layout")?;
    let rect = opt(args, "--rect")?.unwrap_or("").as_bytes().to_vec();
    let preset = opt(args, "--preset")?;
    let fonts = fonts(args)?;
    let sargs = SizesArgs {
        width,
        height,
        side: &side,
        defaults: &defaults,
        layout: &layout,
        rect: &rect,
        preset,
    };
    Ok(api::sizes(&sargs, &fonts))
}

/// `export-run-plan --shot S [--side E] [--defaults D] [--base MODE]`: `serve._plan`.
fn cmd_plan(args: &[String]) -> Ran {
    let shot = common::read(common::option(args, "--shot")?)?;
    let side = read_or_empty(args, "--side")?;
    let defaults = read_or_empty(args, "--defaults")?;
    let base = opt(args, "--base")?.unwrap_or("auto").to_string();
    Ok(api::plan_json(&shot, &side, &defaults, &base))
}
