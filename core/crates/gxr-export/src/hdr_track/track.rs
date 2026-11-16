//! Port of `gxr_studio/finish/hdr_export.py` (`HdrTrack`: `begin`, `warp`, `box`, `crop`, `stamp`).

use gxr_finish::geometry::bounds::{
    MAX_BYTES, MAX_COUNT, bad_geometry, bad_rect, checked_mul_u64, fit_count, too_big,
};
use gxr_finish::geometry::json::{JsonValue, parse_json};
use gxr_finish::geometry::{
    Geometry, checked_float_layer_dims, checked_float_layer_slice, crop_valid_f32_u64,
    parse_geometry, try_vec_f32, warp_float_u64,
};
use gxr_hdr::{Params, params_from_json};
use gxr_raw::{CoreError, Dng, Json};

use super::capture;

/// Failure reason when no paired RAW DNG is available.
pub const NO_RAW: &str = "no_raw";

/// Validate a positive size / dimension passed as JS `f64` (WASM number-parameter rule).
pub fn checked_dim_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || v <= 0.0 {
        return Err(bad_geometry());
    }
    if v > MAX_COUNT as f64 {
        return Err(too_big());
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    Ok(v as u64)
}

/// Validate a non-negative integer size / offset passed as JS `f64`.
pub fn checked_nonneg_u64_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || v < 0.0 {
        return Err(bad_geometry());
    }
    if v > MAX_COUNT as f64 {
        return Err(too_big());
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    Ok(v as u64)
}

/// Validate a `box` x/y offset passed as JS `f64`.
pub fn checked_box_offset_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || v < 0.0 {
        return Err(bad_rect());
    }
    if v > MAX_COUNT as f64 {
        return Err(too_big());
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    Ok(v as u64)
}

/// Validate a `box` width/height passed as JS `f64`.
pub fn checked_box_dim_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || v <= 0.0 {
        return Err(bad_rect());
    }
    if v > MAX_COUNT as f64 {
        return Err(too_big());
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    Ok(v as u64)
}

/// Validate a positive `map_factor` passed as JS `f64`.
pub fn checked_factor_f64(v: f64) -> Result<usize, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || v <= 0.0 {
        return Err(bad_geometry());
    }
    if v > MAX_BYTES as f64 {
        return Err(too_big());
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    Ok(v as usize)
}

/// Validate that `rgb` has `w * h * 3` bytes using checked `u64` arithmetic and the shared limit.
pub(crate) fn checked_rgb_bytes(rgb: &[u8], w: u64, h: u64) -> Result<(usize, usize), CoreError> {
    let (wu, hu, pixels) = gxr_finish::geometry::bounds::checked_mask_dims(w, h)?;
    let nbytes = checked_mul_u64(pixels, 3)?;
    let expected = fit_count(nbytes, 1)?;
    if rgb.len() != expected {
        return Err(bad_geometry());
    }
    Ok((wu, hu))
}

fn bad_state() -> CoreError {
    CoreError::new("bad_input", "HDR 轨道状态格式不对")
}

/// Stateless snapshot of `gxr_studio/finish/hdr_export.py:HdrTrack`.
///
/// When `active == true`, `shape == Some([height, width, 2])`, `problem == None`, and `layer` is
/// row-major `(height, width, 2)` float32 (`[raw_y, clip]`, `NaN` outside the photo).
#[derive(Clone, Debug, PartialEq)]
pub struct HdrTrackState {
    pub active: bool,
    pub shape: Option<[usize; 3]>,
    pub problem: Option<String>,
    pub layer: Vec<f32>,
}

impl HdrTrackState {
    pub fn inactive(problem: impl Into<String>) -> Self {
        Self {
            active: false,
            shape: None,
            problem: Some(problem.into()),
            layer: Vec::new(),
        }
    }

    /// Construct a validated active state from `(height, width)` and flat `(h, w, 2)` float32 values.
    pub fn active(height: usize, width: usize, layer: Vec<f32>) -> Result<Self, CoreError> {
        let state = Self {
            active: true,
            shape: Some([height, width, 2]),
            problem: None,
            layer,
        };
        let _ = state.validate()?;
        Ok(state)
    }

    /// Shared invariant check for all public `HdrTrackState` consumers:
    /// verifies `active` / `shape` / `problem` / `layer` correspondence and validates dimensions,
    /// byte limits, and exact slice length via `checked_float_layer_slice`.
    pub fn validate(&self) -> Result<Option<(usize, usize)>, CoreError> {
        if !self.active {
            if self.shape.is_some() || !self.layer.is_empty() {
                return Err(bad_geometry());
            }
            return Ok(None);
        }
        if self.problem.is_some() {
            return Err(bad_geometry());
        }
        let Some([lh, lw, c]) = self.shape else {
            return Err(bad_geometry());
        };
        if c != 2 {
            return Err(bad_geometry());
        }
        let lh_u = u64::try_from(lh).map_err(|_| too_big())?;
        let lw_u = u64::try_from(lw).map_err(|_| too_big())?;
        let (wu, hu, _, _) = checked_float_layer_slice(&self.layer, lw_u, lh_u, 2)?;
        Ok(Some((hu, wu)))
    }

    pub fn to_meta_json(&self) -> Json {
        let shape = match self.shape {
            Some([h, w, c]) => Json::ints(&[
                i64::try_from(h).unwrap_or(i64::MAX),
                i64::try_from(w).unwrap_or(i64::MAX),
                i64::try_from(c).unwrap_or(i64::MAX),
            ]),
            None => Json::Null,
        };
        let problem = match &self.problem {
            Some(p) => Json::str(p),
            None => Json::Null,
        };
        Json::object([
            ("active", Json::Bool(self.active)),
            ("shape", shape),
            ("problem", problem),
        ])
    }

    fn parse_active_shape(
        val: &JsonValue,
        layer: &[f32],
    ) -> Result<Option<(usize, usize)>, CoreError> {
        let active = match val.get("active") {
            Some(JsonValue::Bool(b)) => *b,
            _ => return Err(bad_state()),
        };
        if !active {
            if !layer.is_empty() {
                return Err(bad_state());
            }
            match val.get("shape") {
                None | Some(JsonValue::Null) => {}
                Some(JsonValue::Array(arr)) if arr.len() == 3 => {
                    let h = arr[0].as_strict_int().ok_or_else(bad_state)?;
                    let w = arr[1].as_strict_int().ok_or_else(bad_state)?;
                    let c = arr[2].as_strict_int().ok_or_else(bad_state)?;
                    if h != 0 || w != 0 || c != 2 {
                        return Err(bad_state());
                    }
                }
                _ => return Err(bad_state()),
            }
            return Ok(None);
        }

        let Some(arr) = val.get("shape").and_then(JsonValue::as_array) else {
            return Err(bad_state());
        };
        if arr.len() != 3 {
            return Err(bad_state());
        }
        let h_i = arr[0].as_strict_int().ok_or_else(bad_state)?;
        let w_i = arr[1].as_strict_int().ok_or_else(bad_state)?;
        let c_i = arr[2].as_strict_int().ok_or_else(bad_state)?;
        let h_u = u64::try_from(h_i).map_err(|_| bad_state())?;
        let w_u = u64::try_from(w_i).map_err(|_| bad_state())?;
        let c_u = u64::try_from(c_i).map_err(|_| bad_state())?;
        if c_u != 2 {
            return Err(bad_state());
        }
        let (wu, hu, _, _) = checked_float_layer_slice(layer, w_u, h_u, 2)?;
        Ok(Some((hu, wu)))
    }

    fn parse_meta(state_json: &str) -> Result<(JsonValue, Option<String>), CoreError> {
        let val = parse_json(state_json, "bad_input", "HDR 轨道状态格式不对")?;
        if !matches!(val, JsonValue::Object(_)) {
            return Err(bad_state());
        }
        let problem = match val.get("problem") {
            None | Some(JsonValue::Null) => None,
            Some(JsonValue::Str(s)) => Some(s.clone()),
            _ => return Err(bad_state()),
        };
        Ok((val, problem))
    }

    /// Construct [`HdrTrackState`] by moving an owned `layer` buffer without cloning.
    pub fn from_json_and_vec(layer: Vec<f32>, state_json: &str) -> Result<Self, CoreError> {
        let (val, problem) = Self::parse_meta(state_json)?;
        let Some((hu, wu)) = Self::parse_active_shape(&val, &layer)? else {
            return Ok(Self {
                active: false,
                shape: None,
                problem,
                layer: Vec::new(),
            });
        };
        Ok(Self {
            active: true,
            shape: Some([hu, wu, 2]),
            problem: None,
            layer,
        })
    }

    pub fn from_json_and_slice(layer: &[f32], state_json: &str) -> Result<Self, CoreError> {
        let (val, problem) = Self::parse_meta(state_json)?;
        let Some((hu, wu)) = Self::parse_active_shape(&val, layer)? else {
            return Ok(Self {
                active: false,
                shape: None,
                problem,
                layer: Vec::new(),
            });
        };
        let mut owned = Vec::new();
        owned
            .try_reserve_exact(layer.len())
            .map_err(|_| too_big())?;
        owned.extend_from_slice(layer);
        Ok(Self {
            active: true,
            shape: Some([hu, wu, 2]),
            problem: None,
            layer: owned,
        })
    }
}

pub fn parse_params_opt(params_json: &str) -> Result<Params, CoreError> {
    let params = if params_json.trim().is_empty() {
        Params::default()
    } else {
        params_from_json(params_json)?
    };
    if params.map_factor <= 0 {
        return Err(bad_geometry());
    }
    let _ = usize::try_from(params.map_factor).map_err(|_| too_big())?;
    Ok(params)
}

/// Port of `gxr_studio/finish/hdr_export.py:68 HdrTrack.begin`.
pub fn begin(
    rgb: &[u8],
    w: u64,
    h: u64,
    dng_bytes: Option<&[u8]>,
    dng_name: &str,
    jpeg_bytes: Option<&[u8]>,
    params: &Params,
) -> Result<HdrTrackState, CoreError> {
    let (wu, hu) = checked_rgb_bytes(rgb, w, h)?;
    let factor = usize::try_from(params.map_factor)
        .ok()
        .filter(|&f| f > 0)
        .ok_or_else(bad_geometry)?;

    let Some(dng_raw) = dng_bytes else {
        return Ok(HdrTrackState::inactive(NO_RAW));
    };

    let mut dng = match Dng::open(dng_raw, dng_name) {
        Ok(d) => d,
        Err(e) if e.kind == "raw_decode" => return Ok(HdrTrackState::inactive("raw_unreadable")),
        Err(e) => return Err(e),
    };

    let raw_cap = match capture::from_dng(dng.data) {
        Ok(cap) => cap,
        Err(e) => {
            dng.release();
            if gxr_hdr::is_unsupported(&e) {
                return Ok(HdrTrackState::inactive(e.kind));
            }
            return Err(e);
        }
    };

    let (raw_y, raw_clip) = match gxr_hdr::raw_luma(&mut dng) {
        Ok(pair) => pair,
        Err(e) if e.kind == "raw_decode" => return Ok(HdrTrackState::inactive("raw_unreadable")),
        Err(e) if gxr_hdr::is_unsupported(&e) => return Ok(HdrTrackState::inactive(e.kind)),
        Err(e) => return Err(e),
    };

    if let Some(jpg) = jpeg_bytes
        && let Err(e) = capture::check_capture(jpg, &raw_cap)
    {
        if gxr_hdr::is_unsupported(&e) {
            return Ok(HdrTrackState::inactive(e.kind));
        }
        return Err(e);
    }

    let grid = gxr_hdr::sdr_luma_grid(rgb, hu, wu, factor)?;
    let al = match gxr_hdr::align(&grid, &raw_y) {
        Ok(a) => a,
        Err(e) if gxr_hdr::is_unsupported(&e) => return Ok(HdrTrackState::inactive(e.kind)),
        Err(e) => return Err(e),
    };
    if let Err(e) = gxr_hdr::check_alignment(&al) {
        if gxr_hdr::is_unsupported(&e) {
            return Ok(HdrTrackState::inactive(e.kind));
        }
        return Err(e);
    }

    let (reg_y, reg_clip) =
        match gxr_hdr::register(&raw_y, &raw_clip, (hu, wu), &al, (grid.h, grid.w)) {
            Ok(pair) => pair,
            Err(e) if gxr_hdr::is_unsupported(&e) => return Ok(HdrTrackState::inactive(e.kind)),
            Err(e) => return Err(e),
        };

    let (_, _, _, elems) = checked_float_layer_dims(w, h, 2)?;
    let mut layer = try_vec_f32(elems, 0.0_f32)?;
    for (i, (&yv, &cv)) in reg_y.iter().zip(reg_clip.iter()).enumerate() {
        layer[i * 2] = yv;
        layer[i * 2 + 1] = if cv { 1.0_f32 } else { 0.0_f32 };
    }

    Ok(HdrTrackState {
        active: true,
        shape: Some([hu, wu, 2]),
        problem: None,
        layer,
    })
}

/// Port of `gxr_studio/finish/hdr_export.py:89 HdrTrack.warp`.
pub fn warp_track(state: &HdrTrackState, geom_json: &str) -> Result<HdrTrackState, CoreError> {
    let dims = state.validate()?;
    let geom: Geometry = parse_geometry(geom_json)?;
    let Some((lh, lw)) = dims else {
        return Ok(state.clone());
    };
    let warped = warp_float_u64(&state.layer, lw as u64, lh as u64, 2, &geom)?;
    Ok(HdrTrackState {
        active: true,
        shape: Some([warped.height, warped.width, 2]),
        problem: None,
        layer: warped.data,
    })
}

/// Port of `gxr_studio/finish/hdr_export.py:93 HdrTrack.box`.
pub fn box_track(
    state: &HdrTrackState,
    x: u64,
    y: u64,
    w: u64,
    h: u64,
) -> Result<HdrTrackState, CoreError> {
    let dims = state.validate()?;
    if w == 0 || h == 0 {
        return Err(bad_rect());
    }
    let (wu, hu, _, out_elems) = checked_float_layer_dims(w, h, 2)?;
    let Some((lh, lw)) = dims else {
        return Ok(state.clone());
    };
    let x_end = x.checked_add(w).ok_or_else(bad_rect)?;
    let y_end = y.checked_add(h).ok_or_else(bad_rect)?;
    if x_end > lw as u64 || y_end > lh as u64 {
        return Err(bad_rect());
    }
    let xu = usize::try_from(x).map_err(|_| bad_rect())?;
    let yu = usize::try_from(y).map_err(|_| bad_rect())?;
    let cap = fit_count(out_elems, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(cap).map_err(|_| too_big())?;
    let row_elems = wu * 2;
    for row in yu..yu + hu {
        let start = (row * lw + xu) * 2;
        let end = start + row_elems;
        let slice = state.layer.get(start..end).ok_or_else(bad_rect)?;
        out.extend_from_slice(slice);
    }
    Ok(HdrTrackState {
        active: true,
        shape: Some([hu, wu, 2]),
        problem: None,
        layer: out,
    })
}

pub fn parse_rect4_json(rect_json: &str) -> Result<[f64; 4], CoreError> {
    let val = parse_json(rect_json, "bad_rect", "裁剪框超出画面范围")?;
    let arr = val.as_array().ok_or_else(bad_rect)?;
    if arr.len() != 4 {
        return Err(bad_rect());
    }
    let mut out = [0.0_f64; 4];
    for (i, item) in arr.iter().enumerate() {
        let f = item.as_f64().ok_or_else(bad_rect)?;
        if !f.is_finite() {
            return Err(bad_rect());
        }
        out[i] = f;
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/hdr_export.py:97 HdrTrack.crop`.
pub fn crop_track(state: &HdrTrackState, rect_json: &str) -> Result<HdrTrackState, CoreError> {
    let dims = state.validate()?;
    let rect = parse_rect4_json(rect_json)?;
    let Some((lh, lw)) = dims else {
        return Ok(state.clone());
    };
    let cropped = crop_valid_f32_u64(&state.layer, lw as u64, lh as u64, 2, rect)?;
    Ok(HdrTrackState {
        active: true,
        shape: Some([cropped.height, cropped.width, 2]),
        problem: None,
        layer: cropped.data,
    })
}

fn stamp_dilated(
    state: &HdrTrackState,
    mask: &[u8],
    w: u64,
    h: u64,
) -> Result<Option<Vec<bool>>, CoreError> {
    let dims = state.validate()?;
    let Some((lh, lw)) = dims else {
        if !(w == 0 && h == 0 && mask.is_empty()) {
            let _ = gxr_finish::geometry::bounds::checked_mask_slice(mask, w, h)?;
        }
        return Ok(None);
    };
    let (wu, hu, _) = gxr_finish::geometry::bounds::checked_mask_slice(mask, w, h)?;
    if wu != lw || hu != lh {
        return Err(bad_geometry());
    }
    if !mask.iter().any(|&b| b != 0) {
        return Ok(None);
    }
    let bool_mask: Vec<bool> = mask.iter().map(|&b| b != 0).collect();
    Ok(Some(gxr_hdr::binary_dilation(&bool_mask, lh, lw, 2)?))
}

/// Apply `stamp_track` in place on an owned [`HdrTrackState`] without cloning its layer.
pub fn stamp_track_in_place(
    state: &mut HdrTrackState,
    mask: &[u8],
    w: u64,
    h: u64,
) -> Result<(), CoreError> {
    let Some(dilated) = stamp_dilated(state, mask, w, h)? else {
        return Ok(());
    };
    for (i, &hit) in dilated.iter().enumerate() {
        if hit {
            state.layer[i * 2] = f32::NAN;
            state.layer[i * 2 + 1] = f32::NAN;
        }
    }
    Ok(())
}

/// Port of `gxr_studio/finish/hdr_export.py:101 HdrTrack.stamp` from a caller-provided `(h, w)`
/// uint8/bool stamp mask (`!= 0` where stamp modified the image), dilated by `iterations=2` -> NaN.
pub fn stamp_track(
    state: &HdrTrackState,
    mask: &[u8],
    w: u64,
    h: u64,
) -> Result<HdrTrackState, CoreError> {
    let mut out = state.clone();
    stamp_track_in_place(&mut out, mask, w, h)?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{finish, frame_track};

    fn active_2x3() -> HdrTrackState {
        HdrTrackState::active(
            2,
            3,
            vec![
                0.1, 0.0, 0.2, 0.0, 0.3, 1.0, // row 0
                0.4, 0.0, 0.5, 0.0, 0.6, 1.0, // row 1
            ],
        )
        .unwrap()
    }

    #[test]
    fn begin_without_dng_sets_no_raw() {
        let rgb = vec![128_u8; 8 * 8 * 3];
        let st = begin(&rgb, 8, 8, None, "<bytes>", None, &Params::default()).unwrap();
        assert!(!st.active);
        assert_eq!(st.problem.as_deref(), Some("no_raw"));
        assert!(st.layer.is_empty());
    }

    #[test]
    fn begin_bad_dng_sets_raw_unreadable() {
        let rgb = vec![128_u8; 8 * 8 * 3];
        let st = begin(
            &rgb,
            8,
            8,
            Some(b"NOT_A_DNG"),
            "bad.dng",
            None,
            &Params::default(),
        )
        .unwrap();
        assert!(!st.active);
        assert_eq!(st.problem.as_deref(), Some("raw_unreadable"));
    }

    #[test]
    fn box_and_stamp_update_layer_and_nan() {
        let st = HdrTrackState::active(5, 5, vec![0.5_f32; 5 * 5 * 2]).unwrap();
        let mut mask = vec![0_u8; 25];
        mask[2 * 5 + 2] = 1;
        let stamped = stamp_track(&st, &mask, 5, 5).unwrap();
        // Manhattan distance <= 2 from (2,2) -> (0,2) is NaN, corner (0,0) has dist 4 -> stays 0.5
        assert!(stamped.layer[4].is_nan());
        assert!(stamped.layer[5].is_nan());
        assert_eq!(stamped.layer[0], 0.5);

        let boxed = box_track(&active_2x3(), 1, 0, 2, 2).unwrap();
        assert_eq!(boxed.shape, Some([2, 2, 2]));
        assert_eq!(boxed.layer, vec![0.2, 0.0, 0.3, 1.0, 0.5, 0.0, 0.6, 1.0]);
    }

    #[test]
    fn validates_f64_numbers_and_json_depth() {
        assert_eq!(checked_dim_f64(4_294_967_297.0).unwrap_err(), too_big());
        assert_eq!(
            checked_dim_f64(9_007_199_254_740_992.0).unwrap_err(),
            too_big()
        );
        assert_eq!(checked_dim_f64(-1.0).unwrap_err(), bad_geometry());
        assert_eq!(checked_dim_f64(f64::NAN).unwrap_err(), bad_geometry());
        assert_eq!(checked_dim_f64(f64::INFINITY).unwrap_err(), bad_geometry());
        assert_eq!(checked_dim_f64(1.5).unwrap_err(), bad_geometry());

        let deep = format!("{}0{}", "[".repeat(129), "]".repeat(129));
        let err = HdrTrackState::from_json_and_slice(&[], &deep).unwrap_err();
        assert_eq!((err.kind, err.message.as_str()), ("json", "JSON 嵌套太深"));
    }

    #[test]
    fn public_state_consumers_reject_malformed_states_without_panic() {
        let layout =
            r#"{"width":5,"height":4,"bgfx":null,"items":[{"k":"photo","x":1,"y":1,"w":3,"h":2}]}"#;
        let rgb = vec![0_u8; 3 * 2 * 3];
        let params = Params::default();
        let assert_all_consumers_err = |st: &HdrTrackState, expected: &CoreError| {
            assert_eq!(warp_track(st, "{}").unwrap_err(), *expected);
            assert_eq!(box_track(st, 0, 0, 1, 1).unwrap_err(), *expected);
            assert_eq!(crop_track(st, "[0,0,1,1]").unwrap_err(), *expected);
            assert_eq!(
                stamp_track(st, &[1, 0, 0, 0, 0, 0], 3, 2).unwrap_err(),
                *expected
            );
            assert_eq!(stamp_track(st, &[0; 6], 3, 2).unwrap_err(), *expected);
            assert_eq!(frame_track(st, layout).unwrap_err(), *expected);
            assert_eq!(frame_track(st, "null").unwrap_err(), *expected);
            assert_eq!(
                finish(b"hdr", st, "jpeg", &rgb, 3, 2, &params).unwrap_err(),
                *expected
            );
            assert_eq!(
                finish(b"png", st, "png", &rgb, 3, 2, &params).unwrap_err(),
                *expected
            );
        };

        let empty = HdrTrackState {
            active: true,
            shape: Some([2, 3, 2]),
            problem: None,
            layer: vec![],
        };
        assert_all_consumers_err(&empty, &bad_geometry());

        let short = HdrTrackState {
            active: true,
            shape: Some([2, 3, 2]),
            problem: None,
            layer: vec![0.0; 11],
        };
        assert_all_consumers_err(&short, &bad_geometry());

        let huge = HdrTrackState {
            active: true,
            shape: Some([2_147_483_648, 1, 2]),
            problem: None,
            layer: vec![],
        };
        assert_all_consumers_err(&huge, &too_big());

        let inactive_with_shape = HdrTrackState {
            active: false,
            shape: Some([2, 3, 2]),
            problem: Some("no_raw".to_string()),
            layer: vec![],
        };
        assert_all_consumers_err(&inactive_with_shape, &bad_geometry());

        let inactive_with_layer = HdrTrackState {
            active: false,
            shape: None,
            problem: Some("no_raw".to_string()),
            layer: vec![1.0],
        };
        assert_all_consumers_err(&inactive_with_layer, &bad_geometry());

        let active_no_shape = HdrTrackState {
            active: true,
            shape: None,
            problem: None,
            layer: vec![],
        };
        assert_all_consumers_err(&active_no_shape, &bad_geometry());

        let bad_channels = HdrTrackState {
            active: true,
            shape: Some([2, 3, 3]),
            problem: None,
            layer: vec![0.0; 18],
        };
        assert_all_consumers_err(&bad_channels, &bad_geometry());
        assert_eq!(
            HdrTrackState::active(2_147_483_648, 1, vec![]).unwrap_err(),
            too_big()
        );
    }

    #[test]
    fn owned_layer_and_in_place_stamp_match_borrowed_bitwise() {
        let raw: Vec<f32> = (0..(5 * 5 * 2)).map(|i| (i as f32) * 0.1 + 0.05).collect();
        let meta = r#"{"active":true,"shape":[5,5,2],"problem":null}"#;
        let borrowed = HdrTrackState::from_json_and_slice(&raw, meta).unwrap();
        let mut owned = HdrTrackState::from_json_and_vec(raw, meta).unwrap();
        let mut mask = vec![0_u8; 25];
        mask[2 * 5 + 2] = 1;
        let ref_stamped = stamp_track(&borrowed, &mask, 5, 5).unwrap();
        stamp_track_in_place(&mut owned, &mask, 5, 5).unwrap();
        assert_eq!(owned.shape, ref_stamped.shape);
        let a_bits: Vec<u32> = owned.layer.iter().map(|v| v.to_bits()).collect();
        let b_bits: Vec<u32> = ref_stamped.layer.iter().map(|v| v.to_bits()).collect();
        assert_eq!(a_bits, b_bits);
    }
}
