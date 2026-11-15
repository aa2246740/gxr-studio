//! Port of `gxr_studio/finish/hdr_export.py` (`_to_grid`, `finish`).

use gxr_finish::geometry::bounds::{bad_geometry, checked_mask_dims};
use gxr_finish::geometry::{checked_float_layer_slice, try_vec_f32};
use gxr_hdr::{
    GainMeta, Params, block_mean, compute_gain, ensure_finite, is_unsupported, py_round,
    quantise_gain, sdr_luma_grid,
};
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
    _plane: &[f32],
    _src_w: usize,
    _src_h: usize,
    _dst_w: usize,
    _dst_h: usize,
) -> Result<Vec<f32>, CoreError> {
    // Pending `gxr-finish::resample` merge into `origin/main` (Wave-2 W2-3).
    Err(CoreError::new(
        "unsupported",
        "to_grid BOX resize 等待 resample 合并",
    ))
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
    clip.try_reserve_exact(gn_u)
        .map_err(|_| gxr_finish::geometry::bounds::too_big())?;

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

/// First half of `hdr_export.finish`: validates format/track, runs `_to_grid`, `compute_gain`,
/// and `quantise_gain`, returning either a skip reason or the uint8 gain plane + `GainMeta`.
#[derive(Clone, Debug)]
pub enum FinishPrepared {
    Skipped {
        reason: String,
    },
    Ready {
        codes: Vec<u8>,
        gh: usize,
        gw: usize,
        quality: i64,
        meta: GainMeta,
        gain_max_stops: f64,
    },
}

impl FinishPrepared {
    pub fn report_json(&self) -> Result<Json, CoreError> {
        let json = match self {
            Self::Skipped { reason } => {
                Json::object([("hdr", Json::Bool(false)), ("reason", Json::str(reason))])
            }
            Self::Ready {
                codes: _,
                gh,
                gw,
                quality,
                meta,
                gain_max_stops,
            } => {
                let meta_json = Json::object([
                    ("gain_min", Json::Float(meta.gain_min)),
                    ("gain_max", Json::Float(meta.gain_max)),
                    ("gamma", Json::Float(meta.gamma)),
                    ("offset_sdr", Json::Float(meta.offset_sdr)),
                    ("offset_hdr", Json::Float(meta.offset_hdr)),
                    ("cap_min", Json::Float(meta.cap_min)),
                    ("cap_max", Json::Float(meta.cap_max)),
                ]);
                Json::object([
                    ("hdr", Json::Bool(true)),
                    ("gain_max_stops", Json::Float(*gain_max_stops)),
                    (
                        "gain_shape",
                        Json::ints(&[
                            i64::try_from(*gh).unwrap_or(i64::MAX),
                            i64::try_from(*gw).unwrap_or(i64::MAX),
                        ]),
                    ),
                    ("quality", Json::Int(*quality)),
                    ("meta", meta_json),
                ])
            }
        };
        ensure_finite(&json)?;
        Ok(json)
    }
}

/// Port of `gxr_studio/finish/hdr_export.py:147 finish` up to gainmap quantisation.
pub fn finish_prepare(
    state: &HdrTrackState,
    fmt: &str,
    rgb: &[u8],
    w: u64,
    h: u64,
    params: &Params,
) -> Result<FinishPrepared, CoreError> {
    let (wu, hu) = checked_rgb_bytes(rgb, w, h)?;
    let factor = usize::try_from(params.map_factor)
        .ok()
        .filter(|&f| f > 0)
        .ok_or_else(bad_geometry)?;

    if fmt != "jpeg" {
        return Ok(FinishPrepared::Skipped {
            reason: NOT_JPEG.to_string(),
        });
    }
    if !state.active {
        return Ok(FinishPrepared::Skipped {
            reason: state.problem.clone().unwrap_or_else(|| NO_RAW.to_string()),
        });
    }
    let Some([lh, lw, 2]) = state.shape else {
        return Err(CoreError::new("bad_input", "HDR 轨道状态格式不对"));
    };

    let grid = to_grid(&state.layer, lw as u64, lh as u64, w, h, factor)?;
    let sdr = sdr_luma_grid(rgb, hu, wu, factor)?;
    let result = match compute_gain(&sdr.v, &grid.raw_y, &grid.clip, grid.gh, grid.gw, params) {
        Ok(r) => r,
        Err(e) if is_unsupported(&e) => {
            return Ok(FinishPrepared::Skipped {
                reason: e.kind.to_string(),
            });
        }
        Err(e) => return Err(e),
    };
    let codes = quantise_gain(&result.delta, &result.meta);
    let gain_max_stops = py_round(result.meta.gain_max, 3)?;
    Ok(FinishPrepared::Ready {
        codes,
        gh: grid.gh,
        gw: grid.gw,
        quality: params.quality,
        meta: result.meta,
        gain_max_stops,
    })
}

/// Final wrapped result of `hdr_export.finish`.
#[derive(Clone, Debug, PartialEq)]
pub enum FinishWrapped {
    Skipped { bytes: Vec<u8>, reason: String },
    Hdr { bytes: Vec<u8>, gain_max_stops: f64 },
}

impl FinishWrapped {
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

/// Second half of `hdr_export.finish`: wraps `payload` and encoded `gain_jpeg` via
/// `gxr_hdr::assemble`, mapping `ContainerError` (`kind == "container"`) to
/// `Skipped { reason: "container" }` with `payload` unchanged.
pub fn finish_wrap(
    payload: &[u8],
    gain_jpeg: &[u8],
    meta: &GainMeta,
    gain_max_stops: f64,
) -> Result<FinishWrapped, CoreError> {
    if !gain_max_stops.is_finite() {
        return Err(bad_geometry());
    }
    match gxr_hdr::assemble(payload, gain_jpeg, meta) {
        Ok(bytes) => Ok(FinishWrapped::Hdr {
            bytes,
            gain_max_stops,
        }),
        Err(e) if e.kind == "container" => Ok(FinishWrapped::Skipped {
            bytes: payload.to_vec(),
            reason: "container".to_string(),
        }),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finish_prepare_returns_not_jpeg_and_problem_reasons() {
        let rgb = vec![128_u8; 8 * 8 * 3];
        let inactive = HdrTrackState::inactive("not_aligned");
        let p = Params::default();

        let png = finish_prepare(&inactive, "png", &rgb, 8, 8, &p).unwrap();
        assert!(matches!(png, FinishPrepared::Skipped { ref reason } if reason == "not_jpeg"));

        let no_raw = finish_prepare(&inactive, "jpeg", &rgb, 8, 8, &p).unwrap();
        assert!(
            matches!(no_raw, FinishPrepared::Skipped { ref reason } if reason == "not_aligned")
        );

        // Active flat dark scene triggers `no_curve`
        let active = HdrTrackState {
            active: true,
            shape: Some([8, 8, 2]),
            problem: None,
            layer: vec![0.1_f32; 8 * 8 * 2],
        };
        let dark = finish_prepare(&active, "jpeg", &rgb, 8, 8, &p).unwrap();
        assert!(matches!(dark, FinishPrepared::Skipped { ref reason } if reason == "no_curve"));
    }

    #[test]
    fn to_grid_masks_partial_nan_blocks_and_clip() {
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
    }

    #[test]
    fn finish_wrap_returns_container_reason_on_bad_jpeg() {
        let out = finish_wrap(b"not-a-jpeg", b"bad", &GainMeta::default(), 1.23).unwrap();
        assert_eq!(
            out,
            FinishWrapped::Skipped {
                bytes: b"not-a-jpeg".to_vec(),
                reason: "container".to_string(),
            }
        );
    }
}
