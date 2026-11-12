//! wasm-bindgen bindings of the GXR core (see docs/mobile.md).
//!
//! `dng_describe` prints the same bytes as `gxr-cli dng-describe` (JSON plus the trailing
//! newline `println!` adds). The file name is not part of a successful description; error text
//! that quotes the name uses `<bytes>` here and the path's base name in the CLI.
#![forbid(unsafe_code)]

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_raw::{Dng, error_json};

/// Core version, the same string `gxr-cli version` prints inside its JSON.
#[wasm_bindgen]
pub fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// `describe(open(bytes), with_scene=True)` as the CLI prints it, including the trailing newline.
#[wasm_bindgen]
pub fn dng_describe(bytes: &[u8]) -> String {
    let (_ok, text) = gxr_raw::describe_json(bytes, "<bytes>");
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
    let dng = Dng::open(bytes, "<bytes>").map_err(rejected)?;
    let codes = gxr_raw::half_res_codes(&dng).map_err(rejected)?;
    let oriented = gxr_raw::orient_array(&codes, dng.orientation).map_err(rejected)?;
    Ok(HalfCodes {
        height: u32::try_from(oriented.height).unwrap_or(u32::MAX),
        width: u32::try_from(oriented.width).unwrap_or(u32::MAX),
        values: oriented.values,
    })
}
