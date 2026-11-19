//! Port of `gxr_studio/render/camera_recipe.py`: resolve an EXIF Copyright `GXR1` tag
//! against the loaded camera data (`recorded_recipe` + `_catalog_recipe` + `request_for` checks).

use std::collections::BTreeMap;

use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

use crate::catalog_names::{camera_unit, catalog_generation, resolve_catalog_position};
use crate::datcodec::{entries_from_dat, profile};
use crate::knob_table::{MenuData, MenuItem, embedded_menu, read as read_knob_table};
use crate::recipe_tags::{KNOB_ORDER, decoded_levels, knob_default, parse_recipe_tag};

const ERR_CATALOG_MISSING: &str = "请先导入拍摄时使用的相机配方文件。";
const ERR_AMOUNT_UNKNOWN: &str = "强度未知，无法生成拍摄配方预测。请核对拍摄时使用的相机配方文件。";
const ERR_KNOBS_MISMATCH: &str = "相机记录的旋钮与配方目录不匹配。";
const ERR_KNOB_NOT_AVAILABLE: &str = "无法保存配方。有一个旋钮无法使用。";
const ERR_LEVEL_NOT_ALLOWED: &str = "无法保存配方。这个档位不能用。";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CameraRecipe {
    pub unit: String,
    pub slot: u8,
    pub kind: String,
    pub id: Option<String>,
    pub levels: BTreeMap<String, i64>,
    pub levels_unknown: bool,
}

impl CameraRecipe {
    pub fn to_recipe_json(&self) -> Json {
        let levels = Json::Object(
            self.levels
                .iter()
                .map(|(k, &v)| (k.clone(), Json::Int(v)))
                .collect(),
        );
        let mut fields = vec![
            ("kind", Json::str(&self.kind)),
            ("unit", Json::str(&self.unit)),
            ("slot", Json::Int(i64::from(self.slot))),
            ("levels", levels),
        ];
        if let Some(ref id) = self.id {
            fields.push(("id", Json::str(id)));
        }
        Json::object(fields)
    }
}

fn unit_index(unit: &str) -> Option<usize> {
    match unit {
        "jlaunch" => Some(0),
        "qlaunch" => Some(1),
        "l08firm" => Some(2),
        _ => None,
    }
}

fn recipe_items(menu: &MenuData, unit: u8, slot: u8) -> Result<&[MenuItem], CoreError> {
    let rec = menu
        .recipes
        .iter()
        .find(|r| r.unit == unit && r.slot == slot)
        .ok_or_else(|| CoreError::new("value", "unit/slot"))?;
    let start = usize::from(rec.first_item);
    let end = start + usize::from(rec.item_count);
    menu.items
        .get(start..end)
        .ok_or_else(|| CoreError::new("value", "unit/slot"))
}

fn amount_levels_of(control_names: &[&str], items: &[MenuItem]) -> Option<Vec<i64>> {
    let (_, item) = control_names
        .iter()
        .zip(items.iter())
        .find(|&(&name, _)| name == "amount")?;
    let levels: Vec<i64> = item.points.iter().map(|p| i64::from(p.level)).collect();
    let start = if item.cap_lower != 0 { 1 } else { 0 };
    let end = if item.cap_upper != 0 {
        levels.len().saturating_sub(1)
    } else {
        levels.len()
    };
    if start <= end {
        Some(levels[start..end].to_vec())
    } else {
        Some(Vec::new())
    }
}

/// Port of `gxr_studio/render/camera_recipe.py:50 recorded_recipe` (plus `_catalog_recipe` and `menu.request_for`).
///
/// `dat` is `Some((dat_bytes, library_bytes))` when camera data is loaded, or `None` when none is loaded.
pub fn recorded_recipe(
    tag_text: &str,
    dat: Option<(&[u8], Option<&[u8]>)>,
) -> Result<Option<CameraRecipe>, CoreError> {
    let Some(tag) = parse_recipe_tag(tag_text) else {
        return Ok(None);
    };
    let Some(target) = resolve_catalog_position(&tag.catalog_identity, &tag.unit, tag.position)
    else {
        return Ok(None);
    };
    let Some((raw, library)) = dat else {
        return Err(CoreError::new(
            "camera_recipe_catalog_missing",
            ERR_CATALOG_MISSING,
        ));
    };
    let (parsed_dat, _entries) = entries_from_dat(raw, library)?;
    let prof = profile(parsed_dat.generation);
    let lib_gen = prof.and_then(|p| p.library_generation);
    if catalog_generation(&tag.catalog_identity) != lib_gen {
        return Err(CoreError::new(
            "camera_recipe_catalog_missing",
            ERR_CATALOG_MISSING,
        ));
    }

    let unit_str = camera_unit(&tag.unit).ok_or_else(|| CoreError::new("value", "unit/slot"))?;
    let u_idx = unit_index(unit_str).ok_or_else(|| CoreError::new("value", "unit/slot"))?;

    let builtin_count: u8 = if lib_gen == Some(178) { 4 } else { 8 };
    let (kind, id, slot): (&str, Option<String>, u8) = if target.kind == "builtin" {
        let Some(slot) = target.slot else {
            return Ok(None);
        };
        if slot < 1 || slot > builtin_count {
            return Ok(None);
        }
        ("builtin", None, slot)
    } else {
        let matched = parsed_dat
            .recipes
            .iter()
            .enumerate()
            .find(|(_, rec)| ((rec.unit_mask >> u_idx) & 1 != 0) && rec.name == target.name);
        let Some((i, rec)) = matched else {
            return Ok(None);
        };
        let prefix = format!("record/{i}/{}/", rec.name);
        let mut hash_input = Vec::with_capacity(prefix.len() + rec.entry.len() + 2);
        hash_input.extend_from_slice(prefix.as_bytes());
        hash_input.extend_from_slice(&rec.entry);
        hash_input.extend_from_slice(b"{}");
        ("record", Some(sha256_hex(&hash_input)), rec.base)
    };

    let base_menu = embedded_menu()?;
    let menu = if let Some(ref kv) = parsed_dat.kv2_block {
        let mount_50mm = prof.is_some_and(|p| p.mount_knobs_from_50mm);
        read_knob_table(kv, base_menu, mount_50mm)?
    } else {
        base_menu
    };
    let items = recipe_items(&menu, u_idx as u8, slot)?;
    let mut control_names: Vec<&str> = Vec::with_capacity(items.len());
    for it in items {
        let ctrl = menu
            .controls
            .get(usize::from(it.control))
            .ok_or_else(|| CoreError::new("value", "unit/slot"))?;
        control_names.push(ctrl.name.as_str());
    }

    let amt_levels = amount_levels_of(&control_names, items);
    let stored_levels = decoded_levels(&tag, amt_levels.as_deref());
    let has_amount_code = tag
        .knob_code
        .as_deref()
        .is_some_and(|c| !c.is_empty() && c.as_bytes()[0] != b'-');
    if has_amount_code && stored_levels.is_some_and(|lv| lv[0].is_none()) {
        return Err(CoreError::new("camera_amount_unknown", ERR_AMOUNT_UNKNOWN));
    }

    let mut levels: BTreeMap<String, i64> = BTreeMap::new();
    if let Some(slv) = stored_levels {
        for (&name, &val) in KNOB_ORDER.iter().zip(slv.iter()) {
            if let Some(v) = val {
                levels.insert(name.to_string(), v);
            }
        }
        if control_names.iter().any(|&c| !levels.contains_key(c)) {
            return Err(CoreError::new("camera_knobs_mismatch", ERR_KNOBS_MISMATCH));
        }
    }

    // Validate like `CameraData.request_for` + `Menu.request_for` (no clamping, reject unsupported non-neutral knobs).
    if kind == "builtin" && lib_gen == Some(178) && slot > 4 {
        return Err(CoreError::new(
            "recipe_not_found",
            format!(
                "g{} 已移除这个旧配方入口，请从完整配方列表重新选择。",
                parsed_dat.generation
            ),
        ));
    }
    for (name, &val) in &levels {
        if !control_names.iter().any(|&c| c == name) && val != knob_default(name) {
            return Err(CoreError::new("knob_not_available", ERR_KNOB_NOT_AVAILABLE));
        }
    }
    for (&name, it) in control_names.iter().zip(items.iter()) {
        let lv = levels
            .get(name)
            .copied()
            .unwrap_or_else(|| i64::from(it.default_level));
        if it.points.iter().all(|p| i64::from(p.level) != lv) {
            return Err(CoreError::new("level_not_allowed", ERR_LEVEL_NOT_ALLOWED));
        }
    }

    Ok(Some(CameraRecipe {
        unit: unit_str.to_string(),
        slot,
        kind: kind.to_string(),
        id,
        levels,
        levels_unknown: tag.levels.is_none(),
    }))
}

/// Shared JSON of `recorded_recipe` (matches `gxr-cli camdata-camera-recipe` and WASM `camdata_camera_recipe`).
pub fn camera_recipe_json(
    tag_text: &str,
    dat: Option<(&[u8], Option<&[u8]>)>,
) -> Result<String, CoreError> {
    let json = match recorded_recipe(tag_text, dat)? {
        None => Json::object([("recipe", Json::Null)]),
        Some(rec) => Json::object([
            ("recipe", rec.to_recipe_json()),
            ("levels_unknown", Json::Bool(rec.levels_unknown)),
        ]),
    };
    Ok(json.render())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_tag_and_unknown_identity_return_none_without_dat() {
        assert_eq!(recorded_recipe("", None).expect("empty"), None);
        assert_eq!(
            recorded_recipe("Copyright 2026 Someone", None).expect("plain"),
            None
        );
        // Valid tag syntax but unknown catalog identity -> None (no catalog_missing error).
        assert_eq!(
            recorded_recipe("GXR1 deadbeef00 50 01", None).expect("unknown id"),
            None
        );
        // Known catalog identity but out-of-range position -> None.
        assert_eq!(
            recorded_recipe("GXR1 6adf847602 50 99", None).expect("oob pos"),
            None
        );
    }

    #[test]
    fn known_catalog_without_dat_returns_catalog_missing() {
        for tag in [
            "GXR1 d25d2f0a68 50 01",
            "GXR1 6a3a5a3c02 28 48",
            "GXR1 6adf847602 50 47 A99999999-",
            "GXR1 0ccc86c19b MT 70 A99999999-",
        ] {
            let err = recorded_recipe(tag, None).expect_err("must fail without dat");
            assert_eq!(err.kind, "camera_recipe_catalog_missing");
            assert_eq!(err.message, ERR_CATALOG_MISSING);
        }
    }
}
