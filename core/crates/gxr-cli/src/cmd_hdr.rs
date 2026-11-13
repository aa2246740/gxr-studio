//! `hdr-*` commands. Owner: wave-1 unit hdr.
//!
//! ```text
//! hdr-filter --op gaussian|uniform|minimum|label|dilate --in a.npy --out b.npy
//!            [--sigma 1] [--size 3] [--iterations 2]
//! hdr-align --sdr s.npy --sdr-shape H W --raw r.npy --raw-shape H W
//! hdr-gain --sdr s.npy --raw r.npy --clip c.npy --shape H W [--params p.json] --out g.npy
//! hdr-assemble --primary a.jpg --gain b.jpg --meta m.json --out o.npy
//! hdr-capture --raw tags.json --jpeg tags.json
//! ```
//!
//! `.npy` is format 1.0, little-endian, C order. `hdr-assemble` writes the file
//! bytes as a uint8 array of shape `(1, N)` so the WASM compare can see them.
//! `hdr-filter` writes shape `(H, W)`.

use std::collections::BTreeMap;

use gxr_hdr::{
    Field, GainMeta, Params, align, app_segment, assemble, binary_dilation, block_mean, capture_of, check_alignment,
    compute_gain, differs, field_json, fit_curve, gaussian_filter, iso_full, label_u8, minimum_filter, quantise_gain,
    uniform_filter, Plane,
};
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "hdr-filter" => filter(args),
        "hdr-align" => align_cmd(args),
        "hdr-gain" => gain_cmd(args),
        "hdr-assemble" => assemble_cmd(args),
        "hdr-capture" => capture_cmd(args),
        "hdr-round" => round_cmd(args),
        "hdr-frac" => frac_cmd(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn opt<'a>(args: &'a [String], name: &str) -> Result<&'a str, Failure> {
    common::option(args, name)
}

fn opt_or<'a>(args: &'a [String], name: &str, default: &'a str) -> &'a str {
    common::option(args, name).unwrap_or(default)
}

fn shape2(args: &[String], name: &str) -> Result<(usize, usize), Failure> {
    let text = opt(args, name)?;
    let mut parts = text.split_whitespace();
    let h: usize = parts
        .next()
        .ok_or_else(|| Failure::Usage(format!("bad {name}")))?
        .parse()
        .map_err(|_| Failure::Usage(format!("bad {name}")))?;
    let w: usize = parts
        .next()
        .ok_or_else(|| Failure::Usage(format!("bad {name}")))?
        .parse()
        .map_err(|_| Failure::Usage(format!("bad {name}")))?;
    if parts.next().is_some() {
        return Err(Failure::Usage(format!("bad {name}")));
    }
    Ok((h, w))
}

struct Npy {
    descr: String,
    shape: Vec<usize>,
    bytes: Vec<u8>,
}

fn read_npy(path: &str) -> Result<Npy, Failure> {
    let bytes = std::fs::read(path).map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")))?;
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(Failure::Usage(format!("{path} is not an npy file")));
    }
    let (header_at, header_len) = if bytes[6] == 1 {
        (10usize, usize::from(u16::from_le_bytes([bytes[8], bytes[9]])))
    } else if bytes[6] == 2 {
        if bytes.len() < 12 {
            return Err(Failure::Usage(format!("{path} header is short")));
        }
        (12, u32::from_le_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]) as usize)
    } else {
        return Err(Failure::Usage(format!("{path} has an unknown npy version")));
    };
    let header_end = header_at + header_len;
    if header_end > bytes.len() {
        return Err(Failure::Usage(format!("{path} header is short")));
    }
    let header = std::str::from_utf8(&bytes[header_at..header_end])
        .map_err(|_| Failure::Usage(format!("{path} header is not text")))?;
    let descr = between(header, "'descr': '", "'").ok_or_else(|| Failure::Usage(format!("{path} has no descr")))?;
    let shape_text = between(header, "'shape': (", ")").ok_or_else(|| Failure::Usage(format!("{path} has no shape")))?;
    let shape: Vec<usize> = shape_text
        .split(',')
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(|p| p.parse::<usize>())
        .collect::<Result<_, _>>()
        .map_err(|_| Failure::Usage(format!("{path} has a bad shape")))?;
    Ok(Npy {
        descr: descr.to_string(),
        shape,
        bytes: bytes[header_end..].to_vec(),
    })
}

fn between<'a>(text: &'a str, start: &str, end: &str) -> Option<&'a str> {
    let at = text.find(start)? + start.len();
    let stop = text[at..].find(end)? + at;
    Some(&text[at..stop])
}

fn f64s(npy: &Npy) -> Result<Vec<f64>, Failure> {
    match npy.descr.as_str() {
        "<f8" | "|f8" => {
            if npy.bytes.len() % 8 != 0 {
                return Err(Failure::Usage("f8 payload is short".into()));
            }
            Ok(npy.bytes.chunks_exact(8).map(|c| f64::from_le_bytes(c.try_into().unwrap_or([0; 8]))).collect())
        }
        "<f4" => {
            if npy.bytes.len() % 4 != 0 {
                return Err(Failure::Usage("f4 payload is short".into()));
            }
            Ok(npy
                .bytes
                .chunks_exact(4)
                .map(|c| f64::from(f32::from_le_bytes(c.try_into().unwrap_or([0; 4]))))
                .collect())
        }
        _ => Err(Failure::Usage(format!("unsupported descr {}", npy.descr))),
    }
}

fn f32s(npy: &Npy) -> Result<Vec<f32>, Failure> {
    match npy.descr.as_str() {
        "<f4" => Ok(npy
            .bytes
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes(c.try_into().unwrap_or([0; 4])))
            .collect()),
        "<f8" => Ok(f64s(npy)?.into_iter().map(|v| v as f32).collect()),
        _ => Err(Failure::Usage(format!("unsupported descr {}", npy.descr))),
    }
}

fn u8s(npy: &Npy) -> Result<Vec<u8>, Failure> {
    match npy.descr.as_str() {
        "|u1" | "<u1" | "|b1" | "<b1" => Ok(npy.bytes.clone()),
        _ => Err(Failure::Usage(format!("unsupported descr {}", npy.descr))),
    }
}

fn hw(npy: &Npy) -> Result<(usize, usize), Failure> {
    if npy.shape.len() != 2 {
        return Err(Failure::Usage(format!("expected a 2-d array, got {:?}", npy.shape)));
    }
    Ok((npy.shape[0], npy.shape[1]))
}

fn write_raw(path: &str, descr: &str, shape: &[usize], payload: &[u8]) -> Result<Json, Failure> {
    let dims: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        format!("({})", dims.join(", "))
    };
    let mut dict = format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape_text}, }}");
    let total = 10 + dict.len() + 1;
    dict.push_str(&" ".repeat((64 - total % 64) % 64));
    dict.push('\n');
    let mut out = b"\x93NUMPY\x01\x00".to_vec();
    let len = u16::try_from(dict.len()).map_err(|_| Failure::Usage("npy header is too long".into()))?;
    out.extend_from_slice(&len.to_le_bytes());
    out.extend_from_slice(dict.as_bytes());
    out.extend_from_slice(payload);
    std::fs::write(path, &out).map_err(|e| Failure::Usage(format!("cannot write {path}: {e}")))?;
    let dims_i: Vec<i64> = shape.iter().map(|d| *d as i64).collect();
    Ok(Json::object([
        ("out", Json::str(path)),
        ("dtype", Json::str(descr)),
        ("shape", Json::ints(&dims_i)),
    ]))
}

fn write_f64(path: &str, values: &[f64], h: usize, w: usize) -> Result<Json, Failure> {
    let mut payload = Vec::with_capacity(values.len() * 8);
    for v in values {
        payload.extend_from_slice(&v.to_le_bytes());
    }
    write_raw(path, "<f8", &[h, w], &payload)
}

fn write_u8(path: &str, values: &[u8], h: usize, w: usize) -> Result<Json, Failure> {
    write_raw(path, "|u1", &[h, w], values)
}

fn write_i32(path: &str, values: &[i32], h: usize, w: usize) -> Result<Json, Failure> {
    let mut payload = Vec::with_capacity(values.len() * 4);
    for v in values {
        payload.extend_from_slice(&v.to_le_bytes());
    }
    write_raw(path, "<i4", &[h, w], &payload)
}

fn core(err: CoreError) -> Failure {
    Failure::Core(err)
}

fn filter(args: &[String]) -> Result<Output, Failure> {
    let op = opt(args, "--op")?;
    let input = read_npy(opt(args, "--in")?)?;
    let (h, w) = hw(&input)?;
    let out = opt(args, "--out")?;
    let json = match op {
        "gaussian" => {
            let sigma: f64 = opt(args, "--sigma")?.parse().map_err(|_| Failure::Usage("bad --sigma".into()))?;
            let plane = if input.descr == "<f4" {
                Plane::F32(f32s(&input)?)
            } else {
                Plane::F64(f64s(&input)?)
            };
            match gaussian_filter(&plane, h, w, sigma).map_err(core)? {
                Plane::F64(v) => write_f64(out, &v, h, w)?,
                Plane::F32(v) => {
                    let mut payload = Vec::new();
                    for x in v {
                        payload.extend_from_slice(&x.to_le_bytes());
                    }
                    write_raw(out, "<f4", &[h, w], &payload)?
                }
                Plane::U8(_) => return Err(Failure::Usage("gaussian returned bytes".into())),
            }
        }
        "uniform" => {
            let size: i64 = opt(args, "--size")?.parse().map_err(|_| Failure::Usage("bad --size".into()))?;
            let plane = Plane::F64(f64s(&input)?);
            match uniform_filter(&plane, h, w, size).map_err(core)? {
                Plane::F64(v) => write_f64(out, &v, h, w)?,
                _ => return Err(Failure::Usage("uniform dtype".into())),
            }
        }
        "minimum" => {
            let size: i64 = opt(args, "--size")?.parse().map_err(|_| Failure::Usage("bad --size".into()))?;
            if input.descr.contains("u1") || input.descr.contains("b1") {
                match minimum_filter(&Plane::U8(u8s(&input)?), h, w, size).map_err(core)? {
                    Plane::U8(v) => write_u8(out, &v, h, w)?,
                    _ => return Err(Failure::Usage("minimum dtype".into())),
                }
            } else {
                match minimum_filter(&Plane::F64(f64s(&input)?), h, w, size).map_err(core)? {
                    Plane::F64(v) => write_f64(out, &v, h, w)?,
                    _ => return Err(Failure::Usage("minimum dtype".into())),
                }
            }
        }
        "label" => {
            let (labels, count) = label_u8(&u8s(&input)?, h, w).map_err(core)?;
            let json = write_i32(out, &labels, h, w)?;
            let Json::Object(mut pairs) = json else {
                return Err(Failure::Usage("label json".into()));
            };
            pairs.push(("count".into(), Json::Int(i64::from(count))));
            Json::Object(pairs)
        }
        "dilate" => {
            let iterations: i64 = opt_or(args, "--iterations", "2").parse().map_err(|_| Failure::Usage("bad --iterations".into()))?;
            let values = binary_dilation(&u8s(&input)?, h, w, iterations).map_err(core)?;
            write_u8(out, &values, h, w)?
        }
        "block-mean" => {
            let factor: usize = opt(args, "--size")?.parse().map_err(|_| Failure::Usage("bad --size".into()))?;
            let (values, gh, gw) = block_mean(&f64s(&input)?, h, w, factor).map_err(core)?;
            write_f64(out, &values, gh, gw)?
        }
        "map" => {
            let sy = read_npy(opt(args, "--sy")?)?;
            let sx = read_npy(opt(args, "--sx")?)?;
            let (oh, ow) = hw(&sy)?;
            let syv = f32s(&sy)?;
            let sxv = f32s(&sx)?;
            let plane = if input.descr == "<f4" {
                Plane::F32(f32s(&input)?)
            } else {
                Plane::F64(f64s(&input)?)
            };
            match gxr_hdr::map_coordinates(&plane, h, w, &syv, &sxv, oh, ow).map_err(core)? {
                Plane::F64(v) => write_f64(out, &v, oh, ow)?,
                Plane::F32(v) => {
                    let mut payload = Vec::new();
                    for x in v {
                        payload.extend_from_slice(&x.to_le_bytes());
                    }
                    write_raw(out, "<f4", &[oh, ow], &payload)?
                }
                Plane::U8(_) => return Err(Failure::Usage("map dtype".into())),
            }
        }
        _ => return Err(Failure::Usage(format!("unknown filter {op}"))),
    };
    Ok(Output { text: json.render(), ok: true })
}

fn align_cmd(args: &[String]) -> Result<Output, Failure> {
    let sdr = read_npy(opt(args, "--sdr")?)?;
    let raw = read_npy(opt(args, "--raw")?)?;
    let (h, w) = hw(&sdr)?;
    let (rh, rw) = hw(&raw)?;
    let al = align(&f64s(&sdr)?, h, w, &f32s(&raw)?, rh, rw).map_err(core)?;
    let mut json = al.to_json();
    if args.iter().any(|a| a == "--check") {
        if let Err(err) = check_alignment(&al) {
            return Err(core(err));
        }
    }
    let Json::Object(pairs) = &mut json else {
        return Err(Failure::Usage("align json".into()));
    };
    let _ = pairs;
    Ok(Output { text: json.render(), ok: true })
}

fn params_of(args: &[String]) -> Result<Params, Failure> {
    let mut p = Params::default();
    if let Ok(path) = opt(args, "--params") {
        let text = std::fs::read_to_string(path).map_err(|e| Failure::Usage(format!("cannot read {path}: {e}")))?;
        let map = flat_numbers(&text)?;
        if let Some(v) = map.get("map_factor") {
            p.map_factor = *v as i64;
        }
        if let Some(v) = map.get("max_stops") {
            p.max_stops = *v;
        }
        if let Some(v) = map.get("knee") {
            p.knee = *v;
        }
        if let Some(v) = map.get("hdr_slope") {
            p.hdr_slope = *v;
        }
        if let Some(v) = map.get("gate_lo") {
            p.gate_lo = *v;
        }
        if let Some(v) = map.get("gate_hi") {
            p.gate_hi = *v;
        }
        if let Some(v) = map.get("quality") {
            p.quality = *v as i64;
        }
    }
    Ok(p)
}

fn gain_cmd(args: &[String]) -> Result<Output, Failure> {
    let sdr = read_npy(opt(args, "--sdr")?)?;
    let raw = read_npy(opt(args, "--raw")?)?;
    let clip = read_npy(opt(args, "--clip")?)?;
    let (h, w) = hw(&sdr)?;
    let p = params_of(args)?;
    let plane = compute_gain(&f64s(&sdr)?, &f32s(&raw)?, &u8s(&clip)?, h, w, &p).map_err(core)?;
    let codes = quantise_gain(&plane.delta, 0.0, plane.gmax, 1.0).map_err(core)?;
    let out = write_u8(opt(args, "--out")?, &codes, h, w)?;
    let Json::Object(mut pairs) = out else {
        return Err(Failure::Usage("gain json".into()));
    };
    pairs.push(("gmax".into(), Json::Float(plane.gmax)));
    pairs.push(("gamma".into(), Json::Float(1.0)));
    pairs.push(("offset_sdr".into(), Json::Float(1.0 / 64.0)));
    pairs.push(("offset_hdr".into(), Json::Float(1.0 / 64.0)));
    pairs.push(("stats".into(), plane.stats));
    Ok(Output {
        text: Json::Object(pairs).render(),
        ok: true,
    })
}

fn meta_of(text: &str) -> Result<GainMeta, Failure> {
    let map = flat_numbers(text)?;
    let mut meta = GainMeta::default();
    if let Some(v) = map.get("gain_min") {
        meta.gain_min = *v;
    }
    if let Some(v) = map.get("gain_max") {
        meta.gain_max = *v;
    }
    if let Some(v) = map.get("gamma") {
        meta.gamma = *v;
    }
    if let Some(v) = map.get("offset_sdr") {
        meta.offset_sdr = *v;
    }
    if let Some(v) = map.get("offset_hdr") {
        meta.offset_hdr = *v;
    }
    if let Some(v) = map.get("cap_min") {
        meta.cap_min = *v;
    }
    if let Some(v) = map.get("cap_max") {
        meta.cap_max = *v;
    }
    Ok(meta)
}

fn assemble_cmd(args: &[String]) -> Result<Output, Failure> {
    let primary = std::fs::read(opt(args, "--primary")?).map_err(|e| Failure::Usage(e.to_string()))?;
    let gain = std::fs::read(opt(args, "--gain")?).map_err(|e| Failure::Usage(e.to_string()))?;
    let meta_text = std::fs::read_to_string(opt(args, "--meta")?).map_err(|e| Failure::Usage(e.to_string()))?;
    let bytes = assemble(&primary, &gain, &meta_of(&meta_text)?).map_err(core)?;
    let json = write_u8(opt(args, "--out")?, &bytes, 1, bytes.len())?;
    Ok(Output { text: json.render(), ok: true })
}

fn capture_cmd(args: &[String]) -> Result<Output, Failure> {
    let raw_tags = fields_of(&std::fs::read_to_string(opt(args, "--raw")?).map_err(|e| Failure::Usage(e.to_string()))?)?;
    let jpeg_tags = fields_of(&std::fs::read_to_string(opt(args, "--jpeg")?).map_err(|e| Failure::Usage(e.to_string()))?)?;
    let raw = capture_of(&raw_tags);
    let jpeg = capture_of(&jpeg_tags);
    let json = Json::object([
        ("raw", field_json(&raw)),
        ("jpeg", field_json(&jpeg)),
        ("differs", Json::Bool(differs(&raw, &jpeg))),
    ]);
    Ok(Output { text: json.render(), ok: true })
}

fn round_cmd(args: &[String]) -> Result<Output, Failure> {
    let x: f64 = opt(args, "--x")?.parse().map_err(|_| Failure::Usage("bad --x".into()))?;
    let n: i32 = opt(args, "--n")?.parse().map_err(|_| Failure::Usage("bad --n".into()))?;
    let y = gxr_hdr::py_round_pub(x, n).map_err(core)?;
    Ok(Output {
        text: Json::object([("value", Json::Float(y))]).render(),
        ok: true,
    })
}

fn frac_cmd(args: &[String]) -> Result<Output, Failure> {
    let x: f64 = opt(args, "--x")?.parse().map_err(|_| Failure::Usage("bad --x".into()))?;
    let (n, d) = gxr_hdr::frac_pub(x).map_err(core)?;
    Ok(Output {
        text: Json::object([("numer", Json::Int(n as i64)), ("denom", Json::Int(d as i64))]).render(),
        ok: true,
    })
}

fn flat_numbers(text: &str) -> Result<BTreeMap<String, f64>, Failure> {
    let mut map = BTreeMap::new();
    let body = text.trim().trim_start_matches('{').trim_end_matches('}');
    if body.trim().is_empty() {
        return Ok(map);
    }
    for part in body.split(',') {
        let Some((k, v)) = part.split_once(':') else {
            return Err(Failure::Usage(format!("bad json {part}")));
        };
        let key = k.trim().trim_matches('"').to_string();
        let value: f64 = v.trim().parse().map_err(|_| Failure::Usage(format!("bad number {v}")))?;
        map.insert(key, value);
    }
    Ok(map)
}

fn fields_of(text: &str) -> Result<BTreeMap<i64, Field>, Failure> {
    let mut map = BTreeMap::new();
    let body = text.trim().trim_start_matches('{').trim_end_matches('}');
    if body.trim().is_empty() {
        return Ok(map);
    }
    for part in body.split(',') {
        let Some((k, v)) = part.split_once(':') else {
            return Err(Failure::Usage(format!("bad json {part}")));
        };
        let key: i64 = k.trim().trim_matches('"').parse().map_err(|_| Failure::Usage(format!("bad tag {k}")))?;
        let value = v.trim();
        if value.starts_with('"') {
            map.insert(key, Field::Text(value.trim_matches('"').to_string()));
        } else {
            let number: f64 = value.parse().map_err(|_| Failure::Usage(format!("bad field {value}")))?;
            map.insert(key, Field::Number(number));
        }
    }
    Ok(map)
}

// Silence unused imports if a helper is only used on one path.
#[allow(dead_code)]
fn _keep(meta: &GainMeta) -> Result<Vec<u8>, CoreError> {
    let _ = (app_segment, iso_full, fit_curve);
    iso_full(meta)
}
