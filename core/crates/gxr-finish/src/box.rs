//! Port of `gxr_studio/gpu/box.py`.

use gxr_raw::{CoreError, Json};

use crate::crop::is_known_preset;
use crate::geometry::bounds::{checked_mask_dims, try_vec_u8};
use crate::geometry::json::{JsonValue, parse_json};
use crate::geometry::{
    Geometry, clamp_norm_rect, crop_box, fit_preset_in_valid, header_value, needs_valid_crop,
    normalized_valid_rect, output_size, parse_geometry, warp_valid_u64,
};

pub const FULL: [f64; 4] = [0.0, 0.0, 1.0, 1.0];

/// Port of `gxr_studio/gpu/box.py:24 final_box` (checked `u64` entry).
pub fn final_box_u64(
    width: u64,
    height: u64,
    geom: &Geometry,
    crop_rect: Option<[f64; 4]>,
    crop_preset: Option<&str>,
) -> Result<([usize; 4], [f64; 4]), CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let (wu, hu, pixels) = checked_mask_dims(width, height)?;
    let (vw, vh, valid) = if geom.is_identity() {
        (wu, hu, try_vec_u8(pixels, 1)?)
    } else {
        warp_valid_u64(width, height, geom)?
    };
    let valid_rect = normalized_valid_rect(&valid, vw, vh)?;
    let fitted = if needs_valid_crop(geom) {
        fit_preset_in_valid(&valid, vw, vh, crop_preset, crop_rect.unwrap_or(FULL))?
    } else {
        None
    };
    if let Some([x, y, bw, bh]) = fitted {
        return Ok(([x, y, bw, bh], valid_rect));
    }
    if needs_valid_crop(geom) {
        let rect = match crop_rect {
            None => valid_rect,
            Some(r) if r == FULL => valid_rect,
            Some(r) => clamp_norm_rect(&valid, vw, vh, r, None)?,
        };
        return Ok((crop_box(vw, vh, rect)?, valid_rect));
    }
    if let Some(r) = crop_rect
        && r != FULL
    {
        return Ok((crop_box(vw, vh, r)?, valid_rect));
    }
    let (ow, oh) = output_size(wu, hu, geom)?;
    Ok(([0, 0, ow, oh], valid_rect))
}

/// Port of `gxr_studio/gpu/box.py:24 final_box`
pub fn final_box(
    width: usize,
    height: usize,
    geom: &Geometry,
    crop_rect: Option<[f64; 4]>,
    crop_preset: Option<&str>,
) -> Result<([usize; 4], [f64; 4]), CoreError> {
    final_box_u64(width as u64, height as u64, geom, crop_rect, crop_preset)
}

fn parse_final_box_crop(crop_json: &str) -> Result<(Option<[f64; 4]>, Option<String>), CoreError> {
    let trimmed = crop_json.trim();
    if trimmed.is_empty() {
        return Ok((None, None));
    }
    let val = parse_json(trimmed, "bad_rect", "裁剪框超出画面范围")?;
    if matches!(val, JsonValue::Null) {
        return Ok((None, None));
    }
    if !matches!(val, JsonValue::Object(_)) {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let rect = match val.get("rect") {
        None | Some(JsonValue::Null) => None,
        Some(v) => {
            let Some(arr) = v.as_array() else {
                return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
            };
            if arr.len() != 4 {
                return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
            }
            let mut out = [0.0; 4];
            for (i, item) in arr.iter().enumerate() {
                let f = item
                    .as_f64()
                    .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?;
                if !f.is_finite() {
                    return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
                }
                out[i] = f;
            }
            if out[2] <= 0.0 || out[3] <= 0.0 {
                return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
            }
            Some(out)
        }
    };
    let preset = match val.get("preset") {
        None | Some(JsonValue::Null) => None,
        Some(v) => {
            let s = v
                .as_str()
                .ok_or_else(|| CoreError::new("unknown_preset", "无法裁剪。请重新选择比例。"))?;
            if !is_known_preset(s) {
                return Err(CoreError::new(
                    "unknown_preset",
                    "无法裁剪。请重新选择比例。",
                ));
            }
            Some(s.to_string())
        }
    };
    Ok((rect, preset))
}

/// Shared CLI + WASM helper: parse geometry & crop JSON, compute `final_box`, render JSON.
pub fn final_box_json_u64(
    width: u64,
    height: u64,
    geometry_json: &str,
    crop_json: &str,
) -> Result<Json, CoreError> {
    let geom = parse_geometry(geometry_json)?;
    let (crop_rect, crop_preset) = parse_final_box_crop(crop_json)?;
    let ([bx, by, bw, bh], valid) =
        final_box_u64(width, height, &geom, crop_rect, crop_preset.as_deref())?;
    Ok(Json::object([
        (
            "box",
            Json::ints(&[bx as i64, by as i64, bw as i64, bh as i64]),
        ),
        ("valid", Json::floats(&valid)),
        ("header", Json::str(header_value(valid))),
    ]))
}

/// Shared CLI + WASM helper (`usize` convenience wrapper).
pub fn final_box_json(
    width: usize,
    height: usize,
    geometry_json: &str,
    crop_json: &str,
) -> Result<Json, CoreError> {
    final_box_json_u64(width as u64, height as u64, geometry_json, crop_json)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_unrepresentable_crop_rect_coordinates_without_panicking() {
        let geom = parse_geometry(r#"{"straighten":5}"#).unwrap();
        for crop_str in [
            r#"{"rect":[1e18,0,1e18,0.5]}"#,
            r#"{"rect":[0,1e18,0.5,1e18]}"#,
            r#"{"rect":[1e308,0,1e308,0.5]}"#,
            r#"{"rect":[1e18,0,1e18,0.5],"preset":"16:9"}"#,
        ] {
            for g in [Geometry::default(), geom] {
                let err = final_box_json(32, 24, &g.to_json().render(), crop_str).unwrap_err();
                assert_eq!(
                    err,
                    CoreError::new("bad_rect", "裁剪框超出画面范围"),
                    "{crop_str}"
                );
            }
        }
        // Normal out-of-range rect still clamps safely.
        let ok = final_box(
            32,
            24,
            &Geometry::default(),
            Some([-0.2, 0.1, 0.5, 0.5]),
            None,
        )
        .unwrap();
        assert_eq!(ok.0, [0, 2, 16, 12]);
        let ok_clamp = final_box(
            32,
            24,
            &Geometry::default(),
            Some([2.0, 0.0, 0.5, 0.5]),
            None,
        )
        .unwrap();
        assert_eq!(ok_clamp.0, [31, 0, 1, 12]);
    }
}
