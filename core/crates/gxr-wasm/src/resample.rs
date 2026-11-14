//! Resample exports. Owner: wave-2 unit resample.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::geometry::bounds::too_big;
use gxr_finish::{Filter, Mode, resample_f32_u64, resample_u8_u64};
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

fn dim_from_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 {
        return Err(CoreError::new(
            "value_error",
            "dimensions must be finite integers",
        ));
    }
    if v <= 0.0 {
        return Err(CoreError::new(
            "value_error",
            "height and width must be > 0",
        ));
    }
    if v > f64::from(i32::MAX) {
        return Err(too_big());
    }
    Ok(v as u64)
}

fn channels_from_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || v < 0.0 {
        return Err(CoreError::new(
            "value_error",
            "channel count must be a finite non-negative integer",
        ));
    }
    if v > f64::from(u32::MAX) {
        return Err(CoreError::new(
            "value_error",
            "channel count does not match mode",
        ));
    }
    Ok(v as u64)
}

/// Port of Pillow 12.2.0 `Image.resize((out_w, out_h), filter)` for 8-bpc images (`L`, `RGB`, `RGBA`).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn resample_u8(
    data: &[u8],
    w: f64,
    h: f64,
    channels: f64,
    mode: &str,
    filter: &str,
    out_w: f64,
    out_h: f64,
) -> Result<Vec<u8>, JsValue> {
    let parsed_mode = Mode::parse(mode).map_err(rejected)?;
    let parsed_filter = Filter::parse(filter).map_err(rejected)?;
    let w = dim_from_f64(w).map_err(rejected)?;
    let h = dim_from_f64(h).map_err(rejected)?;
    let ch = channels_from_f64(channels).map_err(rejected)?;
    if ch != parsed_mode.channels() as u64 {
        return Err(rejected(CoreError::new(
            "value_error",
            "channel count does not match mode",
        )));
    }
    let out_w = dim_from_f64(out_w).map_err(rejected)?;
    let out_h = dim_from_f64(out_h).map_err(rejected)?;
    resample_u8_u64(data, w, h, parsed_mode, parsed_filter, out_w, out_h).map_err(rejected)
}

/// Port of Pillow 12.2.0 `Image.resize((out_w, out_h), filter)` for 32-bpc `f32` images (`mode = "F"`).
#[wasm_bindgen]
pub fn resample_f32(
    data: &[f32],
    w: f64,
    h: f64,
    filter: &str,
    out_w: f64,
    out_h: f64,
) -> Result<Vec<f32>, JsValue> {
    let parsed_filter = Filter::parse(filter).map_err(rejected)?;
    let w = dim_from_f64(w).map_err(rejected)?;
    let h = dim_from_f64(h).map_err(rejected)?;
    let out_w = dim_from_f64(out_w).map_err(rejected)?;
    let out_h = dim_from_f64(out_h).map_err(rejected)?;
    resample_f32_u64(data, w, h, parsed_filter, out_w, out_h).map_err(rejected)
}
