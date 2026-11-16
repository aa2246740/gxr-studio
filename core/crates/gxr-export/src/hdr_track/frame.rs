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

/// Placement of an active photo HDR layer inside a framed canvas without materialising
/// the full-canvas `(canvas_h, canvas_w, 2)` float32 buffer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FramePlacement {
    pub canvas_w: usize,
    pub canvas_h: usize,
    pub photo_x: usize,
    pub photo_y: usize,
}

/// Result of [`frame_placement`]: keeps the photo-sized [`HdrTrackState`] and records
/// any canvas placement separately.
#[derive(Clone, Debug, PartialEq)]
pub struct FramedTrack {
    pub state: HdrTrackState,
    pub placement: Option<FramePlacement>,
}

enum PlacementDecision {
    NoOp,
    InactiveFrameLayout,
    Place(FramePlacement),
}

fn inspect_frame_placement(
    dims: Option<(usize, usize)>,
    layout_json: &str,
) -> Result<PlacementDecision, CoreError> {
    let val = parse_json(layout_json, "bad_frame", INVALID_LAYOUT)?;
    if matches!(val, JsonValue::Null) {
        return Ok(PlacementDecision::NoOp);
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
    let (cwu, chu, _, _) = checked_float_layer_dims(cw, ch, 2)?;

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

    let Some((lh, lw)) = dims else {
        return Ok(PlacementDecision::NoOp);
    };

    if is_blur {
        return Ok(PlacementDecision::InactiveFrameLayout);
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
                return Ok(PlacementDecision::InactiveFrameLayout);
            }
            photo_box = Some((px as usize, py as usize));
        }
    }

    if photo_count != 1 {
        return Ok(PlacementDecision::InactiveFrameLayout);
    }
    let Some((px, py)) = photo_box else {
        return Ok(PlacementDecision::InactiveFrameLayout);
    };

    Ok(PlacementDecision::Place(FramePlacement {
        canvas_w: cwu,
        canvas_h: chu,
        photo_x: px,
        photo_y: py,
    }))
}

/// Validate `layout_json` and keep the photo-sized [`HdrTrackState`] with its [`FramePlacement`]
/// instead of allocating the full-canvas `(canvas_h, canvas_w, 2)` float32 buffer.
pub fn frame_placement(state: HdrTrackState, layout_json: &str) -> Result<FramedTrack, CoreError> {
    let dims = state.validate()?;
    match inspect_frame_placement(dims, layout_json)? {
        PlacementDecision::NoOp => Ok(FramedTrack {
            state,
            placement: None,
        }),
        PlacementDecision::InactiveFrameLayout => Ok(FramedTrack {
            state: HdrTrackState::inactive("frame_layout"),
            placement: None,
        }),
        PlacementDecision::Place(place) => Ok(FramedTrack {
            state,
            placement: Some(place),
        }),
    }
}

/// Port of `gxr_studio/finish/hdr_export.py:113 HdrTrack.frame` from a `FrameLayout` JSON.
///
/// `null` layout (`style == "none"`) is a no-op. Any `blur` backdrop (`blur_fill`) or mismatched
/// photo placement turns an active track inactive with `problem = "frame_layout"`, matching Python.
pub fn frame_track(state: &HdrTrackState, layout_json: &str) -> Result<HdrTrackState, CoreError> {
    let dims = state.validate()?;
    match inspect_frame_placement(dims, layout_json)? {
        PlacementDecision::NoOp => Ok(state.clone()),
        PlacementDecision::InactiveFrameLayout => Ok(HdrTrackState::inactive("frame_layout")),
        PlacementDecision::Place(place) => {
            let (lh, lw) = dims.ok_or_else(bad_geometry)?;
            let canvas_elems = u64::try_from(place.canvas_w)
                .map_err(|_| bad_geometry())?
                .checked_mul(u64::try_from(place.canvas_h).map_err(|_| bad_geometry())?)
                .and_then(|v| v.checked_mul(2))
                .ok_or_else(bad_geometry)?;
            let mut canvas = try_vec_f32(canvas_elems, f32::NAN)?;
            let row_elems = lw * 2;
            for r in 0..lh {
                let src_off = r * row_elems;
                let dst_off = ((place.photo_y + r) * place.canvas_w + place.photo_x) * 2;
                canvas[dst_off..dst_off + row_elems]
                    .copy_from_slice(&state.layer[src_off..src_off + row_elems]);
            }
            Ok(HdrTrackState {
                active: true,
                shape: Some([place.canvas_h, place.canvas_w, 2]),
                problem: None,
                layer: canvas,
            })
        }
    }
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
    fn places_photo_on_nan_canvas_and_keeps_compact_placement() {
        let layout = r#"{"width":5,"height":4,"photo":[1,1],"bg":[255,255,255],"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}],"bgfx":null}"#;
        let out = frame_track(&sample_state(), layout).unwrap();
        assert!(out.active);
        assert_eq!(out.shape, Some([4, 5, 2]));
        assert!(out.layer[0].is_nan());
        assert!(out.layer[1].is_nan());
        assert_eq!(out.layer[6 * 2], 0.1);
        assert_eq!(out.layer[(2 * 5 + 3) * 2 + 1], 1.0);

        let placed = frame_placement(sample_state(), layout).unwrap();
        assert!(placed.state.active);
        assert_eq!(placed.state.shape, Some([2, 3, 2]));
        assert_eq!(
            placed.placement,
            Some(FramePlacement {
                canvas_w: 5,
                canvas_h: 4,
                photo_x: 1,
                photo_y: 1,
            })
        );
    }

    #[test]
    fn blur_bgfx_becomes_frame_layout_problem() {
        let layout = r#"{"width":5,"height":4,"photo":[1,1],"bg":[24,24,28],"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}],"bgfx":{"kind":"blur","sigma":90}}"#;
        let out = frame_track(&sample_state(), layout).unwrap();
        assert!(!out.active);
        assert_eq!(out.problem.as_deref(), Some("frame_layout"));

        let placed = frame_placement(sample_state(), layout).unwrap();
        assert!(!placed.state.active);
        assert_eq!(placed.state.problem.as_deref(), Some("frame_layout"));
        assert_eq!(placed.placement, None);
    }

    #[test]
    fn null_layout_is_noop_and_inactive_preserves_prior_problem() {
        let st = sample_state();
        assert_eq!(frame_track(&st, "null").unwrap(), st);
        assert_eq!(
            frame_placement(st.clone(), "null").unwrap(),
            FramedTrack {
                state: st,
                placement: None
            }
        );

        let blur = r#"{"width":5,"height":4,"photo":[1,1],"bg":[24,24,28],"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}],"bgfx":{"kind":"blur"}}"#;
        let inactive = HdrTrackState::inactive("no_raw");
        let out = frame_track(&inactive, blur).unwrap();
        assert!(!out.active);
        assert_eq!(out.problem.as_deref(), Some("no_raw"));
    }
}
