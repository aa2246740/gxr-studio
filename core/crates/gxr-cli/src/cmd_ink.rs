//! `ink-*` subcommands for GXR Studio M4 `ink` parity testing.

use std::io::{BufReader, BufWriter, Read, Write};

use gxr_finish::geometry::bounds::{checked_mul_u64, fit_count, too_big};
use gxr_finish::ink::{
    InkImage, InkMode, LogoInput, Pcg64, background, blurred_background, gaussian_blur, mean_f32,
    mean_u8_as_f32, pairwise_sum_f32, paper_grain, run_ink_ops,
};
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "ink-ops" => cmd_ink_ops(args)?,
        "ink-reduce" => cmd_ink_reduce(args)?,
        "ink-blur" => cmd_ink_blur(args)?,
        "ink-backdrop" => cmd_ink_backdrop(args)?,
        "ink-grain" => cmd_ink_grain(args)?,
        "ink-libm" => cmd_ink_libm(args)?,
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

fn write_npy_raw(path: &str, descr: &str, shape: &[usize], raw: &[u8]) -> Result<(), Failure> {
    let dims: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        format!("({})", dims.join(", "))
    };
    let mut dict =
        format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape_text}, }}");
    dict.push_str(&" ".repeat((64 - (11 + dict.len()) % 64) % 64));
    dict.push('\n');
    let mut file =
        std::fs::File::create(path).map_err(|e| usage(format!("cannot create {path}: {e}")))?;
    let mut writer = BufWriter::new(&mut file);
    writer
        .write_all(b"\x93NUMPY\x01\x00")
        .and_then(|()| {
            let len = u16::try_from(dict.len()).map_err(std::io::Error::other)?;
            writer.write_all(&len.to_le_bytes())
        })
        .and_then(|()| writer.write_all(dict.as_bytes()))
        .and_then(|()| writer.write_all(raw))
        .and_then(|()| writer.flush())
        .map_err(|e| usage(format!("cannot write {path}: {e}")))
}

fn stream_npy_header(writer: &mut impl Write, descr: &str, shape: &[usize]) -> Result<(), Failure> {
    let dims: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        format!("({})", dims.join(", "))
    };
    let mut dict =
        format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape_text}, }}");
    dict.push_str(&" ".repeat((64 - (11 + dict.len()) % 64) % 64));
    dict.push('\n');
    let len = u16::try_from(dict.len()).map_err(|_| usage("npy header too long"))?;
    writer
        .write_all(b"\x93NUMPY\x01\x00")
        .and_then(|()| writer.write_all(&len.to_le_bytes()))
        .and_then(|()| writer.write_all(dict.as_bytes()))
        .map_err(|e| usage(format!("cannot write npy header: {e}")))
}

fn parse_rgb_triplet(raw: &str) -> Result<[u8; 3], Failure> {
    let parts: Vec<&str> = raw.split(',').map(str::trim).collect();
    if parts.len() != 3 {
        return Err(usage("--bg expects R,G,B"));
    }
    Ok([
        parts[0].parse().map_err(|_| usage("invalid --bg R"))?,
        parts[1].parse().map_err(|_| usage("invalid --bg G"))?,
        parts[2].parse().map_err(|_| usage("invalid --bg B"))?,
    ])
}

fn cmd_ink_backdrop(args: &[String]) -> Result<Json, Failure> {
    let out_path = common::option(args, "--out")?;
    let width: u64 = common::option(args, "--w")?
        .parse()
        .map_err(|_| usage("invalid --w"))?;
    let height: u64 = common::option(args, "--h")?
        .parse()
        .map_err(|_| usage("invalid --h"))?;
    let out = if let Some(in_path) = opt_str(args, "--in")? {
        let sigma: f64 = common::option(args, "--sigma")?
            .parse()
            .map_err(|_| usage("invalid --sigma"))?;
        let sat: f64 = match opt_str(args, "--sat")? {
            Some(v) => v.parse().map_err(|_| usage("invalid --sat"))?,
            None => 1.0,
        };
        let veil: f64 = common::option(args, "--veil")?
            .parse()
            .map_err(|_| usage("invalid --veil"))?;
        let photo = read_image_npy(in_path, Some("RGB"))?;
        blurred_background(&photo, width, height, sigma, sat, veil)?
    } else {
        let bg = parse_rgb_triplet(common::option(args, "--bg")?)?;
        let falloff: Option<f64> = match opt_str(args, "--falloff")? {
            Some(v) => Some(v.parse().map_err(|_| usage("invalid --falloff"))?),
            None => None,
        };
        background(width, height, bg, falloff)?
    };
    let out_shape = vec![out.height, out.width, 3];
    let _ = write_npy_u8(out_path, &out_shape, &out.data)?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("mode", Json::str(out.mode.as_str())),
        (
            "shape",
            Json::ints(&[out.height as i64, out.width as i64, 3]),
        ),
    ]))
}

fn cmd_ink_grain(args: &[String]) -> Result<Json, Failure> {
    if let Some(draws_s) = opt_str(args, "--draws")? {
        let draws: usize = draws_s.parse().map_err(|_| usage("invalid --draws"))?;
        let u64_draws: usize = match opt_str(args, "--u64-draws")? {
            Some(v) => v.parse().map_err(|_| usage("invalid --u64-draws"))?,
            None => draws,
        };
        let seed: u64 = match opt_str(args, "--seed")? {
            Some(v) => v.parse().map_err(|_| usage("invalid --seed"))?,
            None => 42,
        };
        let u64_out = opt_str(args, "--u64-out")?;
        let normal_out = opt_str(args, "--normal-out")?.or(opt_str(args, "--out")?);
        if u64_out.is_none() && normal_out.is_none() {
            return Err(usage("--draws mode requires --u64-out and/or --normal-out"));
        }
        if let Some(path) = u64_out {
            let mut rng = Pcg64::from_seed(seed);
            let mut file = std::fs::File::create(path)
                .map_err(|e| usage(format!("cannot create {path}: {e}")))?;
            let mut writer = BufWriter::new(&mut file);
            stream_npy_header(&mut writer, "<u8", &[u64_draws])?;
            for _ in 0..u64_draws {
                writer
                    .write_all(&rng.next_u64().to_le_bytes())
                    .map_err(|e| usage(format!("write failed: {e}")))?;
            }
            writer
                .flush()
                .map_err(|e| usage(format!("flush failed: {e}")))?;
        }
        if let Some(path) = normal_out {
            let mut rng = Pcg64::from_seed(seed);
            let mut file = std::fs::File::create(path)
                .map_err(|e| usage(format!("cannot create {path}: {e}")))?;
            let mut writer = BufWriter::new(&mut file);
            stream_npy_header(&mut writer, "<f8", &[draws])?;
            for _ in 0..draws {
                writer
                    .write_all(&rng.standard_normal().to_le_bytes())
                    .map_err(|e| usage(format!("write failed: {e}")))?;
            }
            writer
                .flush()
                .map_err(|e| usage(format!("flush failed: {e}")))?;
        }
        return Ok(Json::object([
            ("draws", Json::Int(draws as i64)),
            ("seed", Json::str(seed.to_string())),
            ("u64_out", Json::str(u64_out.unwrap_or(""))),
            ("normal_out", Json::str(normal_out.unwrap_or(""))),
        ]));
    }

    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let amount: f64 = common::option(args, "--amount")?
        .parse()
        .map_err(|_| usage("invalid --amount"))?;
    let canvas = read_image_npy(in_path, Some("RGB"))?;
    let out = paper_grain(&canvas, amount)?;
    let out_shape = vec![out.height, out.width, 3];
    let _ = write_npy_u8(out_path, &out_shape, &out.data)?;
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("mode", Json::str(out.mode.as_str())),
        (
            "shape",
            Json::ints(&[out.height as i64, out.width as i64, 3]),
        ),
    ]))
}

fn cmd_ink_libm(args: &[String]) -> Result<Json, Failure> {
    if !args.iter().any(|a| a == "--stream") {
        let op = common::option(args, "--op")?;
        let in_path = common::option(args, "--in")?;
        let out_path = common::option(args, "--out")?;
        let npy = read_npy_file(in_path)?;
        if npy.descr != "<f8" {
            return Err(usage("ink-libm expects float64 npy"));
        }
        let mut out_bytes = Vec::with_capacity(npy.data.len());
        for c in npy.data.chunks_exact(8) {
            let v = f64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]);
            let y = match op {
                "ln" => gxr_math::ln(v),
                "log1p" => gxr_math::log1p(v),
                "exp" => gxr_math::exp(v),
                "sin" => gxr_math::sin(v),
                "cos" => gxr_math::cos(v),
                _ => return Err(usage(format!("unknown --op {op:?}"))),
            };
            out_bytes.extend_from_slice(&y.to_le_bytes());
        }
        write_npy_raw(out_path, "<f8", &npy.shape, &out_bytes)?;
        return Ok(Json::object([("out", Json::str(out_path))]));
    }

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut reader = BufReader::new(stdin.lock());
    let mut writer = BufWriter::new(stdout.lock());
    let mut hdr = [0u8; 17];
    loop {
        let mut first = [0u8; 1];
        match reader.read(&mut first) {
            Ok(0) => break,
            Ok(1) => hdr[0] = first[0],
            Ok(_) => unreachable!(),
            Err(e) => return Err(usage(format!("stream read error: {e}"))),
        }
        reader
            .read_exact(&mut hdr[1..])
            .map_err(|e| usage(format!("stream header read error: {e}")))?;
        let tag = hdr[0];
        let count_u64 = u64::from_le_bytes([
            hdr[9], hdr[10], hdr[11], hdr[12], hdr[13], hdr[14], hdr[15], hdr[16],
        ]);
        let count = fit_count(count_u64, 8).map_err(Failure::Core)?;
        let nbytes = fit_count(checked_mul_u64(count_u64, 8).map_err(Failure::Core)?, 1)
            .map_err(Failure::Core)?;
        let mut buf = Vec::new();
        buf.try_reserve_exact(nbytes)
            .map_err(|_| Failure::Core(too_big()))?;
        buf.resize(nbytes, 0);
        reader
            .read_exact(&mut buf)
            .map_err(|e| usage(format!("stream payload read error: {e}")))?;
        let _ = count;
        for chunk in buf.chunks_exact(8) {
            let v = f64::from_le_bytes([
                chunk[0], chunk[1], chunk[2], chunk[3], chunk[4], chunk[5], chunk[6], chunk[7],
            ]);
            let y = match tag {
                0 => gxr_math::ln(v),
                1 => gxr_math::log1p(v),
                2 => gxr_math::exp(v),
                3 => gxr_math::sin(v),
                4 => gxr_math::cos(v),
                _ => return Err(usage(format!("unknown ink-libm stream tag {tag}"))),
            };
            writer
                .write_all(&y.to_le_bytes())
                .map_err(|e| usage(format!("stream write error: {e}")))?;
        }
        writer
            .flush()
            .map_err(|e| usage(format!("stream flush error: {e}")))?;
    }
    Ok(Json::object([("ok", Json::Bool(true))]))
}
