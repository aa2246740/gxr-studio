//! `source-*` commands: which picture an export starts from (`gxr_export::source`).
//!
//! ```text
//! source-plan --shot S.json --edit E.json [--dat GXRCE.DAT [--library GXRFX.BIN]]
//!             [--entry ENTRY.bin] [--calib DIR] [--binding TEXT]
//! source-selection --shot S.json --edit E.json --base MODE
//! source-render --dng FILE --entry ENTRY.bin --stock STOCK04.bin --unit U --calib DIR
//!               --iso N [--adjust A.json] --out OUT.npy
//! source-color-image --shot S.json --edit E.json --base MODE --out OUT.npy
//!               [--dng FILE] [--jpg JPG.npy] [--preview PREVIEW.npy] [--dat ... --library ...]
//!               [--entry ...] [--calib DIR] [--binding TEXT] [--cube C.cube --space SPACE.json]
//! ```

use gxr_color::{decode_npy, encode_npy};
use gxr_export::source::{
    CalibPack, Pictures, Request, color_image_json, plan_json, render, selection_json,
};
use gxr_raw::{CoreError, Image, Json};

use crate::common::{Failure, Output, base_name, option, read};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "source-plan" => run_plan(args),
        "source-selection" => run_selection(args),
        "source-render" => run_render(args),
        "source-color-image" => run_color_image(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn maybe<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    if args.iter().any(|a| a == name) {
        option(args, name).map(Some)
    } else {
        Ok(None)
    }
}

fn bytes_of(path: &str) -> Result<Vec<u8>, Failure> {
    std::fs::read(path).map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")))
}

fn maybe_bytes(args: &[String], name: &str) -> Result<Option<Vec<u8>>, Failure> {
    maybe(args, name)?.map(bytes_of).transpose()
}

/// Every file directly in `dir`, by name: the files a phone user picks for the pack.
fn read_pack(dir: &str) -> Result<CalibPack, Failure> {
    let entries =
        std::fs::read_dir(dir).map_err(|e| Failure::Usage(format!("cannot list {dir}: {e}")))?;
    let mut files = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| Failure::Usage(format!("cannot list {dir}: {e}")))?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        files.push((name, bytes_of(&path.to_string_lossy())?));
    }
    files.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(CalibPack { files })
}

fn maybe_pack(args: &[String]) -> Result<Option<CalibPack>, Failure> {
    maybe(args, "--calib")?.map(read_pack).transpose()
}

/// A `(h, w, 3)` uint8 `.npy`.
fn read_rgb(path: &str) -> Result<Image<u8>, Failure> {
    let npy = decode_npy(&bytes_of(path)?)?;
    if npy.descr != "|u1" || npy.shape.len() != 3 || npy.shape[2] != 3 {
        return Err(Failure::Usage(format!(
            "{path} must be a (h, w, 3) uint8 array"
        )));
    }
    Ok(Image {
        height: npy.shape[0],
        width: npy.shape[1],
        channels: 3,
        values: npy.values,
    })
}

fn write_rgb(out: &str, rgb: &Image<u8>) -> Result<(), Failure> {
    let bytes = encode_npy("|u1", &[rgb.height, rgb.width, rgb.channels], &rgb.values);
    std::fs::write(out, bytes).map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))
}

fn run_plan(args: &[String]) -> Result<Output, Failure> {
    let shot = bytes_of(option(args, "--shot")?)?;
    let edit = bytes_of(option(args, "--edit")?)?;
    let dat = maybe_bytes(args, "--dat")?;
    let library = maybe_bytes(args, "--library")?;
    let entry = maybe_bytes(args, "--entry")?;
    let pack = maybe_pack(args)?;
    let binding = maybe(args, "--binding")?.unwrap_or("");
    let req = Request {
        shot: &shot,
        edit: &edit,
        dat: dat.as_deref(),
        library: library.as_deref(),
        entry: entry.as_deref(),
        pack: pack.as_ref(),
        binding,
    };
    let (ok, text) = plan_json(&req);
    Ok(Output { text, ok })
}

fn run_selection(args: &[String]) -> Result<Output, Failure> {
    let shot = bytes_of(option(args, "--shot")?)?;
    let edit = bytes_of(option(args, "--edit")?)?;
    let text = selection_json(&shot, &edit, option(args, "--base")?)?;
    Ok(Output { text, ok: true })
}

fn run_render(args: &[String]) -> Result<Output, Failure> {
    let path = option(args, "--dng")?;
    let entry = bytes_of(option(args, "--entry")?)?;
    let stock = bytes_of(option(args, "--stock")?)?;
    let unit = option(args, "--unit")?;
    let pack = read_pack(option(args, "--calib")?)?;
    let iso_text = option(args, "--iso")?;
    let iso: i64 = iso_text
        .parse()
        .map_err(|_| Failure::Usage(format!("bad --iso {iso_text:?}")))?;
    let tones = maybe_bytes(args, "--adjust")?.unwrap_or_else(|| b"{}".to_vec());
    let out = option(args, "--out")?;
    let data = read(path)?;
    let rgb = render(
        &data,
        &base_name(path),
        &entry,
        &stock,
        unit,
        &pack,
        iso,
        &tones,
    )?;
    write_rgb(out, &rgb)?;
    Ok(Output {
        text: shape_json(out, &rgb)?.render(),
        ok: true,
    })
}

fn shape_json(out: &str, rgb: &Image<u8>) -> Result<Json, Failure> {
    let dim = |d: usize| {
        i64::try_from(d)
            .map(Json::Int)
            .map_err(|_| Failure::Core(CoreError::new("value", "picture too big")))
    };
    Ok(Json::object([
        ("out", Json::str(out)),
        ("dtype", Json::str("uint8")),
        (
            "shape",
            Json::Array(vec![dim(rgb.height)?, dim(rgb.width)?, dim(rgb.channels)?]),
        ),
    ]))
}

fn run_color_image(args: &[String]) -> Result<Output, Failure> {
    let shot = bytes_of(option(args, "--shot")?)?;
    let edit = bytes_of(option(args, "--edit")?)?;
    let base = option(args, "--base")?;
    let out = option(args, "--out")?;
    let dat = maybe_bytes(args, "--dat")?;
    let library = maybe_bytes(args, "--library")?;
    let entry = maybe_bytes(args, "--entry")?;
    let pack = maybe_pack(args)?;
    let binding = maybe(args, "--binding")?.unwrap_or("");
    let dng = maybe_bytes(args, "--dng")?;
    let jpg = maybe(args, "--jpg")?.map(read_rgb).transpose()?;
    let preview = maybe(args, "--preview")?.map(read_rgb).transpose()?;
    let cube = maybe_bytes(args, "--cube")?;
    let space = maybe_bytes(args, "--space")?
        .map(|b| String::from_utf8(b).map_err(|_| Failure::Usage("--space is not UTF-8".into())))
        .transpose()?;
    let req = Request {
        shot: &shot,
        edit: &edit,
        dat: dat.as_deref(),
        library: library.as_deref(),
        entry: entry.as_deref(),
        pack: pack.as_ref(),
        binding,
    };
    let pics = Pictures {
        dng: dng.as_deref(),
        jpg: jpg.as_ref(),
        preview: preview.as_ref(),
        cube: cube.as_deref(),
        space: space.as_deref(),
    };
    match color_image_json(&req, base, &pics) {
        Ok((text, rgb)) => {
            write_rgb(out, &rgb)?;
            Ok(Output { text, ok: true })
        }
        Err(text) => Ok(Output { text, ok: false }),
    }
}
