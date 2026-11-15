//! HDR for the phone export, through hdr-track's public API (`crate::hdr_track`); nothing is
//! re-ported here. It follows `finish/serve.py:export_finish(hdr=True)`:
//! - `export_prepare`: `HdrTrack.begin` on the base picture (`_stage_rgb`), then the same
//!   `warp` / `box` / `crop` as `compose.finish_unframed` applied to the photo;
//! - `export_complete`: `track.stamp` with the browser's stamp mask (photo stamps only; a border
//!   stamp is painted after the frame and never touches the track), `track.frame` from the
//!   frame layout, then `hdr_export.finish` on this unit's single encode and the fitted array.

use gxr_finish::Geometry;
use gxr_finish::geometry::fit::{clamp_norm_rect, fit_preset_in_valid, normalized_valid_rect};
use gxr_finish::geometry::warp::{needs_valid_crop, warp_valid_u64};
use gxr_frame::Val;
use gxr_raw::{CoreError, Json};

use crate::hdr_track::{
    FinishOutput, HdrTrackState, begin, box_track, crop_track, finish, frame_track,
    parse_params_opt, stamp_track, warp_track,
};

use super::encode::Rgb;
use super::result::HdrOutcome;

const FULL: [f64; 4] = [0.0, 0.0, 1.0, 1.0];

fn json_text(v: &Json) -> String {
    v.render()
}

/// `HdrTrack(dng, jpeg=jpg).begin(base)`: the DNG when the shot has one, the camera JPG's
/// original bytes for the capture check when the shot has one. Empty bytes mean not given.
pub fn begin_track(
    base: &Rgb,
    dng: Option<&[u8]>,
    dng_name: &str,
    jpeg: Option<&[u8]>,
) -> Result<HdrTrackState, CoreError> {
    let params = parse_params_opt("")?;
    begin(
        &base.data,
        base.width as u64,
        base.height as u64,
        dng,
        dng_name,
        jpeg,
        &params,
    )
}

/// The track's `warp` / `box` / `crop` along the branch `compose.finish_unframed` takes for a
/// base of `w × h`: the decisions are recomputed with gxr-finish's own `warp_valid`,
/// `fit_preset_in_valid`, `normalized_valid_rect` and `clamp_norm_rect`, as raster does.
pub fn track_geometry(
    state: HdrTrackState,
    w: usize,
    h: usize,
    geom: &Geometry,
    crop_rect: Option<[f64; 4]>,
    crop_preset: Option<&str>,
) -> Result<HdrTrackState, CoreError> {
    let mut state = state;
    if !geom.is_identity() {
        state = warp_track(&state, &json_text(&geom.to_json()))?;
    }
    if needs_valid_crop(geom) {
        let (vw, vh, valid) = warp_valid_u64(w as u64, h as u64, geom)?;
        let target = crop_rect.unwrap_or(FULL);
        if let Some([x, y, bw, bh]) = fit_preset_in_valid(&valid, vw, vh, crop_preset, target)? {
            return box_track(&state, x as u64, y as u64, bw as u64, bh as u64);
        }
        let rect = match crop_rect {
            None => normalized_valid_rect(&valid, vw, vh)?,
            Some(r) if r == FULL => normalized_valid_rect(&valid, vw, vh)?,
            Some(r) => clamp_norm_rect(&valid, vw, vh, r, None)?,
        };
        return crop_track(&state, &json_text(&Json::floats(&rect)));
    }
    match crop_rect {
        Some(r) if r != FULL => crop_track(&state, &json_text(&Json::floats(&r))),
        _ => Ok(state),
    }
}

/// `state.hdr` for `export_complete`: the track's meta (`active`, `shape`, `problem`) and the
/// frame layout `track.frame` needs. The layer itself travels separately as float32.
pub fn hdr_state_json(state: &HdrTrackState, frame_layout: &Json) -> Json {
    let mut pairs = vec![("asked".to_string(), Json::Bool(true))];
    if let Json::Object(meta) = state.to_meta_json() {
        pairs.extend(meta);
    }
    pairs.push(("frame".to_string(), frame_layout.clone()));
    Json::Object(pairs)
}

fn render_val(v: &Val) -> String {
    super::prepare::val_json(v).render()
}

/// The browser's stamp mask: the photo-sized picture where the stamp changed a black or a white
/// canvas, one byte per pixel (non-zero = changed).
pub struct StampMask<'a> {
    pub data: &'a [u8],
    pub width: u64,
    pub height: u64,
}

/// `track.stamp` (mask), `track.frame`, `hdr_export.finish(payload, track, "jpeg", rgb)`.
/// Returns the file and the outcome (`hdr`, `reason`).
pub fn finish_hdr(
    hdr: &Val,
    layer: &[f32],
    mask: Option<StampMask<'_>>,
    payload: Vec<u8>,
    rgb: &Rgb,
) -> Result<(Vec<u8>, HdrOutcome), CoreError> {
    let meta = Json::object([
        (
            "active",
            Json::Bool(matches!(hdr.get("active"), Some(Val::Bool(true)))),
        ),
        (
            "shape",
            hdr.get("shape")
                .map_or(Json::Null, super::prepare::val_json),
        ),
        (
            "problem",
            hdr.get("problem")
                .map_or(Json::Null, super::prepare::val_json),
        ),
    ]);
    let mut state = HdrTrackState::from_json_and_slice(layer, &meta.render())?;
    if let Some(m) = mask
        && !m.data.is_empty()
    {
        state = stamp_track(&state, m.data, m.width, m.height)?;
    }
    let frame = hdr
        .get("frame")
        .map_or_else(|| "null".to_string(), render_val);
    state = frame_track(&state, &frame)?;
    let params = parse_params_opt("")?;
    let out = finish(
        &payload,
        &state,
        "jpeg",
        &rgb.data,
        rgb.width as u64,
        rgb.height as u64,
        &params,
    )?;
    Ok(match out {
        FinishOutput::Hdr { bytes, .. } => (
            bytes,
            HdrOutcome {
                hdr: true,
                reason: None,
            },
        ),
        FinishOutput::Skipped { bytes, reason } => (
            bytes,
            HdrOutcome {
                hdr: false,
                reason: Some(reason),
            },
        ),
    })
}
