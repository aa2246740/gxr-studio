//! Port of `gxr_studio/finish/hdr_export.py:113 HdrTrack.frame`.

use gxr_finish::geometry::bounds::bad_geometry;
use gxr_finish::geometry::json::{JsonValue, parse_json};
use gxr_finish::geometry::{checked_float_layer_dims, try_vec_f32};
use gxr_raw::CoreError;

use super::track::HdrTrackState;

const INVALID_LAYOUT: &str = "边框版面格式不对。";

fn bad_layout() -> CoreError {
    CoreError::new("bad_frame", INVALID_LAYOUT)
}

/// Port of `gxr_studio/finish/hdr_export.py:113 HdrTrack.frame` from a `FrameLayout` JSON.
///
/// `null` layout (`style == "none"`) is a no-op. Any `blur` backdrop (`blur_fill`) or mismatched
/// photo placement turns an active track inactive with `problem = "frame_layout"`, matching Python.
pub fn frame_track(state: &HdrTrackState, layout_json: &str) -> Result<HdrTrackState, CoreError> {
    let val = parse_json(layout_json, "bad_frame", INVALID_LAYOUT)?;
    if matches!(val, JsonValue::Null) {
        return Ok(state.clone());
    }
    if !matches!(val, JsonValue::Object(_)) {
        return Err(bad_layout());
    }

    let cw_i = val
        .get("width")
        .and_then(JsonValue::as_strict_int)
        .ok_or_else(bad_layout)?;
    let ch_i = val
        .get("height")
        .and_then(JsonValue::as_strict_int)
        .ok_or_else(bad_layout)?;
    let cw = u64::try_from(cw_i).map_err(|_| bad_layout())?;
    let ch = u64::try_from(ch_i).map_err(|_| bad_layout())?;
    let (cwu, chu, _, canvas_elems) = checked_float_layer_dims(cw, ch, 2)?;

    let is_blur = match val.get("bgfx") {
        None | Some(JsonValue::Null) => false,
        Some(bgfx) => {
            if !matches!(bgfx, JsonValue::Object(_)) {
                return Err(bad_layout());
            }
            bgfx.get("kind").and_then(JsonValue::as_str) == Some("blur")
        }
    };

    let items = val
        .get("items")
        .and_then(JsonValue::as_array)
        .ok_or_else(bad_layout)?;

    if !state.active {
        return Ok(state.clone());
    }
    let Some([lh, lw, 2]) = state.shape else {
        return Err(CoreError::new("bad_input", "HDR 轨道状态格式不对"));
    };

    if is_blur {
        return Ok(HdrTrackState::inactive("frame_layout"));
    }

    let mut photo_box: Option<(usize, usize)> = None;
    let mut photo_count = 0usize;
    for item in items {
        if item.get("k").and_then(JsonValue::as_str) == Some("photo") {
            photo_count += 1;
            let px = item
                .get("x")
                .and_then(JsonValue::as_strict_int)
                .ok_or_else(bad_layout)?;
            let py = item
                .get("y")
                .and_then(JsonValue::as_strict_int)
                .ok_or_else(bad_layout)?;
            let pw = item
                .get("w")
                .and_then(JsonValue::as_strict_int)
                .ok_or_else(bad_layout)?;
            let ph = item
                .get("h")
                .and_then(JsonValue::as_strict_int)
                .ok_or_else(bad_layout)?;
            let lw_i = i64::try_from(lw).map_err(|_| bad_geometry())?;
            let lh_i = i64::try_from(lh).map_err(|_| bad_geometry())?;
            let cw_i64 = i64::try_from(cwu).map_err(|_| bad_geometry())?;
            let ch_i64 = i64::try_from(chu).map_err(|_| bad_geometry())?;
            let fits = pw == lw_i
                && ph == lh_i
                && px >= 0
                && py >= 0
                && px.checked_add(lw_i).is_some_and(|e| e <= cw_i64)
                && py.checked_add(lh_i).is_some_and(|e| e <= ch_i64);
            if !fits {
                return Ok(HdrTrackState::inactive("frame_layout"));
            }
            photo_box = Some((px as usize, py as usize));
        }
    }

    if photo_count != 1 {
        return Ok(HdrTrackState::inactive("frame_layout"));
    }
    let Some((px, py)) = photo_box else {
        return Ok(HdrTrackState::inactive("frame_layout"));
    };

    let mut canvas = try_vec_f32(canvas_elems, f32::NAN)?;
    let row_elems = lw * 2;
    for r in 0..lh {
        let src_off = r * row_elems;
        let dst_off = ((py + r) * cwu + px) * 2;
        canvas[dst_off..dst_off + row_elems]
            .copy_from_slice(&state.layer[src_off..src_off + row_elems]);
    }

    Ok(HdrTrackState {
        active: true,
        shape: Some([chu, cwu, 2]),
        problem: None,
        layer: canvas,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_state() -> HdrTrackState {
        HdrTrackState {
            active: true,
            shape: Some([2, 3, 2]),
            problem: None,
            layer: vec![
                0.1, 0.0, 0.2, 0.0, 0.3, 1.0, // row 0
                0.4, 0.0, 0.5, 0.0, 0.6, 1.0, // row 1
            ],
        }
    }

    #[test]
    fn places_photo_on_nan_canvas() {
        let layout = r#"{"width":5,"height":4,"photo":[1,1],"bg":[255,255,255],"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}],"bgfx":null}"#;
        let out = frame_track(&sample_state(), layout).unwrap();
        assert!(out.active);
        assert_eq!(out.shape, Some([4, 5, 2]));
        assert!(out.layer[0].is_nan());
        assert!(out.layer[1].is_nan());
        assert_eq!(out.layer[6 * 2], 0.1);
        assert_eq!(out.layer[(2 * 5 + 3) * 2 + 1], 1.0);
    }

    #[test]
    fn blur_bgfx_becomes_frame_layout_problem() {
        let layout = r#"{"width":5,"height":4,"photo":[1,1],"bg":[24,24,28],"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}],"bgfx":{"kind":"blur","sigma":90}}"#;
        let out = frame_track(&sample_state(), layout).unwrap();
        assert!(!out.active);
        assert_eq!(out.problem.as_deref(), Some("frame_layout"));
    }

    #[test]
    fn null_layout_is_noop_and_inactive_preserves_prior_problem() {
        let st = sample_state();
        assert_eq!(frame_track(&st, "null").unwrap(), st);

        let blur = r#"{"width":5,"height":4,"photo":[1,1],"bg":[24,24,28],"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}],"bgfx":{"kind":"blur"}}"#;
        let inactive = HdrTrackState::inactive("no_raw");
        let out = frame_track(&inactive, blur).unwrap();
        assert!(!out.active);
        assert_eq!(out.problem.as_deref(), Some("no_raw"));
    }
}
