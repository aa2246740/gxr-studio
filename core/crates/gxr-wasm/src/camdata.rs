//! Camdata exports. Owner: wave-1 unit camdata.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_camdata::{
    ENTRY_BYTES, check_json_depth, entries_from_dat, knob_table_json, menu_json, parse_json,
};
use gxr_raw::error_json;

fn rejected(err: gxr_raw::CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

/// Parse `GXRCE.DAT` (and optional `GXRFX.BIN` for r5 generations).
///
/// Returns JSON identical to `gxr-cli camdata-parse` (with trailing newline):
/// `generation`, `units` (slot numbers + per-entry sha256), `looks`, `recipes`, `users`, `has_kv2`,
/// `kv2_sha256`. Use `camdata_entries` to retrieve the 24 x 552 entry bytes.
#[wasm_bindgen]
pub fn camdata_parse(dat_bytes: &[u8], library_bytes: Option<Vec<u8>>) -> Result<String, JsValue> {
    let (_dat, _entries, text) =
        parse_json(dat_bytes, library_bytes.as_deref()).map_err(rejected)?;
    Ok(format!("{text}\n"))
}

/// Return the 24 x 552 = 13248 entry bytes (unit 0..2, slot 1..8 in row-major order).
#[wasm_bindgen]
pub fn camdata_entries(
    dat_bytes: &[u8],
    library_bytes: Option<Vec<u8>>,
) -> Result<Vec<u8>, JsValue> {
    let (_dat, entries) =
        entries_from_dat(dat_bytes, library_bytes.as_deref()).map_err(rejected)?;
    let mut out = Vec::with_capacity(24 * ENTRY_BYTES);
    for e in &entries {
        out.extend_from_slice(e);
    }
    Ok(out)
}

/// Resolve the knob menu from embedded `final.json`, overridden by the DAT's kv block when present.
#[wasm_bindgen]
pub fn camdata_menu(
    dat_bytes: Option<Vec<u8>>,
    library_bytes: Option<Vec<u8>>,
) -> Result<String, JsValue> {
    let text = match dat_bytes.as_deref() {
        Some(raw) => menu_json(Some((raw, library_bytes.as_deref()))),
        None => menu_json(None),
    }
    .map_err(rejected)?;
    Ok(format!("{text}\n"))
}

/// Apply a standalone kv2/kv3 knob block over the embedded `final.json` menu.
#[wasm_bindgen]
pub fn camdata_knob_table(
    block_bytes: &[u8],
    mount_knobs_from_50mm: bool,
) -> Result<String, JsValue> {
    let text = knob_table_json(block_bytes, mount_knobs_from_50mm).map_err(rejected)?;
    Ok(format!("{text}\n"))
}

/// Validate JSON input (including the 128-level nesting limit) and return `{"ok":true}` + newline.
#[wasm_bindgen]
pub fn camdata_parse_json(json_bytes: &[u8]) -> Result<String, JsValue> {
    let text = check_json_depth(json_bytes).map_err(rejected)?;
    Ok(format!("{text}\n"))
}
