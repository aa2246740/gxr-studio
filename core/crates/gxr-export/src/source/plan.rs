//! Port of `render/service.py` `RenderService.plan` and the non-precise `renderer`, without
//! the home folder: the camera data is the DAT the user picked, the calibration pack is a
//! list of named files, and the resolved recipe entry is an input (the resolve unit makes it).

use gxr_color::{ChromaBake, Table, UnitModel, bake_chroma, channel_gains, display_table};
use gxr_raw::CoreError;
use gxr_raw::sha256::sha256_hex;

use super::fail::Fail;
use super::input::{Edit, RecipeSel, Shot, UNITS};

/// `service.ISO_SPLIT`.
pub const ISO_SPLIT: i64 = 566;
/// `service.EXPORT_STEP`.
pub const EXPORT_STEP: f64 = 0.375;
pub const UNCALIBRATED_LABEL: &str = "电脑预测 未校准";
pub const CALIBRATED_LABEL: &str = "电脑预测";
pub const MODEL_NOTE: &str = "镜头单元、配方单元或 ISO 不在实拍校准范围";

/// Port of gxr_studio/render/recipes.py:211 unit_of_lens.
pub fn unit_of_lens(lens: Option<&str>, model: Option<&str>) -> Option<&'static str> {
    let text = format!("{} {}", lens.unwrap_or(""), model.unwrap_or("")).to_lowercase();
    if text.contains("mount") {
        return Some("l08firm");
    }
    if text.contains("50mm") {
        return Some("jlaunch");
    }
    if text.contains("28mm") {
        return Some("qlaunch");
    }
    None
}

/// `calib.UNIT_ISOS`.
fn unit_isos(unit: &str) -> &'static [i64] {
    match unit {
        "jlaunch" => &[200, 1600],
        _ => &[200],
    }
}

/// A calibration pack as the phone receives it: the files the user picked, by name.
#[derive(Clone, Debug, Default)]
pub struct CalibPack {
    pub files: Vec<(String, Vec<u8>)>,
}

impl CalibPack {
    /// The file called `name` (the last one, if the picker gave two).
    pub fn read(&self, name: &str) -> Option<Vec<u8>> {
        self.files
            .iter()
            .rev()
            .find(|(n, _)| n == name)
            .map(|(_, b)| b.clone())
    }
}

/// `RenderService._camera` / `_camera_error` after `_refresh`.
#[derive(Clone, Debug)]
pub enum Camera {
    /// No DAT imported (`camera_data_missing`).
    Missing,
    /// `CameraData(home)` failed (`camera_data_invalid`).
    Invalid,
    /// The 24 entries in `(unit 0..2, slot 1..8)` order.
    Ready(Vec<[u8; gxr_camdata::ENTRY_BYTES]>),
}

impl Camera {
    /// `CameraData(home)` from the DAT bytes (and the GXRFX.BIN library for r5 cards): the
    /// entries and the knob table must both read, as in `recipes.py:123-149`.
    pub fn from_dat(dat: Option<&[u8]>, library: Option<&[u8]>) -> Self {
        let Some(raw) = dat else {
            return Self::Missing;
        };
        let Ok((_dat, entries)) = gxr_camdata::entries_from_dat(raw, library) else {
            return Self::Invalid;
        };
        if gxr_camdata::resolve_menu(Some((raw, library))).is_err() {
            return Self::Invalid;
        }
        Self::Ready(entries.to_vec())
    }

    /// `store.load_stock04(home, unit)`.
    pub fn stock04(&self, unit: &str) -> Option<&[u8]> {
        let Self::Ready(entries) = self else {
            return None;
        };
        let u = UNITS.iter().position(|n| *n == unit)?;
        entries.get(u * 8 + 3).map(<[u8; 552]>::as_slice)
    }
}

/// Port of gxr_studio/render/service.py:99 Plan (the fields a builtin selection fills).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    pub unit: String,
    pub slot: i64,
    pub kind: String,
    pub defaulted: bool,
    pub entry: Vec<u8>,
    pub entry_sha256: String,
    pub model_iso: i64,
    pub calibrated: bool,
    pub reasons: Vec<&'static str>,
    pub label: &'static str,
    pub record_sha256: String,
    pub binding_sha256: String,
}

/// Everything `plan` and `renderer` read besides the shot and the edit.
#[derive(Clone, Copy, Debug)]
pub struct Context<'a> {
    pub camera: &'a Camera,
    pub pack: Option<&'a CalibPack>,
    /// `CameraData.resolve(sel).entry` for the selection `plan` makes.
    pub entry: Option<&'a [u8]>,
    /// `RenderService.engine_pin()`: the desktop derives it from files in its home folder,
    /// so the caller supplies it.
    pub binding: &'a str,
}

/// `calib.model_for(home, iso, unit=unit)`: the pick and the built model.
fn model_for(ctx: &Context<'_>, iso: i64, unit: &str) -> Result<UnitModel, CoreError> {
    let missing = || CoreError::new("file_not_found", "缺少校准数据");
    let pack = ctx.pack.ok_or_else(missing)?;
    let manifest = pack.read("manifest.json").ok_or_else(missing)?;
    let stock = ctx.camera.stock04(unit).ok_or_else(missing)?;
    let read = |name: &str| pack.read(name);
    let (_pick, ctx_bytes, ring) = gxr_color::model_for(&manifest, stock, iso, unit, &read)?;
    gxr_color::unit_model(stock, &ctx_bytes, &ring)
}

/// Port of gxr_studio/render/service.py:293 _require.
fn require(ctx: &Context<'_>, unit: &str) -> Result<(), Fail> {
    match ctx.camera {
        Camera::Missing => {
            return Err(Fail::unavailable(
                "camera_data_missing",
                "无法生成电脑预测。请先导入相机配方文件。",
                "missing",
            ));
        }
        Camera::Invalid => {
            return Err(Fail::unavailable(
                "camera_data_invalid",
                "无法读取相机配方文件。请重新导入后再试。",
                "invalid",
            ));
        }
        Camera::Ready(_) => {}
    }
    // `unit_status(home, unit)` is "ready" exactly when `model_for(home, 200, unit=unit)`
    // builds; any unit being ready clears `_calib_error`, so this unit's status decides.
    if model_for(ctx, 200, unit).is_err() {
        return Err(Fail::unavailable(
            "calibration_missing",
            "无法生成电脑预测。请先导入校准数据。",
            "calibration",
        ));
    }
    Ok(())
}

/// `edit.recipe or RecipeSel(photo_unit or "jlaunch", 4)`, and whether it was defaulted.
pub fn selection(shot: &Shot, recipe: Option<&RecipeSel>) -> (RecipeSel, bool) {
    match recipe {
        Some(sel) => (sel.clone(), false),
        None => (
            RecipeSel {
                kind: "builtin".to_string(),
                unit: unit_of_lens(shot.lens.as_deref(), None)
                    .unwrap_or("jlaunch")
                    .to_string(),
                slot: 4,
                id: None,
            },
            true,
        ),
    }
}

/// Port of gxr_studio/render/service.py:340 RenderService.plan.
///
/// Local recipes live in the desktop library (`recipes/repository.py`), which the phone does
/// not have: after the pair check Python makes, they are refused with `local_recipe_unsupported`.
pub fn plan(shot: &Shot, recipe: Option<&RecipeSel>, ctx: &Context<'_>) -> Result<Plan, Fail> {
    let photo_unit = unit_of_lens(shot.lens.as_deref(), None);
    let (sel, defaulted) = selection(shot, recipe);
    if sel.kind == "local" {
        if !(shot.has_dng && shot.has_jpg) {
            return Err(Fail::unavailable(
                "local_recipe_needs_pair",
                "完整本机配方需要RAW+JPG照片。",
                "unsupported",
            ));
        }
        return Err(Fail::unavailable(
            "local_recipe_unsupported",
            "手机上还不能用电脑里的本机配方。",
            "local_recipe",
        ));
    }
    require(ctx, &sel.unit)?;
    let entry = ctx.entry.ok_or_else(|| {
        Fail::Core(CoreError::new(
            "bad_request",
            "需要这个配方解析后的条目（entry）",
        ))
    })?;
    let entry_sha256 = sha256_hex(entry);
    let iso = shot.iso;
    let model_iso = if iso.is_some_and(|i| i != 0 && i >= ISO_SPLIT) {
        1600
    } else {
        200
    };
    let crossed = photo_unit.is_none()
        || !iso.is_some_and(|i| unit_isos(&sel.unit).contains(&i))
        || Some(sel.unit.as_str()) != photo_unit;
    let reasons = if crossed { vec![MODEL_NOTE] } else { vec![] };
    Ok(Plan {
        unit: sel.unit,
        slot: sel.slot,
        kind: sel.kind,
        defaulted,
        entry: entry.to_vec(),
        record_sha256: entry_sha256.clone(),
        entry_sha256,
        model_iso,
        calibrated: reasons.is_empty(),
        label: if reasons.is_empty() {
            CALIBRATED_LABEL
        } else {
            UNCALIBRATED_LABEL
        },
        reasons,
        binding_sha256: ctx.binding.to_string(),
    })
}

/// The labelling half of `raw.Renderer` for a recipe prediction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RendererInfo {
    pub id: &'static str,
    pub label: &'static str,
    pub filename_tag: &'static str,
    pub calibrated: bool,
    pub description: String,
}

/// The `desc` text and `Renderer` fields of `renderer(..., precise=False)` (service.py:562).
pub fn renderer_info(plan: &Plan) -> RendererInfo {
    let description = format!(
        "GXR Studio computer prediction (not camera-measured): {} recipe {:02}{}, ISO model {}{}. {} Record SHA256 {}; entry SHA256 {}; binding {}.",
        plan.unit,
        plan.slot,
        if plan.defaulted {
            " (stock 04 simulation)"
        } else {
            ""
        },
        plan.model_iso,
        if plan.calibrated {
            ""
        } else {
            ", NOT calibrated for this lens unit/ISO"
        },
        plan.label,
        plan.record_sha256,
        plan.entry_sha256,
        plan.binding_sha256,
    );
    RendererInfo {
        id: if plan.calibrated {
            "recipe-prediction"
        } else {
            "recipe-prediction-uncalibrated"
        },
        label: plan.label,
        filename_tag: "",
        calibrated: plan.calibrated,
        description,
    }
}

/// What a recipe renderer's `display` uses: the bake, the adjustment gains and the tone table.
pub struct RecipeDisplay {
    pub bake: ChromaBake,
    pub gains: Option<[f64; 3]>,
    pub table: Option<Table>,
}

/// A recipe renderer: its plan, its labels and its `display`.
pub struct RecipeRenderer {
    pub plan: Plan,
    pub info: RendererInfo,
    pub display: RecipeDisplay,
}

/// `chain.bake_chroma(model_for(iso, unit), entry, EXPORT_STEP)` with the edit's gains and
/// tone table: the `display` of `renderer(..., precise=False)` for a known entry and model.
pub fn recipe_display(
    pack: &CalibPack,
    stock04: &[u8],
    unit: &str,
    iso: i64,
    entry: &[u8],
    adjust: &gxr_color::Adjust,
    curves: Option<&gxr_color::Curves>,
) -> Result<RecipeDisplay, CoreError> {
    let gains = channel_gains(adjust);
    let table = display_table(adjust, curves)?;
    let missing = || CoreError::new("file_not_found", "缺少校准数据");
    let manifest = pack.read("manifest.json").ok_or_else(missing)?;
    let read = |name: &str| pack.read(name);
    let (_pick, ctx_bytes, ring) = gxr_color::model_for(&manifest, stock04, iso, unit, &read)?;
    let model = gxr_color::unit_model(stock04, &ctx_bytes, &ring)?;
    let bake = bake_chroma(&model, entry, EXPORT_STEP)?;
    Ok(RecipeDisplay { bake, gains, table })
}

/// Port of gxr_studio/render/service.py:546 RenderService.renderer, `precise=False`.
///
/// `self.model(...)` repeats `_require`, which `plan` has just passed on the same inputs.
pub fn renderer(shot: &Shot, edit: &Edit, ctx: &Context<'_>) -> Result<RecipeRenderer, Fail> {
    let plan = plan(shot, edit.recipe.as_ref(), ctx)?;
    let pack = ctx
        .pack
        .ok_or_else(|| CoreError::new("file_not_found", "缺少校准数据"))?;
    let stock = ctx
        .camera
        .stock04(&plan.unit)
        .ok_or_else(|| CoreError::new("file_not_found", "缺少原厂 04"))?;
    let display = recipe_display(
        pack,
        stock,
        &plan.unit,
        plan.model_iso,
        &plan.entry,
        &edit.adjust,
        edit.curves.as_ref(),
    )?;
    let info = renderer_info(&plan);
    Ok(RecipeRenderer {
        plan,
        info,
        display,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lens_units() {
        assert_eq!(
            unit_of_lens(Some("GR LENS A12 50mm F2.5 MACRO"), None),
            Some("jlaunch")
        );
        assert_eq!(
            unit_of_lens(Some("RICOH LENS A12 28mm"), None),
            Some("qlaunch")
        );
        assert_eq!(unit_of_lens(Some("GXR MOUNT A12"), None), Some("l08firm"));
        assert_eq!(unit_of_lens(Some("S10 24-72mm"), None), None);
        assert_eq!(unit_of_lens(None, None), None);
    }

    #[test]
    fn missing_camera_comes_first() {
        let shot = Shot {
            has_dng: true,
            has_jpg: false,
            dng_name: String::new(),
            lens: None,
            iso: Some(200),
        };
        let ctx = Context {
            camera: &Camera::Missing,
            pack: None,
            entry: None,
            binding: "",
        };
        let err = plan(&shot, None, &ctx).unwrap_err();
        assert!(matches!(err, Fail::Unavailable { ref code, .. } if code == "camera_data_missing"));
    }
}
