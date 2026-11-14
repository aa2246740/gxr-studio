//! The two calls the phone makes, shared by `gxr-cli` and `gxr-wasm` so both give the same
//! bytes: bake a recipe, and apply a bake to codes.

use gxr_raw::{CoreError, Json, sha256::sha256_hex};

use crate::calib::unit_model;
use crate::chain::{STEP, bake_bytes, bake_chroma, chroma_u8, from_bytes};
use crate::json_in::{Value, parse};
use crate::knobs::apply;
use crate::model::Codes;
use crate::params::{bake_meta, knobs_request, meta_of, tones_of};
use crate::tones::{channel_gains, display_table};

/// Bake `entry` for the unit whose original 04 entry is `stock`.
///
/// `params` is `{"step": 0.375, "knobs": {...}}`, both optional. With `knobs` (the
/// `color-knobs-apply` parameters), the entry is `knobs.apply(stock, entry, request)` first,
/// and `menu` must be given. Returns the `bake_bytes` table and the meta JSON text.
pub fn bake_with_params(
    entry: &[u8],
    stock: &[u8],
    menu: Option<&[u8]>,
    ctx: &[u8],
    ring: &[u8],
    params: &[u8],
) -> Result<(Vec<u8>, String), CoreError> {
    let params = parse(params)?;
    let step = match params.get("step") {
        None | Some(Value::Null) => STEP,
        Some(Value::Float(f)) => *f,
        Some(Value::Int(i)) => i32::try_from(*i)
            .map(f64::from)
            .map_err(|_| CoreError::new("value", "step"))?,
        Some(_) => return Err(CoreError::new("value", "step")),
    };
    let entry = match params.get("knobs") {
        None | Some(Value::Null) => entry.to_vec(),
        Some(knobs) => {
            let menu = menu.ok_or_else(|| CoreError::new("value", "knobs need the menu JSON"))?;
            apply(stock, entry, &knobs_request(knobs, menu)?)?.0
        }
    };
    let model = unit_model(stock, ctx, ring)?;
    let bk = bake_chroma(&model, &entry, step)?;
    Ok((bake_bytes(&bk), bake_meta(&bk).render()))
}

/// What `color-bake` prints and `color_bake` returns as `summary`: the table's size and
/// sha256, and the meta.
pub fn bake_summary(bin: &[u8], meta: &str) -> String {
    let meta = parse(meta.as_bytes()).map_or(Json::Null, |v| to_json(&v));
    Json::object([
        (
            "bytes",
            Json::Int(i64::try_from(bin.len()).map_or(i64::MAX, |v| v)),
        ),
        ("sha256", Json::str(sha256_hex(bin))),
        ("meta", meta),
    ])
    .render()
}

fn to_json(v: &Value) -> Json {
    match v {
        Value::Null => Json::Null,
        Value::Bool(b) => Json::Bool(*b),
        Value::Int(i) => Json::Int(*i),
        Value::Float(f) => Json::Float(*f),
        Value::Str(s) => Json::str(s.clone()),
        Value::Array(items) => Json::Array(items.iter().map(to_json).collect()),
        Value::Object(pairs) => {
            Json::Object(pairs.iter().map(|(k, v)| (k.clone(), to_json(v))).collect())
        }
    }
}

/// `chroma_u8(codes, bake, channel_gains(adjust), display_table(adjust, curves))`.
///
/// `params` is `{"adjust": {...}, "curves": {...}}`, both optional.
pub fn apply_with_params(
    bin: &[u8],
    meta: &[u8],
    codes: &[f32],
    params: &[u8],
) -> Result<Vec<u8>, CoreError> {
    if !codes.len().is_multiple_of(3) {
        return Err(CoreError::new(
            "value",
            "codes must be a multiple of 3 values",
        ));
    }
    let (nx, ny, origin, step, coefficients) = meta_of(&parse(meta)?)?;
    let bk = from_bytes(bin, nx, ny, origin, step, coefficients)?;
    let (adjust, curves) = tones_of(&parse(params)?)?;
    let table = display_table(&adjust, curves.as_ref())?;
    Ok(chroma_u8(
        Codes::F32(codes),
        &bk,
        channel_gains(&adjust),
        table.as_ref(),
    ))
}
