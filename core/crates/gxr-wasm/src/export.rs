//! Export metadata exports. Owner: wave-2 unit export-meta.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_export::meta::{
    DEFAULT_NOW_YEAR, dng_preview_location, photo_meta_from_bytes, read_source_exif,
};
use gxr_raw::{Json, error_json};

fn rejected(err: &gxr_raw::CoreError) -> JsValue {
    JsValue::from_str(&error_json(err).render())
}

/// Extract source EXIF `{base, sub}` JSON matching `gxr_studio/export.py:write_with_exif`.
#[wasm_bindgen]
pub fn export_meta_exif(bytes: &[u8]) -> Result<String, JsValue> {
    let text = read_source_exif(bytes).to_json().render();
    Ok(format!("{text}\n"))
}

/// Format frame metadata strings and ISO `taken` / `file_time` from JPG and/or DNG bytes.
///
/// `tz_offset_min` is minutes east of UTC (`local = UTC + tz_offset_min`).
#[wasm_bindgen]
pub fn export_meta_photo(
    jpg_or_null: Option<Vec<u8>>,
    dng_or_null: Option<Vec<u8>>,
    name: &str,
    mtime_ms: Option<f64>,
    tz_offset_min: Option<i32>,
) -> Result<String, JsValue> {
    let res = photo_meta_from_bytes(
        jpg_or_null.as_deref(),
        dng_or_null.as_deref(),
        name,
        mtime_ms,
        tz_offset_min.unwrap_or(0),
        DEFAULT_NOW_YEAR,
    )
    .map_err(|e| rejected(&e))?;
    Ok(format!("{}\n", res.to_json().render()))
}

/// Find `{offset, length}` of the largest Pillow-decodable embedded JPEG preview in a DNG, or `null`.
#[wasm_bindgen]
pub fn export_meta_preview(dng: &[u8]) -> Result<String, JsValue> {
    let json = dng_preview_location(dng).map_or(Json::Null, |r| r.to_json());
    Ok(format!("{}\n", json.render()))
}
