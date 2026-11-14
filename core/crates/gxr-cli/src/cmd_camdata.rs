//! `camdata-*` commands. Owner: wave-1 unit camdata.

use std::io::Read;
use std::path::Path;

use gxr_camdata::{
    ENTRY_BYTES, MAX_DAT_BYTES, MAX_LIBRARY_BYTES, NAMES, SLOTS, check_json_depth,
    entries_from_dat, knob_table_json, menu_json, parse_json, parse_recipe_tag,
    parse_recipe_tag_bytes, recipe_tag_to_json,
};
use gxr_raw::{CoreError, Json};

use crate::common::{self, Failure, Output};

fn opt_option<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    args.get(at + 1)
        .map(|s| Some(s.as_str()))
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

fn read_bounded(path: &str, limit: usize) -> Result<Vec<u8>, Failure> {
    let file = std::fs::File::open(path).map_err(|e| {
        let text = e.to_string();
        let strerror = text.split(" (os error").next().unwrap_or(&text).to_string();
        Failure::Core(CoreError::new(
            "dat_error",
            format!("读不了这个文件（{strerror}）"),
        ))
    })?;
    let mut buf = Vec::new();
    file.take((limit + 1) as u64)
        .read_to_end(&mut buf)
        .map_err(|e| {
            Failure::Core(CoreError::new(
                "dat_error",
                format!("读不了这个文件（{e}）"),
            ))
        })?;
    Ok(buf)
}

fn read_optional_library(args: &[String]) -> Result<Option<Vec<u8>>, Failure> {
    match opt_option(args, "--library")? {
        Some(path) => Ok(Some(read_bounded(path, MAX_LIBRARY_BYTES)?)),
        None => Ok(None),
    }
}

fn write_entries_dir(dir: &str, entries: &[[u8; ENTRY_BYTES]; 24]) -> Result<(), Failure> {
    let root = Path::new(dir);
    for (unit_idx, &unit_name) in NAMES.iter().enumerate() {
        let recipes_dir = root.join("units").join(unit_name).join("recipes");
        std::fs::create_dir_all(&recipes_dir)
            .map_err(|e| Failure::Usage(format!("cannot create {}: {e}", recipes_dir.display())))?;
        for (slot_idx, &slot) in SLOTS.iter().enumerate() {
            let dest = recipes_dir.join(format!("{slot:02}.entry"));
            std::fs::write(&dest, entries[unit_idx * 8 + slot_idx])
                .map_err(|e| Failure::Usage(format!("cannot write {}: {e}", dest.display())))?;
        }
    }
    Ok(())
}

fn write_u8_npy(out: &str, payload: &[u8], shape: &[usize; 2]) -> Result<Json, Failure> {
    let mut dict = format!(
        "{{'descr': '|u1', 'fortran_order': False, 'shape': ({}, {}), }}",
        shape[0], shape[1]
    );
    let total = 10 + dict.len() + 1;
    dict.push_str(&" ".repeat((64 - total % 64) % 64));
    dict.push('\n');
    let mut buf = Vec::with_capacity(10 + dict.len() + payload.len());
    buf.extend_from_slice(b"\x93NUMPY\x01\x00");
    buf.extend_from_slice(&(dict.len() as u16).to_le_bytes());
    buf.extend_from_slice(dict.as_bytes());
    buf.extend_from_slice(payload);
    std::fs::write(out, &buf).map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    Ok(Json::object([
        ("out", Json::str(out)),
        ("dtype", Json::str("uint8")),
        ("shape", Json::ints(&[shape[0] as i64, shape[1] as i64])),
    ]))
}

fn decode_hex(hex: &str) -> Result<Vec<u8>, Failure> {
    if !hex.len().is_multiple_of(2) {
        return Err(Failure::Usage("hex string must have even length".into()));
    }
    let bytes = hex.as_bytes();
    let mut out = Vec::with_capacity(bytes.len() / 2);
    for pair in bytes.chunks_exact(2) {
        let s = std::str::from_utf8(pair).map_err(|_| Failure::Usage("invalid hex".into()))?;
        let val = u8::from_str_radix(s, 16).map_err(|_| Failure::Usage("invalid hex".into()))?;
        out.push(val);
    }
    Ok(out)
}

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "camdata-parse" => {
            let dat_path = common::option(args, "--dat")?;
            let out_dir = opt_option(args, "--out-dir")?;
            let raw = read_bounded(dat_path, MAX_DAT_BYTES)?;
            let library = read_optional_library(args)?;
            let (_dat, entries, text) = parse_json(&raw, library.as_deref())?;
            if let Some(dir) = out_dir {
                write_entries_dir(dir, &entries)?;
            }
            Ok(Output { text, ok: true })
        }
        "camdata-entries" => {
            let dat_path = common::option(args, "--dat")?;
            let out_path = common::option(args, "--out")?;
            let raw = read_bounded(dat_path, MAX_DAT_BYTES)?;
            let library = read_optional_library(args)?;
            let (_dat, entries) = entries_from_dat(&raw, library.as_deref())?;
            let mut flat = Vec::with_capacity(24 * ENTRY_BYTES);
            for e in &entries {
                flat.extend_from_slice(e);
            }
            let json = write_u8_npy(out_path, &flat, &[24, ENTRY_BYTES])?;
            Ok(Output {
                text: json.render(),
                ok: true,
            })
        }
        "camdata-menu" => {
            let text = match opt_option(args, "--dat")? {
                Some(dat_path) => {
                    let raw = read_bounded(dat_path, MAX_DAT_BYTES)?;
                    let library = read_optional_library(args)?;
                    menu_json(Some((&raw, library.as_deref())))?
                }
                None => menu_json(None)?,
            };
            Ok(Output { text, ok: true })
        }
        "camdata-knob-table" => {
            let kv_path = common::option(args, "--kv")?;
            let mount_50mm = args.iter().any(|a| a == "--mount-50mm");
            let block = read_bounded(kv_path, MAX_DAT_BYTES)?;
            let text = knob_table_json(&block, mount_50mm)?;
            Ok(Output { text, ok: true })
        }
        "camdata-parse-json" => {
            let json_path = common::option(args, "--json")?;
            const MAX_JSON_BYTES: usize = 4 * 1024 * 1024;
            let raw = read_bounded(json_path, MAX_JSON_BYTES)?;
            if raw.len() > MAX_JSON_BYTES {
                return Err(Failure::Core(CoreError::new("json", "JSON 文件过大")));
            }
            let text = check_json_depth(&raw)?;
            Ok(Output { text, ok: true })
        }
        "camdata-recipe-tag" => {
            let parsed = if let Some(hex) = opt_option(args, "--tag-hex")? {
                let bytes = decode_hex(hex)?;
                parse_recipe_tag_bytes(&bytes)
            } else {
                let text = common::option(args, "--tag")?;
                parse_recipe_tag(text)
            };
            Ok(Output {
                text: recipe_tag_to_json(parsed.as_ref()).render(),
                ok: true,
            })
        }
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}
