//! HDR track exports (wave-2 unit hdr-track): RAW luminance layer across finish stages and
//! gainmap preparation/wrapping. All numeric parameters arrive as `f64` and are checked before
//! casting (UNIT-COMMON "WASM number parameters").

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_export::{
    FinishOutput, HdrTrackState, begin, box_track, checked_box_dim_f64, checked_box_offset_f64,
    checked_dim_f64, checked_factor_f64, checked_nonneg_u64_f64, crop_track, finish, frame_track,
    parse_params_opt, parse_rect4_json, stamp_track, to_grid, warp_track,
};
use gxr_finish::geometry::bounds::too_big;
use gxr_finish::geometry::{crop_valid_f32_u64, parse_geometry, warp_float_u64};
use gxr_hdr::{ensure_finite, f32_le_bytes};
use gxr_raw::{CoreError, Json, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

fn to_u32(v: usize) -> Result<u32, JsValue> {
    u32::try_from(v).map_err(|_| rejected(too_big()))
}

/// Float32 layer output for direct `warp_float` / `crop_valid` parity.
#[wasm_bindgen]
pub struct HdrTrackLayer {
    height: u32,
    width: u32,
    channels: u32,
    report: String,
    data: Vec<f32>,
}

#[wasm_bindgen]
impl HdrTrackLayer {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn channels(&self) -> u32 {
        self.channels
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn bytes(self) -> Result<Vec<u8>, JsValue> {
        f32_le_bytes(&self.data).map_err(rejected)
    }
}

/// Stateless `HdrTrackState` result: JSON metadata plus row-major `(h, w, 2)` float32 payload.
#[wasm_bindgen]
pub struct HdrTrackResult {
    active: bool,
    height: u32,
    width: u32,
    state: String,
    layer: Vec<f32>,
}

#[wasm_bindgen]
impl HdrTrackResult {
    #[wasm_bindgen(getter)]
    pub fn active(&self) -> bool {
        self.active
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn state(&self) -> String {
        self.state.clone()
    }

    pub fn layer_bytes(self) -> Result<Vec<u8>, JsValue> {
        if !self.active {
            return Ok(Vec::new());
        }
        f32_le_bytes(&self.layer).map_err(rejected)
    }
}

fn wrap_track_state(st: HdrTrackState) -> Result<HdrTrackResult, JsValue> {
    let meta = st.to_meta_json();
    ensure_finite(&meta).map_err(rejected)?;
    let (h, w) = match st.shape {
        Some([lh, lw, 2]) if st.active => (to_u32(lh)?, to_u32(lw)?),
        _ => (0, 0),
    };
    Ok(HdrTrackResult {
        active: st.active,
        height: h,
        width: w,
        state: format!("{}\n", meta.render()),
        layer: st.layer,
    })
}

/// Output of `hdrtrack_to_grid`.
#[wasm_bindgen]
pub struct HdrToGridResult {
    gh: u32,
    gw: u32,
    report: String,
    raw_y: Vec<f32>,
    clip: Vec<u8>,
}

#[wasm_bindgen]
impl HdrToGridResult {
    #[wasm_bindgen(getter)]
    pub fn gh(&self) -> u32 {
        self.gh
    }

    #[wasm_bindgen(getter)]
    pub fn gw(&self) -> u32 {
        self.gw
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn clip_bytes(&self) -> Vec<u8> {
        self.clip.clone()
    }

    pub fn raw_y_bytes(self) -> Result<Vec<u8>, JsValue> {
        f32_le_bytes(&self.raw_y).map_err(rejected)
    }
}

/// Output of `hdrtrack_finish`.
#[wasm_bindgen]
pub struct HdrFinishResult {
    hdr: bool,
    report: String,
    bytes: Vec<u8>,
}

#[wasm_bindgen]
impl HdrFinishResult {
    #[wasm_bindgen(getter)]
    pub fn hdr(&self) -> bool {
        self.hdr
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn bytes(self) -> Vec<u8> {
        self.bytes
    }
}

/// `gxr_studio/finish/geometry.py:warp_float` on `(h, w, channels)` float32.
#[wasm_bindgen]
pub fn hdrtrack_warp_float(
    layer: &[f32],
    w: f64,
    h: f64,
    channels: f64,
    geom_json: &str,
) -> Result<HdrTrackLayer, JsValue> {
    let wu = checked_dim_f64(w).map_err(rejected)?;
    let hu = checked_dim_f64(h).map_err(rejected)?;
    let cu = checked_dim_f64(channels).map_err(rejected)?;
    let geom = parse_geometry(geom_json).map_err(rejected)?;
    let out = warp_float_u64(layer, wu, hu, cu, &geom).map_err(rejected)?;
    let report = Json::object([
        ("dtype", Json::str("<f4")),
        (
            "shape",
            Json::ints(&[
                i64::try_from(out.height).unwrap_or(i64::MAX),
                i64::try_from(out.width).unwrap_or(i64::MAX),
                i64::try_from(out.channels).unwrap_or(i64::MAX),
            ]),
        ),
    ]);
    Ok(HdrTrackLayer {
        height: to_u32(out.height)?,
        width: to_u32(out.width)?,
        channels: to_u32(out.channels)?,
        report: format!("{}\n", report.render()),
        data: out.data,
    })
}

/// `gxr_studio/finish/geometry.py:crop_valid` on `(h, w, channels)` float32.
#[wasm_bindgen]
pub fn hdrtrack_crop_valid(
    layer: &[f32],
    w: f64,
    h: f64,
    channels: f64,
    rect_json: &str,
) -> Result<HdrTrackLayer, JsValue> {
    let wu = checked_dim_f64(w).map_err(rejected)?;
    let hu = checked_dim_f64(h).map_err(rejected)?;
    let cu = checked_dim_f64(channels).map_err(rejected)?;
    let rect = parse_rect4_json(rect_json).map_err(rejected)?;
    let out = crop_valid_f32_u64(layer, wu, hu, cu, rect).map_err(rejected)?;
    let report = Json::object([
        ("dtype", Json::str("<f4")),
        (
            "shape",
            Json::ints(&[
                i64::try_from(out.height).unwrap_or(i64::MAX),
                i64::try_from(out.width).unwrap_or(i64::MAX),
                i64::try_from(out.channels).unwrap_or(i64::MAX),
            ]),
        ),
    ]);
    Ok(HdrTrackLayer {
        height: to_u32(out.height)?,
        width: to_u32(out.width)?,
        channels: to_u32(out.channels)?,
        report: format!("{}\n", report.render()),
        data: out.data,
    })
}

/// `HdrTrack.begin(rgb)` from uint8 `(h, w, 3)` RGB, optional DNG bytes, optional paired JPEG bytes.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn hdrtrack_begin(
    rgb: &[u8],
    w: f64,
    h: f64,
    has_dng: bool,
    dng: &[u8],
    dng_name: &str,
    has_jpeg: bool,
    jpeg: &[u8],
    params_json: &str,
) -> Result<HdrTrackResult, JsValue> {
    let wu = checked_dim_f64(w).map_err(rejected)?;
    let hu = checked_dim_f64(h).map_err(rejected)?;
    let params = parse_params_opt(params_json).map_err(rejected)?;
    let dng_opt = if has_dng { Some(dng) } else { None };
    let jpg_opt = if has_jpeg { Some(jpeg) } else { None };
    let st = begin(rgb, wu, hu, dng_opt, dng_name, jpg_opt, &params).map_err(rejected)?;
    wrap_track_state(st)
}

/// `HdrTrack.warp(geom)`.
#[wasm_bindgen]
pub fn hdrtrack_warp(
    layer: &[f32],
    state_json: &str,
    geom_json: &str,
) -> Result<HdrTrackResult, JsValue> {
    let state = HdrTrackState::from_json_and_slice(layer, state_json).map_err(rejected)?;
    let out = warp_track(&state, geom_json).map_err(rejected)?;
    wrap_track_state(out)
}

/// `HdrTrack.box(x, y, w, h)`.
#[wasm_bindgen]
pub fn hdrtrack_box(
    layer: &[f32],
    state_json: &str,
    x: f64,
    y: f64,
    w: f64,
    h: f64,
) -> Result<HdrTrackResult, JsValue> {
    let state = HdrTrackState::from_json_and_slice(layer, state_json).map_err(rejected)?;
    let xu = checked_box_offset_f64(x).map_err(rejected)?;
    let yu = checked_box_offset_f64(y).map_err(rejected)?;
    let wu = checked_box_dim_f64(w).map_err(rejected)?;
    let hu = checked_box_dim_f64(h).map_err(rejected)?;
    let out = box_track(&state, xu, yu, wu, hu).map_err(rejected)?;
    wrap_track_state(out)
}

/// `HdrTrack.crop(rect)`.
#[wasm_bindgen]
pub fn hdrtrack_crop(
    layer: &[f32],
    state_json: &str,
    rect_json: &str,
) -> Result<HdrTrackResult, JsValue> {
    let state = HdrTrackState::from_json_and_slice(layer, state_json).map_err(rejected)?;
    let out = crop_track(&state, rect_json).map_err(rejected)?;
    wrap_track_state(out)
}

/// `HdrTrack.stamp(...)` from a caller-provided `(h, w)` uint8 mask.
#[wasm_bindgen]
pub fn hdrtrack_stamp(
    layer: &[f32],
    state_json: &str,
    mask: &[u8],
    w: f64,
    h: f64,
) -> Result<HdrTrackResult, JsValue> {
    let state = HdrTrackState::from_json_and_slice(layer, state_json).map_err(rejected)?;
    let wu = checked_nonneg_u64_f64(w).map_err(rejected)?;
    let hu = checked_nonneg_u64_f64(h).map_err(rejected)?;
    let out = stamp_track(&state, mask, wu, hu).map_err(rejected)?;
    wrap_track_state(out)
}

/// `HdrTrack.frame(...)` from a `FrameLayout` JSON (`null` when `style == "none"`).
#[wasm_bindgen]
pub fn hdrtrack_frame(
    layer: &[f32],
    state_json: &str,
    layout_json: &str,
) -> Result<HdrTrackResult, JsValue> {
    let state = HdrTrackState::from_json_and_slice(layer, state_json).map_err(rejected)?;
    let out = frame_track(&state, layout_json).map_err(rejected)?;
    wrap_track_state(out)
}

/// `_to_grid(layer, (out_w, out_h), factor)`.
#[wasm_bindgen]
pub fn hdrtrack_to_grid(
    layer: &[f32],
    w: f64,
    h: f64,
    out_w: f64,
    out_h: f64,
    factor: f64,
) -> Result<HdrToGridResult, JsValue> {
    let wu = checked_dim_f64(w).map_err(rejected)?;
    let hu = checked_dim_f64(h).map_err(rejected)?;
    let owu = checked_dim_f64(out_w).map_err(rejected)?;
    let ohu = checked_dim_f64(out_h).map_err(rejected)?;
    let f = checked_factor_f64(factor).map_err(rejected)?;
    let grid = to_grid(layer, wu, hu, owu, ohu, f).map_err(rejected)?;
    let report = Json::object([
        ("dtype", Json::str("<f4")),
        (
            "shape",
            Json::ints(&[
                i64::try_from(grid.gh).unwrap_or(i64::MAX),
                i64::try_from(grid.gw).unwrap_or(i64::MAX),
            ]),
        ),
    ]);
    Ok(HdrToGridResult {
        gh: to_u32(grid.gh)?,
        gw: to_u32(grid.gw)?,
        report: format!("{}\n", report.render()),
        raw_y: grid.raw_y,
        clip: grid.clip.into_iter().map(u8::from).collect(),
    })
}

/// Unified `hdr_export.finish`: computes gainmap, encodes grayscale JPEG, and wraps `payload`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn hdrtrack_finish(
    payload: &[u8],
    layer: &[f32],
    state_json: &str,
    fmt: &str,
    rgb: &[u8],
    w: f64,
    h: f64,
    params_json: &str,
) -> Result<HdrFinishResult, JsValue> {
    let state = HdrTrackState::from_json_and_slice(layer, state_json).map_err(rejected)?;
    let wu = checked_dim_f64(w).map_err(rejected)?;
    let hu = checked_dim_f64(h).map_err(rejected)?;
    let params = parse_params_opt(params_json).map_err(rejected)?;
    let out = finish(payload, &state, fmt, rgb, wu, hu, &params).map_err(rejected)?;
    let report = out.report_json().map_err(rejected)?;
    let (hdr, bytes) = match out {
        FinishOutput::Skipped { bytes, .. } => (false, bytes),
        FinishOutput::Hdr { bytes, .. } => (true, bytes),
    };
    Ok(HdrFinishResult {
        hdr,
        report: format!("{}\n", report.render()),
        bytes,
    })
}
