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

/// White-balanced crop rows `[y0, y1)`, the same array as `gxr-cli dng-wb-block`.
#[wasm_bindgen]
pub fn dng_wb_block(bytes: &[u8], y0: i32, y1: i32) -> Result<HalfCodes, JsValue> {
    dng_wb_block_named(bytes, "<bytes>", y0, y1)
}

/// `dng_wb_block` with the file name the native CLI puts in error text.
#[wasm_bindgen]
pub fn dng_wb_block_named(
    bytes: &[u8],
    name: &str,
    y0: i32,
    y1: i32,
) -> Result<HalfCodes, JsValue> {
    let dng = Dng::open(bytes, name).map_err(rejected)?;
    let block = gxr_raw::wb_mosaic_block(&dng, i64::from(y0), i64::from(y1)).map_err(rejected)?;
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
