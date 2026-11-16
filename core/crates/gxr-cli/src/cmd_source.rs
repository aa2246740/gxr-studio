//! `source-*` commands: which picture an export starts from (`gxr_export::source`).
//!
//! ```text
//! source-plan --shot S.json --edit E.json [--dat GXRCE.DAT [--library GXRFX.BIN]]
//!             [--entry ENTRY.bin] [--calib DIR] [--binding TEXT]
//! source-selection --shot S.json --edit E.json --base MODE [--space SPACE.json]
//! source-render --dng FILE --entry ENTRY.bin --stock STOCK04.bin --unit U --calib DIR
//!               --iso N [--adjust A.json] --out OUT.npy
//! source-color-image --shot S.json --edit E.json --base MODE --out OUT.npy
//!               [--dng FILE] [--jpg JPG.npy] [--preview PREVIEW.npy] [--dat ... --library ...]
//!               [--entry ...] [--calib DIR] [--binding TEXT] [--cube C.cube --space SPACE.json]
//! ```

use gxr_color::encode_npy;
use gxr_export::source::{
    CAMERA_JPG, CalibPack, PREVIEW, Pictures, Request, check_rgb_shape, color_image_json,
    iso_from_number, plan_json, render, selection_json,
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

/// A 3-axis uint8 `.npy` (version 1 or 2 header, C order), as `np.save` writes it.
fn read_rgb(path: &str, what: &str) -> Result<Image<u8>, Failure> {
    parse_rgb(bytes_of(path)?, path, what)
}

/// The header is parsed, then the declared shape and the payload length go through the
/// shared `check_rgb_shape` before any pixel is copied (UNIT-COMMON "validate before anything
/// else"); the payload then stays in the buffer the file was read into.
fn parse_rgb(mut data: Vec<u8>, path: &str, what: &str) -> Result<Image<u8>, Failure> {
    let bad = || Failure::Usage(format!("{path} must be a 3-axis uint8 .npy"));
    if data.len() < 10 || &data[..6] != b"\x93NUMPY" {
        return Err(bad());
    }
    let (len, at): (usize, usize) = match data[6] {
        1 => (usize::from(u16::from_le_bytes([data[8], data[9]])), 10),
        2 | 3 if data.len() >= 12 => (
            usize::try_from(u32::from_le_bytes([data[8], data[9], data[10], data[11]]))
                .map_err(|_| bad())?,
            12,
        ),
        _ => return Err(bad()),
    };
    let end = at
        .checked_add(len)
        .filter(|&e| e <= data.len())
        .ok_or_else(bad)?;
    let header = std::str::from_utf8(&data[at..end]).map_err(|_| bad())?;
    if !header.contains("'descr': '|u1'") || !header.contains("'fortran_order': False") {
        return Err(bad());
    }
    let shape_at = header.find("'shape': (").ok_or_else(bad)? + "'shape': (".len();
    let shape_end = header[shape_at..].find(')').ok_or_else(bad)? + shape_at;
    let dims: Vec<usize> = header[shape_at..shape_end]
        .split(',')
        .map(str::trim)
        .filter(|d| !d.is_empty())
        .map(|d| d.parse::<usize>().map_err(|_| bad()))
        .collect::<Result<_, _>>()?;
    let [h, w, c] = dims[..] else {
        return Err(bad());
    };
    check_rgb_shape(h, w, c, data.len() - end, what)?;
    // Drop the header in place: no second payload-sized buffer.
    data.drain(..end);
    Ok(Image {
        height: h,
        width: w,
        channels: c,
        values: data,
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
    let space =
        match maybe_bytes(args, "--space")? {
            Some(bytes) => Some(String::from_utf8(bytes).map_err(|_| {
                Failure::Core(CoreError::new("bad_request", "--space is not UTF-8"))
            })?),
            None => None,
        };
    let text = selection_json(&shot, &edit, option(args, "--base")?, space.as_deref())?;
    Ok(Output { text, ok: true })
}

fn run_render(args: &[String]) -> Result<Output, Failure> {
    let path = option(args, "--dng")?;
    let entry = bytes_of(option(args, "--entry")?)?;
    let stock = bytes_of(option(args, "--stock")?)?;
    let unit = option(args, "--unit")?;
    let pack = read_pack(option(args, "--calib")?)?;
    let iso_text = option(args, "--iso")?;
    // Read as a number, then the same check WASM applies to a JavaScript number.
    let iso_number: f64 = iso_text
        .parse()
        .map_err(|_| Failure::Usage(format!("bad --iso {iso_text:?}")))?;
    let iso = iso_from_number(iso_number)?;
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
    let jpg = maybe(args, "--jpg")?
        .map(|p| read_rgb(p, CAMERA_JPG))
        .transpose()?;
    let preview = maybe(args, "--preview")?
        .map(|p| read_rgb(p, PREVIEW))
        .transpose()?;
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

#[cfg(test)]
mod tests {
    use super::*;

    fn npy(shape: &str, payload: usize) -> Vec<u8> {
        let body = format!("{{'descr': '|u1', 'fortran_order': False, 'shape': {shape}, }}");
        let pad = (64 - (10 + body.len() + 1) % 64) % 64;
        let mut header = body.into_bytes();
        header.extend(std::iter::repeat_n(b' ', pad));
        header.push(b'\n');
        let mut out = b"\x93NUMPY\x01\x00".to_vec();
        out.extend_from_slice(&u16::try_from(header.len()).unwrap().to_le_bytes());
        out.extend_from_slice(&header);
        out.resize(out.len() + payload, 7);
        out
    }

    fn refused(data: Vec<u8>) -> CoreError {
        match parse_rgb(data, "x.npy", CAMERA_JPG) {
            Err(Failure::Core(e)) => e,
            Err(Failure::Usage(u)) => panic!("usage error {u}"),
            Ok(_) => panic!("accepted"),
        }
    }

    /// Sol r2 P2: bad shapes are refused on the header and the payload length, before any copy.
    #[test]
    fn bad_pictures_are_refused_before_any_copy() {
        let big = 8_388_611;
        let e = refused(npy("(1, 2, 3)", big));
        assert_eq!(e.message, "相机 JPG的像素字节数和宽高不符");
        let e = refused(npy("(1, 2, 3)", 2));
        assert_eq!(e.message, "相机 JPG的像素字节数和宽高不符");
        let e = refused(npy("(4294967295, 4294967295, 3)", big));
        assert_eq!(
            (e.kind, e.message.as_str()),
            ("value", gxr_color::ARRAY_TOO_BIG)
        );
        let e = refused(npy("(1, 2, 4)", big));
        assert_eq!(e.message, "相机 JPG要是 RGB 三通道");
        let e = refused(npy("(0, 2, 3)", 0));
        assert_eq!(e.message, "相机 JPG的宽高要大于 0");
    }

    /// A good picture keeps the file's buffer: the payload is not copied into a new one.
    #[test]
    fn good_pictures_keep_the_read_buffer() {
        let data = npy("(2, 4, 3)", 24);
        let start = data.as_ptr();
        let img = parse_rgb(data, "x.npy", PREVIEW).unwrap_or_else(|_| panic!("refused"));
        assert_eq!((img.height, img.width, img.values.len()), (2, 4, 24));
        assert_eq!(img.values.as_ptr(), start);
        assert!(img.values.iter().all(|&v| v == 7));
    }
}
