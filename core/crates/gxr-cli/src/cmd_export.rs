//! `export-*` commands. Owner: wave-2 export (export-meta: `export-meta-*`; export-pipeline:
//! `export-run-*`, thin wrappers over `gxr_export::pipeline::api`, which `gxr-wasm` calls too).

use gxr_export::meta::{
    DEFAULT_NOW_YEAR, dng_preview_location, photo_meta_from_bytes, read_dng_meta, read_jpeg_meta,
    read_source_exif,
};
use gxr_export::pipeline::api::{self, Picture, PrepareArgs, SizesArgs, SourceFiles};
use gxr_export::pipeline::{ExportFail, ExportOpts};
use gxr_export::source::CalibPack;
use gxr_frame::Fonts;
use gxr_raw::Json;
use gxr_raw::sha256::sha256_hex;

#[path = "cmd_export_ink.rs"]
mod cmd_export_ink;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    if command.starts_with("export-run-") {
        return run_pipeline(command, args);
    }
    run_meta(command, args)
}

fn run_pipeline(command: &str, args: &[String]) -> Result<Output, Failure> {
    let result = match command {
        "export-run-complete" => cmd_complete(args)?,
        "export-run-ink" => cmd_export_ink::cmd_ink(args)?,
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

/// `export-run-complete --canvas RGB.npy [--w W --h H] --state S (--src SOURCE | --exif EXIF.json)
/// [--hdr-layer LAYER.npy] [--stamp-mask MASK.npy [--mask-w W --mask-h H]] --out OUT.jpg`
/// (`--w` / `--h` override the array's size, as numbers, for the size-check cases).
fn cmd_complete(args: &[String]) -> Ran {
    let (w, h, canvas) = read_rgb_npy(common::option(args, "--canvas")?)?;
    let (w, h) = (
        num_arg(args, "--w")?.unwrap_or(w),
        num_arg(args, "--h")?.unwrap_or(h),
    );
    let state = common::read(common::option(args, "--state")?)?;
    let out = common::option(args, "--out")?;
    let exif = opt(args, "--exif")?
        .map(|p| {
            String::from_utf8(common::read(p)?)
                .map_err(|_| Failure::Usage("--exif is not UTF-8".into()))
        })
        .transpose()?;
    let src = read_or_empty(args, "--src")?;
    let picture = Picture {
        data: &canvas,
        width: w,
        height: h,
    };
    // No `--exif`: the source file's bytes (none given = no EXIF), the same path as WASM.
    let source = match &exif {
        Some(text) => api::ExifSource::Json(text),
        None => api::ExifSource::File(&src),
    };
    let layer = opt(args, "--hdr-layer")?.map(read_f32_npy).transpose()?;
    let mask = opt(args, "--stamp-mask")?.map(read_mask_npy).transpose()?;
    let (mw, mh, mask_bytes) = mask.unwrap_or((0.0, 0.0, Vec::new()));
    let (mw, mh) = (
        num_arg(args, "--mask-w")?.unwrap_or(mw),
        num_arg(args, "--mask-h")?.unwrap_or(mh),
    );
    let hdr = (layer.is_some() || !mask_bytes.is_empty()).then(|| api::HdrArgs {
        layer: layer.as_deref().unwrap_or(&[]),
        mask: &mask_bytes,
        mask_width: mw,
        mask_height: mh,
    });
    match api::complete(picture, &state, source, hdr) {
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

/// `export-run-prepare --shot S [--side E] [--defaults D] [--layout L] [--base MODE]
/// [--quality Q] [--long-edge N] [--hdr] [--font NAME=PATH...] [--dat DAT] [--library GXRFX.BIN] [--entry ENTRY.bin]
/// [--calib DIR] [--binding TEXT] [--dng DNG] [--jpg RGB.npy] [--jpg-bytes JPG] [--preview RGB.npy]
/// [--cube C.cube --space SPACE.json] [--mtime-ms MS --tz-offset-min M] [--w W --h H]
/// [--hdr-out LAYER.npy] --out CANVAS.npy`.
fn cmd_prepare(args: &[String]) -> Ran {
    let shot = common::read(common::option(args, "--shot")?)?;
    let side = read_or_empty(args, "--side")?;
    let defaults = read_or_empty(args, "--defaults")?;
    let layout = read_or_empty(args, "--layout")?;
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
    let jpg = opt(args, "--jpg")?.map(read_rgb_npy).transpose()?.map(size);
    let preview = opt(args, "--preview")?.map(read_rgb_npy).transpose()?;
    let pack = opt(args, "--calib")?.map(read_pack).transpose()?;
    let dat = read_or_empty(args, "--dat")?;
    let library = read_or_empty(args, "--library")?;
    let entry = read_or_empty(args, "--entry")?;
    let dng = read_or_empty(args, "--dng")?;
    let jpg_bytes = read_or_empty(args, "--jpg-bytes")?;
    let cube = read_or_empty(args, "--cube")?;
    let space = String::from_utf8(read_or_empty(args, "--space")?)
        .map_err(|_| Failure::Usage("--space is not UTF-8".into()))?;
    let binding = opt(args, "--binding")?.unwrap_or("").to_string();
    let files = SourceFiles {
        dat: &dat,
        library: &library,
        entry: &entry,
        pack: pack.as_ref(),
        binding: &binding,
        dng: &dng,
        jpg_bytes: &jpg_bytes,
        cube: &cube,
        space: &space,
        mtime_ms: num_arg(args, "--mtime-ms")?,
        tz_offset_min: num_arg(args, "--tz-offset-min")?.unwrap_or(0.0),
    };
    let pargs = PrepareArgs {
        shot: &shot,
        side: &side,
        defaults: &defaults,
        layout: &layout,
        base: &base,
        opts,
    };
    let result = api::prepare(
        &pargs,
        &files,
        picture(jpg.as_ref()),
        picture(preview.as_ref()),
        &fonts,
    );
    match result {
        Ok(p) => {
            write_rgb_npy(out, p.canvas.width, p.canvas.height, &p.canvas.data)?;
            if let Some(path) = opt(args, "--hdr-out")? {
                let layer = p.hdr_layer.unwrap_or_default();
                let (h, w) = hdr_shape(&p.json);
                write(path, &crate::npy::encode(&layer, &[h, w, 2]))?;
            }
            Ok(Ok(Json::object([
                ("original", Json::Bool(p.original)),
                ("prepared", p.json),
            ])))
        }
        Err(e) => Ok(Err(e)),
    }
}

/// `prepared.state.hdr.shape` as `(h, w)`; `(0, 0)` when the track is inactive.
fn hdr_shape(json: &Json) -> (usize, usize) {
    let get = |j: &Json, k: &str| match j {
        Json::Object(p) => p.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone()),
        _ => None,
    };
    let shape = get(json, "state")
        .and_then(|s| get(&s, "hdr"))
        .and_then(|h| get(&h, "shape"));
    match shape {
        Some(Json::Array(v)) => match v.as_slice() {
            [Json::Int(h), Json::Int(w), _] => (
                usize::try_from(*h).unwrap_or(0),
                usize::try_from(*w).unwrap_or(0),
            ),
            _ => (0, 0),
        },
        _ => (0, 0),
    }
}

/// A little-endian float32 `.npy` (`<f4`, C order) → its values (any shape).
fn read_f32_npy(path: &str) -> Result<Vec<f32>, Failure> {
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
    if !header.contains("'<f4'") || header.contains("'fortran_order': True") {
        return Err(bad("want a C-order <f4 array"));
    }
    let body = &bytes[at + len..];
    if body.len() % 4 != 0 {
        return Err(bad("payload is not whole float32s"));
    }
    Ok(body
        .chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect())
}

/// A 2-D uint8 or bool `.npy` `(H, W)` → `(w, h, bytes)`.
fn read_mask_npy(path: &str) -> Result<RgbNpy, Failure> {
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
        _ => return Err(bad("unsupported .npy version")),
    };
    let header = bytes
        .get(at..at.saturating_add(len))
        .ok_or_else(|| bad("short header"))?;
    let header = String::from_utf8_lossy(header);
    if !(header.contains("'|u1'") || header.contains("'|b1'"))
        || header.contains("'fortran_order': True")
    {
        return Err(bad("want a C-order uint8 or bool array"));
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
    let [h, w] = dims.as_slice() else {
        return Err(bad("want shape (H, W)"));
    };
    Ok((*w, *h, bytes[at + len..].to_vec()))
}

/// A calibration folder as export-source's `CalibPack` (files sorted by name, as `source-*`).
fn read_pack(dir: &str) -> Result<CalibPack, Failure> {
    let list =
        std::fs::read_dir(dir).map_err(|e| Failure::Usage(format!("cannot list {dir}: {e}")))?;
    let mut files = Vec::new();
    for entry in list {
        let entry = entry.map_err(|e| Failure::Usage(format!("cannot list {dir}: {e}")))?;
        let path = entry.path();
        if path.is_file() {
            let name = entry.file_name().to_string_lossy().into_owned();
            files.push((name, common::read(&path.to_string_lossy())?));
        }
    }
    files.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(CalibPack { files })
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

// ---- export-meta

fn run_meta(command: &str, args: &[String]) -> Result<Output, Failure> {
    if command == "export-meta-read" {
        return Ok(Output {
            text: cmd_read(args)?,
            ok: true,
        });
    }
    if command == "export-meta-batch" {
        return Ok(Output {
            text: cmd_batch(args)?,
            ok: true,
        });
    }
    let json = match command {
        "export-meta-exif" => cmd_exif(args)?,
        "export-meta-photo" => cmd_photo(args)?,
        "export-meta-preview" => cmd_preview(args)?,
        "export-meta-tiff-tags" => cmd_tiff_tags(args)?,
        "export-meta-ifd" => cmd_ifd(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn opt_option<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    args.get(at + 1)
        .map(|s| Some(s.as_str()))
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

fn parse_now_year(args: &[String]) -> Result<i32, Failure> {
    match opt_option(args, "--now-year")? {
        Some(s) => s
            .parse::<i32>()
            .map_err(|_| Failure::Usage(format!("bad --now-year {s:?}"))),
        None => Ok(DEFAULT_NOW_YEAR),
    }
}

fn cmd_exif(args: &[String]) -> Result<Json, Failure> {
    let src = common::option(args, "--src")?;
    let bytes = common::read(src)?;
    Ok(read_source_exif(&bytes).to_json())
}

fn cmd_photo(args: &[String]) -> Result<Json, Failure> {
    let jpg = match opt_option(args, "--jpg")? {
        Some(p) => Some(common::read(p)?),
        None => None,
    };
    let dng = match opt_option(args, "--dng")? {
        Some(p) => Some(common::read(p)?),
        None => None,
    };
    let name = opt_option(args, "--name")?.unwrap_or("");
    let mtime_ms = match opt_option(args, "--mtime-ms")? {
        Some(s) => Some(
            s.parse::<f64>()
                .map_err(|_| Failure::Usage(format!("bad --mtime-ms {s:?}")))?,
        ),
        None => None,
    };
    let tz_offset_min = match opt_option(args, "--tz-offset-min")? {
        Some(s) => s
            .parse::<f64>()
            .map_err(|_| Failure::Usage(format!("bad --tz-offset-min {s:?}")))?,
        None => 0.0,
    };
    let now_year = parse_now_year(args)?;
    let res = photo_meta_from_bytes(
        jpg.as_deref(),
        dng.as_deref(),
        name,
        mtime_ms,
        tz_offset_min,
        now_year,
    )?;
    Ok(res.to_json())
}

fn cmd_read(args: &[String]) -> Result<String, Failure> {
    let path = common::option(args, "--file")?;
    let bytes = common::read(path)?;
    let now_year = parse_now_year(args)?;
    let is_dng = match opt_option(args, "--kind")? {
        Some("dng" | "tiff") => true,
        Some("jpg" | "jpeg") => false,
        Some(other) => return Err(Failure::Usage(format!("bad --kind {other:?}"))),
        None => is_dng_path(path),
    };
    let meta = if is_dng {
        read_dng_meta(&bytes, now_year)
    } else {
        read_jpeg_meta(&bytes, now_year)
    };
    Ok(meta.to_json_string())
}

fn is_dng_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    lower.ends_with(".dng") || lower.ends_with(".tif") || lower.ends_with(".tiff")
}

fn cmd_preview(args: &[String]) -> Result<Json, Failure> {
    let dng_path = common::option(args, "--dng")?;
    let bytes = common::read(dng_path)?;
    Ok(dng_preview_location(&bytes).map_or(Json::Null, |r| r.to_json()))
}

fn open_json(bytes: &[u8]) -> Json {
    match gxr_export::meta::tiff_exif::open_image(bytes) {
        Ok(gxr_export::meta::tiff_exif::ImageDoc::Jpeg(j)) => Json::object([
            ("ok", Json::Bool(true)),
            ("width", Json::Int(i64::from(j.width))),
            ("height", Json::Int(i64::from(j.height))),
        ]),
        Ok(gxr_export::meta::tiff_exif::ImageDoc::Tiff(t)) => Json::object([
            ("ok", Json::Bool(true)),
            ("width", Json::Int(i64::from(t.width))),
            ("height", Json::Int(i64::from(t.height))),
        ]),
        Err(msg) => {
            let (kind, detail) = msg.split_once(':').unwrap_or(("Error", msg.as_str()));
            Json::object([
                ("ok", Json::Bool(false)),
                ("kind", Json::str(kind)),
                ("message", Json::str(detail.trim_start())),
            ])
        }
    }
}

fn cmd_batch(args: &[String]) -> Result<String, Failure> {
    let manifest_path = common::option(args, "--manifest")?;
    let manifest = std::fs::read_to_string(manifest_path)
        .map_err(|e| Failure::Usage(format!("cannot read {manifest_path}: {e}")))?;
    let mtime_ms = match opt_option(args, "--mtime-ms")? {
        Some(s) => Some(
            s.parse::<f64>()
                .map_err(|_| Failure::Usage(format!("bad --mtime-ms {s:?}")))?,
        ),
        None => None,
    };
    let tz_offset_min = match opt_option(args, "--tz-offset-min")? {
        Some(s) => s
            .parse::<f64>()
            .map_err(|_| Failure::Usage(format!("bad --tz-offset-min {s:?}")))?,
        None => 0.0,
    };
    let now_year = parse_now_year(args)?;

    let mut lines = Vec::new();
    for raw_line in manifest.lines() {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let (path, name) = trimmed.split_once('\t').unwrap_or((trimmed, ""));
        let row_res: Result<String, Failure> = (|| {
            let bytes = common::read(path)?;
            let open = open_json(&bytes).render();
            let read = read_jpeg_meta(&bytes, now_year).to_json_string();
            let photo = match photo_meta_from_bytes(
                Some(&bytes),
                None,
                name,
                mtime_ms,
                tz_offset_min,
                now_year,
            ) {
                Ok(res) => Json::object([("ok", res.to_json())]).render(),
                Err(e) => gxr_raw::error_json(&e).render(),
            };
            let exif = read_source_exif(&bytes).to_json().render();
            Ok(format!(
                "{{\"exit\":0,\"open\":{open},\"read\":{read},\"photo\":{photo},\"exif\":{exif}}}"
            ))
        })();
        match row_res {
            Ok(line) => lines.push(line),
            Err(err) => lines.push(common::failure_row_json(&err, crate::USAGE).render()),
        }
    }
    Ok(lines.join("\n"))
}

fn parse_group(args: &[String]) -> Result<Option<u16>, Failure> {
    match opt_option(args, "--group")? {
        Some(s) => s
            .parse::<u16>()
            .map(Some)
            .map_err(|_| Failure::Usage(format!("bad --group {s:?}"))),
        None => Ok(None),
    }
}

fn cmd_tiff_tags(args: &[String]) -> Result<Json, Failure> {
    let group = parse_group(args)?;
    let mut entries = Vec::new();
    for tag in 0..=u16::MAX {
        if let Some(len) = gxr_export::meta::tiff_ifd::tiff_tag_length(tag, group) {
            entries.push((tag.to_string(), Json::Int(i64::try_from(len).unwrap_or(-1))));
        }
    }
    Ok(Json::object([
        (
            "group",
            group.map_or(Json::Null, |g| Json::Int(i64::from(g))),
        ),
        ("lengths", Json::object(entries)),
    ]))
}

fn cmd_ifd(args: &[String]) -> Result<Json, Failure> {
    let path = common::option(args, "--file")?;
    let group = parse_group(args)?;
    let fixup = args.iter().any(|a| a == "--fixup");
    let bytes = common::read(path)?;
    let hdr = bytes
        .get(..8)
        .ok_or_else(|| gxr_raw::CoreError::raw_decode("SyntaxError: not a TIFF file"))?;
    let (endian, bigtiff, off) = gxr_export::meta::tiff_ifd::parse_tiff_header(hdr, &bytes)
        .map_err(gxr_raw::CoreError::raw_decode)?;
    let ifd = gxr_export::meta::tiff_ifd::load_ifd(&bytes, endian, bigtiff, off, group, fixup)
        .map_err(gxr_raw::CoreError::raw_decode)?;
    let tags: Vec<(String, Json)> = ifd
        .tags
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_exif_json()))
        .collect();
    Ok(Json::object([
        ("tags", Json::object(tags)),
        (
            "next",
            Json::Int(i64::try_from(ifd.next).unwrap_or(i64::MAX)),
        ),
    ]))
}
