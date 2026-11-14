//! `lut-*` and `scene-*` commands. Owner: wave-1 unit lut.

use std::io::{BufReader, BufWriter, Read, Write};
use std::path::Path;

use gxr_lut::{
    Cube, LutTableResult, RenderSpec, Target, apply_u8, checked_elements, checked_product_u64,
    decode, encode, fit_count_u64, identify, lut_bytes, mul_u64, scene_colour, scene_xyz,
    too_big_core, try_vec_cap,
};
use gxr_raw::{CoreError, Dng, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "lut-parse" => run_parse(args)?,
        "lut-apply" => run_apply(args)?,
        "lut-encode" => run_encode(args)?,
        "lut-table" => run_table(args)?,
        "lut-identify" => run_identify(args)?,
        "lut-libm" => run_libm(args)?,
        "scene-xyz" => run_scene_xyz(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn has_flag(args: &[String], flag: &str) -> bool {
    args.iter().any(|a| a == flag)
}

fn opt_maybe<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    let val = args
        .get(at + 1)
        .map(String::as_str)
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))?;
    Ok(Some(val))
}

fn parse_space_arg(raw_arg: &str) -> Result<RenderSpec, Failure> {
    let trimmed = raw_arg.trim_start();
    let text = if trimmed.starts_with('{') || trimmed.starts_with('[') {
        raw_arg.to_string()
    } else {
        std::fs::read_to_string(raw_arg)
            .map_err(|e| Failure::Usage(format!("cannot read space JSON {raw_arg}: {e}")))?
    };
    Ok(RenderSpec::from_json(&text)?)
}

fn run_parse(args: &[String]) -> Result<Json, Failure> {
    let path = common::option(args, "--cube")?;
    let cube = Cube::read(Path::new(path))?;
    Ok(cube.summary_json())
}

fn run_apply(args: &[String]) -> Result<Json, Failure> {
    let cube_path = common::option(args, "--cube")?;
    let space_arg = common::option(args, "--space")?;
    let rgb_path = common::option(args, "--rgb")?;
    let out_path = common::option(args, "--out")?;
    let as_u8 = has_flag(args, "--u8");
    let xyz_path = opt_maybe(args, "--xyz")?;

    let cube = Cube::read(Path::new(cube_path))?;
    let spec = parse_space_arg(space_arg)?;
    let target = Target::from_spec(&cube, &spec);

    let (shape, rgb_flat) = read_npy_f64(rgb_path)?;
    let rgb_triples = into_triples(&shape, &rgb_flat)?;
    let xyz_triples = match xyz_path {
        Some(p) => {
            let (x_shape, x_flat) = read_npy_f64(p)?;
            if x_shape != shape {
                return Err(Failure::Core(CoreError::new(
                    "value_error",
                    format!("xyz shape {x_shape:?} does not match rgb shape {shape:?}"),
                )));
            }
            Some(into_triples(&x_shape, &x_flat)?)
        }
        None => None,
    };

    if as_u8 {
        let pixels = apply_u8(&target, &rgb_triples, xyz_triples.as_deref())?;
        let total =
            checked_elements(&[u64::try_from(pixels.len()).map_err(|_| too_big_core())?, 3])?;
        let mut flat = try_vec_cap(total)?;
        for px in pixels {
            flat.extend_from_slice(&px);
        }
        write_npy_u8(out_path, &flat, &shape)
    } else {
        let (pixels, stats) = target.render(&rgb_triples, xyz_triples.as_deref())?;
        let total =
            checked_elements(&[u64::try_from(pixels.len()).map_err(|_| too_big_core())?, 3])?;
        let mut flat = try_vec_cap(total)?;
        for px in pixels {
            flat.extend_from_slice(&px);
        }
        let mut dims = Vec::with_capacity(shape.len());
        for &d in &shape {
            dims.push(i64::try_from(d).map_err(|_| Failure::Usage("shape too big".into()))?);
        }
        let _ = common::write_npy(out_path, &flat, &shape, "float64")?;
        Ok(Json::object([
            ("out", Json::str(out_path)),
            ("dtype", Json::str("float64")),
            ("shape", Json::ints(&dims)),
            (
                "stats",
                Json::object([
                    (
                        "input_clipped_share",
                        Json::Float(stats.input_clipped_share),
                    ),
                    (
                        "display_gamut_clipped_share",
                        Json::Float(stats.display_gamut_clipped_share),
                    ),
                ]),
            ),
        ]))
    }
}

fn run_encode(args: &[String]) -> Result<Json, Failure> {
    let space = common::option(args, "--space")?;
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let is_decode = has_flag(args, "--decode");

    let (shape, values) = read_npy_f64(in_path)?;
    let out_vals = if is_decode {
        decode(&values, space)?
    } else {
        encode(&values, space)?
    };
    common::write_npy(out_path, &out_vals, &shape, "float64")
}

fn run_table(args: &[String]) -> Result<Json, Failure> {
    let cube_path = common::option(args, "--cube")?;
    let space_arg = common::option(args, "--space")?;
    let out_path = common::option(args, "--out")?;
    let meta_path = common::option(args, "--meta")?;

    let cube = Cube::read(Path::new(cube_path))?;
    let spec = parse_space_arg(space_arg)?;
    let target = Target::from_spec(&cube, &spec);

    let res = lut_bytes(&target)?;
    let summary = res.summary_json();
    match &res {
        LutTableResult::Table { bytes, meta } => {
            std::fs::write(out_path, bytes)
                .map_err(|e| Failure::Usage(format!("cannot write {out_path}: {e}")))?;
            std::fs::write(meta_path, format!("{}\n", meta.to_json().render()))
                .map_err(|e| Failure::Usage(format!("cannot write {meta_path}: {e}")))?;
        }
        LutTableResult::Refused { reason } => {
            let doc = Json::object([("reason", Json::str(*reason))]);
            std::fs::write(meta_path, format!("{}\n", doc.render()))
                .map_err(|e| Failure::Usage(format!("cannot write {meta_path}: {e}")))?;
        }
    }
    Ok(summary)
}

fn run_identify(args: &[String]) -> Result<Json, Failure> {
    let cube_path = common::option(args, "--cube")?;
    let filename = common::option(args, "--name")?;
    let cube = Cube::read(Path::new(cube_path))?;
    Ok(identify(&cube, filename).to_json())
}

fn run_scene_xyz(args: &[String]) -> Result<Json, Failure> {
    let dng_path = common::option(args, "--dng")?;
    let codes_path = common::option(args, "--codes")?;
    let out_path = common::option(args, "--out")?;

    let data = common::read(dng_path)?;
    let dng = Dng::open(&data, &common::base_name(dng_path))?;
    let colour = scene_colour(&dng)?;

    let (shape, flat_codes) = read_npy_f64(codes_path)?;
    let codes = into_triples(&shape, &flat_codes)?;
    let xyz = scene_xyz(&colour, &codes)?;

    let total = checked_elements(&[u64::try_from(xyz.len()).map_err(|_| too_big_core())?, 3])?;
    let mut out_flat = try_vec_cap(total)?;
    for px in xyz {
        out_flat.extend_from_slice(&px);
    }
    let _ = common::write_npy(out_path, &out_flat, &shape, "float64")?;

    let mut dims = Vec::with_capacity(shape.len());
    for &d in &shape {
        dims.push(i64::try_from(d).map_err(|_| Failure::Usage("shape too big".into()))?);
    }
    let affine_json = gxr_lut::affine_of(&colour).map(|a| a.to_json());
    Ok(Json::object([
        ("out", Json::str(out_path)),
        ("dtype", Json::str("float64")),
        ("shape", Json::ints(&dims)),
        ("neutral", Json::floats(&colour.neutral)),
        (
            "matrix",
            Json::Array(colour.matrix.iter().map(|r| Json::floats(r)).collect()),
        ),
        ("affine", Json::opt(affine_json)),
    ]))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LibmOp {
    Pow,
    PowBase,
    Log10,
    Log2,
}

impl LibmOp {
    fn from_name(name: &str) -> Result<Self, Failure> {
        match name {
            "pow" => Ok(Self::Pow),
            "pow_base" => Ok(Self::PowBase),
            "log10" => Ok(Self::Log10),
            "log2" => Ok(Self::Log2),
            _ => Err(Failure::Usage(format!("unknown --op {name:?}"))),
        }
    }

    fn from_tag(tag: u8) -> Result<Self, Failure> {
        match tag {
            0 => Ok(Self::Pow),
            1 => Ok(Self::PowBase),
            2 => Ok(Self::Log10),
            3 => Ok(Self::Log2),
            _ => Err(Failure::Usage(format!("unknown libm stream op tag {tag}"))),
        }
    }
}

fn apply_libm_op(op: LibmOp, param: f64, values: &[f64]) -> Result<Vec<f64>, Failure> {
    let mut out = try_vec_cap(values.len())?;
    for &v in values {
        let y = match op {
            LibmOp::Pow => gxr_math::powf(v, param),
            LibmOp::PowBase => gxr_math::powf(param, v),
            LibmOp::Log10 => gxr_math::log10(v),
            LibmOp::Log2 => gxr_math::log2(v),
        };
        out.push(y);
    }
    Ok(out)
}

fn run_libm(args: &[String]) -> Result<Json, Failure> {
    if has_flag(args, "--stream") {
        return run_libm_stream();
    }
    let op_name = common::option(args, "--op")?;
    let op = LibmOp::from_name(op_name)?;
    let param = match opt_maybe(args, "--param")? {
        Some(raw) => raw
            .parse::<f64>()
            .map_err(|e| Failure::Usage(format!("invalid --param {raw:?}: {e}")))?,
        None => 0.0,
    };
    let in_path = common::option(args, "--in")?;
    let out_path = common::option(args, "--out")?;
    let (shape, values) = read_npy_f64(in_path)?;
    let out_vals = apply_libm_op(op, param, &values)?;
    common::write_npy(out_path, &out_vals, &shape, "float64")
}

fn run_libm_stream() -> Result<Json, Failure> {
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
            Err(e) => return Err(Failure::Usage(format!("stream read error: {e}"))),
        }
        reader
            .read_exact(&mut hdr[1..])
            .map_err(|e| Failure::Usage(format!("stream header read error: {e}")))?;
        let op = LibmOp::from_tag(hdr[0])?;
        let param = f64::from_le_bytes([
            hdr[1], hdr[2], hdr[3], hdr[4], hdr[5], hdr[6], hdr[7], hdr[8],
        ]);
        let count_u64 = u64::from_le_bytes([
            hdr[9], hdr[10], hdr[11], hdr[12], hdr[13], hdr[14], hdr[15], hdr[16],
        ]);
        let count = fit_count_u64(count_u64)?;
        let nbytes = fit_count_u64(mul_u64(count_u64, 8)?)?;
        let mut buf = try_vec_cap(nbytes)?;
        buf.resize(nbytes, 0);
        reader
            .read_exact(&mut buf)
            .map_err(|e| Failure::Usage(format!("stream payload read error: {e}")))?;
        let mut vals = try_vec_cap(count)?;
        for chunk in buf.chunks_exact(8) {
            vals.push(f64::from_le_bytes([
                chunk[0], chunk[1], chunk[2], chunk[3], chunk[4], chunk[5], chunk[6], chunk[7],
            ]));
        }
        let out_vals = apply_libm_op(op, param, &vals)?;
        for y in out_vals {
            writer
                .write_all(&y.to_le_bytes())
                .map_err(|e| Failure::Usage(format!("stream write error: {e}")))?;
        }
        writer
            .flush()
            .map_err(|e| Failure::Usage(format!("stream flush error: {e}")))?;
    }
    Ok(Json::object([("ok", Json::Bool(true))]))
}

fn into_triples(shape: &[usize], flat: &[f64]) -> Result<Vec<[f64; 3]>, Failure> {
    if shape.last() != Some(&3) || !flat.len().is_multiple_of(3) {
        return Err(Failure::Core(CoreError::new(
            "value_error",
            "LUT 输入必须为有限 RGB",
        )));
    }
    let mut out = try_vec_cap(flat.len() / 3)?;
    for chunk in flat.chunks_exact(3) {
        out.push([chunk[0], chunk[1], chunk[2]]);
    }
    Ok(out)
}

fn write_npy_u8(path: &str, values: &[u8], shape: &[usize]) -> Result<Json, Failure> {
    let dims_str: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims_str.len() == 1 {
        format!("({},)", dims_str[0])
    } else {
        format!("({})", dims_str.join(", "))
    };
    let mut dict = format!("{{'descr': '|u1', 'fortran_order': False, 'shape': {shape_text}, }}");
    let total = 10 + dict.len() + 1;
    dict.push_str(&" ".repeat((64 - total % 64) % 64));
    dict.push('\n');
    let len = u16::try_from(dict.len()).map_err(|_| Failure::Usage("npy header too big".into()))?;
    let mut file = std::fs::File::create(path)
        .map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))?;
    file.write_all(b"\x93NUMPY\x01\x00")
        .and_then(|()| file.write_all(&len.to_le_bytes()))
        .and_then(|()| file.write_all(dict.as_bytes()))
        .and_then(|()| file.write_all(values))
        .and_then(|()| file.flush())
        .map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))?;

    let mut dims = Vec::with_capacity(shape.len());
    for &dim in shape {
        dims.push(i64::try_from(dim).map_err(|_| Failure::Usage("shape too big".into()))?);
    }
    Ok(Json::object([
        ("out", Json::str(path)),
        ("dtype", Json::str("uint8")),
        ("shape", Json::ints(&dims)),
    ]))
}

fn read_npy_f64(path: &str) -> Result<(Vec<usize>, Vec<f64>), Failure> {
    let bytes =
        std::fs::read(path).map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")))?;
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(Failure::Usage(format!("{path} is not a .npy file")));
    }
    let (hdr_start, hdr_len) = match (bytes[6], bytes[7]) {
        (1, 0) => (
            10usize,
            usize::from(u16::from_le_bytes([bytes[8], bytes[9]])),
        ),
        (2, 0) if bytes.len() >= 12 => {
            let l = u32::from_le_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]) as usize;
            (12usize, l)
        }
        _ => return Err(Failure::Usage(format!("{path} unsupported .npy version"))),
    };
    let hdr_end = hdr_start
        .checked_add(hdr_len)
        .filter(|&e| e <= bytes.len())
        .ok_or_else(|| Failure::Usage(format!("{path} truncated .npy header")))?;
    let header = std::str::from_utf8(&bytes[hdr_start..hdr_end])
        .map_err(|_| Failure::Usage(format!("{path} invalid .npy header")))?;

    if header.contains("'fortran_order': True") {
        return Err(Failure::Usage(format!(
            "{path} Fortran-order .npy unsupported"
        )));
    }
    let descr = extract_quoted_field(header, "'descr'")
        .ok_or_else(|| Failure::Usage(format!("{path} missing descr")))?;
    let shape_u64 =
        extract_shape_u64(header).ok_or_else(|| Failure::Usage(format!("{path} invalid shape")))?;
    let count_u64 = checked_product_u64(&shape_u64)?;
    let count = fit_count_u64(count_u64)?;
    let mut shape = try_vec_cap(shape_u64.len())?;
    for &d in &shape_u64 {
        shape.push(fit_count_u64(d)?);
    }
    let payload = &bytes[hdr_end..];
    let values = match descr {
        "<f8" | "=f8" => {
            let nbytes = mul_u64(count_u64, 8)?;
            if payload.len() as u64 != nbytes {
                return Err(Failure::Usage(format!("{path} payload length mismatch")));
            }
            let mut out = try_vec_cap(count)?;
            for c in payload.chunks_exact(8) {
                out.push(f64::from_le_bytes([
                    c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7],
                ]));
            }
            out
        }
        "<f4" | "=f4" => {
            let nbytes = mul_u64(count_u64, 4)?;
            if payload.len() as u64 != nbytes {
                return Err(Failure::Usage(format!("{path} payload length mismatch")));
            }
            let mut out = try_vec_cap(count)?;
            for c in payload.chunks_exact(4) {
                out.push(f64::from(f32::from_le_bytes([c[0], c[1], c[2], c[3]])));
            }
            out
        }
        "<u2" | "=u2" => {
            let nbytes = mul_u64(count_u64, 2)?;
            if payload.len() as u64 != nbytes {
                return Err(Failure::Usage(format!("{path} payload length mismatch")));
            }
            let mut out = try_vec_cap(count)?;
            for c in payload.chunks_exact(2) {
                out.push(f64::from(u16::from_le_bytes([c[0], c[1]])));
            }
            out
        }
        "|u1" | "<u1" | "=u1" => {
            if payload.len() as u64 != count_u64 {
                return Err(Failure::Usage(format!("{path} payload length mismatch")));
            }
            let mut out = try_vec_cap(count)?;
            for &b in payload {
                out.push(f64::from(b));
            }
            out
        }
        other => {
            return Err(Failure::Usage(format!(
                "{path} unsupported npy dtype {other:?}"
            )));
        }
    };
    Ok((shape, values))
}

fn extract_quoted_field<'a>(header: &'a str, key: &str) -> Option<&'a str> {
    let at = header.find(key)?;
    let rest = &header[at + key.len()..];
    let q1 = rest.find('\'')?;
    let after = &rest[q1 + 1..];
    let q2 = after.find('\'')?;
    Some(&after[..q2])
}

fn extract_shape_u64(header: &str) -> Option<Vec<u64>> {
    let at = header.find("'shape'")?;
    let rest = &header[at + 7..];
    let open = rest.find('(')?;
    let inner = &rest[open + 1..];
    let close = inner.find(')')?;
    let content = inner[..close].trim();
    if content.is_empty() {
        return Some(Vec::new());
    }
    let mut dims = Vec::new();
    for part in content.split(',') {
        let p = part.trim();
        if p.is_empty() {
            continue;
        }
        dims.push(p.parse::<u64>().ok()?);
    }
    Some(dims)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn libm_ops_match_gxr_math_bits() {
        let x = [0.684_044_233_807_267_1, 0.18, 2.5];
        let got_pow = apply_libm_op(LibmOp::Pow, 2.4, &x).ok().expect("pow");
        let got_base = apply_libm_op(LibmOp::PowBase, 10.0, &x)
            .ok()
            .expect("pow_base");
        let got_l10 = apply_libm_op(LibmOp::Log10, 0.0, &x).ok().expect("log10");
        let got_l2 = apply_libm_op(LibmOp::Log2, 0.0, &x).ok().expect("log2");
        for (i, &v) in x.iter().enumerate() {
            assert_eq!(got_pow[i].to_bits(), gxr_math::powf(v, 2.4).to_bits());
            assert_eq!(got_base[i].to_bits(), gxr_math::powf(10.0, v).to_bits());
            assert_eq!(got_l10[i].to_bits(), gxr_math::log10(v).to_bits());
            assert_eq!(got_l2[i].to_bits(), gxr_math::log2(v).to_bits());
        }
        assert!(LibmOp::from_name("bad").is_err());
        assert!(LibmOp::from_tag(9).is_err());
    }
}
