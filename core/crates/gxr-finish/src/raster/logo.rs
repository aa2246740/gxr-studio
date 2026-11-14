//! Port of `gxr_studio/finish/logo_store.py` (keying + trimming + stats) and
//! `gxr_studio/finish/logo_math.py:_alpha_luma` / `finish-logo-fit` (via `gxr-frame`).

use gxr_frame::{LogoInfo, Val, aspect_mult, clogo_item, coverage_comp, fit_logo, pick_logo_mode};
use gxr_hdr::label;
use gxr_raw::{CoreError, Json};

use crate::geometry::bounds::{checked_rgba_slice, fit_count, too_big, try_vec_u8};
use crate::geometry::json::{JsonValue, parse_json};

pub const WARN_OPAQUE: &str = "这张图是不透明的，标志可能带着背景。可以打开「去掉白底」。";

#[derive(Clone, Debug, PartialEq)]
pub struct LogoKeyResult {
    pub width: usize,
    pub height: usize,
    pub rgba: Vec<u8>,
    pub has_white_background: bool,
    pub keyed: bool,
    pub tol: i64,
    pub has_ink: bool,
    pub coverage: f64,
    pub opaque: bool,
    pub lum: f64,
    pub alpha_luma: f64,
    pub info: LogoInfo,
    pub warning: String,
}

impl LogoKeyResult {
    pub fn report_json(&self) -> Json {
        let info_json = Json::object([
            ("id", Json::str(&self.info.id)),
            ("name", Json::str(&self.info.name)),
            ("w", Json::Int(self.info.w)),
            ("h", Json::Int(self.info.h)),
            ("coverage", Json::Float(self.info.coverage)),
            ("opaque", Json::Bool(self.info.opaque)),
            ("lum", Json::Float(self.info.lum)),
        ]);
        Json::object([
            ("dtype", Json::str("uint8")),
            (
                "shape",
                Json::ints(&[self.height as i64, self.width as i64, 4]),
            ),
            (
                "has_white_background",
                Json::Bool(self.has_white_background),
            ),
            ("keyed", Json::Bool(self.keyed)),
            ("tol", Json::Int(self.tol)),
            ("has_ink", Json::Bool(self.has_ink)),
            ("coverage", Json::Float(self.coverage)),
            ("opaque", Json::Bool(self.opaque)),
            ("lum", Json::Float(self.lum)),
            ("alpha_luma", Json::Float(self.alpha_luma)),
            ("info", info_json),
            ("warning", Json::str(&self.warning)),
        ])
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub enum KeyMode {
    #[default]
    Auto,
    Bool(bool),
}

/// Port of `gxr_studio/finish/logo_store.py:160 has_white_background`.
pub fn has_white_background(rgba: &[u8], w: u64, h: u64) -> Result<bool, CoreError> {
    let (wu, hu, _, _) = checked_rgba_slice(rgba, w, h)?;
    for px in rgba.chunks_exact(4) {
        if px[3] < 250 {
            return Ok(false);
        }
    }
    let corners = [(0, 0), (0, wu - 1), (hu - 1, 0), (hu - 1, wu - 1)];
    for (r, c) in corners {
        let base = (r * wu + c) * 4;
        let min_rgb = rgba[base].min(rgba[base + 1]).min(rgba[base + 2]);
        if min_rgb < 238 {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Port of `gxr_studio/finish/logo_store.py:171 key_white`.
///
/// Mirrors NumPy `int16` arithmetic verbatim (including `int16` wrap on `white_frac * 255 // 160`),
/// SciPy default 4-connectivity `ndimage.label` via `gxr_hdr::label`, and Pillow's edge-clamped 3×3
/// `MaxFilter(3)` / `MinFilter(3)`.
pub fn key_white(rgba: &[u8], w: u64, h: u64, tol: i64) -> Result<Vec<u8>, CoreError> {
    let (wu, hu, pixels, rgba_len) = checked_rgba_slice(rgba, w, h)?;
    let clamped_tol = tol.clamp(0, 60) as i16;
    let npix = fit_count(pixels, 1)?;
    let mut near = Vec::new();
    near.try_reserve_exact(npix).map_err(|_| too_big())?;
    for px in rgba.chunks_exact(4) {
        let min_rgb = i16::from(px[0].min(px[1]).min(px[2]));
        near.push((255_i16 - min_rgb) <= clamped_tol);
    }

    let (labels, count) = label(&near, hu, wu)?;
    let label_cap = fit_count((count.max(0) as u64).saturating_add(1), 1)?;
    let mut is_border = vec![false; label_cap];

    for c in 0..wu {
        let top = labels[c];
        if top > 0 {
            is_border[top as usize] = true;
        }
        let bot = labels[(hu - 1) * wu + c];
        if bot > 0 {
            is_border[bot as usize] = true;
        }
    }
    for r in 0..hu {
        let left = labels[r * wu];
        if left > 0 {
            is_border[left as usize] = true;
        }
        let right = labels[r * wu + (wu - 1)];
        if right > 0 {
            is_border[right as usize] = true;
        }
    }

    let mut seen = Vec::new();
    seen.try_reserve_exact(npix).map_err(|_| too_big())?;
    let mut alpha = try_vec_u8(pixels, 255)?;
    for (idx, &lab) in labels.iter().enumerate() {
        let is_bg = lab > 0 && is_border[lab as usize];
        seen.push(is_bg);
        if is_bg {
            alpha[idx] = 0;
        }
    }

    let mut out = try_vec_u8(rgba_len, 0)?;
    for r in 0..hu {
        let r0 = r.saturating_sub(1);
        let r1 = (r + 1).min(hu - 1);
        for c in 0..wu {
            let c0 = c.saturating_sub(1);
            let c1 = (c + 1).min(wu - 1);
            let mut max3 = 0_u8;
            let mut min3 = 255_u8;
            for rr in r0..=r1 {
                let row_off = rr * wu;
                for cc in c0..=c1 {
                    let a = alpha[row_off + cc];
                    max3 = max3.max(a);
                    min3 = min3.min(a);
                }
            }
            let idx = r * wu + c;
            let p = idx * 4;
            let soft = if max3 != min3 && !seen[idx] {
                let min_rgb = i16::from(rgba[p].min(rgba[p + 1]).min(rgba[p + 2]));
                let wf = (255_i16 - min_rgb).clamp(0, 255);
                wf.wrapping_mul(255).div_euclid(160).clamp(0, 255) as u8
            } else {
                alpha[idx]
            };
            out[p] = rgba[p];
            out[p + 1] = rgba[p + 1];
            out[p + 2] = rgba[p + 2];
            out[p + 3] = soft;
        }
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/logo_store.py:188 trim_ink`.
pub fn trim_ink(
    rgba: &[u8],
    w: u64,
    h: u64,
    thresh: u8,
) -> Result<(usize, usize, Vec<u8>, bool), CoreError> {
    let (wu, hu, _, rgba_len) = checked_rgba_slice(rgba, w, h)?;
    let mut min_x = wu;
    let mut min_y = hu;
    let mut max_x = 0_usize;
    let mut max_y = 0_usize;
    let mut any = false;
    for y in 0..hu {
        for x in 0..wu {
            let a = rgba[(y * wu + x) * 4 + 3];
            if a > thresh {
                any = true;
                min_x = min_x.min(x);
                min_y = min_y.min(y);
                max_x = max_x.max(x);
                max_y = max_y.max(y);
            }
        }
    }
    if !any {
        let mut out = try_vec_u8(rgba_len, 0)?;
        out.copy_from_slice(rgba);
        return Ok((wu, hu, out, false));
    }
    let bw = max_x - min_x + 1;
    let bh = max_y - min_y + 1;
    let row_bytes = bw * 4;
    let mut out = Vec::new();
    let total_bytes = fit_count((bw as u64) * (bh as u64) * 4, 1)?;
    out.try_reserve_exact(total_bytes).map_err(|_| too_big())?;
    for y in min_y..=max_y {
        let off = (y * wu + min_x) * 4;
        out.extend_from_slice(&rgba[off..off + row_bytes]);
    }
    Ok((bw, bh, out, true))
}

fn round_n(v: f64, decimals: usize) -> f64 {
    format!("{v:.decimals$}").parse::<f64>().unwrap_or(v)
}

fn pairwise_sum_f32(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        let mut res = -0.0_f32;
        for &v in a {
            res += v;
        }
        res
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - n % 8 {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum_f32(&a[..n2]) + pairwise_sum_f32(&a[n2..])
    }
}

/// Port of `gxr_studio/finish/logo_math.py:126 _alpha_luma` (plus `coverage` for [`stats_of`]).
pub fn alpha_luma(rgba: &[u8], w: u64, h: u64) -> Result<(bool, f64, f64), CoreError> {
    let (_, _, pixels, _) = checked_rgba_slice(rgba, w, h)?;
    let count = fit_count(pixels, 1)?;
    let mut alphas = Vec::new();
    alphas.try_reserve_exact(count).map_err(|_| too_big())?;
    let mut weighted = Vec::new();
    weighted.try_reserve_exact(count).map_err(|_| too_big())?;
    let mut opaque = true;
    for px in rgba.chunks_exact(4) {
        if px[3] < 250 {
            opaque = false;
        }
        let a = f32::from(px[3]) / 255.0_f32;
        let r = f32::from(px[0]);
        let g = f32::from(px[1]);
        let b = f32::from(px[2]);
        let luma = r * 0.299_f32 + g * 0.587_f32 + b * 0.114_f32;
        alphas.push(a);
        weighted.push(luma * a);
    }
    let alpha_sum = 0.0_f32 + pairwise_sum_f32(&alphas);
    let weighted_luma = 0.0_f32 + pairwise_sum_f32(&weighted);
    let coverage = f64::from(alpha_sum / (pixels as f32));
    let mean_lum = f64::from(weighted_luma / alpha_sum.max(1e-6_f32));
    Ok((opaque, mean_lum, coverage))
}

/// Port of `gxr_studio/finish/logo_store.py:197 _stats_of`.
pub fn stats_of(rgba: &[u8], w: u64, h: u64) -> Result<(f64, bool, f64), CoreError> {
    let (opaque, mean_lum, coverage) = alpha_luma(rgba, w, h)?;
    Ok((round_n(coverage, 6), opaque, round_n(mean_lum, 3)))
}

/// Keying + trimming part of `gxr_studio/finish/logo_store.py:226 prepare_uploaded_logo`
/// (before Lanczos resize / PNG re-encode).
pub fn prepare_logo_key(
    rgba: &[u8],
    w: u64,
    h: u64,
    key_mode: KeyMode,
    tol: i64,
    id: &str,
    name: &str,
) -> Result<LogoKeyResult, CoreError> {
    let (wu, hu, _, _) = checked_rgba_slice(rgba, w, h)?;
    let clamped_tol = tol.clamp(0, 60);
    let white_bg = has_white_background(rgba, w, h)?;
    let orig_opaque = rgba.chunks_exact(4).all(|px| px[3] >= 250);
    let wanted = match key_mode {
        KeyMode::Auto => white_bg,
        KeyMode::Bool(b) => b,
    };
    let use_key = wanted && orig_opaque;
    let keyed_buf;
    let working = if use_key {
        keyed_buf = key_white(rgba, w, h, clamped_tol)?;
        keyed_buf.as_slice()
    } else {
        rgba
    };
    let (tw, th, trimmed, has_ink) = trim_ink(working, wu as u64, hu as u64, 8)?;
    let (opaque, raw_lum, raw_cov) = alpha_luma(&trimmed, tw as u64, th as u64)?;
    let coverage = round_n(raw_cov, 6);
    let lum = round_n(raw_lum, 3);
    let warning = if orig_opaque && !white_bg && !use_key {
        WARN_OPAQUE.to_string()
    } else {
        String::new()
    };
    let info = LogoInfo {
        id: id.to_string(),
        name: name.to_string(),
        w: tw as i64,
        h: th as i64,
        coverage,
        opaque,
        lum,
    };
    Ok(LogoKeyResult {
        width: tw,
        height: th,
        rgba: trimmed,
        has_white_background: white_bg,
        keyed: use_key,
        tol: if use_key { clamped_tol } else { 0 },
        has_ink,
        coverage,
        opaque,
        lum,
        alpha_luma: raw_lum,
        info,
        warning,
    })
}

fn bad_logo_request(msg: &str) -> CoreError {
    CoreError::new("bad_request", msg)
}

fn req_str<'a>(obj: &'a JsonValue, key: &str, msg: &str) -> Result<&'a str, CoreError> {
    obj.get(key)
        .and_then(JsonValue::as_str)
        .ok_or_else(|| bad_logo_request(msg))
}

fn opt_str<'a>(
    obj: &'a JsonValue,
    key: &str,
    default: &'a str,
    msg: &str,
) -> Result<&'a str, CoreError> {
    match obj.get(key) {
        None | Some(JsonValue::Null) => Ok(default),
        Some(v) => v.as_str().ok_or_else(|| bad_logo_request(msg)),
    }
}

fn req_int(obj: &JsonValue, key: &str, msg: &str) -> Result<i64, CoreError> {
    obj.get(key)
        .and_then(JsonValue::as_strict_int)
        .ok_or_else(|| bad_logo_request(msg))
}

fn opt_int(obj: &JsonValue, key: &str, default: i64, msg: &str) -> Result<i64, CoreError> {
    match obj.get(key) {
        None | Some(JsonValue::Null) => Ok(default),
        Some(v) => v.as_strict_int().ok_or_else(|| bad_logo_request(msg)),
    }
}

fn req_f64(obj: &JsonValue, key: &str, msg: &str) -> Result<f64, CoreError> {
    let Some(v) = obj.get(key).and_then(JsonValue::as_f64) else {
        return Err(bad_logo_request(msg));
    };
    if !v.is_finite() {
        return Err(bad_logo_request(msg));
    }
    Ok(v)
}

fn opt_f64(obj: &JsonValue, key: &str, default: f64, msg: &str) -> Result<f64, CoreError> {
    match obj.get(key) {
        None | Some(JsonValue::Null) => Ok(default),
        Some(raw) => {
            let Some(v) = raw.as_f64() else {
                return Err(bad_logo_request(msg));
            };
            if !v.is_finite() {
                return Err(bad_logo_request(msg));
            }
            Ok(v)
        }
    }
}

fn val_to_json(v: &Val) -> Json {
    match v {
        Val::Null => Json::Null,
        Val::Bool(b) => Json::Bool(*b),
        Val::Int(n) => Json::Int(*n),
        Val::Float(f) => Json::Float(*f),
        Val::Str(s) => Json::Str(s.clone()),
        Val::Arr(items) => Json::Array(items.iter().map(val_to_json).collect()),
        Val::Obj(pairs) => Json::Object(
            pairs
                .iter()
                .map(|(k, v)| (k.clone(), val_to_json(v)))
                .collect(),
        ),
    }
}

/// Calls `gxr_frame::{fit_logo, pick_logo_mode, aspect_mult, coverage_comp, clogo_item}` for
/// `finish-logo-fit` / `finish_logo_fit`.
pub fn fit_logo_json(info_json: &str, request_json: &str) -> Result<String, CoreError> {
    let info_val = parse_json(info_json, "bad_request", "标志信息格式不对。")?;
    let req_val = parse_json(request_json, "bad_request", "标志排版请求格式不对。")?;
    if !matches!(info_val, JsonValue::Object(_)) || !matches!(req_val, JsonValue::Object(_)) {
        return Err(bad_logo_request("请求无效。请刷新页面后重试。"));
    }

    let id = req_str(&info_val, "id", "标志的 id 要是字符串")?;
    let name = opt_str(&info_val, "name", "", "标志的 name 要是字符串")?;
    let w = req_int(&info_val, "w", "标志的 w 要是整数")?;
    let h = req_int(&info_val, "h", "标志的 h 要是整数")?;
    if w < 0 || h < 0 {
        return Err(bad_logo_request("标志的宽高不能为负数"));
    }
    let coverage = req_f64(&info_val, "coverage", "标志的 coverage 要是数字")?;
    let lum = req_f64(&info_val, "lum", "标志的 lum 要是数字")?;
    let Some(JsonValue::Bool(opaque)) = info_val.get("opaque") else {
        return Err(bad_logo_request("标志的 opaque 要是布尔值"));
    };
    let logo = LogoInfo {
        id: id.to_string(),
        name: name.to_string(),
        w,
        h,
        coverage,
        opaque: *opaque,
        lum,
    };

    let cap = req_int(&req_val, "cap", "标志的 cap 要是整数")?;
    let max_h = req_int(&req_val, "max_h", "标志的 max_h 要是整数")?;
    let max_w = req_int(&req_val, "max_w", "标志的 max_w 要是整数")?;
    let size = opt_str(&req_val, "size", "medium", "标志的 size 要是字符串")?;
    let x = opt_int(&req_val, "x", 0, "标志的 x 要是整数")?;
    let y = opt_int(&req_val, "y", 0, "标志的 y 要是整数")?;
    let mode = opt_str(&req_val, "mode", "auto", "标志的 mode 要是字符串")?;
    let bg_lum = opt_f64(&req_val, "bg_lum", 255.0, "标志的 bg_lum 要是数字")?;
    let ink = match req_val.get("ink") {
        None | Some(JsonValue::Null) => [0_i64, 0, 0],
        Some(v) => {
            let Some(arr) = v.as_array() else {
                return Err(bad_logo_request("标志的 ink 要是 3 个整数"));
            };
            if arr.len() != 3 {
                return Err(bad_logo_request("标志的 ink 要是 3 个整数"));
            }
            [
                arr[0]
                    .as_strict_int()
                    .ok_or_else(|| bad_logo_request("标志的 ink 要是 3 个整数"))?,
                arr[1]
                    .as_strict_int()
                    .ok_or_else(|| bad_logo_request("标志的 ink 要是 3 个整数"))?,
                arr[2]
                    .as_strict_int()
                    .ok_or_else(|| bad_logo_request("标志的 ink 要是 3 个整数"))?,
            ]
        }
    };

    let (fw, fh) = fit_logo(&logo, cap, max_h, max_w, size)?;
    let picked_mode = pick_logo_mode(&logo, mode, bg_lum);
    let a_mult = aspect_mult(logo.aspect());
    let c_comp = coverage_comp(logo.coverage);
    let item = clogo_item(&logo, x, y, fw, fh, mode, ink, bg_lum);
    Ok(Json::object([
        ("w", Json::Int(fw)),
        ("h", Json::Int(fh)),
        ("mode", Json::str(&picked_mode)),
        ("aspect_mult", Json::Float(a_mult)),
        ("coverage_comp", Json::Float(c_comp)),
        ("item", val_to_json(&item)),
    ])
    .render())
}
