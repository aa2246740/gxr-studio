//! Port of `gxr_studio/finish/hdr_export.py` (`_to_grid`, `finish`).

use gxr_finish::geometry::bounds::{bad_geometry, checked_mask_dims, too_big};
use gxr_finish::geometry::{checked_float_layer_slice, try_vec_f32};
use gxr_finish::{Filter, resample_f32};
use gxr_hdr::{
    Params, block_mean, compute_gain, ensure_finite, is_unsupported, py_round, quantise_gain,
    sdr_luma_grid,
};
use gxr_jpeg::encode_luma_jpeg;
use gxr_raw::{CoreError, Json};

use super::track::{HdrTrackState, NO_RAW, checked_rgb_bytes};

/// Failure reason when export format is not `"jpeg"`.
pub const NOT_JPEG: &str = "not_jpeg";

#[allow(clippy::cast_possible_truncation)]
const W_PHOTO_THRESH: f32 = 0.999_f64 as f32;
#[allow(clippy::cast_possible_truncation)]
const W_MIN_EPS: f32 = 1e-6_f64 as f32;
#[allow(clippy::cast_possible_truncation)]
const CLIP_THRESH: f32 = 0.01_f64 as f32;

/// Output of `_to_grid`: `(raw_y, clip)` on the gain grid `(gh, gw)`.
#[derive(Clone, Debug, PartialEq)]
pub struct ToGridOutput {
    pub raw_y: Vec<f32>,
    pub clip: Vec<bool>,
    pub gh: usize,
    pub gw: usize,
}

fn box_resize_f32(
    plane: &[f32],
    src_w: usize,
    src_h: usize,
    dst_w: usize,
    dst_h: usize,
) -> Result<Vec<f32>, CoreError> {
    resample_f32(plane, src_w, src_h, Filter::Box, dst_w, dst_h)
}

/// Port of `gxr_studio/finish/hdr_export.py:135 _to_grid`.
pub fn to_grid(
    layer: &[f32],
    lw: u64,
    lh: u64,
    out_w: u64,
    out_h: u64,
    factor: usize,
) -> Result<ToGridOutput, CoreError> {
    let (lwu, lhu, _, _) = checked_float_layer_slice(layer, lw, lh, 2)?;
    let (owu, ohu, _) = checked_mask_dims(out_w, out_h)?;
    if factor == 0 {
        return Err(bad_geometry());
    }

    let n = u64::try_from(lwu)
        .map_err(|_| bad_geometry())?
        .checked_mul(u64::try_from(lhu).map_err(|_| bad_geometry())?)
        .ok_or_else(bad_geometry)?;

    let mut p0 = try_vec_f32(n, 0.0_f32)?;
    let mut p1 = try_vec_f32(n, 0.0_f32)?;
    let mut p2 = try_vec_f32(n, 0.0_f32)?;

    for (i, px) in layer.chunks_exact(2).enumerate() {
        if px[0].is_finite() {
            p0[i] = px[0];
            p1[i] = px[1];
            p2[i] = 1.0_f32;
        }
    }

    if (lwu, lhu) != (owu, ohu) {
        p0 = box_resize_f32(&p0, lwu, lhu, owu, ohu)?;
        p1 = box_resize_f32(&p1, lwu, lhu, owu, ohu)?;
        p2 = box_resize_f32(&p2, lwu, lhu, owu, ohu)?;
    }

    let (y_grid, gh, gw) = block_mean(&p0, ohu, owu, factor)?;
    let (clip_grid, _, _) = block_mean(&p1, ohu, owu, factor)?;
    let (w_grid, _, _) = block_mean(&p2, ohu, owu, factor)?;

    let gn = u64::try_from(gh)
        .ok()
        .and_then(|h| h.checked_mul(u64::try_from(gw).ok()?))
        .ok_or_else(bad_geometry)?;
    let mut raw_y = try_vec_f32(gn, f32::NAN)?;
    let mut clip = Vec::new();
    let gn_u = usize::try_from(gn).map_err(|_| bad_geometry())?;
    clip.try_reserve_exact(gn_u).map_err(|_| too_big())?;

    for i in 0..gn_u {
        let w = w_grid[i];
        let denom = if w > W_MIN_EPS { w } else { W_MIN_EPS };
        if w > W_PHOTO_THRESH {
            raw_y[i] = y_grid[i] / denom;
        }
        clip.push((clip_grid[i] / denom) > CLIP_THRESH);
    }

    Ok(ToGridOutput {
        raw_y,
        clip,
        gh,
        gw,
    })
}

/// Final result of `hdr_export.finish`.
#[derive(Clone, Debug, PartialEq)]
pub enum FinishOutput {
    Skipped { bytes: Vec<u8>, reason: String },
    Hdr { bytes: Vec<u8>, gain_max_stops: f64 },
}

impl FinishOutput {
    pub fn bytes(&self) -> &[u8] {
        match self {
            Self::Skipped { bytes, .. } | Self::Hdr { bytes, .. } => bytes,
        }
    }

    pub fn report_json(&self) -> Result<Json, CoreError> {
        let json = match self {
            Self::Skipped { reason, .. } => {
                Json::object([("hdr", Json::Bool(false)), ("reason", Json::str(reason))])
            }
            Self::Hdr { gain_max_stops, .. } => Json::object([
                ("hdr", Json::Bool(true)),
                ("gain_max_stops", Json::Float(*gain_max_stops)),
            ]),
        };
        ensure_finite(&json)?;
        Ok(json)
    }
}

/// Port of `gxr_studio/finish/hdr_export.py:147 finish`: validates format/track, runs
/// `_to_grid`, `compute_gain`, `quantise_gain`, encodes the gainmap as grayscale JPEG via
/// `gxr_jpeg::encode_luma_jpeg`, and wraps `payload` with `gxr_hdr::assemble`.
pub fn finish(
    payload: &[u8],
    state: &HdrTrackState,
    fmt: &str,
    rgb: &[u8],
    w: u64,
    h: u64,
    params: &Params,
) -> Result<FinishOutput, CoreError> {
    let dims = state.validate()?;
    let (wu, hu) = checked_rgb_bytes(rgb, w, h)?;
    let factor = usize::try_from(params.map_factor)
        .ok()
        .filter(|&f| f > 0)
        .ok_or_else(bad_geometry)?;

    if fmt != "jpeg" {
        return Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: NOT_JPEG.to_string(),
        });
    }
    let Some((lh, lw)) = dims else {
        return Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: state.problem.clone().unwrap_or_else(|| NO_RAW.to_string()),
        });
    };

    let grid = to_grid(&state.layer, lw as u64, lh as u64, w, h, factor)?;
    let sdr = sdr_luma_grid(rgb, hu, wu, factor)?;
    let result = match compute_gain(&sdr.v, &grid.raw_y, &grid.clip, grid.gh, grid.gw, params) {
        Ok(r) => r,
        Err(e) if is_unsupported(&e) => {
            return Ok(FinishOutput::Skipped {
                bytes: payload.to_vec(),
                reason: e.kind.to_string(),
            });
        }
        Err(e) => return Err(e),
    };
    let codes = quantise_gain(&result.delta, &result.meta);
    let gw_u32 = u32::try_from(grid.gw).map_err(|_| bad_geometry())?;
    let gh_u32 = u32::try_from(grid.gh).map_err(|_| bad_geometry())?;
    let q_i32 = i32::try_from(params.quality)
        .map_err(|_| CoreError::new("bad_quality", "品质要在 1 到 100 之间。"))?;
    let gain_jpeg = encode_luma_jpeg(&codes, gw_u32, gh_u32, q_i32)?;
    let gain_max_stops = py_round(result.meta.gain_max, 3)?;

    match gxr_hdr::assemble(payload, &gain_jpeg, &result.meta) {
        Ok(bytes) => Ok(FinishOutput::Hdr {
            bytes,
            gain_max_stops,
        }),
        Err(e) if e.kind == "container" => Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: "container".to_string(),
        }),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gxr_jpeg::encode_jpeg;

    #[test]
    fn finish_returns_not_jpeg_problem_no_curve_and_container_reasons() {
        let rgb = vec![128_u8; 8 * 8 * 3];
        let payload = encode_jpeg(&rgb, 8, 8, 92, "{}").unwrap();
        let inactive = HdrTrackState::inactive("not_aligned");
        let p = Params::default();

        let png = finish(&payload, &inactive, "png", &rgb, 8, 8, &p).unwrap();
        assert_eq!(
            png,
            FinishOutput::Skipped {
                bytes: payload.clone(),
                reason: "not_jpeg".to_string(),
            }
        );

        let no_raw = finish(&payload, &inactive, "jpeg", &rgb, 8, 8, &p).unwrap();
        assert_eq!(
            no_raw,
            FinishOutput::Skipped {
                bytes: payload.clone(),
                reason: "not_aligned".to_string(),
            }
        );

        // Active flat scene triggers `no_curve`
        let active = HdrTrackState {
            active: true,
            shape: Some([8, 8, 2]),
            problem: None,
            layer: vec![0.1_f32; 8 * 8 * 2],
        };
        let dark = finish(&payload, &active, "jpeg", &rgb, 8, 8, &p).unwrap();
        assert_eq!(
            dark,
            FinishOutput::Skipped {
                bytes: payload,
                reason: "no_curve".to_string(),
            }
        );
    }

    #[test]
    fn finish_produces_hdr_and_maps_container_errors() {
        let (w, h) = (128_usize, 128_usize);
        let mut rgb = vec![0_u8; w * h * 3];
        let mut layer = vec![0.0_f32; w * h * 2];
        for y in 0..h {
            for x in 0..w {
                let hi = x >= w / 2;
                let sdr: u8 = if hi { 220 } else { 80 };
                let idx = (y * w + x) * 3;
                rgb[idx] = sdr;
                rgb[idx + 1] = sdr;
                rgb[idx + 2] = sdr;
                let lidx = (y * w + x) * 2;
                layer[lidx] = if hi { 2.5 } else { 0.08 };
                layer[lidx + 1] = 0.0;
            }
        }
        let state = HdrTrackState {
            active: true,
            shape: Some([h, w, 2]),
            problem: None,
            layer,
        };
        let p = Params::default();
        let bad = finish(b"not-a-jpeg", &state, "jpeg", &rgb, w as u64, h as u64, &p).unwrap();
        assert_eq!(
            bad,
            FinishOutput::Skipped {
                bytes: b"not-a-jpeg".to_vec(),
                reason: "container".to_string(),
            }
        );

        let payload = encode_jpeg(&rgb, w as u32, h as u32, 92, "{}").unwrap();
        let ok = finish(&payload, &state, "jpeg", &rgb, w as u64, h as u64, &p).unwrap();
        assert!(matches!(ok, FinishOutput::Hdr { .. }));
        assert!(ok.bytes().len() > payload.len());
    }

    #[test]
    fn to_grid_masks_partial_nan_blocks_and_resizes_with_box() {
        // 4x4 layer with factor 4 -> 1x1 block; one NaN pixel makes w = 15/16 < 0.999 -> NaN.
        let mut layer = vec![0.5_f32; 4 * 4 * 2];
        layer[1] = 1.0; // clip at (0, 0)
        let out_valid = to_grid(&layer, 4, 4, 4, 4, 4).unwrap();
        assert_eq!((out_valid.gh, out_valid.gw), (1, 1));
        assert_eq!(out_valid.raw_y[0], 0.5);
        assert!(out_valid.clip[0]);

        layer[0] = f32::NAN;
        let out_nan = to_grid(&layer, 4, 4, 4, 4, 4).unwrap();
        assert!(out_nan.raw_y[0].is_nan());

        // Resizing 16x16 -> 8x8 via F-mode BOX downscale
        let mut big = Vec::with_capacity(16 * 16 * 2);
        for _ in 0..(16 * 16) {
            big.push(0.25_f32);
            big.push(0.0_f32);
        }
        let resized = to_grid(&big, 16, 16, 8, 8, 4).unwrap();
        assert_eq!((resized.gh, resized.gw), (2, 2));
        assert!(resized.raw_y.iter().all(|&v| (v - 0.25).abs() < 1e-6));
        assert!(resized.clip.iter().all(|&c| !c));
    }
}
