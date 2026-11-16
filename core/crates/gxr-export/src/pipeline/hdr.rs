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
    FinishOutput, FramedTrack, HdrTrackState, begin, box_track, crop_track, finish, finish_placed,
    frame_placement, frame_track, parse_params_opt, stamp_track_in_place, warp_track,
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

fn apply_mask(
    mut state: HdrTrackState,
    mask: Option<StampMask<'_>>,
) -> Result<HdrTrackState, CoreError> {
    if let Some(m) = mask
        && !m.data.is_empty()
    {
        stamp_track_in_place(&mut state, m.data, m.width, m.height)?;
    }
    Ok(state)
}

fn stamp_from_inputs(
    hdr: &Val,
    layer: &[f32],
    mask: Option<StampMask<'_>>,
) -> Result<HdrTrackState, CoreError> {
    let state = HdrTrackState::from_json_and_slice(layer, &render_val(hdr))?;
    apply_mask(state, mask)
}

/// Bounded-memory equivalent of [`track_final`] that moves an owned `layer` without copying
/// and records its frame offset via [`frame_placement`].
pub fn track_final_placed_owned(
    hdr: &Val,
    layer: Vec<f32>,
    mask: Option<StampMask<'_>>,
) -> Result<FramedTrack, CoreError> {
    let state = HdrTrackState::from_json_and_vec(layer, &render_val(hdr))?;
    let state = apply_mask(state, mask)?;
    let frame = hdr
        .get("frame")
        .map_or_else(|| "null".to_string(), render_val);
    frame_placement(state, &frame)
}

/// Bounded-memory equivalent of [`track_final`] that keeps the photo-sized HDR layer and records
/// its frame offset via [`frame_placement`].
pub fn track_final_placed(
    hdr: &Val,
    layer: &[f32],
    mask: Option<StampMask<'_>>,
) -> Result<FramedTrack, CoreError> {
    let state = stamp_from_inputs(hdr, layer, mask)?;
    let frame = hdr
        .get("frame")
        .map_or_else(|| "null".to_string(), render_val);
    frame_placement(state, &frame)
}

/// `track.stamp` (mask) and `track.frame` on the state `export_prepare` wrote. `state.hdr` goes
/// unchanged to hdr-track's strict reader (`HdrTrackState::from_json_and_slice`), so a malformed
/// state (`42`, `{}`, `"active": "true"`, …) is its `bad_input` error, never an `no_raw` export
/// (Sol r1 P2-02). Neither step needs the pixels, so `export_complete` runs this first, before any
/// copy, fit or encode.
pub fn track_final(
    hdr: &Val,
    layer: &[f32],
    mask: Option<StampMask<'_>>,
) -> Result<HdrTrackState, CoreError> {
    let state = stamp_from_inputs(hdr, layer, mask)?;
    let frame = hdr
        .get("frame")
        .map_or_else(|| "null".to_string(), render_val);
    frame_track(&state, &frame)
}

fn finish_outcome(out: FinishOutput) -> (Vec<u8>, HdrOutcome) {
    match out {
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
    }
}

/// Streaming `hdr_export.finish` directly from a [`FramedTrack`].
pub fn finish_hdr_placed(
    framed: FramedTrack,
    payload: Vec<u8>,
    rgb: &Rgb,
) -> Result<(Vec<u8>, HdrOutcome), CoreError> {
    let params = parse_params_opt("")?;
    let out = finish_placed(
        &payload,
        framed,
        "jpeg",
        &rgb.data,
        rgb.width as u64,
        rgb.height as u64,
        &params,
    )?;
    Ok(finish_outcome(out))
}

/// `hdr_export.finish(payload, track, "jpeg", rgb)`: the file and the outcome (`hdr`, `reason`).
pub fn finish_hdr(
    state: &HdrTrackState,
    payload: Vec<u8>,
    rgb: &Rgb,
) -> Result<(Vec<u8>, HdrOutcome), CoreError> {
    let params = parse_params_opt("")?;
    let out = finish(
        &payload,
        state,
        "jpeg",
        &rgb.data,
        rgb.width as u64,
        rgb.height as u64,
        &params,
    )?;
    Ok(finish_outcome(out))
}
