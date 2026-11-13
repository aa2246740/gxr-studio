//! JPEG exports. Owner: wave-1 unit jpeg.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_raw::error_json;

/// Port of `gxr_studio/export.py:_finish` single JPEG encode with EXIF and sRGB ICC.
#[wasm_bindgen]
pub fn jpeg_encode(
    rgb_u8: &[u8],
    w: u32,
    h: u32,
    quality: i32,
    exif_json: &str,
) -> Result<Vec<u8>, JsValue> {
    gxr_jpeg::encode_jpeg(rgb_u8, w, h, quality, exif_json)
        .map_err(|err| JsValue::from_str(&error_json(&err).render()))
}
