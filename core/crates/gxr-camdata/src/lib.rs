//! Port of `gxr_studio/camdata/` (DAT codec, r5 dual-file codec, knob_table, recipe_tags, menu).
#![forbid(unsafe_code)]

pub mod bounds;
pub mod crc32;
pub mod datcodec;
pub mod datcodec_looks;
pub mod datcodec_pack;
pub mod datcodec_types;
pub mod engine_bindings;
pub mod json_parse;
pub mod knob_table;
pub mod r5;
pub mod recipe_tags;

pub use datcodec::{
    Dat, ENTRY_BYTES, FullRecipe, Look, MAX_DAT_BYTES, Module, NAMES, SLOTS, UserEntry,
    entries_from_dat, parse, peek_generation,
};
pub use datcodec_pack::pack;
pub use knob_table::{
    MenuData, embedded_menu, menu_to_json, read as read_knob_table, resolve_menu,
};
pub use r5::{LIBRARY_NAME, MAX_LIBRARY_BYTES};
pub use recipe_tags::{RecipeTag, parse_recipe_tag, parse_recipe_tag_bytes, recipe_tag_to_json};

use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

/// Shared JSON summary of `parse` / `entries_from_dat` (matches `gxr-cli camdata-parse` and WASM `camdata_parse`).
pub fn parse_json(
    raw: &[u8],
    library: Option<&[u8]>,
) -> Result<(Dat, [[u8; ENTRY_BYTES]; 24], String), CoreError> {
    let (dat, entries) = entries_from_dat(raw, library)?;
    let mut units_json = Vec::with_capacity(3);
    for (unit_idx, &unit_name) in NAMES.iter().enumerate() {
        let mut slot_nums = Vec::with_capacity(8);
        let mut entry_items = Vec::with_capacity(8);
        for (slot_idx, &slot) in SLOTS.iter().enumerate() {
            let e = &entries[unit_idx * 8 + slot_idx];
            slot_nums.push(i64::from(slot));
            entry_items.push(Json::object([
                ("slot", Json::Int(i64::from(slot))),
                ("sha256", Json::str(sha256_hex(e))),
            ]));
        }
        units_json.push(Json::object([
            ("unit", Json::str(unit_name)),
            ("unit_index", Json::Int(unit_idx as i64)),
            ("slots", Json::ints(&slot_nums)),
            ("entries", Json::Array(entry_items)),
        ]));
    }
    let looks_json: Vec<Json> = dat
        .looks
        .iter()
        .map(|look| {
            Json::object([
                ("name", Json::str(&look.name)),
                ("base", Json::Int(i64::from(look.base))),
                ("unit_mask", Json::Int(i64::from(look.unit_mask))),
                ("sha256", Json::str(sha256_hex(&look.entry))),
            ])
        })
        .collect();
    let recipes_json: Vec<Json> = dat
        .recipes
        .iter()
        .map(|rec| {
            Json::object([
                ("name", Json::str(&rec.name)),
                ("base", Json::Int(i64::from(rec.base))),
                ("unit_mask", Json::Int(i64::from(rec.unit_mask))),
                ("sha256", Json::str(sha256_hex(&rec.entry))),
            ])
        })
        .collect();
    let users_json: Vec<Json> = dat
        .users
        .iter()
        .enumerate()
        .map(|(i, user)| {
            let levels: Vec<i64> = user.levels.iter().map(|&v| i64::from(v)).collect();
            let name = dat.user_names.get(i).map(String::as_str).unwrap_or("");
            Json::object([
                ("name", Json::str(name)),
                ("base", Json::Int(i64::from(user.base))),
                ("levels", Json::ints(&levels)),
            ])
        })
        .collect();
    let kv2_sha = dat.kv2_block.as_deref().map(sha256_hex);
    let json = Json::object([
        ("generation", Json::Int(i64::from(dat.generation))),
        ("units", Json::Array(units_json)),
        ("looks", Json::Array(looks_json)),
        ("recipes", Json::Array(recipes_json)),
        ("users", Json::Array(users_json)),
        ("has_kv2", Json::Bool(dat.kv2_block.is_some())),
        ("kv2_sha256", Json::opt(kv2_sha.map(Json::str))),
    ]);
    Ok((dat, entries, json.render()))
}

/// Shared JSON of `resolve_menu` (matches `gxr-cli camdata-menu` and WASM `camdata_menu`).
pub fn menu_json(dat: Option<(&[u8], Option<&[u8]>)>) -> Result<String, CoreError> {
    let menu = resolve_menu(dat)?;
    Ok(menu_to_json(&menu).render())
}
