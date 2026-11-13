//! JSON parameters shared by `gxr-cli` and `gxr-wasm`: knob requests, tone adjustments, and
//! the bake meta.

use gxr_raw::{CoreError, Json};

use crate::chain::ChromaBake;
use crate::json_in::Value;
use crate::knobs::Request;
use crate::menu::{load as load_menu, request as menu_request};
use crate::model::Coefficients;
use crate::tones::{Adjust, Curves, Points};

fn value(message: &str) -> CoreError {
    CoreError::new("value", message)
}

fn py_int(v: &Value, message: &str) -> Result<i64, CoreError> {
    v.as_int().ok_or_else(|| value(message))
}

fn number(v: &Value, message: &str) -> Result<f64, CoreError> {
    match v {
        // Parsed values are small integers; float(int) is exact for them.
        Value::Int(i) => i32::try_from(*i).map(f64::from).map_err(|_| value(message)),
        Value::Float(f) => Ok(*f),
        _ => Err(value(message)),
    }
}

/// The knob request from `{"unit", "slot", "levels"}` (menu levels) or
/// `{"amount", "doses", "wb_method", "tone_method"}` (raw doses).
///
/// The menu bytes are parsed either way, so a corrupt table is refused even for a dose run.
pub fn knobs_request(params: &Value, menu_bytes: &[u8]) -> Result<Request, CoreError> {
    let menu = load_menu(menu_bytes)?;
    if let Some(levels) = params.get("levels") {
        let unit = params.get("unit").ok_or_else(|| value("unit/slot")).and_then(|v| py_int(v, "unit/slot"))?;
        let slot = params.get("slot").ok_or_else(|| value("unit/slot")).and_then(|v| py_int(v, "unit/slot"))?;
        let items = levels.as_array().ok_or_else(|| value("menu item count"))?;
        let levels: Vec<Option<i64>> = items.iter().map(Value::as_int).collect();
        return menu_request(&menu, unit, slot, &levels);
    }
    let mut request = Request::identity();
    if let Some(amount) = params.get("amount") {
        request.amount = py_int(amount, "amount")?;
    }
    if let Some(doses) = params.get("doses") {
        let items = doses.as_array().filter(|d| d.len() == 10).ok_or_else(|| value("doses"))?;
        for (slot, item) in request.doses.iter_mut().zip(items) {
            *slot = py_int(item, "doses")?;
        }
    }
    if let Some(method) = params.get("wb_method") {
        request.wb_method = py_int(method, "method")?;
    }
    if let Some(method) = params.get("tone_method") {
        request.tone_method = py_int(method, "method")?;
    }
    Ok(request)
}

fn points(v: Option<&Value>) -> Result<Option<Points>, CoreError> {
    let Some(v) = v else { return Ok(None) };
    if matches!(v, Value::Null) {
        return Ok(None);
    }
    let items = v.as_array().ok_or_else(|| value("curve points"))?;
    let mut out = Vec::with_capacity(items.len());
    for p in items {
        let pair = p.as_array().filter(|q| q.len() == 2).ok_or_else(|| value("curve points"))?;
        out.push((number(&pair[0], "curve points")?, number(&pair[1], "curve points")?));
    }
    Ok(Some(out))
}

/// `{"adjust": {...}, "curves": {...} | null}`: the parsed `Adjust` tone fields and `Curves`.
pub fn tones_of(params: &Value) -> Result<(Adjust, Option<Curves>), CoreError> {
    let mut a = Adjust::default();
    if let Some(adj) = params.get("adjust") {
        if let Some(v) = adj.get("exposure_ev") {
            a.exposure_ev = number(v, "exposure_ev")?;
        }
        for (name, slot) in [
            ("warm", &mut a.warm),
            ("tint", &mut a.tint),
            ("contrast", &mut a.contrast),
            ("highlight", &mut a.highlight),
            ("shadow", &mut a.shadow),
        ] {
            if let Some(v) = adj.get(name) {
                *slot = py_int(v, name)?;
            }
        }
    }
    let curves = match params.get("curves") {
        None | Some(Value::Null) => None,
        Some(c) => Some(Curves {
            master: points(c.get("master"))?,
            r: points(c.get("r"))?,
            g: points(c.get("g"))?,
            b: points(c.get("b"))?,
        }),
    };
    Ok((a, curves))
}

/// `bake_bytes`' meta (`nx`, `ny`, `origin`, `step`) plus `input_coefficients` as float64
/// rows: the `.bin` holds them as float32, and `chroma_u8` needs the float64 values.
pub fn bake_meta(bk: &ChromaBake) -> Json {
    let coefficients = match &bk.input_coefficients {
        Some(c) => Json::Array(c.values.chunks(3).map(Json::floats).collect()),
        None => Json::Null,
    };
    let count = |n: usize| Json::Int(i64::try_from(n).map_or(i64::MAX, |v| v));
    Json::object([
        ("nx", count(bk.nx)),
        ("ny", count(bk.ny)),
        ("origin", Json::floats(&bk.origin)),
        ("step", Json::Float(bk.step)),
        ("input_coefficients", coefficients),
    ])
}

/// The grid and the float64 coefficients from a meta JSON written by [`bake_meta`].
pub fn meta_of(meta: &Value) -> Result<(usize, usize, [f64; 2], f64, Option<Coefficients>), CoreError> {
    let size = |key: &str| -> Result<usize, CoreError> {
        let v = meta.get(key).and_then(Value::as_int).ok_or_else(|| value("bake meta needs nx and ny"))?;
        usize::try_from(v).map_err(|_| value("bake meta needs nx and ny"))
    };
    let (nx, ny) = (size("nx")?, size("ny")?);
    let origin = meta.get("origin").and_then(Value::as_array).filter(|o| o.len() == 2).ok_or_else(|| value("bake meta origin"))?;
    let origin = [number(&origin[0], "bake meta origin")?, number(&origin[1], "bake meta origin")?];
    let step = number(meta.get("step").ok_or_else(|| value("bake meta step"))?, "bake meta step")?;
    let coefficients = match meta.get("input_coefficients") {
        None | Some(Value::Null) => None,
        Some(rows) => {
            let rows = rows.as_array().filter(|r| r.len() == 4 || r.len() == 10).ok_or_else(|| value("bake meta input_coefficients"))?;
            let mut values = Vec::with_capacity(rows.len() * 3);
            for row in rows {
                let row = row.as_array().filter(|r| r.len() == 3).ok_or_else(|| value("bake meta input_coefficients"))?;
                for v in row {
                    values.push(number(v, "bake meta input_coefficients")?);
                }
            }
            Some(Coefficients { rows: rows.len(), values })
        }
    };
    Ok((nx, ny, origin, step, coefficients))
}
