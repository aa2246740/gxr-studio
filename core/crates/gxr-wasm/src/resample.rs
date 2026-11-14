//! Resample exports. Owner: wave-2 unit resample.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::{Filter, Mode, resample_f32_u64, resample_u8_u64};
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

/// Port of Pillow 12.2.0 `Image.resize((out_w, out_h), filter)` for 8-bpc images (`L`, `RGB`, `RGBA`).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn resample_u8(
    data: &[u8],
    w: u32,
    h: u32,
    channels: u32,
    mode: &str,
    filter: &str,
    out_w: u32,
    out_h: u32,
) -> Result<Vec<u8>, JsValue> {
    let parsed_mode = Mode::parse(mode).map_err(rejected)?;
    let parsed_filter = Filter::parse(filter).map_err(rejected)?;
    if usize::try_from(channels).ok() != Some(parsed_mode.channels()) {
        return Err(rejected(CoreError::new(
            "value_error",
            "channel count does not match mode",
        )));
    }
    resample_u8_u64(
        data,
        u64::from(w),
        u64::from(h),
        parsed_mode,
        parsed_filter,
        u64::from(out_w),
        u64::from(out_h),
    )
    .map_err(rejected)
}

/// Port of Pillow 12.2.0 `Image.resize((out_w, out_h), filter)` for 32-bpc `f32` images (`mode = "F"`).
#[wasm_bindgen]
pub fn resample_f32(
    data: &[f32],
    w: u32,
    h: u32,
    filter: &str,
    out_w: u32,
    out_h: u32,
) -> Result<Vec<f32>, JsValue> {
    let parsed_filter = Filter::parse(filter).map_err(rejected)?;
    resample_f32_u64(
        data,
        u64::from(w),
        u64::from(h),
        parsed_filter,
        u64::from(out_w),
        u64::from(out_h),
    )
    .map_err(rejected)
}
