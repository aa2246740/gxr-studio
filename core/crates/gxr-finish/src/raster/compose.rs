//! Port of `gxr_studio/finish/compose.py:finish_unframed` (without `apply_stamp` or `HdrTrack`).

use gxr_color::Curves;
use gxr_raw::{CoreError, Json};

use crate::geometry::bounds::{
    bad_geometry, bad_rect, checked_add_u64, checked_mul_u64, checked_rgb_slice, fit_count,
    too_big, try_vec_u8,
};
use crate::geometry::fit::{
    clamp_norm_rect, crop_valid, fit_preset_in_valid, header_value, normalized_valid_rect,
};
use crate::geometry::json::{JsonValue, parse_json};
use crate::geometry::spec::{Geometry, parse_geometry_value};
use crate::geometry::warp::{needs_valid_crop, warp_u64};
use crate::raster::pixels::{
    FinishAdjust, apply_base, apply_extra, parse_curves_value, parse_finish_adjust_value,
};

const FULL_RECT: [f64; 4] = [0.0, 0.0, 1.0, 1.0];

#[derive(Clone, Debug, PartialEq)]
pub struct FinishOptions {
    pub stage: String,
    pub from_raw: bool,
    pub crop_rect: Option<[f64; 4]>,
    pub crop_preset: Option<String>,
}

impl Default for FinishOptions {
    fn default() -> Self {
        Self {
            stage: "final".into(),
            from_raw: false,
            crop_rect: None,
            crop_preset: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FinishUnframed {
    pub width: usize,
    pub height: usize,
    pub rgb: Vec<u8>,
    pub header: Option<String>,
}

impl FinishUnframed {
    pub fn report_json(&self) -> Json {
        Json::object([
            ("dtype", Json::str("uint8")),
            (
                "shape",
                Json::ints(&[self.height as i64, self.width as i64, 3]),
            ),
            (
                "header",
                match &self.header {
                    Some(h) => Json::str(h),
                    None => Json::Null,
                },
            ),
        ])
    }
}

fn parse_rect4_opt(val: &JsonValue) -> Result<Option<[f64; 4]>, CoreError> {
    if matches!(val, JsonValue::Null) {
        return Ok(None);
    }
    let Some(items) = val.as_array() else {
        return Err(bad_rect());
    };
    if items.len() != 4 {
        return Err(bad_rect());
    }
    let mut out = [0.0_f64; 4];
    for (slot, item) in out.iter_mut().zip(items.iter()) {
        let Some(f) = item.as_f64() else {
            return Err(bad_rect());
        };
        if !f.is_finite() {
            return Err(bad_rect());
        }
        *slot = f;
    }
    Ok(Some(out))
}

fn parse_edit(edit_json: &str) -> Result<(FinishAdjust, Option<Curves>, Geometry), CoreError> {
    let trimmed = edit_json.trim();
    if trimmed.is_empty() {
        return Ok((FinishAdjust::default(), None, Geometry::default()));
    }
    let val = parse_json(trimmed, "bad_request", "请求无效。请刷新页面后重试。")?;
    if matches!(val, JsonValue::Null) {
        return Ok((FinishAdjust::default(), None, Geometry::default()));
    }
    if !matches!(val, JsonValue::Object(_)) {
        return Err(CoreError::new(
            "bad_request",
            "请求无效。请刷新页面后重试。",
        ));
    }
    let adjust = match val.get("adjust") {
        None | Some(JsonValue::Null) => FinishAdjust::default(),
        Some(v) => parse_finish_adjust_value(v)?,
    };
    let curves = match val.get("curve").or_else(|| val.get("curves")) {
        None | Some(JsonValue::Null) => None,
        Some(v) => parse_curves_value(v)?,
    };
    let geom = match val.get("geometry") {
        None | Some(JsonValue::Null) => Geometry::default(),
        Some(v) => parse_geometry_value(v)?,
    };
    Ok((adjust, curves, geom))
}

fn parse_options(options_json: &str) -> Result<FinishOptions, CoreError> {
    let trimmed = options_json.trim();
    if trimmed.is_empty() {
        return Ok(FinishOptions::default());
    }
    let val = parse_json(trimmed, "bad_request", "请求无效。请刷新页面后重试。")?;
    if matches!(val, JsonValue::Null) {
        return Ok(FinishOptions::default());
    }
    if !matches!(val, JsonValue::Object(_)) {
        return Err(CoreError::new(
            "bad_request",
            "请求无效。请刷新页面后重试。",
        ));
    }
    let mut opts = FinishOptions::default();
    if let Some(v) = val.get("stage")
        && !matches!(v, JsonValue::Null)
    {
        let Some(s) = v.as_str() else {
            return Err(CoreError::new("bad_request", "stage 无效"));
        };
        if !matches!(s, "final" | "uncropped" | "original") {
            return Err(CoreError::new("bad_request", "stage 无效"));
        }
        opts.stage = s.to_string();
    }
    if let Some(v) = val.get("from_raw")
        && !matches!(v, JsonValue::Null)
    {
        opts.from_raw = v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_request", "from_raw 无效"))?;
    }
    if let Some(crop) = val.get("crop")
        && !matches!(crop, JsonValue::Null)
    {
        if !matches!(crop, JsonValue::Object(_)) {
            return Err(bad_rect());
        }
        if let Some(r) = crop.get("rect") {
            opts.crop_rect = parse_rect4_opt(r)?;
        }
        if let Some(p) = crop.get("preset")
            && !matches!(p, JsonValue::Null)
        {
            opts.crop_preset = Some(p.as_str().ok_or_else(bad_rect)?.to_string());
        }
    }
    if let Some(r) = val.get("crop_rect") {
        opts.crop_rect = parse_rect4_opt(r)?;
    }
    if let Some(p) = val.get("crop_preset") {
        opts.crop_preset = match p {
            JsonValue::Null => None,
            _ => Some(p.as_str().ok_or_else(bad_rect)?.to_string()),
        };
    }
    Ok(opts)
}

fn slice_box(
    rgb: &[u8],
    width: usize,
    height: usize,
    x: usize,
    y: usize,
    bw: usize,
    bh: usize,
) -> Result<(usize, usize, Vec<u8>), CoreError> {
    let _ = checked_rgb_slice(rgb, width as u64, height as u64)?;
    if bw == 0
        || bh == 0
        || x.checked_add(bw).is_none_or(|e| e > width)
        || y.checked_add(bh).is_none_or(|e| e > height)
    {
        return Err(bad_rect());
    }
    let out_bytes = checked_mul_u64(checked_mul_u64(bw as u64, bh as u64)?, 3)?;
    let cap = fit_count(out_bytes, 1)?;
    let mut out = Vec::new();
    out.try_reserve_exact(cap).map_err(|_| too_big())?;
    let row_bytes = fit_count(checked_mul_u64(bw as u64, 3)?, 1)?;
    for row in y..y + bh {
        let px_off = checked_add_u64(checked_mul_u64(row as u64, width as u64)?, x as u64)?;
        let off = fit_count(checked_mul_u64(px_off, 3)?, 1)?;
        let end = off.checked_add(row_bytes).ok_or_else(bad_geometry)?;
        let slice = rgb.get(off..end).ok_or_else(bad_geometry)?;
        out.extend_from_slice(slice);
    }
    Ok((bw, bh, out))
}

/// Port of `gxr_studio/finish/compose.py:60 finish_unframed` (without `apply_stamp` or `HdrTrack`).
pub fn finish_unframed(
    rgb: &[u8],
    w: u64,
    h: u64,
    adjust: &FinishAdjust,
    curves: Option<&Curves>,
    geom: &Geometry,
    opts: &FinishOptions,
) -> Result<FinishUnframed, CoreError> {
    let (wu, hu, _, _) = checked_rgb_slice(rgb, w, h)?;
    let adjusted = if opts.from_raw {
        apply_extra(rgb, w, h, adjust)?
    } else {
        let base = apply_base(rgb, w, h, adjust, curves)?;
        apply_extra(&base, w, h, adjust)?
    };
    let (vw, vh, warped_rgb, valid_opt) = if geom.is_identity() {
        (wu, hu, adjusted, None)
    } else {
        let warped = warp_u64(&adjusted, w, h, geom)?;
        (warped.width, warped.height, warped.rgb, Some(warped.valid))
    };

    if opts.stage == "uncropped" || opts.stage == "original" {
        let valid_rect = match valid_opt.as_deref() {
            None => FULL_RECT,
            Some(valid) => normalized_valid_rect(valid, vw, vh)?,
        };
        return Ok(FinishUnframed {
            width: vw,
            height: vh,
            rgb: warped_rgb,
            header: Some(header_value(valid_rect)),
        });
    }

    let (fw, fh, final_rgb) = if needs_valid_crop(geom) {
        let valid = match valid_opt {
            Some(v) => v,
            None => try_vec_u8(checked_mul_u64(vw as u64, vh as u64)?, 1)?,
        };
        let target = opts.crop_rect.unwrap_or(FULL_RECT);
        if let Some([x, y, bw, bh]) =
            fit_preset_in_valid(&valid, vw, vh, opts.crop_preset.as_deref(), target)?
        {
            slice_box(&warped_rgb, vw, vh, x, y, bw, bh)?
        } else {
            let rect = match opts.crop_rect {
                None => normalized_valid_rect(&valid, vw, vh)?,
                Some(r) if r == FULL_RECT => normalized_valid_rect(&valid, vw, vh)?,
                Some(r) => clamp_norm_rect(&valid, vw, vh, r, None)?,
            };
            crop_valid(&warped_rgb, vw, vh, rect)?
        }
    } else if let Some(rect) = opts.crop_rect
        && rect != FULL_RECT
    {
        crop_valid(&warped_rgb, vw, vh, rect)?
    } else {
        (vw, vh, warped_rgb)
    };

    Ok(FinishUnframed {
        width: fw,
        height: fh,
        rgb: final_rgb,
        header: None,
    })
}

/// Parse JSON inputs (`edit_json`, `options_json`) and run [`finish_unframed`].
pub fn finish_array_u64(
    rgb: &[u8],
    w: u64,
    h: u64,
    edit_json: &str,
    options_json: &str,
) -> Result<FinishUnframed, CoreError> {
    let (adjust, curves, geom) = parse_edit(edit_json)?;
    let opts = parse_options(options_json)?;
    let _ = checked_rgb_slice(rgb, w, h)?;
    finish_unframed(rgb, w, h, &adjust, curves.as_ref(), &geom, &opts)
}
