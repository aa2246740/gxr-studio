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
    JsValue::from_str(&err.to_json())
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

fn checked_unit_slot(unit: f64, slot: f64) -> Option<(i64, i64)> {
    if !unit.is_finite()
        || !slot.is_finite()
        || unit.fract() != 0.0
        || slot.fract() != 0.0
        || !(0.0..=2.0).contains(&unit)
        || !(1.0..=8.0).contains(&slot)
    {
        return None;
    }
    Some((unit as i64, slot as i64))
}

/// Knob controls (`{"controls": [...]}`) for `(unit, slot)` from optional `GXRCE.DAT` (and optional `GXRFX.BIN`).
#[wasm_bindgen]
pub fn color_controls(
    dat_bytes: Option<Vec<u8>>,
    unit: f64,
    slot: f64,
    library_bytes: Option<Vec<u8>>,
) -> Result<String, JsValue> {
    let dat = dat_bytes
        .as_deref()
        .map(|raw| (raw, library_bytes.as_deref()));
    let (u, s) = checked_unit_slot(unit, slot).unwrap_or((-1, -1));
    let text = gxr_color::resolve::controls_json(dat, u, s).map_err(rejected_resolve)?;
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

/// The pack's files as one byte buffer plus `{"name": [offset, length], ...}`. Every range is
/// checked against the buffer before it is used.
fn pack_files(
    index_json: &[u8],
    blob: &[u8],
) -> Result<Vec<(String, Vec<u8>)>, gxr_raw::CoreError> {
    let bad = || gxr_raw::CoreError::new("value", "calibration file index");
    let index = gxr_color::parse_json(index_json)?;
    let gxr_color::Value::Object(pairs) = index else {
        return Err(bad());
    };
    let mut files = Vec::with_capacity(pairs.len());
    for (name, range) in pairs {
        let span = range.as_array().ok_or_else(bad)?;
        let [offset, length] = span else {
            return Err(bad());
        };
        let start = offset
            .as_int()
            .and_then(|v| usize::try_from(v).ok())
            .ok_or_else(bad)?;
        let len = length
            .as_int()
            .and_then(|v| usize::try_from(v).ok())
            .ok_or_else(bad)?;
        let end = start
            .checked_add(len)
            .filter(|&e| e <= blob.len())
            .ok_or_else(bad)?;
        files.push((name, blob[start..end].to_vec()));
    }
    Ok(files)
}

/// `model_for(home, iso, unit=unit)` on a calibration pack held in memory: the same checks as
/// `gxr-cli color-calib-check --dir` (version, unit, stock-04 fingerprint, file list, sha256),
/// the ISO pick, then the model it builds. `manifest` is `manifest.json`; the other files come
/// as `blob` plus `index_json` (`{"name": [offset, length]}`). Returns the CLI's stdout.
#[wasm_bindgen]
pub fn color_calib_check(
    manifest: &[u8],
    index_json: &[u8],
    blob: &[u8],
    stock04: &[u8],
    unit: &str,
    iso: f64,
) -> Result<String, JsValue> {
    // The same number rule as `gxr-cli color-calib-check` (gxr_color::checked_iso).
    let iso = gxr_color::checked_iso(iso).map_err(rejected)?;
    let files = pack_files(index_json, blob).map_err(rejected)?;
    let read = |name: &str| {
        files
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, b)| b.clone())
    };
    let (pick, ctx, ring) =
        gxr_color::model_for(manifest, stock04, iso, unit, &read).map_err(rejected)?;
    let model = gxr_color::unit_model(stock04, &ctx, &ring).map_err(rejected)?;
    let rows = i64::try_from(model.coefficients.rows).map_or(i64::MAX, |v| v);
    let json = gxr_raw::Json::object([
        ("iso", gxr_raw::Json::Int(pick.iso)),
        ("calibrated", gxr_raw::Json::Bool(pick.calibrated)),
        ("model_iso", gxr_raw::Json::Int(pick.model_iso)),
        ("ctx", gxr_raw::Json::str(pick.ctx)),
        ("ring", gxr_raw::Json::str(pick.ring)),
        ("rows", gxr_raw::Json::Int(rows)),
    ]);
    Ok(format!("{}\n", json.render()))
}

/// The checks of a calibration pack that don't need the camera data (`gxr-cli color-calib-check
/// --dir` without `--unit`): version, the unit set, and every listed unit's file list and sha256.
/// The phone runs it before it saves an imported pack. Returns `{"units": [...]}` like the CLI.
#[wasm_bindgen]
pub fn color_calib_pack(
    manifest: &[u8],
    index_json: &[u8],
    blob: &[u8],
) -> Result<String, JsValue> {
    let files = pack_files(index_json, blob).map_err(rejected)?;
    let read = |name: &str| {
        files
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, b)| b.clone())
    };
    let units = gxr_color::check_pack(manifest, &read).map_err(rejected)?;
    let json = gxr_raw::Json::object([(
        "units",
        gxr_raw::Json::Array(units.into_iter().map(gxr_raw::Json::Str).collect()),
    )]);
    Ok(format!("{}\n", json.render()))
}

#[cfg(test)]
mod tests {
    use super::pack_files;

    #[test]
    fn iso_must_be_a_safe_whole_number() {
        assert_eq!(gxr_color::checked_iso(200.0).ok(), Some(200));
        assert_eq!(gxr_color::checked_iso(-1.0).ok(), Some(-1));
        assert_eq!(
            gxr_color::checked_iso(4_294_967_297.0).ok(),
            Some(4_294_967_297)
        );
        assert_eq!(
            gxr_color::checked_iso(9_007_199_254_740_991.0).ok(),
            Some(9_007_199_254_740_991)
        );
        for bad in [
            200.5,
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
            1e300,
            9_007_199_254_740_992.0,
        ] {
            assert!(gxr_color::checked_iso(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn file_ranges_are_checked() {
        let blob = b"abcdef";
        let ok = pack_files(br#"{"a": [0, 2], "b": [2, 4]}"#, blob).unwrap();
        assert_eq!(
            ok,
            vec![
                ("a".to_string(), b"ab".to_vec()),
                ("b".to_string(), b"cdef".to_vec())
            ]
        );
        for bad in [
            &br#"{"a": [0, 7]}"#[..],
            br#"{"a": [6, 1]}"#,
            br#"{"a": [-1, 1]}"#,
            br#"{"a": [0]}"#,
            br#"{"a": [0, 1.5]}"#,
            br#"[]"#,
            br#"{"a": [18446744073709551615, 1]}"#,
        ] {
            assert!(
                pack_files(bad, blob).is_err(),
                "{}",
                String::from_utf8_lossy(bad)
            );
        }
    }
}
