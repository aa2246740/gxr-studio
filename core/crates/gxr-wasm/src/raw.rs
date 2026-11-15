//! RAW exports. Owner: the foundation raw port, plus wave-1 raw-render.
//!
//! `dng_describe` prints the same bytes as `gxr-cli dng-describe` (JSON plus the trailing
//! newline `println!` adds). The file name is not part of a successful description; error text
//! that quotes the name uses `<bytes>` here and the path's base name in the CLI.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_raw::{Dng, error_json};

/// `describe(open(bytes), with_scene=True)` as the CLI prints it, including the trailing newline.
#[wasm_bindgen]
pub fn dng_describe(bytes: &[u8]) -> String {
    dng_describe_named(bytes, "<bytes>")
}

/// `dng_describe` using `name` in error text, so a parity run can match the CLI's base name.
#[wasm_bindgen]
pub fn dng_describe_named(bytes: &[u8], name: &str) -> String {
    let (_ok, text) = gxr_raw::describe_json(bytes, name);
    format!("{text}\n")
}

/// Half-resolution float32 codes, oriented, in row-major `(height, width, 3)` order.
#[wasm_bindgen]
pub struct HalfCodes {
    height: u32,
    width: u32,
    values: Vec<f32>,
}

#[wasm_bindgen]
impl HalfCodes {
    /// Rows of the oriented half-resolution image.
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    /// Columns of the oriented half-resolution image.
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// The float32 samples. Consumes the object.
    pub fn values(self) -> Vec<f32> {
        self.values
    }
}

fn rejected(err: gxr_raw::CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

/// `orient_array(half_res_codes(dng), dng.orientation)`, the array `bin.wgsl` reproduces.
#[wasm_bindgen]
pub fn dng_half_codes(bytes: &[u8]) -> Result<HalfCodes, JsValue> {
    dng_half_codes_named(bytes, "<bytes>")
}

/// `dng_half_codes` using `name` in error text.
#[wasm_bindgen]
pub fn dng_half_codes_named(bytes: &[u8], name: &str) -> Result<HalfCodes, JsValue> {
    let dng = Dng::open(bytes, name).map_err(rejected)?;
    let codes = gxr_raw::half_res_codes(&dng).map_err(rejected)?;
    let oriented = gxr_raw::orient_array(&codes, dng.orientation).map_err(rejected)?;
    let height = u32::try_from(oriented.height).map_err(|_| {
        rejected(gxr_raw::CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    })?;
    let width = u32::try_from(oriented.width).map_err(|_| {
        rejected(gxr_raw::CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    })?;
    Ok(HalfCodes {
        height,
        width,
        values: oriented.values,
    })
}

const I64_MIN_F64: f64 = -9_223_372_036_854_775_808.0;
const I64_UPPER_BOUND_F64: f64 = 9_223_372_036_854_775_808.0;

fn checked_row_i64(v: f64) -> Option<i64> {
    if v.is_finite() && v.fract() == 0.0 && (I64_MIN_F64..I64_UPPER_BOUND_F64).contains(&v) {
        #[allow(clippy::cast_possible_truncation)]
        Some(v as i64)
    } else {
        None
    }
}

fn resolve_row_i64(v: f64, crop_y0: i64) -> Result<i64, gxr_raw::CoreError> {
    if let Some(row) = checked_row_i64(v) {
        return Ok(row);
    }
    (-1_i64)
        .checked_sub(crop_y0)
        .ok_or_else(|| gxr_raw::CoreError::new("index_error", "block outside the raw plane"))
}

/// White-balanced crop rows `[y0, y1)`, the same array as `gxr-cli dng-wb-block`.
#[wasm_bindgen]
pub fn dng_wb_block(bytes: &[u8], y0: f64, y1: f64) -> Result<HalfCodes, JsValue> {
    dng_wb_block_named(bytes, "<bytes>", y0, y1)
}

/// `dng_wb_block` with the file name the native CLI puts in error text.
#[wasm_bindgen]
pub fn dng_wb_block_named(
    bytes: &[u8],
    name: &str,
    y0: f64,
    y1: f64,
) -> Result<HalfCodes, JsValue> {
    let dng = Dng::open(bytes, name).map_err(rejected)?;
    let (cy0, _, _, _) = dng.crop_window();
    let y0_i64 = resolve_row_i64(y0, cy0).map_err(rejected)?;
    let y1_i64 = resolve_row_i64(y1, cy0).map_err(rejected)?;
    let block = gxr_raw::wb_mosaic_block(&dng, y0_i64, y1_i64).map_err(rejected)?;
    let height = u32::try_from(block.height).map_err(|_| {
        rejected(gxr_raw::CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    })?;
    let width = u32::try_from(block.width).map_err(|_| {
        rejected(gxr_raw::CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    })?;
    Ok(HalfCodes {
        height,
        width,
        values: block.values,
    })
}

/// Full-resolution neutral RGB `uint8` pixels, oriented, in row-major `(height, width, 3)` order.
#[wasm_bindgen]
pub struct NeutralImage {
    height: u32,
    width: u32,
    values: Vec<u8>,
}

#[wasm_bindgen]
impl NeutralImage {
    /// Rows of the oriented full-resolution image.
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    /// Columns of the oriented full-resolution image.
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Row-major `uint8` RGB pixels (`height * width * 3`). Consumes the object.
    pub fn values(self) -> Vec<u8> {
        self.values
    }
}

/// Full-resolution neutral RAW render (`render_full(dng, NEUTRAL)`), strip-wise internally.
#[wasm_bindgen]
pub fn dng_neutral(bytes: &[u8]) -> Result<NeutralImage, JsValue> {
    dng_neutral_named(bytes, "<bytes>")
}

/// `dng_neutral` with the file name the native CLI puts in error text.
#[wasm_bindgen]
pub fn dng_neutral_named(bytes: &[u8], name: &str) -> Result<NeutralImage, JsValue> {
    let dng = Dng::open(bytes, name).map_err(rejected)?;
    let img = gxr_raw::render_full(&dng).map_err(rejected)?;
    let height = u32::try_from(img.height).map_err(|_| {
        rejected(gxr_raw::CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    })?;
    let width = u32::try_from(img.width).map_err(|_| {
        rejected(gxr_raw::CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    })?;
    Ok(NeutralImage {
        height,
        width,
        values: img.values,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn row_validation_preserves_i64_and_rejects_bad_floats() {
        assert_eq!(checked_row_i64(0.0), Some(0));
        assert_eq!(checked_row_i64(-0.0), Some(0));
        assert_eq!(checked_row_i64(-1.0), Some(-1));
        assert_eq!(checked_row_i64(4_294_967_297.0), Some(4_294_967_297));
        assert_eq!(
            checked_row_i64(9_007_199_254_740_992.0),
            Some(9_007_199_254_740_992)
        );
        assert_eq!(checked_row_i64(1.5), None);
        assert_eq!(checked_row_i64(f64::NAN), None);
        assert_eq!(checked_row_i64(f64::INFINITY), None);
        assert_eq!(checked_row_i64(f64::NEG_INFINITY), None);
        assert_eq!(checked_row_i64(I64_UPPER_BOUND_F64), None);

        assert_eq!(resolve_row_i64(4_294_967_296.0, 0).unwrap(), 4_294_967_296);
        assert_eq!(resolve_row_i64(f64::NAN, 0).unwrap(), -1);
        assert_eq!(resolve_row_i64(f64::INFINITY, 5).unwrap(), -6);
        assert_eq!(resolve_row_i64(1.5, i64::MIN).unwrap(), i64::MAX);
    }
}
