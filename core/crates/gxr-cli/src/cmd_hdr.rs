//! `hdr-*` commands (wave-1 unit hdr). Arrays are `.npy` format 1.0, little-endian, C order.
//! A float plane may be `<f4` or `<f8` where Python accepts either; masks are `|b1` or `|u1`.
//! stdout never contains a path, so `gxr-wasm` can print the same bytes.
//!
//! ```text
//! hdr-filter --op gaussian|uniform|minimum|label|dilate --in A.npy --out B.npy
//!            [--sigma S] [--size N] [--iterations N]
//! hdr-map --in A.npy --sy Y.npy --sx X.npy --out B.npy       map_coordinates(order=1, nearest)
//! hdr-block-mean --in A.npy --factor F --out B.npy
//! hdr-luma --rgb RGB.npy --factor F --out Y.npy               sdr_luma_grid (uint8 (h, w, 3))
//! hdr-srgb --out L.npy                                       srgb_to_linear of codes 0..=255 (float32)
//! hdr-raw-luma DNG --out Y.npy --clip-out C.npy               raw_luma without the metadata
//! hdr-phase --a A.npy --b B.npy                               _phase_shift
//! hdr-fit --x X.npy --y Y.npy --valid V.npy                   fit_curve
//! hdr-align --sdr S.npy --raw R.npy                           align + check_alignment
//! hdr-register --raw R.npy --clip C.npy --align AL.json --shape H W --grid H W --out Y.npy --clip-out C2.npy
//! hdr-gain --sdr S.npy --raw R.npy --clip C.npy [--params P.json] --out G.npy [--delta-out D.npy]
//! hdr-segments --in F.jpg                                     jpeg_segments
//! hdr-assemble --primary A.jpg --gain B.jpg --meta M.json --out O.npy   file bytes as uint8 (1, N)
//! hdr-capture --raw TAGS.json --jpeg TAGS.json                _capture of both + differs
//! ```

use gxr_hdr as hdr;
use gxr_raw::{Dng, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "hdr-filter" => filter(args)?,
        "hdr-map" => map_cmd(args)?,
        "hdr-block-mean" => block_mean_cmd(args)?,
        "hdr-luma" => luma_cmd(args)?,
        "hdr-srgb" => {
            let lin: Vec<f32> = (0..=255u8).map(hdr::srgb_to_linear).collect();
            write(args, "--out", "<f4", &[256], &le_f32(&lin))?
        }
        "hdr-raw-luma" => raw_luma_cmd(args)?,
        "hdr-phase" => phase_cmd(args)?,
        "hdr-fit" => fit_cmd(args)?,
        "hdr-align" => hdr::align_report(
            &f32_plane(&npy(args, "--sdr")?)?,
            &f32_plane(&npy(args, "--raw")?)?,
        )?,
        "hdr-register" => register_cmd(args)?,
        "hdr-gain" => gain_cmd(args)?,
        "hdr-segments" => segments_cmd(args)?,
        "hdr-assemble" => assemble_cmd(args)?,
        "hdr-capture" => capture_json(&text(args, "--raw")?, &text(args, "--jpeg")?)?,
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

fn text(args: &[String], name: &str) -> Result<String, Failure> {
    let path = common::option(args, name)?;
    std::fs::read_to_string(path).map_err(|e| usage(format!("cannot read {path}: {e}")))
}

fn bytes(args: &[String], name: &str) -> Result<Vec<u8>, Failure> {
    let path = common::option(args, name)?;
    std::fs::read(path).map_err(|e| usage(format!("cannot read {path}: {e}")))
}

fn number<T: std::str::FromStr>(args: &[String], name: &str) -> Result<T, Failure> {
    let v = common::option(args, name)?;
    v.parse().map_err(|_| usage(format!("bad {name} {v:?}")))
}

fn pair(args: &[String], name: &str) -> Result<(usize, usize), Failure> {
    let at = args
        .iter()
        .position(|a| a == name)
        .ok_or_else(|| usage(format!("missing {name}")))?;
    let get = |k: usize| -> Result<usize, Failure> {
        args.get(at + k)
            .and_then(|v| v.parse().ok())
            .ok_or_else(|| usage(format!("{name} needs two sizes")))
    };
    Ok((get(1)?, get(2)?))
}

/// A parsed `.npy`: dtype descr, shape and the raw little-endian payload.
struct Npy {
    descr: String,
    shape: Vec<usize>,
    data: Vec<u8>,
}

fn npy(args: &[String], name: &str) -> Result<Npy, Failure> {
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
    let data = raw[at + len..].to_vec();
    let n = shape.iter().try_fold(1usize, |a, &d| a.checked_mul(d))?;
    let size = match descr.as_str() {
        "<f8" | "<i8" => 8,
        "<f4" | "<i4" => 4,
        "|u1" | "|b1" => 1,
        _ => return None,
    };
    (n.checked_mul(size)? == data.len()).then_some(Npy { descr, shape, data })
}

fn hw(a: &Npy) -> Result<(usize, usize), Failure> {
    match a.shape.as_slice() {
        [h, w] => Ok((*h, *w)),
        s => Err(usage(format!("expected a 2-d array, got shape {s:?}"))),
    }
}

fn f32s(a: &Npy) -> Result<Vec<f32>, Failure> {
    match a.descr.as_str() {
        "<f4" => Ok(a
            .data
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
            .collect()),
        d => Err(usage(format!("expected float32, got {d}"))),
    }
}

fn f64s(a: &Npy) -> Result<Vec<f64>, Failure> {
    match a.descr.as_str() {
        "<f8" => Ok(a
            .data
            .chunks_exact(8)
            .map(|c| f64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
            .collect()),
        d => Err(usage(format!("expected float64, got {d}"))),
    }
}

fn mask(a: &Npy) -> Result<Vec<bool>, Failure> {
    match a.descr.as_str() {
        "|b1" | "|u1" => Ok(a.data.iter().map(|&b| b != 0).collect()),
        d => Err(usage(format!("expected a bool mask, got {d}"))),
    }
}

fn f32_plane(a: &Npy) -> Result<hdr::Plane32, Failure> {
    let (h, w) = hw(a)?;
    Ok(hdr::Plane32 { h, w, v: f32s(a)? })
}

/// Write an `.npy` (format 1.0) and describe it without the path.
fn write(
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
    let dims: Vec<i64> = shape
        .iter()
        .map(|&d| i64::try_from(d).unwrap_or(i64::MAX))
        .collect();
    Ok(Json::object([
        ("dtype", Json::str(descr)),
        ("shape", Json::ints(&dims)),
    ]))
}

fn le_f32(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn le_f64(v: &[f64]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn bools(v: &[bool]) -> Vec<u8> {
    v.iter().map(|&b| u8::from(b)).collect()
}

fn filter(args: &[String]) -> Result<Json, Failure> {
    let op = common::option(args, "--op")?;
    let a = npy(args, "--in")?;
    let (h, w) = hw(&a)?;
    let out = |descr: &str, payload: Vec<u8>| write(args, "--out", descr, &[h, w], &payload);
    match (op, a.descr.as_str()) {
        ("gaussian", "<f4") => out(
            "<f4",
            le_f32(&hdr::gaussian_filter(
                &f32s(&a)?,
                h,
                w,
                number(args, "--sigma")?,
            )?),
        ),
        ("gaussian", "<f8") => out(
            "<f8",
            le_f64(&hdr::gaussian_filter(
                &f64s(&a)?,
                h,
                w,
                number(args, "--sigma")?,
            )?),
        ),
        ("uniform", "<f4") => out(
            "<f4",
            le_f32(&hdr::uniform_filter(
                &f32s(&a)?,
                h,
                w,
                number(args, "--size")?,
            )?),
        ),
        ("uniform", "<f8") => out(
            "<f8",
            le_f64(&hdr::uniform_filter(
                &f64s(&a)?,
                h,
                w,
                number(args, "--size")?,
            )?),
        ),
        ("minimum", "<f4") => out(
            "<f4",
            le_f32(&hdr::minimum_filter(
                &f32s(&a)?,
                h,
                w,
                number(args, "--size")?,
            )?),
        ),
        ("minimum", "<f8") => out(
            "<f8",
            le_f64(&hdr::minimum_filter(
                &f64s(&a)?,
                h,
                w,
                number(args, "--size")?,
            )?),
        ),
        ("minimum", "|u1" | "|b1") => {
            let d = a.descr.clone();
            out(
                &d,
                hdr::minimum_filter(&a.data, h, w, number(args, "--size")?)?,
            )
        }
        ("label", "|b1" | "|u1") => {
            let (labels, count) = hdr::label(&mask(&a)?, h, w)?;
            let desc = out("<i4", labels.iter().flat_map(|x| x.to_le_bytes()).collect())?;
            Ok(Json::object([
                ("count", Json::Int(i64::from(count))),
                ("out", desc),
            ]))
        }
        ("dilate", "|b1" | "|u1") => out(
            "|b1",
            bools(&hdr::binary_dilation(
                &mask(&a)?,
                h,
                w,
                number(args, "--iterations")?,
            )?),
        ),
        (op, d) => Err(usage(format!("--op {op} does not take {d}"))),
    }
}

fn map_cmd(args: &[String]) -> Result<Json, Failure> {
    let a = npy(args, "--in")?;
    let (h, w) = hw(&a)?;
    let (sy, sx) = (npy(args, "--sy")?, npy(args, "--sx")?);
    let shape = sy.shape.clone();
    let (y, x) = (f32s(&sy)?, f32s(&sx)?);
    match a.descr.as_str() {
        "<f4" => write(
            args,
            "--out",
            "<f4",
            &shape,
            &le_f32(&hdr::map_coordinates(&f32s(&a)?, h, w, &y, &x)?),
        ),
        _ => write(
            args,
            "--out",
            "<f8",
            &shape,
            &le_f64(&hdr::map_coordinates(&f64s(&a)?, h, w, &y, &x)?),
        ),
    }
}

fn block_mean_cmd(args: &[String]) -> Result<Json, Failure> {
    let a = npy(args, "--in")?;
    let (h, w) = hw(&a)?;
    let factor: usize = number(args, "--factor")?;
    if a.descr == "<f4" {
        let (v, gh, gw) = hdr::block_mean(&f32s(&a)?, h, w, factor)?;
        write(args, "--out", "<f4", &[gh, gw], &le_f32(&v))
    } else {
        let (v, gh, gw) = hdr::block_mean(&f64s(&a)?, h, w, factor)?;
        write(args, "--out", "<f8", &[gh, gw], &le_f64(&v))
    }
}

fn luma_cmd(args: &[String]) -> Result<Json, Failure> {
    let rgb = npy(args, "--rgb")?;
    let [h, w, 3] = rgb.shape.as_slice() else {
        return Err(usage("--rgb must be (h, w, 3)"));
    };
    if rgb.descr != "|u1" {
        return Err(usage("--rgb must be uint8"));
    }
    let grid = hdr::sdr_luma_grid(&rgb.data, *h, *w, number(args, "--factor")?)?;
    write(args, "--out", "<f4", &[grid.h, grid.w], &le_f32(&grid.v))
}

fn raw_luma_cmd(args: &[String]) -> Result<Json, Failure> {
    let path = args.first().ok_or_else(|| usage("missing <path>"))?;
    let data = common::read(path)?;
    let mut dng = Dng::open(&data, &common::base_name(path))?;
    let (y, clip) = hdr::raw_luma(&mut dng)?;
    let ys = write(args, "--out", "<f4", &[y.h, y.w], &le_f32(&y.v))?;
    let cs = write(args, "--clip-out", "|b1", &[y.h, y.w], &bools(&clip))?;
    Ok(Json::object([("y", ys), ("clip", cs)]))
}

fn phase_cmd(args: &[String]) -> Result<Json, Failure> {
    let (a, b) = (npy(args, "--a")?, npy(args, "--b")?);
    let (h, w) = hw(&a)?;
    if b.shape != a.shape {
        return Err(usage("--a and --b differ in shape"));
    }
    let (dy, dx) = hdr::phase_shift(&f32s(&a)?, &f32s(&b)?, h, w)?;
    Ok(Json::object([("dy", Json::Int(dy)), ("dx", Json::Int(dx))]))
}

fn fit_cmd(args: &[String]) -> Result<Json, Failure> {
    let x = f32s(&npy(args, "--x")?)?;
    let y = f32s(&npy(args, "--y")?)?;
    let valid = mask(&npy(args, "--valid")?)?;
    if x.len() != y.len() || x.len() != valid.len() {
        return Err(usage("--x, --y and --valid differ in size"));
    }
    let (cx, cy) = hdr::fit_curve(&x, &y, &valid)?;
    Ok(Json::object([
        ("cx", Json::floats(&cx)),
        ("cy", Json::floats(&cy)),
    ]))
}

fn register_cmd(args: &[String]) -> Result<Json, Failure> {
    let raw = f32_plane(&npy(args, "--raw")?)?;
    let clip = mask(&npy(args, "--clip")?)?;
    let al_text = text(args, "--align")?;
    let fields = match hdr::parse_json(&al_text)? {
        hdr::Value::Obj(pairs) => pairs,
        _ => return Err(usage("--align must be a JSON object")),
    };
    let get = |k: &str| {
        fields
            .iter()
            .find(|(name, _)| name == k)
            .map(|(_, v)| v.clone())
    };
    let num = |k: &str| match get(k) {
        Some(hdr::Value::Float(f)) => Ok(f),
        #[allow(clippy::cast_precision_loss)]
        Some(hdr::Value::Int(n)) => Ok(n as f64),
        _ => Err(usage(format!("--align needs {k}"))),
    };
    let int = |k: &str| match get(k) {
        Some(hdr::Value::Int(n)) => Ok(n),
        _ => Err(usage(format!("--align needs integer {k}"))),
    };
    let al = hdr::Alignment {
        scale: num("scale")?,
        dy: int("dy")?,
        dx: int("dx")?,
        correlation: 0.0,
        tiles_moved: 0,
        shift_frac: 0.0,
        tiles_unverified: 0,
    };
    let (shape, grid) = (pair(args, "--shape")?, pair(args, "--grid")?);
    let (y, c) = hdr::register(&raw, &clip, shape, &al, grid)?;
    let ys = write(args, "--out", "<f4", &[shape.0, shape.1], &le_f32(&y))?;
    let cs = write(args, "--clip-out", "|b1", &[shape.0, shape.1], &bools(&c))?;
    Ok(Json::object([("y", ys), ("clip", cs)]))
}

fn gain_cmd(args: &[String]) -> Result<Json, Failure> {
    let sdr = f32_plane(&npy(args, "--sdr")?)?;
    let raw = f32_plane(&npy(args, "--raw")?)?;
    let clip = mask(&npy(args, "--clip")?)?;
    if (raw.h, raw.w) != (sdr.h, sdr.w) {
        return Err(usage("--sdr and --raw differ in shape"));
    }
    let params = match common::option(args, "--params") {
        Ok(_) => hdr::params_from_json(&text(args, "--params")?)?,
        Err(_) => hdr::Params::default(),
    };
    let result = hdr::compute_gain(&sdr.v, &raw.v, &clip, sdr.h, sdr.w, &params)?;
    let codes = hdr::quantise_gain(&result.delta, &result.meta);
    write(args, "--out", "|u1", &[sdr.h, sdr.w], &codes)?;
    if common::option(args, "--delta-out").is_ok() {
        write(
            args,
            "--delta-out",
            "<f4",
            &[sdr.h, sdr.w],
            &le_f32(&result.delta),
        )?;
    }
    Ok(hdr::gain_report(&result)?)
}

fn segments_cmd(args: &[String]) -> Result<Json, Failure> {
    let data = bytes(args, "--in")?;
    let (segs, sos, eoi) = hdr::jpeg_segments(&data, 0)?;
    let to_i = |v: usize| i64::try_from(v).unwrap_or(i64::MAX);
    let list: Vec<Json> = segs
        .iter()
        .map(|s| Json::ints(&[i64::from(s.marker), to_i(s.start), to_i(s.end)]))
        .collect();
    Ok(Json::object([
        ("segments", Json::Array(list)),
        ("sos", Json::Int(to_i(sos))),
        ("eoi", Json::Int(to_i(eoi))),
    ]))
}

fn assemble_cmd(args: &[String]) -> Result<Json, Failure> {
    let primary = bytes(args, "--primary")?;
    let gain = bytes(args, "--gain")?;
    let meta = hdr::meta_from_json(&text(args, "--meta")?)?;
    let out = hdr::assemble(&primary, &gain, &meta)?;
    write(args, "--out", "|u1", &[1, out.len()], &out)
}

/// `_capture` of both tag maps and `differs`.
pub fn capture_json(raw: &str, jpeg: &str) -> Result<Json, Failure> {
    let side = |t: &str| -> Result<Vec<(String, hdr::Field)>, Failure> {
        hdr::capture_of(&hdr::tags_from_json(t)?)
            .ok_or_else(|| usage("a text tag holds a list; str(list) is not ported"))
    };
    let (r, j) = (side(raw)?, side(jpeg)?);
    Ok(Json::object([
        ("raw", hdr::field_json(&r)),
        ("jpeg", hdr::field_json(&j)),
        ("differs", Json::Bool(hdr::differs(&r, &j))),
    ]))
}
