//! Port of `gxr_studio/finish/logo_store.py` (keying + trimming + stats) and
//! `gxr_studio/finish/logo_math.py:_alpha_luma` / `finish-logo-fit` dispatch.
//!
//! Note: `gxr_frame::{aspect_mult, coverage_comp, fit_logo, pick_logo_mode, clogo_item}` are
//! implemented in `gxr-frame/src/logo.rs` but not currently re-exported by `gxr-frame/src/lib.rs`,
//! so `fit_logo_json` returns an explicit `unsupported` `CoreError` without copying that module.

use gxr_frame::LogoInfo;
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

/// Validates JSON depth/syntax for `finish-logo-fit` and reports the missing `gxr-frame` exports.
///
/// `gxr_frame` does not re-export `aspect_mult`, `coverage_comp`, `fit_logo`, `pick_logo_mode`, or
/// `clogo_item` from `core/crates/gxr-frame/src/lib.rs`, so this returns `unsupported` rather than
/// duplicating `gxr-frame/src/logo.rs`.
pub fn fit_logo_json(info_json: &str, request_json: &str) -> Result<String, CoreError> {
    let info_val = parse_json(info_json, "bad_request", "标志信息格式不对。")?;
    let req_val = parse_json(request_json, "bad_request", "标志排版请求格式不对。")?;
    if !matches!(info_val, JsonValue::Object(_)) || !matches!(req_val, JsonValue::Object(_)) {
        return Err(CoreError::new(
            "bad_request",
            "请求无效。请刷新页面后重试。",
        ));
    }
    Err(CoreError::new(
        "unsupported",
        "gxr_frame::{aspect_mult, coverage_comp, fit_logo, pick_logo_mode, clogo_item} 未在 gxr-frame/src/lib.rs 导出",
    ))
}
