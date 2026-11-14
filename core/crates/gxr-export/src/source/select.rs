//! Port of gxr_studio/finish/serve.py:189 color_image and :118 _raw_rgb (`full=True`): which
//! picture an export starts from, its labels, and whether it came from the RAW renderer.
//!
//! Instead of the library the caller passes the DNG bytes, the camera JPG already decoded and
//! EXIF-transposed (`load_jpeg(path, None)`; the browser's `createImageBitmap`), and the
//! largest embedded DNG preview already decoded (`_embedded_rgb`).

use gxr_raw::{CoreError, Dng, Image};

use super::fail::{Fail, Labels, NO_RAW};
use super::input::{Edit, Shot};
use super::lut::{LutSource, lut_image};
use super::plan::{Context, renderer};
use super::render::render_recipe;

/// What `color_image` reads besides the edit and the base mode.
#[derive(Clone, Copy)]
pub struct Sources<'a> {
    pub shot: &'a Shot,
    pub dng: Option<&'a [u8]>,
    pub jpg: Option<&'a Image<u8>>,
    pub preview: Option<&'a Image<u8>>,
    pub ctx: Context<'a>,
    pub lut: Option<&'a LutSource>,
}

/// `color_image`'s `(rgb, from_raw, labels)`, plus which branch produced the pixels.
#[derive(Clone, Debug)]
pub struct Picked {
    pub rgb: Image<u8>,
    pub from_raw: bool,
    pub labels: Labels,
    /// `camera-jpg`, `camera-jpg-fallback`, `raw-recipe`, `raw-neutral`, `raw-preview`,
    /// `lut-display` or `lut-log`.
    pub path: &'static str,
}

fn need(what: &str) -> Fail {
    Fail::Core(CoreError::new("bad_request", format!("需要{what}")))
}

/// `lib.abspath(files["jpg"]["relpath"])` decoded: the caller must pass it when used.
fn camera_jpg(src: &Sources<'_>) -> Result<Image<u8>, Fail> {
    src.jpg.cloned().ok_or_else(|| need("相机 JPG 的像素"))
}

fn is_local(edit: &Edit) -> bool {
    edit.recipe.as_ref().is_some_and(|r| r.kind == "local")
}

/// `open_dng(path)` on the shot's DNG.
pub fn open_dng<'a>(src: &Sources<'a>) -> Result<Dng<'a>, Fail> {
    let data = src.dng.ok_or_else(|| need("DNG 文件"))?;
    Ok(Dng::open(data, &src.shot.dng_name)?)
}

/// Port of gxr_studio/finish/serve.py:118 _raw_rgb with `full=True`.
pub fn raw_rgb(src: &Sources<'_>, edit: &Edit) -> Result<(Image<u8>, Labels, &'static str), Fail> {
    let dng = match open_dng(src) {
        Ok(dng) => dng,
        Err(Fail::Core(e)) if e.kind == "raw_decode" => {
            if is_local(edit) {
                return Err(Fail::Core(e));
            }
            let Some(preview) = src.preview else {
                return Err(Fail::Core(e));
            };
            let detail = format!(
                "没有全尺寸传感器数据。已改用 {}×{} 的预览。",
                preview.width, preview.height
            );
            let labels = Labels::of(&[
                ("kind", "original"),
                ("label", "original"),
                ("text", "原图"),
                ("id", "neutral-preview"),
                ("tag", "_neutral"),
                ("description", ""),
                ("detail", &detail),
                ("key", ""),
                ("cache", "miss"),
                ("calibrated", "0"),
                ("limited", "1"),
            ]);
            return Ok((preview.clone(), labels, "raw-preview"));
        }
        Err(e) => return Err(e),
    };
    let recipe = match renderer(src.shot, edit, &src.ctx) {
        Ok(r) => Some(r),
        Err(Fail::Unavailable { .. }) if edit.recipe.is_none() => None,
        Err(e) => return Err(e),
    };
    let Some(r) = recipe else {
        let rgb = gxr_raw::render_full(&dng)?;
        let labels = Labels::of(&[
            ("kind", "original"),
            ("label", "original"),
            ("text", "中性转换"),
            ("id", "neutral-preview"),
            ("tag", "_neutral"),
            (
                "description",
                "GXR Studio neutral-preview: white-balanced linear sensor data to sRGB, no recipe applied",
            ),
            ("detail", ""),
            ("key", ""),
            ("cache", "miss"),
            ("calibrated", "0"),
        ]);
        return Ok((rgb, labels, "raw-neutral"));
    };
    let rgb = render_recipe(&dng, &r.display)?;
    // `renderer.description` is never empty for a recipe, so `note` is the description.
    // `recipe_binding` is None: only local recipes carry a `selection_id`.
    let labels = Labels::of(&[
        ("kind", "prediction"),
        ("label", "prediction"),
        ("text", r.info.label),
        ("id", r.info.id),
        ("tag", r.info.filename_tag),
        ("description", &r.info.description),
        ("detail", ""),
        ("key", ""),
        ("cache", "miss"),
        ("calibrated", if r.info.calibrated { "1" } else { "0" }),
    ]);
    Ok((rgb, labels, "raw-recipe"))
}

fn camera_labels(detail: &str) -> Labels {
    Labels::of(&[
        ("kind", "original"),
        ("label", "original"),
        ("text", "原图"),
        ("id", "camera-jpg"),
        ("tag", ""),
        ("description", "原图"),
        ("detail", detail),
        ("calibrated", "0"),
        ("cache", "miss"),
        ("key", ""),
    ])
}

/// Port of gxr_studio/finish/serve.py:189 color_image with `full=True` (`max_edge` unused).
pub fn color_image(src: &Sources<'_>, edit: &Edit, base: &str) -> Result<Picked, Fail> {
    if edit.lut.is_some() {
        return lut_image(src, edit, base);
    }
    let shot = src.shot;
    let (has_jpg, has_dng) = (shot.has_jpg, shot.has_dng);
    let mode = if base == "auto" {
        if edit.recipe.is_some() {
            "recipe"
        } else if has_jpg {
            "camera-jpg"
        } else {
            "standard"
        }
    } else {
        base
    };
    if mode == "camera-jpg" {
        if !has_jpg {
            return Err(Fail::NoRaw(NO_RAW.to_string()));
        }
        return Ok(Picked {
            rgb: camera_jpg(src)?,
            from_raw: false,
            labels: camera_labels(""),
            path: "camera-jpg",
        });
    }
    if mode == "neutral" {
        if !has_dng {
            return Err(Fail::NoRaw(NO_RAW.to_string()));
        }
        // `EditState()`: no recipe, so `renderer` plans the unit's stock 04 when it can, and
        // falls back to NEUTRAL only on `RenderUnavailable`. The labels say neutral either way.
        let (rgb, mut labels, path) = raw_rgb(src, &Edit::empty())?;
        labels.set("kind", "original");
        labels.set("label", "original");
        labels.set("text", "中性转换");
        labels.set("id", "neutral-preview");
        labels.set("tag", "_neutral");
        if labels.get("description").is_none_or(str::is_empty) {
            labels.set(
                "description",
                "GXR Studio neutral-preview: white-balanced linear sensor data to sRGB, no recipe applied",
            );
        }
        return Ok(Picked {
            rgb,
            from_raw: true,
            labels,
            path,
        });
    }
    if !has_dng {
        if is_local(edit) {
            return Err(Fail::edit(
                "local_recipe_needs_pair",
                "完整本机配方需要RAW+JPG照片。",
            ));
        }
        // `approx.ENABLED` is False, so the approximate-recipe branch always raises NoRaw.
        return Err(Fail::NoRaw(NO_RAW.to_string()));
    }
    let raw_edit = if mode == "recipe" {
        edit.clone()
    } else {
        Edit {
            recipe: None,
            lut: None,
            adjust: edit.adjust,
            curves: edit.curves.clone(),
        }
    };
    let (rgb, mut labels, path) = match raw_rgb(src, &raw_edit) {
        Ok(found) => found,
        Err(exc) if exc.is_caught_by_color_image() => {
            if is_local(&raw_edit) || !has_jpg {
                return Err(exc);
            }
            let detail = format!("没有全尺寸传感器数据。已改用相机 JPG。{}", exc.text());
            return Ok(Picked {
                rgb: camera_jpg(src)?,
                from_raw: false,
                labels: camera_labels(&detail),
                path: "camera-jpg-fallback",
            });
        }
        Err(exc) => return Err(exc),
    };
    if mode == "standard" && labels.get("id") != Some("neutral-preview") {
        labels.set("kind", "prediction");
        labels.set("label", "prediction");
    }
    Ok(Picked {
        rgb,
        from_raw: true,
        labels,
        path,
    })
}
