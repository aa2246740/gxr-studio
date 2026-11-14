//! The calls `gxr-cli` and `gxr-wasm` share, so both print the same bytes.

use gxr_lut::{Cube, RenderSpec};
use gxr_raw::{CoreError, Dng, Image, Json};

use super::fail::Fail;
use super::input::{Shot, parse_edit, parse_shot, parse_tones};
use super::lut::LutSource;
use super::plan::{CalibPack, Camera, Context, plan, recipe_display, renderer_info, selection};
use super::render::render_recipe;
use super::select::{Sources, color_image};

/// The raw inputs of one source command, as bytes.
#[derive(Default)]
pub struct Request<'a> {
    pub shot: &'a [u8],
    pub edit: &'a [u8],
    pub dat: Option<&'a [u8]>,
    pub library: Option<&'a [u8]>,
    pub entry: Option<&'a [u8]>,
    pub pack: Option<&'a CalibPack>,
    pub binding: &'a str,
}

/// A calibration pack from `[[name, bytes], ...]` (`index_json`) over the concatenated files,
/// the way `frame_layout` takes its fonts.
pub fn pack_from_index(index_json: &[u8], blob: &[u8]) -> Result<CalibPack, CoreError> {
    let bad = || CoreError::new("bad_request", "校准文件清单应为 [[名字, 字节数], ...]");
    let index = gxr_color::parse_json(index_json)?;
    let items = index.as_array().ok_or_else(bad)?;
    let mut files = Vec::with_capacity(items.len().min(64));
    let mut at: usize = 0;
    for item in items {
        let pair = item.as_array().ok_or_else(bad)?;
        let [name, len] = pair else {
            return Err(bad());
        };
        let name = name.as_str().ok_or_else(bad)?;
        let len = len
            .as_int()
            .and_then(|n| usize::try_from(n).ok())
            .ok_or_else(bad)?;
        let end = at
            .checked_add(len)
            .filter(|&e| e <= blob.len())
            .ok_or_else(|| CoreError::new("bad_request", "校准文件清单比字节长"))?;
        files.push((name.to_string(), blob[at..end].to_vec()));
        at = end;
    }
    if at != blob.len() {
        return Err(CoreError::new("bad_request", "校准文件清单比字节短"));
    }
    Ok(CalibPack { files })
}

fn sel_json(sel: &super::input::RecipeSel, defaulted: bool) -> Json {
    Json::object([
        ("kind", Json::str(sel.kind.clone())),
        ("unit", Json::str(sel.unit.clone())),
        ("slot", Json::Int(sel.slot)),
        ("defaulted", Json::Bool(defaulted)),
    ])
}

/// `source-plan`: `RenderService.plan` and the non-precise renderer's labels.
/// Returns whether it succeeded and the JSON text.
pub fn plan_json(req: &Request<'_>) -> (bool, String) {
    match plan_inner(req) {
        Ok(json) => (true, json.render()),
        Err(fail) => (false, fail.to_json().render()),
    }
}

fn plan_inner(req: &Request<'_>) -> Result<Json, Fail> {
    let shot = parse_shot(req.shot)?;
    let edit = parse_edit(req.edit)?;
    let camera = Camera::from_dat(req.dat, req.library);
    let ctx = Context {
        camera: &camera,
        pack: req.pack,
        entry: req.entry,
        binding: req.binding,
    };
    let (sel, defaulted) = selection(&shot, edit.recipe.as_ref());
    let p = plan(&shot, edit.recipe.as_ref(), &ctx)?;
    let info = renderer_info(&p);
    let reasons: Vec<Json> = p.reasons.iter().map(|r| Json::str(*r)).collect();
    Ok(Json::object([
        ("selection", sel_json(&sel, defaulted)),
        (
            "plan",
            Json::object([
                ("unit", Json::str(p.unit.clone())),
                ("slot", Json::Int(p.slot)),
                ("defaulted", Json::Bool(p.defaulted)),
                ("entry_sha256", Json::str(p.entry_sha256.clone())),
                ("model_iso", Json::Int(p.model_iso)),
                ("calibrated", Json::Bool(p.calibrated)),
                ("reasons", Json::Array(reasons)),
                ("label", Json::str(p.label)),
                ("record_sha256", Json::str(p.record_sha256.clone())),
                ("binding_sha256", Json::str(p.binding_sha256.clone())),
                ("selection_id", Json::Null),
            ]),
        ),
        (
            "renderer",
            Json::object([
                ("id", Json::str(info.id)),
                ("label", Json::str(info.label)),
                ("filename_tag", Json::str(info.filename_tag)),
                ("calibrated", Json::Bool(info.calibrated)),
                ("description", Json::str(info.description)),
                ("recipe_binding", Json::Null),
            ]),
        ),
    ]))
}

/// `source-selection`: which recipe `color_image` will plan for this base mode, so the caller
/// knows which entry to resolve. `null` when the picture does not come from a recipe render.
pub fn selection_json(shot: &[u8], edit: &[u8], base: &str) -> Result<String, CoreError> {
    let shot: Shot = parse_shot(shot)?;
    let edit = parse_edit(edit)?;
    let mode = if base == "auto" {
        if edit.recipe.is_some() {
            "recipe"
        } else if shot.has_jpg {
            "camera-jpg"
        } else {
            "standard"
        }
    } else {
        base
    };
    let recipe = match mode {
        "camera-jpg" => return Ok(Json::object([("selection", Json::Null)]).render()),
        "recipe" => edit.recipe.as_ref(),
        _ => None,
    };
    let (sel, defaulted) = selection(&shot, recipe);
    Ok(Json::object([("selection", sel_json(&sel, defaulted))]).render())
}

/// `source-render`: `render_full(open_dng(path), renderer)` for a recipe renderer built from
/// the entry, the unit's stock 04, the calibration pack at `iso` and the adjustments.
#[allow(clippy::too_many_arguments)]
pub fn render(
    dng: &[u8],
    name: &str,
    entry: &[u8],
    stock04: &[u8],
    unit: &str,
    pack: &CalibPack,
    iso: i64,
    tones: &[u8],
) -> Result<Image<u8>, CoreError> {
    let (adjust, curves) = parse_tones(tones)?;
    let display = recipe_display(pack, stock04, unit, iso, entry, &adjust, curves.as_ref())?;
    let dng = Dng::open(dng, name)?;
    render_recipe(&dng, &display)
}

/// The decoded pictures and the LUT `source-color-image` may need.
#[derive(Default)]
pub struct Pictures<'a> {
    pub dng: Option<&'a [u8]>,
    pub jpg: Option<&'a Image<u8>>,
    pub preview: Option<&'a Image<u8>>,
    pub cube: Option<&'a [u8]>,
    pub space: Option<&'a str>,
}

/// `finish.serve.export_finish`'s handling of a `RawDecodeError` from the source picture.
fn export_error(fail: Fail, has_jpg: bool) -> Fail {
    if !fail.is_raw_decode() {
        return fail;
    }
    let detail = format!("没有全尺寸传感器数据，也没有更小的预览。{}", fail.text());
    let (code, message) = if has_jpg {
        ("raw_unreadable", "无法读取 RAW 文件。文件可能已损坏。")
    } else {
        ("no_jpg_source", "无法读取 RAW 文件。这张照片没有相机 JPG。")
    };
    Fail::Edit {
        code: code.to_string(),
        message: message.to_string(),
        detail,
    }
}

/// `source-color-image`: the export's base picture. Returns the JSON text and the picture,
/// or the error JSON text.
pub fn color_image_json(
    req: &Request<'_>,
    base: &str,
    pics: &Pictures<'_>,
) -> Result<(String, Image<u8>), String> {
    let shot = parse_shot(req.shot).map_err(|e| Fail::Core(e).to_json().render())?;
    let has_jpg = shot.has_jpg;
    color_image_inner(req, &shot, base, pics)
        .map_err(|fail| export_error(fail, has_jpg).to_json().render())
}

fn color_image_inner(
    req: &Request<'_>,
    shot: &Shot,
    base: &str,
    pics: &Pictures<'_>,
) -> Result<(String, Image<u8>), Fail> {
    let edit = parse_edit(req.edit)?;
    let camera = Camera::from_dat(req.dat, req.library);
    let lut = match (edit.lut.as_ref(), pics.cube) {
        (Some(_), Some(cube)) => Some(LutSource {
            cube: Cube::read_bytes(cube),
            spec: RenderSpec::from_json(pics.space.unwrap_or("{}")),
        }),
        _ => None,
    };
    let src = Sources {
        shot,
        dng: pics.dng,
        jpg: pics.jpg,
        preview: pics.preview,
        ctx: Context {
            camera: &camera,
            pack: req.pack,
            entry: req.entry,
            binding: req.binding,
        },
        lut: lut.as_ref(),
    };
    let picked = color_image(&src, &edit, base)?;
    let shape = [picked.rgb.height, picked.rgb.width, picked.rgb.channels]
        .iter()
        .map(|&d| i64::try_from(d).map(Json::Int))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| CoreError::new("value", "picture too big"))?;
    let json = Json::object([
        ("path", Json::str(picked.path)),
        ("from_raw", Json::Bool(picked.from_raw)),
        ("labels", picked.labels.to_json()),
        ("shape", Json::Array(shape)),
    ]);
    Ok((json.render(), picked.rgb))
}
