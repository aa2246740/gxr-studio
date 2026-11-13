//! `color-*` commands. Owner: wave-1 unit color.

use std::path::Path;

use gxr_color::{Menu, Request, Stats, Value, apply, load_menu, menu_request, parse_json};
use gxr_raw::Json;

use crate::common::{Failure, Output, option};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "color-knobs-apply" => knobs_apply(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn knobs_apply(args: &[String]) -> Result<Output, Failure> {
    let entry = option(args, "--entry")?;
    let params_path = option(args, "--params")?;
    let menu_path = option(args, "--menu")?;
    let out = option(args, "--out")?;
    let stock_path = optional(args, "--stock")?;
    let base = read_bin(entry)?;
    let stock = match stock_path {
        Some(path) => read_bin(path)?,
        None => base.clone(),
    };
    let params = parse_json(&read_bin(params_path)?).map_err(Failure::Core)?;
    let menu_bytes = read_bin(menu_path)?;
    let request = request_from(&params, &menu_bytes).map_err(Failure::Core)?;
    let (bytes, stats) = apply(&stock, &base, &request).map_err(Failure::Core)?;
    std::fs::write(out, &bytes).map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    Ok(Output {
        text: result_json(out, &stats).render(),
        ok: true,
    })
}

fn request_from(params: &Value, menu_bytes: &[u8]) -> Result<Request, gxr_raw::CoreError> {
    if params.get("levels").is_some() {
        let menu = load_menu(menu_bytes)?;
        let unit = required_int(params, "unit")?;
        let slot = required_int(params, "slot")?;
        let levels = level_list(params.get("levels").unwrap_or(&Value::Null))?;
        return menu_request(&menu, unit, slot, &levels);
    }
    // The menu file is part of the command so a raw dose run still names it.
    // Parsing it here rejects a corrupt table even when this call does not consult it.
    let _menu: Menu = load_menu(menu_bytes)?;
    let mut request = Request::identity();
    if let Some(amount) = params.get("amount") {
        request.amount = py_int(amount, "amount")?;
    }
    if let Some(doses) = params.get("doses") {
        request.doses = dose_list(doses)?;
    }
    if let Some(method) = params.get("wb_method") {
        request.wb_method = py_int(method, "method")?;
    }
    if let Some(method) = params.get("tone_method") {
        request.tone_method = py_int(method, "method")?;
    }
    Ok(request)
}

fn required_int(params: &Value, key: &str) -> Result<i64, gxr_raw::CoreError> {
    match params.get(key) {
        Some(value) => py_int(value, "unit/slot"),
        None => Err(gxr_raw::CoreError::new("value", "unit/slot")),
    }
}

fn py_int(value: &Value, message: &str) -> Result<i64, gxr_raw::CoreError> {
    value.as_int().ok_or_else(|| gxr_raw::CoreError::new("value", message))
}

fn dose_list(value: &Value) -> Result<[i64; 10], gxr_raw::CoreError> {
    let items = value.as_array().ok_or_else(|| gxr_raw::CoreError::new("value", "doses"))?;
    if items.len() != 10 {
        return Err(gxr_raw::CoreError::new("value", "doses"));
    }
    let mut doses = [0; 10];
    for (slot, item) in doses.iter_mut().zip(items) {
        *slot = py_int(item, "doses")?;
    }
    Ok(doses)
}

fn level_list(value: &Value) -> Result<Vec<Option<i64>>, gxr_raw::CoreError> {
    let items = value.as_array().ok_or_else(|| gxr_raw::CoreError::new("value", "menu item count"))?;
    let mut levels = Vec::with_capacity(items.len());
    for item in items {
        levels.push(match item {
            Value::Int(v) => Some(*v),
            _ => None,
        });
    }
    Ok(levels)
}

fn result_json(out: &str, stats: &Stats) -> Json {
    Json::object([
        ("out", Json::str(out)),
        ("length", Json::Int(552)),
        (
            "stats",
            Json::object([
                ("clamps", Json::Int(stats.clamps)),
                ("node_repairs", Json::Int(stats.node_repairs)),
                ("monotone_repairs", Json::Int(stats.monotone_repairs)),
                ("slope_repairs", Json::Int(stats.slope_repairs)),
                ("colour_cap", Json::Int(stats.colour_cap)),
                ("matrix_cap", Json::Int(stats.matrix_cap)),
                ("rowsum_cap", Json::Int(stats.rowsum_cap)),
            ]),
        ),
    ])
}

fn optional<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    args.get(at + 1).map(|s| Some(s.as_str())).ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

fn read_bin(path: &str) -> Result<Vec<u8>, Failure> {
    std::fs::read(path).map_err(|e| Failure::Usage(format!("cannot read {}: {e}", Path::new(path).display())))
}
