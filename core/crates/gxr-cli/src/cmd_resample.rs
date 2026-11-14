//! `resample-*` subcommands. Owner: wave-2 unit resample.
//!
//! ```text
//! resample-u8  --in IN.npy --mode RGB|RGBA|L --filter lanczos|box|bilinear|bicubic --width W --height H --out OUT.npy [--in-shape H W] [--channels C]
//! resample-f32 --in IN.npy --filter box|lanczos|bilinear|bicubic --width W --height H --out OUT.npy [--in-shape H W]
//! ```

use gxr_finish::geometry::bounds::{checked_mul_u64, fit_count};
use gxr_finish::resample::{Filter, Mode, resample_f32_u64, resample_u8_u64};
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "resample-u8" => cmd_resample_u8(args)?,
        "resample-f32" => cmd_resample_f32(args)?,
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

fn parse_u64(args: &[String], name: &str) -> Result<u64, Failure> {
    let raw = common::option(args, name)?;
    raw.parse()
        .map_err(|_| usage(format!("bad integer for {name}: {raw:?}")))
}

fn opt_pair_u64(args: &[String], name: &str) -> Result<Option<(u64, u64)>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    let get = |k: usize| -> Result<u64, Failure> {
        args.get(at + k)
            .and_then(|v| v.parse().ok())
            .ok_or_else(|| usage(format!("{name} needs two sizes")))
    };
    Ok(Some((get(1)?, get(2)?)))
}

struct Npy {
    descr: String,
    shape: Vec<usize>,
    data: Vec<u8>,
}

fn read_npy(args: &[String], name: &str) -> Result<Npy, Failure> {
    let path = common::option(args, name)?;
    let raw = std::fs::read(path).map_err(|e| usage(format!("cannot read {path}: {e}")))?;
    parse_npy(&raw).ok_or_else(|| usage(format!("{path} is not a little-endian C-order npy file")))
}

fn parse_npy(raw: &[u8]) -> Option<Npy> {
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
        "<f4" => 4_u64,
        "|u1" => 1_u64,
        _ => return None,
    };
    let _ = fit_count(n, elem_size).ok()?;
    if checked_mul_u64(n, elem_size).ok()? != u64::try_from(payload.len()).ok()? {
        return None;
    }
    let mut data = Vec::new();
    data.try_reserve_exact(payload.len()).ok()?;
    data.extend_from_slice(payload);
    Some(Npy { descr, shape, data })
}

fn write_npy(
    args: &[String],
    name: &str,
    descr: &str,
    shape: &[usize],
    payload: &[u8],
) -> Result<Json, Failure> {
    let path = common::option(args, name)?;
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
        ("dtype", Json::str(descr)),
        ("shape", Json::ints(&dims_i64)),
    ]))
}

fn cmd_resample_u8(args: &[String]) -> Result<Json, Failure> {
    let input = read_npy(args, "--in")?;
    if input.descr != "|u1" {
        return Err(usage(format!("expected uint8 npy, got {}", input.descr)));
    }
    let mode_str = common::option(args, "--mode")?;
    let filter_str = common::option(args, "--filter")?;
    let out_w = parse_u64(args, "--width")?;
    let out_h = parse_u64(args, "--height")?;
    let in_shape = opt_pair_u64(args, "--in-shape")?;
    let ch_opt = if args.iter().any(|a| a == "--channels") {
        Some(parse_u64(args, "--channels")?)
    } else {
        None
    };

    let (w, h, shape_ch, is_3d) = match input.shape.as_slice() {
        [ih, iw] => (*iw as u64, *ih as u64, 1_u64, false),
        [ih, iw, c] => (*iw as u64, *ih as u64, *c as u64, true),
        s => return Err(usage(format!("expected 2-D or 3-D array, got shape {s:?}"))),
    };
    let (w, h) = in_shape.map_or((w, h), |(sh, sw)| (sw, sh));
    let channels = ch_opt.unwrap_or(shape_ch);

    let mode = Mode::parse(mode_str)?;
    let filter = Filter::parse(filter_str)?;
    if channels != mode.channels() as u64 {
        return Err(Failure::Core(CoreError::new(
            "value_error",
            "channel count does not match mode",
        )));
    }

    let res = resample_u8_u64(&input.data, w, h, mode, filter, out_w, out_h)?;
    let ow = out_w as usize;
    let oh = out_h as usize;
    let out_shape: Vec<usize> = if mode == Mode::L && !is_3d {
        vec![oh, ow]
    } else {
        vec![oh, ow, mode.channels()]
    };
    write_npy(args, "--out", "|u1", &out_shape, &res)
}

fn cmd_resample_f32(args: &[String]) -> Result<Json, Failure> {
    let input = read_npy(args, "--in")?;
    if input.descr != "<f4" {
        return Err(usage(format!("expected float32 npy, got {}", input.descr)));
    }
    let filter_str = common::option(args, "--filter")?;
    let out_w = parse_u64(args, "--width")?;
    let out_h = parse_u64(args, "--height")?;
    let in_shape = opt_pair_u64(args, "--in-shape")?;

    let (w, h, is_3d) = match input.shape.as_slice() {
        [ih, iw] => (*iw as u64, *ih as u64, false),
        [ih, iw, 1] => (*iw as u64, *ih as u64, true),
        s => {
            return Err(usage(format!(
                "expected 2-D (H, W) float32 array, got shape {s:?}"
            )));
        }
    };
    let (w, h) = in_shape.map_or((w, h), |(sh, sw)| (sw, sh));

    let filter = Filter::parse(filter_str)?;
    let mut floats = Vec::new();
    floats
        .try_reserve_exact(input.data.len() / 4)
        .map_err(|_| Failure::Core(gxr_finish::geometry::bounds::too_big()))?;
    for c in input.data.chunks_exact(4) {
        floats.push(f32::from_le_bytes([c[0], c[1], c[2], c[3]]));
    }

    let res = resample_f32_u64(&floats, w, h, filter, out_w, out_h)?;
    let mut payload = Vec::new();
    let byte_len = checked_mul_u64(res.len() as u64, 4)?;
    let byte_len_usize = fit_count(byte_len, 1)?;
    payload
        .try_reserve_exact(byte_len_usize)
        .map_err(|_| Failure::Core(gxr_finish::geometry::bounds::too_big()))?;
    for v in res {
        payload.extend_from_slice(&v.to_le_bytes());
    }
    let ow = out_w as usize;
    let oh = out_h as usize;
    let out_shape: Vec<usize> = if is_3d { vec![oh, ow, 1] } else { vec![oh, ow] };
    write_npy(args, "--out", "<f4", &out_shape, &payload)
}
