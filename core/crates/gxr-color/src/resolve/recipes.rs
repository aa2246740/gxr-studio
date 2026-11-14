//! Port of `gxr_studio/render/recipes.py`: menu listing, controls, and recipe resolution from DAT bytes.

use std::collections::BTreeMap;

use gxr_camdata::datcodec::profile;
use gxr_camdata::knob_table::MenuItem;
use gxr_camdata::{ENTRY_BYTES, MenuData, SLOTS, embedded_menu, entries_from_dat, read_knob_table};
use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

use crate::knobs::{Request, Stats, apply};
use crate::menu::{Menu, request as menu_request};

use super::error::{EditError, ResolveError};
use super::params::{RecipeLevel, RecipeSel, UNITS};

pub const UNIT_INFO: [(&str, &str, &str); 3] = [
    ("jlaunch", "50mm（A12）", "50mm"),
    ("qlaunch", "28mm", "28mm"),
    ("l08firm", "MOUNT", "MOUNT"),
];

pub const SLOT_SHORT: [&str; 8] = ["GRP", "GRN", "HCB", "STD", "FCC", "FCN", "LCH", "LVI"];

fn neutral_of(name: &str) -> i64 {
    if name == "amount" { 100 } else { 0 }
}

fn err_unit_slot() -> CoreError {
    CoreError::new("value", "unit/slot")
}

/// Port of `gxr_studio/render/recipes.py:33 Resolved`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub entry: [u8; ENTRY_BYTES],
    pub entry_sha256: String,
    pub levels: Vec<(String, i64)>,
    pub stats: Stats,
}

impl Resolved {
    pub fn to_json(&self) -> Json {
        let mut hex = String::with_capacity(self.entry.len() * 2);
        for b in &self.entry {
            hex.push_str(&format!("{b:02x}"));
        }
        let levels = Json::Object(
            self.levels
                .iter()
                .map(|(k, v)| (k.clone(), Json::Int(*v)))
                .collect(),
        );
        Json::object([
            ("entry", Json::Str(hex)),
            ("entry_sha256", Json::str(&self.entry_sha256)),
            ("levels", levels),
            (
                "stats",
                Json::object([
                    ("clamps", Json::Int(self.stats.clamps)),
                    ("node_repairs", Json::Int(self.stats.node_repairs)),
                    ("monotone_repairs", Json::Int(self.stats.monotone_repairs)),
                    ("slope_repairs", Json::Int(self.stats.slope_repairs)),
                    ("colour_cap", Json::Int(self.stats.colour_cap)),
                    ("matrix_cap", Json::Int(self.stats.matrix_cap)),
                    ("rowsum_cap", Json::Int(self.stats.rowsum_cap)),
                ]),
            ),
        ])
    }
}

#[derive(Clone, Debug)]
struct ControlMeta {
    name: String,
    label: String,
    min: i64,
    max: i64,
    default: i64,
    cap_lower: bool,
    cap_upper: bool,
    amount_step: Option<i64>,
    levels: Vec<i64>,
}

impl ControlMeta {
    fn to_json(&self, default_override: Option<i64>) -> Json {
        Json::object([
            ("name", Json::str(&self.name)),
            ("label", Json::str(&self.label)),
            ("min", Json::Int(self.min)),
            ("max", Json::Int(self.max)),
            (
                "default",
                Json::Int(default_override.unwrap_or(self.default)),
            ),
            ("cap_lower", Json::Bool(self.cap_lower)),
            ("cap_upper", Json::Bool(self.cap_upper)),
            ("amount_step", Json::opt(self.amount_step.map(Json::Int))),
            ("levels", Json::ints(&self.levels)),
        ])
    }
}

/// Port of `gxr_studio/render/recipes.py:40 Menu`.
#[derive(Clone, Debug)]
pub struct MenuCatalog {
    data: MenuData,
    menu: Menu,
}

impl MenuCatalog {
    pub fn embedded() -> Result<Self, CoreError> {
        let data = embedded_menu()?;
        Ok(Self::from_menu_data(data))
    }

    pub fn from_menu_data(data: MenuData) -> Self {
        let menu = Menu::from_camdata(&data);
        Self { data, menu }
    }

    fn items(&self, unit: i64, slot: i64) -> Result<&[MenuItem], CoreError> {
        if !(0..3).contains(&unit) || !(1..=8).contains(&slot) {
            return Err(err_unit_slot());
        }
        let r = self
            .data
            .recipes
            .iter()
            .find(|r| i64::from(r.unit) == unit && i64::from(r.slot) == slot)
            .ok_or_else(err_unit_slot)?;
        let start = usize::from(r.first_item);
        let count = usize::from(r.item_count);
        let end = start.checked_add(count).ok_or_else(err_unit_slot)?;
        self.data.items.get(start..end).ok_or_else(err_unit_slot)
    }

    fn controls_meta(&self, unit: i64, slot: i64) -> Result<Vec<ControlMeta>, CoreError> {
        let items = self.items(unit, slot)?;
        let mut out = Vec::with_capacity(items.len());
        for it in items {
            let c = self
                .data
                .controls
                .get(usize::from(it.control))
                .ok_or_else(err_unit_slot)?;
            out.push(ControlMeta {
                name: c.name.clone(),
                label: c.label.clone(),
                min: i64::from(it.min_level),
                max: i64::from(it.max_level),
                default: i64::from(it.default_level),
                cap_lower: it.cap_lower != 0,
                cap_upper: it.cap_upper != 0,
                amount_step: if it.amount_step == 0 {
                    None
                } else {
                    Some(i64::from(it.amount_step))
                },
                levels: it.points.iter().map(|p| i64::from(p.level)).collect(),
            });
        }
        Ok(out)
    }

    /// Port of `gxr_studio/render/recipes.py:53 Menu.controls`.
    pub fn controls(&self, unit: i64, slot: i64) -> Result<Vec<Json>, CoreError> {
        Ok(self
            .controls_meta(unit, slot)?
            .into_iter()
            .map(|c| c.to_json(None))
            .collect())
    }

    pub fn controls_json(&self, unit: i64, slot: i64) -> Result<Json, CoreError> {
        Ok(Json::object([(
            "controls",
            Json::Array(self.controls(unit, slot)?),
        )]))
    }

    fn builtin_recipe_json(
        &self,
        unit: usize,
        slot: u8,
        entry_sha256: Option<String>,
        position: Option<usize>,
    ) -> Result<Json, CoreError> {
        let controls = self.controls_meta(unit as i64, i64::from(slot))?;
        let has_amount = controls.iter().any(|c| c.name == "amount");
        let controls_json: Vec<Json> = controls.into_iter().map(|c| c.to_json(None)).collect();
        let number = match position {
            Some(pos) => format!("{pos:02}"),
            None => format!("{slot:02}"),
        };
        let mut pairs = vec![
            ("slot", Json::Int(i64::from(slot))),
            ("number", Json::Str(number)),
            ("short", Json::str(SLOT_SHORT[usize::from(slot - 1)])),
            ("is_stock", Json::Bool(slot == 4)),
            ("has_amount", Json::Bool(has_amount)),
            ("entry_sha256", Json::opt(entry_sha256.map(Json::Str))),
            ("controls", Json::Array(controls_json)),
        ];
        if let Some(pos) = position {
            pairs.push(("position", Json::Int(pos as i64)));
        }
        Ok(Json::object(pairs))
    }

    /// Port of `gxr_studio/render/recipes.py:75 Menu.listing`.
    pub fn listing(&self) -> Result<Vec<Json>, CoreError> {
        let mut units = Vec::with_capacity(3);
        for (u, (id, label, lens)) in UNIT_INFO.iter().enumerate() {
            let mut recipes = Vec::with_capacity(8);
            for slot in SLOTS {
                recipes.push(self.builtin_recipe_json(u, slot, None, None)?);
            }
            units.push(Json::object([
                ("id", Json::str(*id)),
                ("label", Json::str(*label)),
                ("lens", Json::str(*lens)),
                ("calibrated", Json::Bool(false)),
                ("recipes", Json::Array(recipes)),
                ("calibration_note", Json::str("未校准")),
            ]));
        }
        Ok(units)
    }

    pub fn listing_json(&self) -> Result<Json, CoreError> {
        Ok(Json::object([("units", Json::Array(self.listing()?))]))
    }

    /// Port of `gxr_studio/render/recipes.py:101 Menu.request_for`.
    pub fn request_for(
        &self,
        sel: &RecipeSel,
    ) -> Result<(Vec<String>, Vec<i64>, Request), ResolveError> {
        self.request_for_given(sel, &sel.levels)
    }

    fn request_for_given(
        &self,
        sel: &RecipeSel,
        given: &[(String, RecipeLevel)],
    ) -> Result<(Vec<String>, Vec<i64>, Request), ResolveError> {
        if sel.kind != "builtin" {
            return Err(EditError::new("recipe_not_found", "找不到这个配方。请重新选择。").into());
        }
        let u = sel.unit_index().ok_or_else(err_unit_slot)?;
        let slot = sel.slot;
        let items = self.items(u as i64, slot)?;
        let mut names = Vec::with_capacity(items.len());
        for it in items {
            let c = self
                .data
                .controls
                .get(usize::from(it.control))
                .ok_or_else(err_unit_slot)?;
            names.push(c.name.clone());
        }
        for (name, value) in given {
            if !names.iter().any(|n| n == name) && value != &RecipeLevel::Int(neutral_of(name)) {
                return Err(EditError::with_detail(
                    "knob_not_available",
                    "无法保存配方。有一个旋钮无法使用。",
                    format!("{slot:02} {name}"),
                )
                .into());
            }
        }
        let mut levels_opt: Vec<Option<i64>> = Vec::with_capacity(items.len());
        let mut levels_i64: Vec<i64> = Vec::with_capacity(items.len());
        for (name, it) in names.iter().zip(items.iter()) {
            let default_lv = i64::from(it.default_level);
            match given.iter().rev().find(|(k, _)| k == name).map(|(_, v)| v) {
                None => {
                    levels_opt.push(Some(default_lv));
                    levels_i64.push(default_lv);
                }
                Some(RecipeLevel::Int(v)) => {
                    levels_opt.push(Some(*v));
                    levels_i64.push(*v);
                }
                Some(RecipeLevel::BigInt { .. }) => {
                    levels_opt.push(None);
                    levels_i64.push(default_lv);
                }
            }
        }
        match menu_request(&self.menu, u as i64, slot, &levels_opt) {
            Ok(req) => Ok((names, levels_i64, req)),
            Err(_) => {
                let bad = names
                    .iter()
                    .zip(levels_opt.iter())
                    .zip(items.iter())
                    .find_map(|((name, lv_opt), it)| {
                        let invalid = match lv_opt {
                            None => true,
                            Some(lv) => it.points.iter().all(|p| i64::from(p.level) != *lv),
                        };
                        invalid.then_some(name.as_str())
                    })
                    .unwrap_or("None");
                Err(EditError::with_detail(
                    "level_not_allowed",
                    "无法保存配方。这个档位不能用。",
                    format!("{bad} {slot:02}"),
                )
                .into())
            }
        }
    }
}

#[derive(Clone, Debug)]
struct ExtraInfo {
    kind: String,
    id: String,
    name: String,
    slot: u8,
    controls: Vec<ControlMeta>,
    defaults: Vec<(String, i64)>,
    entry_sha256: String,
    has_amount: bool,
}

impl ExtraInfo {
    fn to_json(&self, position: Option<usize>) -> Json {
        let controls: Vec<Json> = self
            .controls
            .iter()
            .map(|c| {
                let def = self
                    .defaults
                    .iter()
                    .find(|(k, _)| k == &c.name)
                    .map(|(_, v)| *v);
                c.to_json(def)
            })
            .collect();
        let mut pairs = vec![
            ("kind", Json::str(&self.kind)),
            ("id", Json::str(&self.id)),
            ("name", Json::str(&self.name)),
            ("slot", Json::Int(i64::from(self.slot))),
            ("controls", Json::Array(controls)),
            ("entry_sha256", Json::str(&self.entry_sha256)),
            ("has_amount", Json::Bool(self.has_amount)),
        ];
        if let Some(pos) = position {
            pairs.push(("position", Json::Int(pos as i64)));
            pairs.push(("number", Json::Str(format!("{pos:02}"))));
        }
        Json::object(pairs)
    }
}

#[derive(Clone, Debug)]
struct ExtraItem {
    info: ExtraInfo,
    entry: [u8; ENTRY_BYTES],
}

/// Port of `gxr_studio/render/recipes.py:120 CameraData` (from DAT bytes, without filesystem).
#[derive(Clone, Debug)]
pub struct DatCatalog {
    menu: MenuCatalog,
    generation: u32,
    entries: [[u8; ENTRY_BYTES]; 24],
    extras: BTreeMap<(String, String, usize), ExtraItem>,
    groups: BTreeMap<(usize, String), Vec<ExtraInfo>>,
}

impl DatCatalog {
    pub fn from_bytes(raw: &[u8], library: Option<&[u8]>) -> Result<Self, CoreError> {
        let (dat, entries) = entries_from_dat(raw, library)?;
        let mut menu_data = embedded_menu()?;
        if let Some(kv2) = dat.kv2_block.as_deref() {
            let mount_knobs_from_50mm = profile(dat.generation)
                .map(|p| p.mount_knobs_from_50mm)
                .unwrap_or(false);
            menu_data = read_knob_table(kv2, menu_data, mount_knobs_from_50mm)?;
        }
        let menu = MenuCatalog::from_menu_data(menu_data);
        let mut catalog = Self {
            menu,
            generation: dat.generation,
            entries,
            extras: BTreeMap::new(),
            groups: BTreeMap::new(),
        };
        for unit in 0..3 {
            for (i, rec) in dat.recipes.iter().enumerate() {
                if (rec.unit_mask >> unit) & 1 != 0 {
                    catalog.add_extra(unit, "record", i, &rec.name, rec.base, &rec.entry, &[])?;
                }
            }
            for (i, look) in dat.looks.iter().enumerate() {
                if (look.unit_mask >> unit) & 1 != 0 {
                    catalog.add_extra(unit, "look", i, &look.name, look.base, &look.entry, &[])?;
                }
            }
            for (i, user) in dat.users.iter().enumerate() {
                let defaults = [
                    ("contrast".to_string(), i64::from(user.levels[0])),
                    ("highlight".to_string(), i64::from(user.levels[1])),
                    ("shadow".to_string(), i64::from(user.levels[2])),
                    ("saturation".to_string(), i64::from(user.levels[3])),
                ];
                let slot_idx = usize::from(user.base.checked_sub(1).ok_or_else(err_unit_slot)?);
                let entry = *catalog
                    .entries
                    .get(unit * 8 + slot_idx)
                    .ok_or_else(err_unit_slot)?;
                let name = dat.user_names.get(i).map(String::as_str).unwrap_or("");
                catalog.add_extra(unit, "camera_user", i, name, user.base, &entry, &defaults)?;
            }
        }
        Ok(catalog)
    }

    #[allow(clippy::too_many_arguments)]
    fn add_extra(
        &mut self,
        unit: usize,
        kind: &str,
        index: usize,
        name: &str,
        base: u8,
        entry: &[u8; ENTRY_BYTES],
        defaults: &[(String, i64)],
    ) -> Result<(), CoreError> {
        let defaults_json = if defaults.is_empty() {
            "{}".to_string()
        } else {
            let mut sorted: Vec<&(String, i64)> = defaults.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            let body: Vec<String> = sorted
                .into_iter()
                .map(|(k, v)| format!("{}: {}", Json::str(k).render(), v))
                .collect();
            format!("{{{}}}", body.join(", "))
        };
        let prefix = format!("{kind}/{index}/{name}/");
        let mut hash_input = Vec::with_capacity(prefix.len() + entry.len() + defaults_json.len());
        hash_input.extend_from_slice(prefix.as_bytes());
        hash_input.extend_from_slice(entry);
        hash_input.extend_from_slice(defaults_json.as_bytes());
        let identity = sha256_hex(&hash_input);

        let controls = self.menu.controls_meta(unit as i64, i64::from(base))?;
        let has_amount = controls.iter().any(|c| c.name == "amount");
        let info = ExtraInfo {
            kind: kind.to_string(),
            id: identity.clone(),
            name: name.to_string(),
            slot: base,
            controls,
            defaults: defaults.to_vec(),
            entry_sha256: sha256_hex(entry),
            has_amount,
        };
        self.groups
            .entry((unit, kind.to_string()))
            .or_default()
            .push(info.clone());
        self.extras.insert(
            (kind.to_string(), identity, unit),
            ExtraItem {
                info,
                entry: *entry,
            },
        );
        Ok(())
    }

    pub fn controls(&self, unit: i64, slot: i64) -> Result<Vec<Json>, CoreError> {
        self.menu.controls(unit, slot)
    }

    pub fn controls_json(&self, unit: i64, slot: i64) -> Result<Json, CoreError> {
        self.menu.controls_json(unit, slot)
    }

    pub fn entry_sha256(&self, unit: usize, slot: u8) -> Option<String> {
        if unit >= 3 || !(1..=8).contains(&slot) {
            return None;
        }
        Some(sha256_hex(&self.entries[unit * 8 + usize::from(slot - 1)]))
    }

    /// Port of `gxr_studio/render/recipes.py:166 CameraData.listing`.
    pub fn listing(&self) -> Result<Vec<Json>, CoreError> {
        let lib_gen = profile(self.generation).and_then(|p| p.library_generation);
        let mut units = Vec::with_capacity(3);
        for (u, (id, label, lens)) in UNIT_INFO.iter().enumerate() {
            let recipes = if let Some(lg) = lib_gen {
                let count = if lg == 178 { 4usize } else { 8usize };
                let mut pos = 1usize;
                let mut items = Vec::new();
                for &slot in &SLOTS[..count] {
                    items.push(self.menu.builtin_recipe_json(
                        u,
                        slot,
                        self.entry_sha256(u, slot),
                        Some(pos),
                    )?);
                    pos += 1;
                }
                if let Some(records) = self.groups.get(&(u, "record".to_string())) {
                    for rec in records {
                        items.push(rec.to_json(Some(pos)));
                        pos += 1;
                    }
                }
                items
            } else {
                let mut items = Vec::with_capacity(8);
                for slot in SLOTS {
                    items.push(self.menu.builtin_recipe_json(
                        u,
                        slot,
                        self.entry_sha256(u, slot),
                        None,
                    )?);
                }
                items
            };
            let looks: Vec<Json> = self
                .groups
                .get(&(u, "look".to_string()))
                .map(|v| v.iter().map(|it| it.to_json(None)).collect())
                .unwrap_or_default();
            let users: Vec<Json> = self
                .groups
                .get(&(u, "camera_user".to_string()))
                .map(|v| v.iter().map(|it| it.to_json(None)).collect())
                .unwrap_or_default();
            units.push(Json::object([
                ("id", Json::str(*id)),
                ("label", Json::str(*label)),
                ("lens", Json::str(*lens)),
                ("calibrated", Json::Bool(false)),
                ("recipes", Json::Array(recipes)),
                ("calibration_note", Json::str("未校准")),
                ("looks", Json::Array(looks)),
                ("users", Json::Array(users)),
            ]));
        }
        Ok(units)
    }

    pub fn listing_json(&self) -> Result<Json, CoreError> {
        Ok(Json::object([("units", Json::Array(self.listing()?))]))
    }

    fn extra_for(&self, sel: &RecipeSel) -> Result<&ExtraItem, EditError> {
        let u = sel
            .unit_index()
            .ok_or_else(|| EditError::new("recipe_not_found", "找不到这个配方。请重新选择。"))?;
        let id = sel.id.as_deref().unwrap_or("");
        let item = self
            .extras
            .get(&(sel.kind.clone(), id.to_string(), u))
            .ok_or_else(|| EditError::new("recipe_not_found", "找不到这个配方。请重新选择。"))?;
        if i64::from(item.info.slot) != sel.slot {
            return Err(EditError::new(
                "recipe_not_found",
                "找不到这个配方。请重新选择。",
            ));
        }
        Ok(item)
    }

    /// Port of `gxr_studio/render/recipes.py:186 CameraData.request_for`.
    pub fn request_for(
        &self,
        sel: &RecipeSel,
    ) -> Result<(Vec<String>, Vec<i64>, Request), ResolveError> {
        if sel.kind == "builtin" {
            if profile(self.generation).and_then(|p| p.library_generation) == Some(178)
                && sel.slot > 4
            {
                return Err(EditError::new(
                    "recipe_not_found",
                    format!(
                        "g{} 已移除这个旧配方入口，请从完整配方列表重新选择。",
                        self.generation
                    ),
                )
                .into());
            }
            return self.menu.request_for(sel);
        }
        let extra = self.extra_for(sel)?;
        let mut given: Vec<(String, RecipeLevel)> = extra
            .info
            .defaults
            .iter()
            .map(|(k, v)| (k.clone(), RecipeLevel::Int(*v)))
            .collect();
        for (k, v) in &sel.levels {
            if let Some((_, slot)) = given.iter_mut().find(|(ek, _)| ek == k) {
                *slot = v.clone();
            } else {
                given.push((k.clone(), v.clone()));
            }
        }
        let builtin_sel = RecipeSel {
            unit: sel.unit.clone(),
            slot: sel.slot,
            levels: Vec::new(),
            kind: "builtin".to_string(),
            id: None,
        };
        self.menu.request_for_given(&builtin_sel, &given)
    }

    /// Port of `gxr_studio/render/recipes.py:199 CameraData.resolve`.
    pub fn resolve(&self, sel: &RecipeSel) -> Result<Resolved, ResolveError> {
        let (names, levels, req) = self.request_for(sel)?;
        let u = sel.unit_index().ok_or_else(err_unit_slot)?;
        let slot = usize::try_from(sel.slot).map_err(|_| err_unit_slot())?;
        if slot == 0 || slot > 8 {
            return Err(err_unit_slot().into());
        }
        let stock = &self.entries[u * 8 + 3];
        let base = if sel.kind == "builtin" {
            &self.entries[u * 8 + (slot - 1)]
        } else {
            &self.extra_for(sel)?.entry
        };
        let (entry_vec, stats) = apply(stock, base, &req)?;
        let entry: [u8; ENTRY_BYTES] = entry_vec
            .try_into()
            .map_err(|_| CoreError::new("value", "entry length"))?;
        let entry_sha256 = sha256_hex(&entry);
        let levels_map: Vec<(String, i64)> = names.into_iter().zip(levels).collect();
        Ok(Resolved {
            entry,
            entry_sha256,
            levels: levels_map,
            stats,
        })
    }

    /// Port of `gxr_studio/render/recipes.py:206 CameraData.default_selection`.
    pub fn default_selection(&self, unit: &str) -> Result<RecipeSel, EditError> {
        if !UNITS.contains(&unit) {
            return Err(EditError::with_detail(
                "bad_recipe",
                super::error::INVALID_REQUEST,
                "镜头单元无法识别。",
            ));
        }
        Ok(RecipeSel {
            unit: unit.to_string(),
            slot: 4,
            levels: Vec::new(),
            kind: "builtin".to_string(),
            id: None,
        })
    }
}

/// Port of `gxr_studio/render/recipes.py:211 unit_of_lens`.
pub fn unit_of_lens(lens: Option<&str>, model: Option<&str>) -> Option<&'static str> {
    let text = format!("{} {}", lens.unwrap_or(""), model.unwrap_or("")).to_lowercase();
    if text.contains("mount") {
        Some("l08firm")
    } else if text.contains("50mm") {
        Some("jlaunch")
    } else if text.contains("28mm") {
        Some("qlaunch")
    } else {
        None
    }
}
