//! `color-*` commands. Owner: wave-1 unit color. Flags are documented in docs/mobile.md.

use std::io::Read;
use std::path::Path;

use gxr_camdata::{MAX_DAT_BYTES, MAX_LIBRARY_BYTES};
use gxr_color::resolve::{
    MAX_JSON_BYTES, ResolveError, controls_json, listing_json, parse_edit_json,
    resolve_default_json, resolve_recipe_json,
};
use gxr_color::{
    Codes, Flavour, Stats, Table, apply, apply_with_params, bake_summary, bake_with_params,
    channel_gains, context, decode_npy, display_table, encode_npy, knobs_request, model_for,
    parse_json, predict, quantize_f64, stock_words_of_entry, tones_of, unit_model,
};
use gxr_raw::Json;

use crate::common::{Failure, Output, option};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "color-knobs-apply" => knobs_apply(args),
        "color-calib-check" => calib_check(args),
        "color-predict" => predict_cmd(args),
        "color-bake" => bake_cmd(args),
        "color-apply" => apply_cmd(args),
        "color-tones" => tones_cmd(args),
        "color-parse-edit" => parse_edit_cmd(args),
        "color-listing" => listing_cmd(args),
        "color-controls" => controls_cmd(args),
        "color-resolve" => resolve_cmd(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn ok(json: Json) -> Result<Output, Failure> {
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn count(n: usize) -> Json {
    Json::Int(i64::try_from(n).map_or(i64::MAX, |v| v))
}

fn calib_check(args: &[String]) -> Result<Output, Failure> {
    if optional(args, "--dir")?.is_some() {
        return pack_check(args);
    }
    let npz = read_bin(option(args, "--npz")?)?;
    let entry = read_bin(option(args, "--entry")?)?;
    let words = stock_words_of_entry(&entry)?;
    let checked = context(&npz, &words)?;
    ok(Json::object([
        ("rows", count(checked.rows)),
        ("d0", count(checked.d0.len())),
        ("d1", count(checked.d1.len())),
    ]))
}

/// `model_for(home, iso, unit=unit)` on a pack folder: the pick, then the model it builds.
fn pack_check(args: &[String]) -> Result<Output, Failure> {
    let dir = option(args, "--dir")?.to_string();
    let unit = option(args, "--unit")?;
    let iso: i64 = option(args, "--iso")?
        .parse()
        .map_err(|_| Failure::Usage("--iso must be an integer".into()))?;
    let stock = read_bin(option(args, "--stock")?)?;
    let manifest = read_bin(&format!("{dir}/manifest.json"))?;
    let read = |name: &str| std::fs::read(Path::new(&dir).join(name)).ok();
    let (pick, ctx, ring) = model_for(&manifest, &stock, iso, unit, &read)?;
    let model = unit_model(&stock, &ctx, &ring)?;
    ok(Json::object([
        ("iso", Json::Int(pick.iso)),
        ("calibrated", Json::Bool(pick.calibrated)),
        ("model_iso", Json::Int(pick.model_iso)),
        ("ctx", Json::str(pick.ctx)),
        ("ring", Json::str(pick.ring)),
        ("rows", count(model.coefficients.rows)),
    ]))
}

/// Codes from a `.npy`: float32 or float64, last axis 3. Returns the values and the shape.
enum CodeArray {
    F32(Vec<f32>),
    F64(Vec<f64>),
}

fn read_codes(path: &str) -> Result<(CodeArray, Vec<usize>), Failure> {
    let npy = decode_npy(&read_bin(path)?)?;
    if npy.shape.last() != Some(&3) {
        return Err(Failure::Usage("codes must have a last axis of 3".into()));
    }
    let codes = match npy.descr.as_str() {
        "<f4" => CodeArray::F32(
            npy.values
                .chunks_exact(4)
                .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
                .collect(),
        ),
        "<f8" => CodeArray::F64(npy.f64s()?),
        other => {
            return Err(Failure::Usage(format!(
                "codes must be float32 or float64, not {other}"
            )));
        }
    };
    Ok((codes, npy.shape))
}

impl CodeArray {
    fn view(&self) -> Codes<'_> {
        match self {
            CodeArray::F32(v) => Codes::F32(v),
            CodeArray::F64(v) => Codes::F64(v),
        }
    }
}

fn write_out(out: &str, descr: &str, shape: &[usize], payload: &[u8]) -> Result<Json, Failure> {
    std::fs::write(out, encode_npy(descr, shape, payload))
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    Ok(Json::object([
        ("out", Json::str(out)),
        (
            "dtype",
            Json::str(if descr == "|u1" {
                "uint8"
            } else if descr == "<f8" {
                "float64"
            } else {
                "float32"
            }),
        ),
        (
            "shape",
            Json::Array(shape.iter().map(|&d| count(d)).collect()),
        ),
    ]))
}

fn predict_cmd(args: &[String]) -> Result<Output, Failure> {
    let entry = read_bin(option(args, "--entry")?)?;
    let stock = read_bin(option(args, "--stock")?)?;
    let ctx = read_bin(option(args, "--ctx")?)?;
    let ring = read_bin(option(args, "--ring")?)?;
    let (codes, shape) = read_codes(option(args, "--codes")?)?;
    let out = option(args, "--out")?;
    let half: usize = match optional(args, "--half")? {
        Some(h) => h
            .parse()
            .map_err(|_| Failure::Usage("--half must be 0 or 1".into()))?,
        None => 0,
    };
    let flavour = if args.iter().any(|a| a == "--base") {
        Flavour::Base
    } else {
        Flavour::Unit
    };
    let model = unit_model(&stock, &ctx, &ring)?;
    let rgb = predict(&model, &entry, codes.view(), half, flavour)?;
    let json = if args.iter().any(|a| a == "--float") {
        let payload: Vec<u8> = rgb.iter().flatten().flat_map(|v| v.to_le_bytes()).collect();
        write_out(out, "<f8", &shape, &payload)?
    } else {
        write_out(out, "|u1", &shape, &quantize_f64(&rgb))?
    };
    ok(json)
}

/// `params.json` for a bake: `{"step": 0.375, "knobs": {...}}`, both optional.
fn bake_cmd(args: &[String]) -> Result<Output, Failure> {
    let entry = read_bin(option(args, "--entry")?)?;
    let stock = read_bin(option(args, "--stock")?)?;
    let ctx = read_bin(option(args, "--ctx")?)?;
    let ring = read_bin(option(args, "--ring")?)?;
    let params = match optional(args, "--params")? {
        Some(p) => read_bin(p)?,
        None => b"{}".to_vec(),
    };
    let menu = match optional(args, "--menu")? {
        Some(p) => Some(read_bin(p)?),
        None => None,
    };
    let out = option(args, "--out")?;
    let meta_path = option(args, "--meta")?;
    let (bin, meta) = bake_with_params(&entry, &stock, menu.as_deref(), &ctx, &ring, &params)?;
    let summary = bake_summary(&bin, &meta);
    std::fs::write(out, &bin).map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    std::fs::write(meta_path, meta)
        .map_err(|e| Failure::Usage(format!("cannot write {meta_path}: {e}")))?;
    Ok(Output {
        text: summary,
        ok: true,
    })
}

fn apply_cmd(args: &[String]) -> Result<Output, Failure> {
    let bin = read_bin(option(args, "--bake")?)?;
    let meta = read_bin(option(args, "--meta")?)?;
    let (codes, shape) = read_codes(option(args, "--codes")?)?;
    let params = match optional(args, "--params")? {
        Some(p) => read_bin(p)?,
        None => b"{}".to_vec(),
    };
    let out = option(args, "--out")?;
    let codes32: Vec<f32> = match &codes {
        CodeArray::F32(v) => v.clone(),
        CodeArray::F64(v) => v.iter().map(|&x| x as f32).collect(),
    };
    let rgb = apply_with_params(&bin, &meta, &codes32, &params)?;
    ok(write_out(out, "|u1", &shape, &rgb)?)
}

fn tones_cmd(args: &[String]) -> Result<Output, Failure> {
    let params = parse_json(&read_bin(option(args, "--params")?)?)?;
    let out = option(args, "--out")?;
    let (adjust, curves) = tones_of(&params)?;
    let gains = match channel_gains(&adjust) {
        Some(g) => Json::floats(&g),
        None => Json::Null,
    };
    let table = match display_table(&adjust, curves.as_ref())? {
        None => Json::Null,
        Some(Table::Mono(t)) => write_out(out, "<f4", &[t.len()], &floats32(&t))?,
        Some(Table::Rgb(rows)) => {
            let flat: Vec<f32> = rows.concat();
            write_out(out, "<f4", &[3, rows[0].len()], &floats32(&flat))?
        }
    };
    ok(Json::object([("gains", gains), ("table", table)]))
}

fn floats32(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
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
    let params = parse_json(&read_bin(params_path)?)?;
    let request = knobs_request(&params, &read_bin(menu_path)?)?;
    let (bytes, stats) = apply(&stock, &base, &request)?;
    std::fs::write(out, &bytes).map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    ok(result_json(out, &stats))
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

fn resolve_out(res: Result<String, ResolveError>) -> Result<Output, Failure> {
    match res {
        Ok(text) => Ok(Output { text, ok: true }),
        Err(err) => Ok(Output {
            text: err.to_json().render(),
            ok: false,
        }),
    }
}

fn read_bounded(path: &str, limit: usize) -> Result<Vec<u8>, Failure> {
    let file = std::fs::File::open(path)
        .map_err(|e| Failure::Usage(format!("cannot read {}: {e}", Path::new(path).display())))?;
    let mut buf = Vec::new();
    file.take((limit + 1) as u64)
        .read_to_end(&mut buf)
        .map_err(|e| Failure::Usage(format!("cannot read {}: {e}", Path::new(path).display())))?;
    Ok(buf)
}

fn read_optional_library(args: &[String]) -> Result<Option<Vec<u8>>, Failure> {
    match optional(args, "--library")? {
        Some(path) => Ok(Some(read_bounded(path, MAX_LIBRARY_BYTES)?)),
        None => Ok(None),
    }
}

fn parse_edit_cmd(args: &[String]) -> Result<Output, Failure> {
    let raw = read_bounded(option(args, "--json")?, MAX_JSON_BYTES)?;
    resolve_out(parse_edit_json(&raw))
}

fn listing_cmd(args: &[String]) -> Result<Output, Failure> {
    let library = read_optional_library(args)?;
    let dat = match optional(args, "--dat")? {
        Some(path) => Some(read_bounded(path, MAX_DAT_BYTES)?),
        None => None,
    };
    let dat_ref = dat.as_deref().map(|raw| (raw, library.as_deref()));
    resolve_out(listing_json(dat_ref))
}

fn parse_unit_arg(s: &str) -> Result<i64, Failure> {
    match s {
        "jlaunch" => Ok(0),
        "qlaunch" => Ok(1),
        "l08firm" => Ok(2),
        _ => s
            .parse::<i64>()
            .map_err(|_| Failure::Usage("--unit must be 0..2 or unit id".into())),
    }
}

fn controls_cmd(args: &[String]) -> Result<Output, Failure> {
    let library = read_optional_library(args)?;
    let dat = match optional(args, "--dat")? {
        Some(path) => Some(read_bounded(path, MAX_DAT_BYTES)?),
        None => None,
    };
    let unit = parse_unit_arg(option(args, "--unit")?)?;
    let slot: i64 = option(args, "--slot")?
        .parse()
        .map_err(|_| Failure::Usage("--slot must be an integer".into()))?;
    let dat_ref = dat.as_deref().map(|raw| (raw, library.as_deref()));
    resolve_out(controls_json(dat_ref, unit, slot))
}

fn resolve_cmd(args: &[String]) -> Result<Output, Failure> {
    let dat = read_bounded(option(args, "--dat")?, MAX_DAT_BYTES)?;
    let library = read_optional_library(args)?;
    if args.iter().any(|a| a == "--default") {
        let unit = option(args, "--unit")?;
        return resolve_out(resolve_default_json(&dat, library.as_deref(), unit));
    }
    let recipe = read_bounded(option(args, "--recipe")?, MAX_JSON_BYTES)?;
    resolve_out(resolve_recipe_json(&dat, library.as_deref(), &recipe))
}

fn optional<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    args.get(at + 1)
        .map(|s| Some(s.as_str()))
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

fn read_bin(path: &str) -> Result<Vec<u8>, Failure> {
    std::fs::read(path)
        .map_err(|e| Failure::Usage(format!("cannot read {}: {e}", Path::new(path).display())))
}
