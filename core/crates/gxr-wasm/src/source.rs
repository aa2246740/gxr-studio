//! Export-source exports. Owner: wave-2 unit export-source.
//!
//! `source_plan` and `source_render` run the same `gxr_export::source` calls as
//! `gxr-cli source-plan` and `source-render`, so the bytes match. Errors are thrown as the
//! core error JSON text. A calibration pack is `calib_index_json` (`[[name, bytes], ...]`)
//! over the concatenated files, the way `frame_layout` takes fonts.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_export::source::{Request, pack_from_index, plan_json, render};
use gxr_raw::{CoreError, error_json};

fn rejected(err: &CoreError) -> JsValue {
    JsValue::from_str(&error_json(err).render())
}

fn some(bytes: &[u8]) -> Option<&[u8]> {
    if bytes.is_empty() { None } else { Some(bytes) }
}

/// `RenderService.plan` plus the renderer's labels: the `source-plan` stdout (with the
/// newline `println!` adds). An empty `dat`, `library` or `entry` means "not given".
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn source_plan(
    shot_json: &[u8],
    edit_json: &[u8],
    dat: &[u8],
    library: &[u8],
    entry: &[u8],
    calib_index_json: &[u8],
    calib_bytes: &[u8],
    binding: &str,
) -> Result<String, JsValue> {
    let pack = pack_from_index(calib_index_json, calib_bytes).map_err(|e| rejected(&e))?;
    let req = Request {
        shot: shot_json,
        edit: edit_json,
        dat: some(dat),
        library: some(library),
        entry: some(entry),
        pack: Some(&pack),
        binding,
    };
    let (ok, text) = plan_json(&req);
    if ok {
        Ok(format!("{text}\n"))
    } else {
        Err(JsValue::from_str(&text))
    }
}

/// `render_full(open_dng(dng), renderer)` for the recipe renderer of `entry` at `iso`:
/// uint8 `(h, w, 3)`, oriented, framed as `[h u32 LE][w u32 LE][rgb]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn source_render(
    dng: &[u8],
    name: &str,
    entry: &[u8],
    stock04: &[u8],
    unit: &str,
    calib_index_json: &[u8],
    calib_bytes: &[u8],
    iso: i32,
    adjust_json: &[u8],
) -> Result<Vec<u8>, JsValue> {
    let pack = pack_from_index(calib_index_json, calib_bytes).map_err(|e| rejected(&e))?;
    let tones: &[u8] = if adjust_json.is_empty() {
        b"{}"
    } else {
        adjust_json
    };
    let rgb = render(
        dng,
        name,
        entry,
        stock04,
        unit,
        &pack,
        i64::from(iso),
        tones,
    )
    .map_err(|e| rejected(&e))?;
    let too_big = || {
        rejected(&CoreError::raw_decode(
            "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
        ))
    };
    let h = u32::try_from(rgb.height).map_err(|_| too_big())?;
    let w = u32::try_from(rgb.width).map_err(|_| too_big())?;
    let mut out = Vec::new();
    out.try_reserve_exact(rgb.values.len().checked_add(8).ok_or_else(too_big)?)
        .map_err(|_| too_big())?;
    out.extend_from_slice(&h.to_le_bytes());
    out.extend_from_slice(&w.to_le_bytes());
    out.extend_from_slice(&rgb.values);
    Ok(out)
}
