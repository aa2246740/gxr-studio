//! Port of `gxr_studio/finish/logo_math.py:paint_clogo` and
//! `gxr_studio/finish/logo_store.py:prepare_uploaded_logo` (`finish_logo_prepare`).

use gxr_frame::LogoInfo;
use gxr_raw::{CoreError, Json};

use crate::geometry::json::{JsonValue, parse_json};
use crate::ink::draw::bad_coords;
use crate::ink::image::{InkImage, InkMode, bad_mismatch, mean_u8_as_f32};
use crate::ink::ops::LogoInput;
use crate::ink::paste::paste_image;
use crate::raster::logo::{KeyMode, alpha_luma, prepare_logo_key, stats_of};
use crate::resample::{Filter, Mode, resample_u8_u64};

pub const MIN_SIDE: usize = 64;
pub const STORE_LONG_EDGE: usize = 1200;
pub const ERR_TOO_SMALL: &str = "图片太小了：标志至少要 64 像素高。";

/// Parsed `clogo` layout item parameters for [`paint_clogo`].
#[derive(Clone, Debug)]
pub struct ClogoItem<'a> {
    pub id: &'a str,
    pub x: i64,
    pub y: i64,
    pub w: u64,
    pub h: u64,
    pub mode: &'a str,
    pub ink: [u8; 3],
    pub auto: bool,
    pub auto_ink: Option<[[u8; 3]; 2]>,
}

/// Port of `gxr_studio/finish/logo_math.py:paint_clogo`.
pub fn paint_clogo(
    canvas: &mut InkImage,
    item: &ClogoItem<'_>,
    logos: &[LogoInput],
) -> Result<(), CoreError> {
    if item.id.is_empty() {
        return Ok(());
    }
    let Some(found) = logos.iter().rev().find(|l| l.id == item.id) else {
        return Ok(());
    };
    let src = &found.image;
    if src.mode != InkMode::Rgba {
        return Err(bad_mismatch("clogo source must be RGBA"));
    }

    let big_bytes = resample_u8_u64(
        &src.data,
        src.width as u64,
        src.height as u64,
        Mode::Rgba,
        Filter::Lanczos,
        item.w,
        item.h,
    )?;
    let mut big = InkImage::from_bytes(InkMode::Rgba, item.w, item.h, &big_bytes)?;
    let (opaque, lg, _) = alpha_luma(&src.data, src.width as u64, src.height as u64)?;

    let mut mode = if opaque || item.mode.is_empty() {
        "orig"
    } else {
        item.mode
    };
    let mut ink = item.ink;

    if let Some([light, dark]) = item.auto_ink
        && !opaque
    {
        let cw = canvas.width as i64;
        let ch = canvas.height as i64;
        let x0 = item.x.clamp(0, cw);
        let y0 = item.y.clamp(0, ch);
        let x1 = item.x.saturating_add(item.w as i64).clamp(0, cw);
        let y1 = item.y.saturating_add(item.h as i64).clamp(0, ch);
        let bg_l = if x1 > x0 && y1 > y0 {
            let reg = canvas.crop(x0, y0, x1, y1)?;
            let reg_l = reg.convert(InkMode::L)?;
            f64::from(mean_u8_as_f32(&reg_l.data))
        } else {
            128.0_f64
        };
        ink = if bg_l > 150.0_f64 { dark } else { light };
        if item.auto {
            mode = if (lg - bg_l).abs() < 70.0_f64 {
                "mono"
            } else {
                "orig"
            };
        }
    }

    if mode == "mono" && !opaque {
        for px in big.data.chunks_exact_mut(4) {
            px[0] = ink[0];
            px[1] = ink[1];
            px[2] = ink[2];
        }
    }

    let x1 = item
        .x
        .checked_add(item.w as i64)
        .ok_or_else(|| bad_coords("clogo box overflow"))?;
    let y1 = item
        .y
        .checked_add(item.h as i64)
        .ok_or_else(|| bad_coords("clogo box overflow"))?;
    paste_image(canvas, &big, [item.x, item.y, x1, y1], Some(&big))
}

/// Stored custom logo result after keying, trimming, min-side check, `STORE_LONG_EDGE` resize,
/// and stats computation.
#[derive(Clone, Debug, PartialEq)]
pub struct LogoPrepareResult {
    pub width: usize,
    pub height: usize,
    pub rgba: Vec<u8>,
    pub has_white_background: bool,
    pub keyed: bool,
    pub tol: i64,
    pub coverage: f64,
    pub opaque: bool,
    pub lum: f64,
    pub alpha_luma: f64,
    pub info: LogoInfo,
    pub warning: String,
}

impl LogoPrepareResult {
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
            ("w", Json::Int(self.width as i64)),
            ("h", Json::Int(self.height as i64)),
            (
                "has_white_background",
                Json::Bool(self.has_white_background),
            ),
            ("keyed", Json::Bool(self.keyed)),
            ("tol", Json::Int(self.tol)),
            ("coverage", Json::Float(self.coverage)),
            ("opaque", Json::Bool(self.opaque)),
            ("lum", Json::Float(self.lum)),
            ("alpha_luma", Json::Float(self.alpha_luma)),
            ("info", info_json),
            ("warning", Json::str(&self.warning)),
        ])
    }
}

/// Port of `gxr_studio/finish/logo_store.py:prepare_uploaded_logo` starting from decoded RGBA.
pub fn finish_logo_prepare(
    rgba: &[u8],
    w: u64,
    h: u64,
    key_mode: KeyMode,
    tol: i64,
    id: &str,
    name: &str,
) -> Result<LogoPrepareResult, CoreError> {
    let key_res = prepare_logo_key(rgba, w, h, key_mode, tol, id, name)?;
    if !key_res.has_ink || key_res.width.min(key_res.height) < MIN_SIDE {
        return Err(CoreError::new("logo_too_small", ERR_TOO_SMALL));
    }

    let max_edge = key_res.width.max(key_res.height);
    let (stored_w, stored_h, stored_rgba) = if max_edge > STORE_LONG_EDGE {
        let scale = (STORE_LONG_EDGE as f64) / (max_edge as f64);
        let nw = ((key_res.width as f64) * scale)
            .round_ties_even()
            .max(1.0_f64) as u64;
        let nh = ((key_res.height as f64) * scale)
            .round_ties_even()
            .max(1.0_f64) as u64;
        let resized = resample_u8_u64(
            &key_res.rgba,
            key_res.width as u64,
            key_res.height as u64,
            Mode::Rgba,
            Filter::Lanczos,
            nw,
            nh,
        )?;
        (nw as usize, nh as usize, resized)
    } else {
        (key_res.width, key_res.height, key_res.rgba)
    };

    let (coverage, opaque, lum) = stats_of(&stored_rgba, stored_w as u64, stored_h as u64)?;
    let (_, raw_alpha_luma, _) = alpha_luma(&stored_rgba, stored_w as u64, stored_h as u64)?;
    let info = LogoInfo {
        id: id.to_string(),
        name: name.to_string(),
        w: stored_w as i64,
        h: stored_h as i64,
        coverage,
        opaque,
        lum,
    };

    Ok(LogoPrepareResult {
        width: stored_w,
        height: stored_h,
        rgba: stored_rgba,
        has_white_background: key_res.has_white_background,
        keyed: key_res.keyed,
        tol: key_res.tol,
        coverage,
        opaque,
        lum,
        alpha_luma: raw_alpha_luma,
        info,
        warning: key_res.warning,
    })
}

/// Parsed options from `key_json` in WASM `finish_logo_prepare`.
#[derive(Clone, Debug)]
pub struct LogoPrepareOptions {
    pub key_mode: KeyMode,
    pub tol: i64,
    pub id: String,
    pub name: String,
}

fn bad_logo_opts(msg: &str) -> CoreError {
    CoreError::new("bad_request", msg)
}

pub fn parse_logo_prepare_json(key_json: &str) -> Result<LogoPrepareOptions, CoreError> {
    let trimmed = key_json.trim();
    if trimmed.is_empty() {
        return Ok(LogoPrepareOptions {
            key_mode: KeyMode::Auto,
            tol: 30,
            id: String::new(),
            name: String::new(),
        });
    }
    let val = parse_json(trimmed, "bad_request", "标志参数格式不对。")?;
    if !matches!(val, JsonValue::Object(_)) {
        return Err(bad_logo_opts("key_json must be a JSON object"));
    }
    let key_mode = match val.get("key_mode") {
        None | Some(JsonValue::Null) => KeyMode::Auto,
        Some(JsonValue::Bool(b)) => KeyMode::Bool(*b),
        Some(JsonValue::Str(s)) => {
            if s == "auto" {
                KeyMode::Auto
            } else {
                match s.to_ascii_lowercase().as_str() {
                    "1" | "true" | "yes" | "on" => KeyMode::Bool(true),
                    _ => KeyMode::Bool(false),
                }
            }
        }
        Some(_) => return Err(bad_logo_opts("key_mode must be bool, string, or null")),
    };
    let tol = match val.get("tol") {
        None | Some(JsonValue::Null) => 30,
        Some(v) => v
            .as_strict_int()
            .ok_or_else(|| bad_logo_opts("tol must be integer"))?,
    };
    let id = match val.get("id") {
        None | Some(JsonValue::Null) => String::new(),
        Some(v) => v
            .as_str()
            .ok_or_else(|| bad_logo_opts("id must be string"))?
            .to_string(),
    };
    let name = match val.get("name") {
        None | Some(JsonValue::Null) => String::new(),
        Some(v) => v
            .as_str()
            .ok_or_else(|| bad_logo_opts("name must be string"))?
            .to_string(),
    };
    Ok(LogoPrepareOptions {
        key_mode,
        tol,
        id,
        name,
    })
}
