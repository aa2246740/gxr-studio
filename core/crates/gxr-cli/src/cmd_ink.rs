//! `ink-*` subcommands for GXR Studio M4 `ink` parity testing.

use gxr_finish::geometry::bounds::{checked_mul_u64, fit_count, too_big};
use gxr_finish::ink::{
    InkImage, InkMode, LogoInput, gaussian_blur, mean_f32, mean_u8_as_f32, pairwise_sum_f32,
    run_ink_ops,
};
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "ink-ops" => cmd_ink_ops(args)?,
        "ink-reduce" => cmd_ink_reduce(args)?,
        "ink-blur" => cmd_ink_blur(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn usage(message: impl Into<String>) -> Failure {
    Failure::Usage(message.into())
}

struct NpyData {
    descr: String,
    shape: Vec<usize>,
    data: Vec<u8>,
}

fn parse_npy(raw: &[u8]) -> Option<NpyData> {
    if raw.get(..6)? != b"\x93NUMPY" {
        return None;
    }
    let (at, len) = match raw.get(6)? {
        1 => (
            10usize,
            usize::from(u16::from_le_bytes([*raw.get(8)?, *raw.get(9)?])),
        ),
        2 | 3 => (
            12,
            usize::try_from(u32::from_le_bytes(raw.get(8..12)?.try_into().ok()?)).ok()?,
        ),
        _ => return None,
    };
    let header = std::str::from_utf8(raw.get(at..at.checked_add(len)?)?).ok()?;
    let field = |key: &str, end: char| -> Option<&str> {
        let s = header.find(key)? + key.len();
        let e = header[s..].find(end)? + s;
        Some(&header[s..e])
    };
    if field("'fortran_order': ", ',')? != "False" {
        return None;
    }
    let descr = field("'descr': '", '\'')?.to_string();
    let shape = field("'shape': (", ')')?
        .split(',')
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(|p| p.parse().ok())
        .collect::<Option<Vec<usize>>>()?;
    let payload = raw.get(at.checked_add(len)?..)?;
    let n = shape
        .iter()
        .try_fold(1u64, |a, &d| a.checked_mul(u64::try_from(d).ok()?))?;
    let elem_size = match descr.as_str() {
        "|u1" => 1_u64,
        "<f4" => 4_u64,
        "<f8" => 8_u64,
        _ => return None,
    };
    let _ = fit_count(n, elem_size).ok()?;
    if checked_mul_u64(n, elem_size).ok()? != u64::try_from(payload.len()).ok()? {
        return None;
    }
    let mut data = Vec::new();
    data.try_reserve_exact(payload.len()).ok()?;
    data.extend_from_slice(payload);
    Some(NpyData { descr, shape, data })
}

fn read_npy_file(path: &str) -> Result<NpyData, Failure> {
    let raw = std::fs::read(path).map_err(|e| usage(format!("cannot read {path}: {e}")))?;
    parse_npy(&raw).ok_or_else(|| usage(format!("{path} is not a little-endian C-order npy file")))
}

fn write_npy_u8(path: &str, shape: &[usize], payload: &[u8]) -> Result<Json, Failure> {
    let dims: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        format!("({})", dims.join(", "))
    };
    let mut dict = format!("{{'descr': '|u1', 'fortran_order': False, 'shape': {shape_text}, }}");
    dict.push_str(&" ".repeat((64 - (11 + dict.len()) % 64) % 64));
    dict.push('\n');
    let mut out = b"\x93NUMPY\x01\x00".to_vec();
    out.extend_from_slice(
        &u16::try_from(dict.len())
            .map_err(|_| usage("npy header too long"))?
            .to_le_bytes(),
    );
    out.extend_from_slice(dict.as_bytes());
    out.extend_from_slice(payload);
    std::fs::write(path, out).map_err(|e| usage(format!("cannot write {path}: {e}")))?;
    let dims_i64: Vec<i64> = shape
        .iter()
        .map(|&d| i64::try_from(d).unwrap_or(i64::MAX))
        .collect();
    Ok(Json::object([
        ("dtype", Json::str("uint8")),
        ("shape", Json::ints(&dims_i64)),
    ]))
}

fn read_image_npy(path: &str, mode_override: Option<&str>) -> Result<InkImage, Failure> {
    let npy = read_npy_file(path)?;
    if npy.descr != "|u1" {
        return Err(usage(format!("expected uint8 npy, got {}", npy.descr)));
    }
    let (h, w, inferred_mode) = match npy.shape.as_slice() {
        [h, w] => (*h as u64, *w as u64, InkMode::L),
        [h, w, 1] => (*h as u64, *w as u64, InkMode::L),
        [h, w, 3] => (*h as u64, *w as u64, InkMode::Rgb),
        [h, w, 4] => (*h as u64, *w as u64, InkMode::Rgba),
        s => return Err(usage(format!("unsupported image npy shape {s:?}"))),
    };
    let mode = match mode_override {
        Some(m) => InkMode::parse(m)?,
        None => inferred_mode,
    };
    InkImage::from_bytes(mode, w, h, &npy.data).map_err(Failure::Core)
}

fn opt_str<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    if args.iter().any(|a| a == name) {
        Ok(Some(common::option(args, name)?))
    } else {
        Ok(None)
    }
}

fn read_ops_text(args: &[String]) -> Result<String, Failure> {
    if let Some(path) = opt_str(args, "--ops")? {
        return std::fs::read_to_string(path)
            .map_err(|e| usage(format!("cannot read {path}: {e}")));
    }
    if let Some(inline) = opt_str(args, "--ops-json")? {
        return Ok(inline.to_string());
    }
    Err(usage("missing --ops or --ops-json"))
}

fn collect_logos(args: &[String]) -> Result<Vec<LogoInput>, Failure> {
    let mut logos = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--logo" {
            let spec = args
                .get(i + 1)
                .ok_or_else(|| usage("--logo requires ID=PATH.npy"))?;
            let (id, path) = spec
                .split_once('=')
                .ok_or_else(|| usage("--logo expects ID=PATH.npy"))?;
            let img = read_image_npy(path, Some("RGBA"))?;
            logos.push(LogoInput {
                id: id.to_string(),
                image: img,
            });
            i += 2;
        } else {
            i += 1;
        }
    }
    Ok(logos)
}

fn cmd_ink_ops(args: &[String]) -> Result<Json, Failure> {
    let in_path = common::option(args, "--canvas")?;
    let out_path = common::option(args, "--out")?;
    let mode_override = opt_str(args, "--mode")?;
    let ops_json = read_ops_text(args)?;
    let logos = collect_logos(args)?;
    let canvas = read_image_npy(in_path, mode_override)?;
    let out = run_ink_ops(
        &canvas.data,
        canvas.width as u64,
        canvas.height as u64,
        canvas.mode,
        &ops_json,
        &logos,
    )?;
    let out_shape: Vec<usize> = match out.mode {
        InkMode::L => vec![out.height, out.width],
        InkMode::Rgb => vec![out.height, out.width, 3],
        InkMode::Rgba => vec![out.height, out.width, 4],
    };
    let mut info = write_npy_u8(out_path, &out_shape, &out.data)?;
    let _ = &mut info;
    let dims = [out.height as i64, out.width as i64];
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("mode", Json::str(out.mode.as_str())),
        (
            "shape",
            Json::ints(&match out.mode {
                InkMode::L => vec![dims[0], dims[1]],
                InkMode::Rgb => vec![dims[0], dims[1], 3],
                InkMode::Rgba => vec![dims[0], dims[1], 4],
            }),
        ),
    ]))
}

fn cmd_ink_reduce(args: &[String]) -> Result<Json, Failure> {
    let in_path = common::option(args, "--in")?;
    let op = common::option(args, "--op")?;
    let npy = read_npy_file(in_path)?;
    let val: f32 = match npy.descr.as_str() {
        "<f4" => {
            let mut floats = Vec::new();
            floats
                .try_reserve_exact(npy.data.len() / 4)
                .map_err(|_| Failure::Core(too_big()))?;
            for c in npy.data.chunks_exact(4) {
                floats.push(f32::from_le_bytes([c[0], c[1], c[2], c[3]]));
            }
            match op {
                "sum" => pairwise_sum_f32(&floats),
                "mean" => mean_f32(&floats),
                _ => return Err(usage(format!("unknown reduce op {op:?}"))),
            }
        }
        "|u1" => match op {
            "mean" => mean_u8_as_f32(&npy.data),
            "sum" => {
                let floats: Vec<f32> = npy.data.iter().map(|&v| f32::from(v)).collect();
                pairwise_sum_f32(&floats)
            }
            _ => return Err(usage(format!("unknown reduce op {op:?}"))),
        },
        other => {
            return Err(Failure::Core(CoreError::new(
                "value_error",
                format!("unsupported dtype {other}"),
            )));
        }
    };
    let bits = val.to_bits();
    Ok(Json::object([
        ("op", Json::str(op)),
        ("bits", Json::Int(i64::from(bits))),
        ("hex", Json::str(format!("{bits:08x}"))),
    ]))
}

fn cmd_ink_blur(args: &[String]) -> Result<Json, Failure> {
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let mode_override = opt_str(args, "--mode")?;
    let radius: f64 = common::option(args, "--radius")?
        .parse()
        .map_err(|_| usage("invalid --radius"))?;
    let yradius: f64 = match opt_str(args, "--y-radius")? {
        Some(v) => v.parse().map_err(|_| usage("invalid --y-radius"))?,
        None => radius,
    };
    let im = read_image_npy(in_path, mode_override)?;
    let out = gaussian_blur(&im, radius, yradius)?;
    let out_shape: Vec<usize> = match out.mode {
        InkMode::L => vec![out.height, out.width],
        InkMode::Rgb => vec![out.height, out.width, 3],
        InkMode::Rgba => vec![out.height, out.width, 4],
    };
    let _ = write_npy_u8(out_path, &out_shape, &out.data)?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("mode", Json::str(out.mode.as_str())),
        (
            "shape",
            Json::ints(&out_shape.iter().map(|&d| d as i64).collect::<Vec<_>>()),
        ),
    ]))
}
