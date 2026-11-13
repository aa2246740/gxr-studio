//! Frame and stamp exports. Owner: wave-1 unit frame.
//!
//! Each call takes the request JSON and the spec JSON (the same files `gxr-cli frame-layout
//! --meta … --spec …` reads) and the faces as one buffer: `font_index_json` is
//! `[[name, length], ...]` and `fonts` is those files concatenated in that order. (`gxr-wasm`
//! has no `js-sys`, so a list of byte arrays cannot cross the boundary directly.) The result is
//! the CLI's stdout, trailing newline included; a core error is thrown as its JSON text.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_raw::{CoreError, Json, error_json};

fn rejected(err: &CoreError) -> JsValue {
    JsValue::from_str(&error_json(err).render())
}

fn run(
    meta_json: &str,
    spec_json: &str,
    font_index_json: &str,
    fonts: &[u8],
    layout: fn(&gxr_frame::Val, &gxr_frame::Val, &gxr_frame::Fonts) -> Result<Json, CoreError>,
) -> Result<String, JsValue> {
    let request = gxr_frame::parse_json(meta_json.as_bytes()).map_err(|e| rejected(&e))?;
    let spec = gxr_frame::parse_json(spec_json.as_bytes()).map_err(|e| rejected(&e))?;
    let faces = gxr_frame::fonts_from_pack(font_index_json, fonts).map_err(|e| rejected(&e))?;
    let out = layout(&request, &spec, &faces).map_err(|e| rejected(&e))?;
    Ok(format!("{}\n", out.render()))
}

/// `frame_layout(...).to_json()` (`null` for style `none`).
#[wasm_bindgen]
pub fn frame_layout(
    meta_json: &str,
    spec_json: &str,
    font_index_json: &str,
    fonts: &[u8],
) -> Result<String, JsValue> {
    run(
        meta_json,
        spec_json,
        font_index_json,
        fonts,
        gxr_frame::frame_json,
    )
}

/// `stamp_layout(...).to_json()` for `format_stamp`'s text (`null` when there is nothing to draw).
#[wasm_bindgen]
pub fn stamp_layout(
    meta_json: &str,
    spec_json: &str,
    font_index_json: &str,
    fonts: &[u8],
) -> Result<String, JsValue> {
    run(
        meta_json,
        spec_json,
        font_index_json,
        fonts,
        gxr_frame::stamp_json,
    )
}
