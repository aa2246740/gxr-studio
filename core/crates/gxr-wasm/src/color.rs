//! Colour exports. Owner: wave-1 unit color.
//!
//! `color_bake` and `color_apply` run the same `gxr-color` calls as `gxr-cli color-bake` and
//! `color-apply`, so the bytes match. Errors are thrown as the core error JSON text.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_raw::error_json;

fn rejected(err: gxr_raw::CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

fn rejected_resolve(err: gxr_color::resolve::ResolveError) -> JsValue {
    JsValue::from_str(&err.to_json().render())
}

/// A bake: the float32 table the shader reads, its meta JSON, and the summary
/// `gxr-cli color-bake` prints.
#[wasm_bindgen]
pub struct ColorBake {
    bytes: Vec<u8>,
    meta: String,
    summary: String,
}

#[wasm_bindgen]
impl ColorBake {
    /// `{"nx", "ny", "origin", "step", "input_coefficients"}`.
    #[wasm_bindgen(getter)]
    pub fn meta(&self) -> String {
        self.meta.clone()
    }

    /// `{"bytes", "sha256", "meta"}`, the CLI's stdout including the newline `println!` adds.
    #[wasm_bindgen(getter)]
    pub fn summary(&self) -> String {
        self.summary.clone()
    }

    /// The little-endian float32 table (`bake_bytes`). Consumes the object.
    pub fn bytes(self) -> Vec<u8> {
        self.bytes
    }
}

/// Bake `entry` for the unit whose original 04 entry is `stock`, from a calibration pack's
/// ctx npz and ring JSON. `params_json` is `{"step", "knobs"}` (both optional); `menu_json`
/// is `final.json` (may be empty when `knobs` is absent).
#[wasm_bindgen]
pub fn color_bake(
    entry: &[u8],
    stock: &[u8],
    menu_json: &[u8],
    ctx_npz: &[u8],
    ring_json: &[u8],
    params_json: &[u8],
) -> Result<ColorBake, JsValue> {
    let menu = if menu_json.is_empty() {
        None
    } else {
        Some(menu_json)
    };
    let (bytes, meta) =
        gxr_color::bake_with_params(entry, stock, menu, ctx_npz, ring_json, params_json)
            .map_err(rejected)?;
    let summary = format!("{}\n", gxr_color::bake_summary(&bytes, &meta));
    Ok(ColorBake {
        bytes,
        meta,
        summary,
    })
}

/// `chroma_u8` of float32 codes (RGB interleaved) through a bake. `params_json` is
/// `{"adjust", "curves"}` (both optional).
#[wasm_bindgen]
pub fn color_apply(
    bake_bin: &[u8],
    meta_json: &[u8],
    codes_f32: &[f32],
    params_json: &[u8],
) -> Result<Vec<u8>, JsValue> {
    gxr_color::apply_with_params(bake_bin, meta_json, codes_f32, params_json).map_err(rejected)
}

/// Recipe and knob listing (`{"units": [...]}`) from optional `GXRCE.DAT` (and optional `GXRFX.BIN`).
#[wasm_bindgen]
pub fn color_listing(
    dat_bytes: Option<Vec<u8>>,
    library_bytes: Option<Vec<u8>>,
) -> Result<String, JsValue> {
    let dat = dat_bytes
        .as_deref()
        .map(|raw| (raw, library_bytes.as_deref()));
    let text = gxr_color::resolve::listing_json(dat).map_err(rejected_resolve)?;
    Ok(format!("{text}\n"))
}

/// Knob controls (`{"controls": [...]}`) for `(unit, slot)` from optional `GXRCE.DAT` (and optional `GXRFX.BIN`).
#[wasm_bindgen]
pub fn color_controls(
    dat_bytes: Option<Vec<u8>>,
    unit: i32,
    slot: i32,
    library_bytes: Option<Vec<u8>>,
) -> Result<String, JsValue> {
    let dat = dat_bytes
        .as_deref()
        .map(|raw| (raw, library_bytes.as_deref()));
    let text = gxr_color::resolve::controls_json(dat, i64::from(unit), i64::from(slot))
        .map_err(rejected_resolve)?;
    Ok(format!("{text}\n"))
}

/// Resolve a `recipe` JSON object against `GXRCE.DAT` (and optional `GXRFX.BIN`).
#[wasm_bindgen]
pub fn color_resolve(
    dat_bytes: &[u8],
    recipe_json: &[u8],
    library_bytes: Option<Vec<u8>>,
) -> Result<String, JsValue> {
    let text =
        gxr_color::resolve::resolve_recipe_json(dat_bytes, library_bytes.as_deref(), recipe_json)
            .map_err(rejected_resolve)?;
    Ok(format!("{text}\n"))
}
