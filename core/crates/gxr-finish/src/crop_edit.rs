//! Pure port of `gxr_studio/library/edits.py` (`recovered_frame`, `describe`, `write_crop`)
//! and the `geometry` branch of `gxr_studio/render/edits.py` (`write`).
//!
//! No file I/O and no revisions: callers pass the sidecar subset (`frame`, `geometry`, `crop`)
//! and receive the updated subset plus `describe`.

use gxr_raw::{CoreError, Json};

use crate::crop::{
    Crop, MAX_ANGLE, check_final, crop_box as px_crop_box, default_crop, fit_target_crop,
    target_ratio, validate_crop,
};
use crate::geometry::bounds::checked_mask_dims;
use crate::geometry::json::{JsonValue, parse_json};
use crate::geometry::spec::{Geometry, parse_geometry_value};
use crate::geometry::{fitted_crop, output_size, output_size_u64};

const INVALID_REQUEST: &str = "请求无效。请刷新页面后重试。";

fn no_frame_size() -> CoreError {
    CoreError::new("no_frame_size", "无法裁剪。这张照片的尺寸未知。")
}

fn bad_rect() -> CoreError {
    CoreError::new("bad_rect", "裁剪框超出画面范围")
}

fn bad_angle_invalid() -> CoreError {
    CoreError::new("bad_angle", "无法裁剪。拉直角度无效。")
}

fn check_angle_value(angle_deg: f64) -> Result<f64, CoreError> {
    if !angle_deg.is_finite() {
        return Err(bad_angle_invalid());
    }
    if angle_deg.abs() > MAX_ANGLE + 1e-9 {
        return Err(CoreError::new(
            "bad_angle",
            "无法裁剪。拉直角度要在 ±45° 之内。",
        ));
    }
    Ok(angle_deg)
}

fn round6(v: f64) -> f64 {
    if !v.is_finite() {
        return v;
    }
    format!("{v:.6}").parse::<f64>().unwrap_or(v)
}

fn json_value_to_json(val: &JsonValue) -> Json {
    match val {
        JsonValue::Null => Json::Null,
        JsonValue::Bool(b) => Json::Bool(*b),
        JsonValue::Int(i) => Json::Int(*i),
        JsonValue::Float(f) => Json::Float(*f),
        JsonValue::Str(s) => Json::str(s.as_str()),
        JsonValue::Array(items) => Json::Array(items.iter().map(json_value_to_json).collect()),
        JsonValue::Object(pairs) => Json::object(
            pairs
                .iter()
                .map(|(k, v)| (k.as_str(), json_value_to_json(v))),
        ),
    }
}

fn upsert_field(pairs: &[(String, JsonValue)], key: &str, value: Json) -> Json {
    let mut out: Vec<(String, Json)> = pairs
        .iter()
        .map(|(k, v)| {
            if k == key {
                (k.clone(), value.clone())
            } else {
                (k.clone(), json_value_to_json(v))
            }
        })
        .collect();
    if !pairs.iter().any(|(k, _)| k == key) {
        out.push((key.to_string(), value));
    }
    Json::Object(out)
}

/// Port of `gxr_studio/library/edits.py:75 recovered_frame` size dict `{"orientation", "width", "height"}`.
pub fn recovered_frame(width: i64, height: i64, orientation: i64) -> Result<Json, CoreError> {
    if width <= 0 || height <= 0 {
        return Err(no_frame_size());
    }
    let _ = checked_mask_dims(width as u64, height as u64)?;
    let ori = if (1..=8).contains(&orientation) {
        orientation
    } else {
        1
    };
    Ok(Json::object([
        ("orientation", Json::Int(ori)),
        ("width", Json::Int(width)),
        ("height", Json::Int(height)),
    ]))
}

fn photo_size_from_obj(val: Option<&JsonValue>) -> Option<(i64, i64, i64)> {
    let obj = val?;
    if !matches!(obj, JsonValue::Object(_)) {
        return None;
    }
    let w = obj.get("width").and_then(JsonValue::as_strict_int)?;
    let h = obj.get("height").and_then(JsonValue::as_strict_int)?;
    if w <= 0 || h <= 0 {
        return None;
    }
    let raw_ori = obj
        .get("orientation")
        .and_then(JsonValue::as_strict_int)
        .unwrap_or(1);
    let ori = if (1..=8).contains(&raw_ori) {
        raw_ori
    } else {
        1
    };
    Some((w, h, ori))
}

fn resolve_frame(side: &JsonValue, req: Option<&JsonValue>) -> Option<JsonValue> {
    let frame = side
        .get("frame")
        .filter(|v| matches!(v, JsonValue::Object(_)));
    let w_raw = frame
        .and_then(|f| f.get("width"))
        .and_then(JsonValue::as_strict_int);
    let h_raw = frame
        .and_then(|f| f.get("height"))
        .and_then(JsonValue::as_strict_int);
    let has_size = matches!((w_raw, h_raw), (Some(w), Some(h)) if w > 0 && h > 0);
    let has_style = frame.is_some_and(|f| f.get("style").is_some());
    if has_size && !has_style {
        return frame.cloned();
    }
    let recovered = req
        .and_then(|r| photo_size_from_obj(r.get("photo").or_else(|| r.get("photo_size"))))
        .or_else(|| photo_size_from_obj(side.get("photo").or_else(|| side.get("photo_size"))));
    if let Some((w, h, ori)) = recovered {
        return Some(JsonValue::Object(vec![
            ("orientation".to_string(), JsonValue::Int(ori)),
            ("width".to_string(), JsonValue::Int(w)),
            ("height".to_string(), JsonValue::Int(h)),
        ]));
    }
    if let (Some(w), Some(h)) = (w_raw, h_raw)
        && w > 0
        && h > 0
    {
        let ori = frame
            .and_then(|f| f.get("orientation"))
            .and_then(JsonValue::as_strict_int)
            .unwrap_or(1);
        return Some(JsonValue::Object(vec![
            ("orientation".to_string(), JsonValue::Int(ori)),
            ("width".to_string(), JsonValue::Int(w)),
            ("height".to_string(), JsonValue::Int(h)),
        ]));
    }
    side.get("frame").cloned()
}

/// Port of `gxr_studio/library/edits.py:120 _source_frame`.
fn source_frame(frame: Option<&JsonValue>) -> Result<(usize, usize), CoreError> {
    let Some(obj @ JsonValue::Object(_)) = frame else {
        return Err(no_frame_size());
    };
    let Some(w) = obj.get("width").and_then(JsonValue::as_strict_int) else {
        return Err(no_frame_size());
    };
    let Some(h) = obj.get("height").and_then(JsonValue::as_strict_int) else {
        return Err(no_frame_size());
    };
    if w <= 0 || h <= 0 {
        return Err(no_frame_size());
    }
    let (wu, hu, _) = checked_mask_dims(w as u64, h as u64)?;
    if i64::try_from(wu).is_err() || i64::try_from(hu).is_err() {
        return Err(no_frame_size());
    }
    Ok((wu, hu))
}

/// Port of `gxr_studio/library/edits.py:128 _geometry_of`.
fn geometry_of(geom_val: Option<&JsonValue>) -> Result<Geometry, CoreError> {
    match geom_val {
        Some(obj @ JsonValue::Object(_)) => parse_geometry_value(obj),
        _ => Ok(Geometry::default()),
    }
}

/// Port of `gxr_studio/library/edits.py:133 _frame`.
fn canvas_frame(
    frame: Option<&JsonValue>,
    geom_val: Option<&JsonValue>,
) -> Result<(usize, usize), CoreError> {
    let (src_w, src_h) = source_frame(frame)?;
    let geom = geometry_of(geom_val)?;
    output_size_u64(src_w as u64, src_h as u64, &geom)
}

fn parse_rect4(val: &JsonValue) -> Result<[f64; 4], CoreError> {
    let Some(arr) = val.as_array() else {
        return Err(bad_rect());
    };
    if arr.len() != 4 {
        return Err(bad_rect());
    }
    let mut out = [0.0; 4];
    for (i, item) in arr.iter().enumerate() {
        let f = item.as_f64().ok_or_else(bad_rect)?;
        out[i] = f;
    }
    Ok(out)
}

fn frame_orientation_json(frame: Option<&JsonValue>) -> Json {
    match frame {
        Some(JsonValue::Object(_)) => match frame.and_then(|f| f.get("orientation")) {
            Some(v) => json_value_to_json(v),
            None => Json::Int(1),
        },
        _ => Json::Int(1),
    }
}

fn describe_parts(
    frame: Option<&JsonValue>,
    geom_val: Option<&JsonValue>,
    crop_val: Option<&Crop>,
) -> Result<Json, CoreError> {
    let (w, h) = canvas_frame(frame, geom_val)?;
    let default;
    let crop = match crop_val {
        Some(c) => c,
        None => {
            default = Crop {
                preset: "original".to_string(),
                rect: [0.0, 0.0, 1.0, 1.0],
                angle_deg: 0.0,
            };
            &default
        }
    };
    let [x0, y0, bw, bh] = px_crop_box(crop.rect, w as i64, h as i64);
    let orient_str = if bw >= bh { "landscape" } else { "portrait" };
    Ok(Json::object([
        ("preset", Json::str(&crop.preset)),
        ("rect", Json::floats(&crop.rect)),
        ("angle_deg", Json::Float(crop.angle_deg)),
        ("active", Json::Bool(crop_val.is_some())),
        (
            "frame",
            Json::object([
                ("width", Json::Int(w as i64)),
                ("height", Json::Int(h as i64)),
                ("orientation", frame_orientation_json(frame)),
            ]),
        ),
        (
            "pixels",
            Json::object([
                ("x", Json::Int(x0)),
                ("y", Json::Int(y0)),
                ("width", Json::Int(bw)),
                ("height", Json::Int(bh)),
            ]),
        ),
        ("orientation", Json::str(orient_str)),
    ]))
}

fn crop_from_json_value(val: &JsonValue) -> Result<Option<Crop>, CoreError> {
    match val {
        JsonValue::Null => Ok(None),
        JsonValue::Object(_) => {
            let preset = val
                .get("preset")
                .and_then(JsonValue::as_str)
                .ok_or_else(bad_rect)?;
            let rect_val = val.get("rect").ok_or_else(bad_rect)?;
            let rect = parse_rect4(rect_val)?;
            let angle_deg = match val.get("angle_deg") {
                None | Some(JsonValue::Null) => 0.0,
                Some(v) => v.as_f64().ok_or_else(bad_angle_invalid)?,
            };
            Ok(Some(Crop {
                preset: preset.to_string(),
                rect,
                angle_deg,
            }))
        }
        _ => Err(bad_rect()),
    }
}

/// Port of `gxr_studio/library/edits.py:139 describe` on a sidecar subset JSON.
pub fn describe_json(side_json: &str) -> Result<Json, CoreError> {
    let side = parse_json(side_json, "bad_rect", "裁剪框超出画面范围")?;
    if !matches!(side, JsonValue::Object(_)) {
        return Err(bad_rect());
    }
    let crop = match side.get("crop") {
        None => None,
        Some(v) => crop_from_json_value(v)?,
    };
    describe_parts(side.get("frame"), side.get("geometry"), crop.as_ref())
}

/// Port of `gxr_studio/library/edits.py:162 write_crop` (pure rules + `recovered_frame` + `describe`).
pub fn crop_write_json(side_json: &str, request_json: &str) -> Result<Json, CoreError> {
    let side = parse_json(side_json, "bad_rect", "裁剪框超出画面范围")?;
    if !matches!(side, JsonValue::Object(_)) {
        return Err(bad_rect());
    }
    let req = parse_json(request_json, "bad_rect", "裁剪框超出画面范围")?;
    if !matches!(req, JsonValue::Object(_)) {
        return Err(bad_rect());
    }

    let resolved_frame = resolve_frame(&side, Some(&req));
    let frame_ref = resolved_frame.as_ref();
    let orig_geom_val = side.get("geometry");
    let (w, h) = canvas_frame(frame_ref, orig_geom_val)?;
    let wi = w as i64;
    let hi = h as i64;

    let mut flip = match req.get("flip") {
        None | Some(JsonValue::Null) => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_rect", INVALID_REQUEST))?,
    };
    if let Some(ori) = req.get("orientation").and_then(JsonValue::as_str)
        && (ori == "landscape" || ori == "portrait")
    {
        flip = (ori == "landscape") != (wi >= hi);
    }

    let angle_deg = match req.get("angle_deg") {
        None | Some(JsonValue::Null) => {
            let stored = side
                .get("crop")
                .filter(|v| matches!(v, JsonValue::Object(_)))
                .and_then(|c| c.get("angle_deg"))
                .and_then(JsonValue::as_f64);
            stored.unwrap_or(0.0)
        }
        Some(v) => v.as_f64().ok_or_else(bad_angle_invalid)?,
    };

    let reset = match req.get("reset") {
        None | Some(JsonValue::Null) => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_rect", INVALID_REQUEST))?,
    };
    let fit = match req.get("fit") {
        None | Some(JsonValue::Null) => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_rect", INVALID_REQUEST))?,
    };

    let (out_geom_val, out_crop_val, out_crop_struct) = if reset {
        let geom_json = match orig_geom_val {
            Some(JsonValue::Object(pairs)) => {
                // Validate old geometry before updating straighten just like `_frame(side)` already ran.
                upsert_field(pairs, "straighten", Json::Float(0.0))
            }
            _ => Json::Null,
        };
        let geom_jv = match orig_geom_val {
            Some(JsonValue::Object(pairs)) => {
                let mut next = pairs.clone();
                if let Some(pos) = next.iter().position(|(k, _)| k == "straighten") {
                    next[pos].1 = JsonValue::Float(0.0);
                } else {
                    next.push(("straighten".to_string(), JsonValue::Float(0.0)));
                }
                Some(JsonValue::Object(next))
            }
            _ => None,
        };
        (geom_json, Json::Null, (geom_jv, None))
    } else {
        let preset = match req.get("preset") {
            None | Some(JsonValue::Null) => "free",
            Some(v) => {
                let s = v.as_str().ok_or_else(|| {
                    CoreError::new("unknown_preset", "无法裁剪。请重新选择比例。")
                })?;
                if s.is_empty() { "free" } else { s }
            }
        };
        let geom_now = geometry_of(orig_geom_val)?;
        let mut forced = false;
        let mut crop = match req.get("rect") {
            None | Some(JsonValue::Null) => default_crop(preset, wi, hi, angle_deg, flip)?,
            Some(rect_val) => {
                let angle = check_angle_value(angle_deg)?;
                let _ = target_ratio(preset, wi, hi, false)?;
                let rect = parse_rect4(rect_val)?;
                match validate_crop(
                    preset,
                    rect,
                    angle,
                    wi,
                    hi,
                    geom_now.flip,
                    !geom_now.expands(),
                ) {
                    Ok(c) => c,
                    Err(_) if fit => {
                        forced = true;
                        fit_target_crop(preset, rect, angle, wi, hi)?
                    }
                    Err(e) => return Err(e),
                }
            }
        };
        let geom = Geometry {
            straighten: crop.angle_deg,
            ..geom_now
        };
        let (src_w, src_h) = source_frame(frame_ref)?;
        if let Some(fixed) = fitted_crop(
            &geom,
            Some(crop.rect),
            Some(&crop.preset),
            Some(src_w),
            Some(src_h),
            forced,
        )? {
            crop.rect = fixed;
        }
        check_final(&crop.preset, crop.rect, wi, hi)?;
        let out_crop_json = crop.to_json();
        let (out_geom_json, out_geom_jv) = match orig_geom_val {
            Some(JsonValue::Object(pairs)) => {
                let mut next = pairs.clone();
                if let Some(pos) = next.iter().position(|(k, _)| k == "straighten") {
                    next[pos].1 = JsonValue::Float(crop.angle_deg);
                } else {
                    next.push(("straighten".to_string(), JsonValue::Float(crop.angle_deg)));
                }
                (
                    upsert_field(pairs, "straighten", Json::Float(crop.angle_deg)),
                    Some(JsonValue::Object(next)),
                )
            }
            _ => (
                Json::object([("straighten", Json::Float(crop.angle_deg))]),
                Some(JsonValue::Object(vec![(
                    "straighten".to_string(),
                    JsonValue::Float(crop.angle_deg),
                )])),
            ),
        };
        (out_geom_json, out_crop_json, (out_geom_jv, Some(crop)))
    };

    let (desc_geom_jv, desc_crop_struct) = out_crop_struct;
    let desc = describe_parts(frame_ref, desc_geom_jv.as_ref(), desc_crop_struct.as_ref())?;
    let out_frame = frame_ref.map_or(Json::Null, json_value_to_json);
    Ok(Json::object([
        ("frame", out_frame),
        ("geometry", out_geom_val),
        ("crop", out_crop_val),
        ("describe", desc),
    ]))
}

/// Port of `gxr_studio/render/edits.py:134-178` (`write` geometry branch).
pub fn geometry_write_json(side_json: &str, geometry_json: &str) -> Result<Json, CoreError> {
    let side = parse_json(
        side_json,
        "bad_geometry",
        "纠正参数格式不对。请重新打开这张照片后再试。",
    )?;
    if !matches!(side, JsonValue::Object(_)) {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    let geo_val = parse_json(
        geometry_json,
        "bad_geometry",
        "纠正参数格式不对。请重新打开这张照片后再试。",
    )?;
    let raw_geo_is_null = matches!(geo_val, JsonValue::Null);
    let old_geom = geometry_of(side.get("geometry"))?;
    let geom = parse_geometry_value(&geo_val)?;

    let out_geom_json = if raw_geo_is_null {
        Json::Null
    } else {
        geom.to_json()
    };
    let out_geom_jv = if raw_geo_is_null {
        None
    } else {
        Some(JsonValue::Object(vec![
            ("straighten".to_string(), JsonValue::Float(geom.straighten)),
            ("vertical".to_string(), JsonValue::Int(geom.vertical)),
            ("horizontal".to_string(), JsonValue::Int(geom.horizontal)),
            ("distortion".to_string(), JsonValue::Int(geom.distortion)),
            (
                "rotate90".to_string(),
                JsonValue::Int(geom.rotate90.rem_euclid(4)),
            ),
            ("flip".to_string(), JsonValue::Bool(geom.flip)),
        ]))
    };

    let resolved_frame = resolve_frame(&side, None);
    let frame_ref = resolved_frame.as_ref();
    let (src_w, src_h) = match frame_ref {
        Some(JsonValue::Object(_)) => match source_frame(frame_ref) {
            Ok((w, h)) => (Some(w), Some(h)),
            Err(e) if e.kind == "no_frame_size" => (None, None),
            Err(e) => return Err(e),
        },
        _ => (None, None),
    };

    let crop_obj = side
        .get("crop")
        .filter(|v| matches!(v, JsonValue::Object(_)));
    let stored = match crop_obj.and_then(|c| c.get("rect")) {
        Some(v) if matches!(v, JsonValue::Array(items) if items.len() == 4) => {
            Some(parse_rect4(v)?)
        }
        _ => None,
    };
    let preset_name = crop_obj
        .and_then(|c| c.get("preset"))
        .and_then(JsonValue::as_str);

    let resized = match (src_w, src_h) {
        (Some(w), Some(h)) if old_geom.rotate90.rem_euclid(2) == geom.rotate90.rem_euclid(2) => {
            output_size(w, h, &old_geom)? != output_size(w, h, &geom)?
        }
        _ => false,
    };

    let fixed = fitted_crop(
        &geom,
        stored,
        preset_name,
        src_w,
        src_h,
        resized && stored.is_some(),
    )?;

    let (out_crop_json, desc_crop): (Json, Option<Crop>) = if let Some(rect) = fixed {
        let preset_str = preset_name.filter(|s| !s.is_empty()).unwrap_or("free");
        let rounded = [
            round6(rect[0]),
            round6(rect[1]),
            round6(rect[2]),
            round6(rect[3]),
        ];
        let crop = Crop {
            preset: preset_str.to_string(),
            rect: rounded,
            angle_deg: geom.straighten,
        };
        (crop.to_json(), Some(crop))
    } else if let Some(JsonValue::Object(pairs)) = crop_obj {
        let json = upsert_field(pairs, "angle_deg", Json::Float(geom.straighten));
        let desc_crop = if let (Some(p), Some(r)) = (preset_name, stored) {
            Some(Crop {
                preset: p.to_string(),
                rect: r,
                angle_deg: geom.straighten,
            })
        } else {
            None
        };
        (json, desc_crop)
    } else if geom.straighten.abs() > 1e-9 {
        let crop = Crop {
            preset: "free".to_string(),
            rect: [0.0, 0.0, 1.0, 1.0],
            angle_deg: geom.straighten,
        };
        (crop.to_json(), Some(crop))
    } else {
        (Json::Null, None)
    };

    let desc = if src_w.is_some() && src_h.is_some() {
        describe_parts(frame_ref, out_geom_jv.as_ref(), desc_crop.as_ref())?
    } else {
        Json::Null
    };
    let out_frame = frame_ref.map_or(Json::Null, json_value_to_json);
    Ok(Json::object([
        ("frame", out_frame),
        ("geometry", out_geom_json),
        ("crop", out_crop_json),
        ("describe", desc),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_crop_reset_and_xpan() {
        let side =
            r#"{"frame":{"orientation":1,"width":4288,"height":2848},"geometry":null,"crop":null}"#;
        let res = crop_write_json(side, r#"{"preset":"xpan-65-24","angle_deg":1.5}"#).unwrap();
        let text = res.render();
        assert!(text.contains(r#""preset":"xpan-65-24""#));
        assert!(text.contains(r#""straighten":1.5"#));
        assert!(text.contains(r#""active":true"#));

        let reset = crop_write_json(&text, r#"{"reset":true}"#)
            .unwrap()
            .render();
        assert!(reset.contains(r#""crop":null"#));
        assert!(reset.contains(r#""straighten":0.0"#));
        assert!(reset.contains(r#""active":false"#));
    }

    #[test]
    fn write_geometry_refits_and_rounds6() {
        let side =
            r#"{"frame":{"orientation":1,"width":4288,"height":2848},"geometry":null,"crop":null}"#;
        let res = geometry_write_json(side, r#"{"straighten":2.5}"#)
            .unwrap()
            .render();
        assert!(res.contains(r#""preset":"free""#));
        assert!(res.contains(r#""angle_deg":2.5"#));

        let cleared = geometry_write_json(&res, "null").unwrap().render();
        assert!(cleared.contains(r#""geometry":null"#));
        assert!(cleared.contains(r#""angle_deg":0.0"#));
    }

    #[test]
    fn recovered_frame_normalizes_bad_orientation() {
        assert_eq!(
            recovered_frame(4288, 2848, 6).unwrap().render(),
            r#"{"orientation":6,"width":4288,"height":2848}"#
        );
        assert_eq!(
            recovered_frame(4288, 2848, 99).unwrap().render(),
            r#"{"orientation":1,"width":4288,"height":2848}"#
        );
        assert_eq!(
            recovered_frame(0, 2848, 1).unwrap_err().kind,
            "no_frame_size"
        );
    }
}
