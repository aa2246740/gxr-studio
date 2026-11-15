//! `hdrtrack-*` commands (wave-2 unit hdr-track). Arrays are `.npy` format 1.0, little-endian, C order.
//! stdout never contains a path, so `gxr-wasm` prints identical JSON and bytes.

use gxr_export::{
    HdrTrackState, begin, box_track, checked_box_dim_f64, checked_box_offset_f64, checked_dim_f64,
    checked_factor_f64, checked_nonneg_u64_f64, crop_track, finish, frame_track, parse_params_opt,
    parse_rect4_json, stamp_track, to_grid, warp_track,
};
use gxr_finish::geometry::{crop_valid_f32_u64, parse_geometry, warp_float_u64};
use gxr_hdr::{count, ensure_finite, f32_le_bytes};
use gxr_raw::Json;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    if command == "hdrtrack-begin-batch" {
        return Ok(Output {
            text: begin_batch_cmd(args)?,
            ok: true,
        });
    }
    let json = match command {
        "hdrtrack-warp-float" => warp_float_cmd(args)?,
        "hdrtrack-crop-valid" => crop_valid_cmd(args)?,
        "hdrtrack-begin" => begin_cmd(args)?,
        "hdrtrack-warp" => warp_cmd(args)?,
        "hdrtrack-box" => box_cmd(args)?,
        "hdrtrack-crop" => crop_cmd(args)?,
        "hdrtrack-stamp" => stamp_cmd(args)?,
        "hdrtrack-frame" => frame_cmd(args)?,
        "hdrtrack-to-grid" => to_grid_cmd(args)?,
        "hdrtrack-finish" => finish_cmd(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    ensure_finite(&json)?;
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn usage(message: impl Into<String>) -> Failure {
    Failure::Usage(message.into())
}

fn has_flag(args: &[String], name: &str) -> bool {
    args.iter().any(|a| a == name)
}

fn opt_str<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    if has_flag(args, name) {
        Ok(Some(common::option(args, name)?))
    } else {
        Ok(None)
    }
}

fn read_text_flag(
    args: &[String],
    file_flag: &str,
    inline_flag: &str,
    default: Option<&str>,
) -> Result<String, Failure> {
    if let Some(path) = opt_str(args, file_flag)? {
        return std::fs::read_to_string(path)
            .map_err(|e| usage(format!("cannot read {path}: {e}")));
    }
    if let Some(inline) = opt_str(args, inline_flag)? {
        return Ok(inline.to_string());
    }
    match default {
        Some(d) => Ok(d.to_string()),
        None => Err(usage(format!("missing {file_flag} or {inline_flag}"))),
    }
}

fn f64_flag(args: &[String], name: &str) -> Result<f64, Failure> {
    let raw = common::option(args, name)?;
    raw.parse::<f64>()
        .map_err(|_| usage(format!("bad number for {name}: {raw:?}")))
}

fn f64_or(args: &[String], name: &str, default: usize) -> Result<f64, Failure> {
    if has_flag(args, name) {
        f64_flag(args, name)
    } else {
        #[allow(clippy::cast_precision_loss)]
        Ok(default as f64)
    }
}

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
    let payload = raw.get(at.checked_add(len)?..)?;
    let n = shape
        .iter()
        .try_fold(1u64, |a, &d| a.checked_mul(u64::try_from(d).ok()?))?;
    count(&[n]).ok()?;
    let size = match descr.as_str() {
        "<f4" => 4,
        "|u1" | "<u1" | "u1" | "|b1" => 1,
        _ => return None,
    };
    if n.checked_mul(size)? != u64::try_from(payload.len()).ok()? {
        return None;
    }
    let mut data = Vec::new();
    data.try_reserve_exact(payload.len()).ok()?;
    data.extend_from_slice(payload);
    Some(Npy { descr, shape, data })
}

fn f32s(a: &Npy) -> Result<Vec<f32>, Failure> {
    if a.descr != "<f4" {
        return Err(usage(format!("expected float32 (<f4), got {}", a.descr)));
    }
    let count = a.data.len() / 4;
    let mut out = Vec::new();
    out.try_reserve_exact(count)
        .map_err(|_| gxr_finish::geometry::bounds::too_big())?;
    for c in a.data.chunks_exact(4) {
        out.push(f32::from_le_bytes([c[0], c[1], c[2], c[3]]));
    }
    Ok(out)
}

fn u8_bytes(a: &Npy) -> Result<Vec<u8>, Failure> {
    match a.descr.as_str() {
        "|u1" | "<u1" | "u1" | "|b1" => Ok(a.data.clone()),
        d => Err(usage(format!("expected uint8/bool, got {d}"))),
    }
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
    let dims_json: Vec<i64> = shape
        .iter()
        .map(|&d| i64::try_from(d).unwrap_or(i64::MAX))
        .collect();
    Ok(Json::object([
        ("dtype", Json::str(descr)),
        ("shape", Json::ints(&dims_json)),
    ]))
}

fn write_track_state(args: &[String], state: &HdrTrackState) -> Result<Json, Failure> {
    if state.active {
        let [h, w, c] = state.shape.unwrap_or([0, 0, 2]);
        let bytes = f32_le_bytes(&state.layer)?;
        write_npy(args, "--out", "<f4", &[h, w, c], &bytes)?;
    } else {
        write_npy(args, "--out", "<f4", &[0, 0, 2], &[])?;
    }
    Ok(state.to_meta_json())
}

fn layer_dims_of(a: &Npy) -> Result<(usize, usize, usize), Failure> {
    match a.shape.as_slice() {
        [h, w] => Ok((*h, *w, 1)),
        [h, w, c] => Ok((*h, *w, *c)),
        s => Err(usage(format!("expected 2D or 3D layer, got shape {s:?}"))),
    }
}

fn warp_float_cmd(args: &[String]) -> Result<Json, Failure> {
    let a = npy(args, "--in")?;
    let (h_def, w_def, c_def) = layer_dims_of(&a)?;
    let w = checked_dim_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_dim_f64(f64_or(args, "--h", h_def)?)?;
    let channels = checked_dim_f64(f64_or(args, "--channels", c_def)?)?;
    let geom_json = read_text_flag(args, "--geometry", "--geometry-json", None)?;
    let geom = parse_geometry(&geom_json)?;
    let vals = f32s(&a)?;
    let out = warp_float_u64(&vals, w, h, channels, &geom)?;
    let bytes = f32_le_bytes(&out.data)?;
    write_npy(
        args,
        "--out",
        "<f4",
        &[out.height, out.width, out.channels],
        &bytes,
    )
}

fn crop_valid_cmd(args: &[String]) -> Result<Json, Failure> {
    let a = npy(args, "--in")?;
    let (h_def, w_def, c_def) = layer_dims_of(&a)?;
    let w = checked_dim_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_dim_f64(f64_or(args, "--h", h_def)?)?;
    let channels = checked_dim_f64(f64_or(args, "--channels", c_def)?)?;
    let rect_json = read_text_flag(args, "--rect", "--rect-json", None)?;
    let rect = parse_rect4_json(&rect_json)?;
    let vals = f32s(&a)?;
    let out = crop_valid_f32_u64(&vals, w, h, channels, rect)?;
    let bytes = f32_le_bytes(&out.data)?;
    write_npy(
        args,
        "--out",
        "<f4",
        &[out.height, out.width, out.channels],
        &bytes,
    )
}

fn rgb_dims_of(a: &Npy) -> Result<(usize, usize), Failure> {
    match a.shape.as_slice() {
        [h, w, 3] => Ok((*h, *w)),
        s => Err(usage(format!("expected (h, w, 3) RGB, got shape {s:?}"))),
    }
}

fn begin_cmd(args: &[String]) -> Result<Json, Failure> {
    let rgb_npy = npy(args, "--rgb")?;
    let (h_def, w_def) = rgb_dims_of(&rgb_npy)?;
    let rgb = u8_bytes(&rgb_npy)?;
    let w = checked_dim_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_dim_f64(f64_or(args, "--h", h_def)?)?;
    let params_json = read_text_flag(args, "--params", "--params-json", Some(""))?;
    let params = parse_params_opt(&params_json)?;

    let dng_bytes = if let Some(path) = opt_str(args, "--dng")? {
        Some(std::fs::read(path).map_err(|e| usage(format!("cannot read {path}: {e}")))?)
    } else {
        None
    };
    let dng_name = opt_str(args, "--dng-name")?.unwrap_or("<bytes>");
    let jpeg_bytes = if let Some(path) = opt_str(args, "--jpeg")? {
        Some(std::fs::read(path).map_err(|e| usage(format!("cannot read {path}: {e}")))?)
    } else {
        None
    };

    let state = begin(
        &rgb,
        w,
        h,
        dng_bytes.as_deref(),
        dng_name,
        jpeg_bytes.as_deref(),
        &params,
    )?;
    write_track_state(args, &state)
}

fn read_state(args: &[String]) -> Result<HdrTrackState, Failure> {
    let layer_npy = npy(args, "--layer")?;
    let layer = f32s(&layer_npy)?;
    let state_json = read_text_flag(args, "--state", "--state-json", None)?;
    Ok(HdrTrackState::from_json_and_slice(&layer, &state_json)?)
}

fn warp_cmd(args: &[String]) -> Result<Json, Failure> {
    let state = read_state(args)?;
    let geom_json = read_text_flag(args, "--geometry", "--geometry-json", None)?;
    let out = warp_track(&state, &geom_json)?;
    write_track_state(args, &out)
}

fn box_cmd(args: &[String]) -> Result<Json, Failure> {
    let state = read_state(args)?;
    let x = checked_box_offset_f64(f64_flag(args, "--x")?)?;
    let y = checked_box_offset_f64(f64_flag(args, "--y")?)?;
    let w = checked_box_dim_f64(f64_flag(args, "--w")?)?;
    let h = checked_box_dim_f64(f64_flag(args, "--h")?)?;
    let out = box_track(&state, x, y, w, h)?;
    write_track_state(args, &out)
}

fn crop_cmd(args: &[String]) -> Result<Json, Failure> {
    let state = read_state(args)?;
    let rect_json = read_text_flag(args, "--rect", "--rect-json", None)?;
    let out = crop_track(&state, &rect_json)?;
    write_track_state(args, &out)
}

fn stamp_cmd(args: &[String]) -> Result<Json, Failure> {
    let state = read_state(args)?;
    let mask_npy = npy(args, "--mask")?;
    let (h_def, w_def) = match mask_npy.shape.as_slice() {
        [h, w] => (*h, *w),
        s => return Err(usage(format!("expected 2D mask, got shape {s:?}"))),
    };
    let mask = u8_bytes(&mask_npy)?;
    let w = checked_nonneg_u64_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_nonneg_u64_f64(f64_or(args, "--h", h_def)?)?;
    let out = stamp_track(&state, &mask, w, h)?;
    write_track_state(args, &out)
}

fn frame_cmd(args: &[String]) -> Result<Json, Failure> {
    let state = read_state(args)?;
    let layout_json = read_text_flag(args, "--layout", "--layout-json", None)?;
    let out = frame_track(&state, &layout_json)?;
    write_track_state(args, &out)
}

fn to_grid_cmd(args: &[String]) -> Result<Json, Failure> {
    let a = npy(args, "--layer")?;
    let (h_def, w_def, c) = layer_dims_of(&a)?;
    if c != 2 {
        return Err(usage(format!("expected (h, w, 2) layer, got channels={c}")));
    }
    let w = checked_dim_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_dim_f64(f64_or(args, "--h", h_def)?)?;
    let out_w = checked_dim_f64(f64_flag(args, "--out-w")?)?;
    let out_h = checked_dim_f64(f64_flag(args, "--out-h")?)?;
    let factor = checked_factor_f64(f64_flag(args, "--factor")?)?;
    let layer = f32s(&a)?;
    let grid = to_grid(&layer, w, h, out_w, out_h, factor)?;
    let y_bytes = f32_le_bytes(&grid.raw_y)?;
    let c_bytes: Vec<u8> = grid.clip.iter().map(|&b| u8::from(b)).collect();
    let y_desc = write_npy(args, "--out", "<f4", &[grid.gh, grid.gw], &y_bytes)?;
    write_npy(args, "--clip-out", "|b1", &[grid.gh, grid.gw], &c_bytes)?;
    Ok(y_desc)
}

fn finish_cmd(args: &[String]) -> Result<Json, Failure> {
    let payload_path = common::option(args, "--payload")?;
    let payload = std::fs::read(payload_path)
        .map_err(|e| usage(format!("cannot read {payload_path}: {e}")))?;
    let state = read_state(args)?;
    let fmt = common::option(args, "--fmt")?;
    let rgb_npy = npy(args, "--rgb")?;
    let (h_def, w_def) = rgb_dims_of(&rgb_npy)?;
    let rgb = u8_bytes(&rgb_npy)?;
    let w = checked_dim_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_dim_f64(f64_or(args, "--h", h_def)?)?;
    let params_json = read_text_flag(args, "--params", "--params-json", Some(""))?;
    let params = parse_params_opt(&params_json)?;

    let out = finish(&payload, &state, fmt, &rgb, w, h, &params)?;
    let bytes = out.bytes();
    write_npy(args, "--out", "|u1", &[1, bytes.len()], bytes)?;
    Ok(out.report_json()?)
}

fn begin_batch_cmd(args: &[String]) -> Result<String, Failure> {
    let rgb_npy = npy(args, "--rgb")?;
    let (h_def, w_def) = rgb_dims_of(&rgb_npy)?;
    let rgb = u8_bytes(&rgb_npy)?;
    let w = checked_dim_f64(f64_or(args, "--w", w_def)?)?;
    let h = checked_dim_f64(f64_or(args, "--h", h_def)?)?;
    let params_json = read_text_flag(args, "--params", "--params-json", Some(""))?;
    let params = parse_params_opt(&params_json)?;
    let dng_path = common::option(args, "--dng")?;
    let dng_bytes =
        std::fs::read(dng_path).map_err(|e| usage(format!("cannot read {dng_path}: {e}")))?;
    let dng_name = opt_str(args, "--dng-name")?.unwrap_or("<bytes>");
    let manifest_path = common::option(args, "--manifest")?;
    let manifest = std::fs::read_to_string(manifest_path)
        .map_err(|e| usage(format!("cannot read {manifest_path}: {e}")))?;

    let mut lines = Vec::new();
    for raw_line in manifest.lines() {
        let path = raw_line.trim();
        if path.is_empty() {
            continue;
        }
        let row_res: Result<Json, Failure> = (|| {
            let jpg_bytes =
                std::fs::read(path).map_err(|e| usage(format!("cannot read {path}: {e}")))?;
            let state = begin(
                &rgb,
                w,
                h,
                Some(&dng_bytes),
                dng_name,
                Some(&jpg_bytes),
                &params,
            )?;
            let meta = state.to_meta_json();
            ensure_finite(&meta)?;
            Ok(meta)
        })();
        let row_json = match row_res {
            Ok(state) => Json::object([("exit", Json::Int(0)), ("state", state)]),
            Err(err) => common::failure_row_json(&err, crate::USAGE),
        };
        lines.push(row_json.render());
    }
    Ok(lines.join("\n"))
}
