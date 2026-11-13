//! Port of gxr_studio/camdata/knob_table.py and embedded `final.json` menu.

use std::collections::BTreeMap;

use gxr_raw::{CoreError, Json};

use crate::crc32::{u16_be, u32_be};
use crate::datcodec::{dat_error, parse, profile};
use crate::json_parse::{Val, parse_json};

const FINAL_JSON: &[u8] = include_bytes!("../../../../src/gxr_studio/engine/data/final.json");

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Control {
    pub id: i64,
    pub name: String,
    pub label: String,
    pub dose_index: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuRecipe {
    pub unit: u8,
    pub slot: u8,
    pub first_item: u16,
    pub item_count: u8,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuPoint {
    pub level: i16,
    pub dose: i16,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuItem {
    pub control: u8,
    pub point_count: u8,
    pub min_level: i16,
    pub max_level: i16,
    pub default_level: i16,
    pub cap_lower: u8,
    pub cap_upper: u8,
    pub amount_step: u8,
    pub points: Vec<MenuPoint>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuData {
    pub schema: String,
    pub q: i64,
    pub max_points: i64,
    pub wb_method: i64,
    pub split_method: i64,
    pub units: Vec<String>,
    pub controls: Vec<Control>,
    pub recipes: Vec<MenuRecipe>,
    pub items: Vec<MenuItem>,
}

fn i16_be(data: &[u8], at: usize) -> i16 {
    i16::from_be_bytes([data[at], data[at + 1]])
}

fn req<'a>(v: &'a Val, key: &str) -> Result<&'a Val, CoreError> {
    v.field(key)
        .ok_or_else(|| CoreError::new("camdata", format!("final.json missing {key}")))
}

fn req_i64(v: &Val, key: &str) -> Result<i64, CoreError> {
    req(v, key)?
        .as_i64()
        .ok_or_else(|| CoreError::new("camdata", format!("final.json {key} not int")))
}

fn req_str(v: &Val, key: &str) -> Result<String, CoreError> {
    req(v, key)?
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| CoreError::new("camdata", format!("final.json {key} not str")))
}

/// Port of gxr_studio/engine/menu.py:9 load.
pub fn embedded_menu() -> Result<MenuData, CoreError> {
    let root = parse_json(FINAL_JSON)?;
    let schema = req_str(&root, "schema")?;
    let q = req_i64(&root, "q")?;
    let max_points = req_i64(&root, "max_points")?;
    let wb_method = req_i64(&root, "wb_method")?;
    let split_method = req_i64(&root, "split_method")?;
    let units = req(&root, "units")?
        .as_array()
        .ok_or_else(|| CoreError::new("camdata", "final.json units not array"))?
        .iter()
        .map(|u| {
            u.as_str()
                .map(str::to_string)
                .ok_or_else(|| CoreError::new("camdata", "final.json unit not str"))
        })
        .collect::<Result<Vec<_>, _>>()?;

    let controls = req(&root, "controls")?
        .as_array()
        .ok_or_else(|| CoreError::new("camdata", "final.json controls not array"))?
        .iter()
        .map(|c| {
            Ok(Control {
                id: req_i64(c, "id")?,
                name: req_str(c, "name")?,
                label: req_str(c, "label")?,
                dose_index: req_i64(c, "dose_index")?,
            })
        })
        .collect::<Result<Vec<_>, CoreError>>()?;

    let recipes = req(&root, "recipes")?
        .as_array()
        .ok_or_else(|| CoreError::new("camdata", "final.json recipes not array"))?
        .iter()
        .map(|r| {
            Ok(MenuRecipe {
                unit: req_i64(r, "unit")? as u8,
                slot: req_i64(r, "slot")? as u8,
                first_item: req_i64(r, "first_item")? as u16,
                item_count: req_i64(r, "item_count")? as u8,
            })
        })
        .collect::<Result<Vec<_>, CoreError>>()?;

    let items = req(&root, "items")?
        .as_array()
        .ok_or_else(|| CoreError::new("camdata", "final.json items not array"))?
        .iter()
        .map(|it| {
            let points = req(it, "points")?
                .as_array()
                .ok_or_else(|| CoreError::new("camdata", "final.json points not array"))?
                .iter()
                .map(|p| {
                    Ok(MenuPoint {
                        level: req_i64(p, "level")? as i16,
                        dose: req_i64(p, "dose")? as i16,
                    })
                })
                .collect::<Result<Vec<_>, CoreError>>()?;
            Ok(MenuItem {
                control: req_i64(it, "control")? as u8,
                point_count: req_i64(it, "point_count")? as u8,
                min_level: req_i64(it, "min_level")? as i16,
                max_level: req_i64(it, "max_level")? as i16,
                default_level: req_i64(it, "default_level")? as i16,
                cap_lower: req_i64(it, "cap_lower")? as u8,
                cap_upper: req_i64(it, "cap_upper")? as u8,
                amount_step: req_i64(it, "amount_step")? as u8,
                points,
            })
        })
        .collect::<Result<Vec<_>, CoreError>>()?;

    Ok(MenuData {
        schema,
        q,
        max_points,
        wb_method,
        split_method,
        units,
        controls,
        recipes,
        items,
    })
}

/// Port of gxr_studio/camdata/knob_table.py:11 read.
pub fn read(
    block: &[u8],
    menu: MenuData,
    mount_knobs_from_50mm: bool,
) -> Result<MenuData, CoreError> {
    if block.len() < 44 {
        return Err(dat_error("kv2 点表头长度不合格"));
    }
    let compact = &block[..8] == b"GXKV2R3\0";
    let recipe_count = usize::from(u16_be(block, 22));
    let recipe_base = u32_be(block, 36) as usize;
    let mut recipes = Vec::with_capacity(recipe_count);
    for i in 0..recipe_count {
        let at = recipe_base
            .checked_add(16 * i)
            .ok_or_else(|| dat_error("kv2 配方目录越界"))?;
        if at.checked_add(6).is_none_or(|end| end > block.len()) {
            return Err(dat_error("kv2 配方目录越界"));
        }
        recipes.push(MenuRecipe {
            unit: block[at],
            slot: block[at + 1],
            first_item: u16_be(block, at + 4),
            item_count: block[at + 2],
        });
    }
    if mount_knobs_from_50mm {
        let mut source = BTreeMap::new();
        for r in &recipes {
            if r.unit == 0 {
                source.insert(r.slot, r.clone());
            }
        }
        for r in &mut recipes {
            if r.unit == 2 && r.slot != 4 {
                let src = source
                    .get(&r.slot)
                    .ok_or_else(|| dat_error("kv2 缺少 50mm 旋钮源条目"))?;
                *r = MenuRecipe { unit: 2, ..*src };
            }
        }
    }

    let item_count = usize::from(u16_be(block, 24));
    let item_base = u32_be(block, 40) as usize;
    let mut items = Vec::with_capacity(item_count);
    for i in 0..item_count {
        let at = item_base
            .checked_add(16 * i)
            .ok_or_else(|| dat_error("kv2 项目表越界"))?;
        if at.checked_add(16).is_none_or(|end| end > block.len()) {
            return Err(dat_error("kv2 项目表越界"));
        }
        let control = block[at];
        let count = block[at + 1];
        let lo = i16_be(block, at + 2);
        let hi = i16_be(block, at + 4);
        let default = i16_be(block, at + 6);
        let cap_lower = block[at + 8] & 1;
        let cap_upper = (block[at + 8] >> 1) & 1;
        let step = block[at + 9];
        let points_at = u32_be(block, at + 12) as usize;
        let stride = if compact { 2 } else { 4 };
        let points_end = points_at
            .checked_add(usize::from(count) * stride)
            .ok_or_else(|| dat_error("kv2 点表越界"))?;
        if points_end > block.len() {
            return Err(dat_error("kv2 点表越界"));
        }
        let mut points = Vec::with_capacity(usize::from(count));
        for j in 0..usize::from(count) {
            let (level, dose) = if compact {
                let lv = if control == 0 {
                    (j * usize::from(step)).min(150) as i16
                } else {
                    lo.checked_add(j as i16)
                        .ok_or_else(|| dat_error("kv2 档位溢出"))?
                };
                (lv, i16_be(block, points_at + 2 * j))
            } else {
                (
                    i16_be(block, points_at + 4 * j),
                    i16_be(block, points_at + 4 * j + 2),
                )
            };
            points.push(MenuPoint { level, dose });
        }
        items.push(MenuItem {
            control,
            point_count: count,
            min_level: lo,
            max_level: hi,
            default_level: default,
            cap_lower,
            cap_upper,
            amount_step: step,
            points,
        });
    }

    Ok(MenuData {
        recipes,
        items,
        ..menu
    })
}

/// Port of gxr_studio/render/recipes.py:136-138 menu resolution with optional DAT override.
pub fn resolve_menu(dat: Option<(&[u8], Option<&[u8]>)>) -> Result<MenuData, CoreError> {
    let menu = embedded_menu()?;
    let Some((raw, library)) = dat else {
        return Ok(menu);
    };
    let parsed = parse(raw, library)?;
    let Some(kv2) = parsed.kv2_block.as_deref() else {
        return Ok(menu);
    };
    let mount_knobs_from_50mm = profile(parsed.generation)
        .map(|p| p.mount_knobs_from_50mm)
        .unwrap_or(false);
    read(kv2, menu, mount_knobs_from_50mm)
}

pub fn menu_to_json(menu: &MenuData) -> Json {
    let units: Vec<Json> = menu.units.iter().map(Json::str).collect();
    let controls: Vec<Json> = menu
        .controls
        .iter()
        .map(|c| {
            Json::object([
                ("id", Json::Int(c.id)),
                ("name", Json::str(&c.name)),
                ("label", Json::str(&c.label)),
                ("dose_index", Json::Int(c.dose_index)),
            ])
        })
        .collect();
    let recipes: Vec<Json> = menu
        .recipes
        .iter()
        .map(|r| {
            Json::object([
                ("unit", Json::Int(i64::from(r.unit))),
                ("slot", Json::Int(i64::from(r.slot))),
                ("first_item", Json::Int(i64::from(r.first_item))),
                ("item_count", Json::Int(i64::from(r.item_count))),
            ])
        })
        .collect();
    let items: Vec<Json> = menu
        .items
        .iter()
        .map(|it| {
            let points: Vec<Json> = it
                .points
                .iter()
                .map(|p| {
                    Json::object([
                        ("level", Json::Int(i64::from(p.level))),
                        ("dose", Json::Int(i64::from(p.dose))),
                    ])
                })
                .collect();
            Json::object([
                ("control", Json::Int(i64::from(it.control))),
                ("point_count", Json::Int(i64::from(it.point_count))),
                ("min_level", Json::Int(i64::from(it.min_level))),
                ("max_level", Json::Int(i64::from(it.max_level))),
                ("default_level", Json::Int(i64::from(it.default_level))),
                ("cap_lower", Json::Int(i64::from(it.cap_lower))),
                ("cap_upper", Json::Int(i64::from(it.cap_upper))),
                ("amount_step", Json::Int(i64::from(it.amount_step))),
                ("points", Json::Array(points)),
            ])
        })
        .collect();

    Json::object([
        ("schema", Json::str(&menu.schema)),
        ("q", Json::Int(menu.q)),
        ("max_points", Json::Int(menu.max_points)),
        ("wb_method", Json::Int(menu.wb_method)),
        ("split_method", Json::Int(menu.split_method)),
        ("units", Json::Array(units)),
        ("controls", Json::Array(controls)),
        ("recipes", Json::Array(recipes)),
        ("items", Json::Array(items)),
    ])
}
