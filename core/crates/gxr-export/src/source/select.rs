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

/// Port of gxr_studio/finish/serve.py:118 _raw_rgb with `full=True`. `neutral` skips the
/// renderer plan and always renders NEUTRAL (base mode `neutral`).
pub fn raw_rgb(
    src: &Sources<'_>,
    edit: &Edit,
    neutral: bool,
) -> Result<(Image<u8>, Labels, &'static str), Fail> {
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
    let recipe = if neutral {
        None
    } else {
        match renderer(src.shot, edit, &src.ctx) {
            Ok(r) => Some(r),
            Err(Fail::Unavailable { .. }) if edit.recipe.is_none() => None,
            Err(e) => return Err(e),
        }
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

/// The checks a decoded camera JPG or DNG preview must pass, on its declared shape and its
/// payload length alone, so a caller can run them before it copies any pixel: both sides
/// positive, three channels, `h * w * 3` (checked `u64`) within the shared count limit, and
/// exactly that many bytes. `what` names the picture in the error.
pub fn check_rgb_shape(
    height: usize,
    width: usize,
    channels: usize,
    len: usize,
    what: &str,
) -> Result<(), CoreError> {
    if height == 0 || width == 0 {
        return Err(CoreError::new(
            "bad_request",
            format!("{what}的宽高要大于 0"),
        ));
    }
    if channels != 3 {
        return Err(CoreError::new(
            "bad_request",
            format!("{what}要是 RGB 三通道"),
        ));
    }
    let too_big = || CoreError::new("value", gxr_color::ARRAY_TOO_BIG);
    let count = u64::try_from(height)
        .ok()
        .zip(u64::try_from(width).ok())
        .and_then(|(h, w)| h.checked_mul(w))
        .and_then(|n| n.checked_mul(3))
        .filter(|&n| n <= u64::from(u32::MAX))
        .ok_or_else(too_big)?;
    if u64::try_from(len).ok() != Some(count) {
        return Err(CoreError::new(
            "bad_request",
            format!("{what}的像素字节数和宽高不符"),
        ));
    }
    Ok(())
}

/// `check_rgb_shape` on a decoded picture.
pub(crate) fn check_rgb(img: &Image<u8>, what: &str) -> Result<(), CoreError> {
    check_rgb_shape(img.height, img.width, img.channels, img.values.len(), what)
}

/// The names the pictures carry in their errors.
pub const CAMERA_JPG: &str = "相机 JPG";
pub const PREVIEW: &str = "内嵌预览";

/// Port of gxr_studio/finish/serve.py:189 color_image with `full=True` (`max_edge` unused).
///
/// The public entry: the decoded pictures are checked first (`check_rgb`).
pub fn color_image(src: &Sources<'_>, edit: &Edit, base: &str) -> Result<Picked, Fail> {
    if let Some(jpg) = src.jpg {
        check_rgb(jpg, CAMERA_JPG)?;
    }
    if let Some(preview) = src.preview {
        check_rgb(preview, PREVIEW)?;
    }
    color_image_checked(src, edit, base)
}

/// `color_image` on pictures `check_rgb` has passed (the LUT path re-enters here).
pub(crate) fn color_image_checked(
    src: &Sources<'_>,
    edit: &Edit,
    base: &str,
) -> Result<Picked, Fail> {
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
        // `neutral=True`: never plans a recipe, so the pixels are NEUTRAL (or the embedded
        // preview when there is no full CFA). No adjustment is baked in.
        let (rgb, mut labels, path) = raw_rgb(src, &Edit::empty(), true)?;
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
        // A neutral picture has no adjustment baked in: the finish applies them all, as for a JPG.
        return Ok(Picked {
            rgb,
            from_raw: false,
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
    let (rgb, mut labels, path) = match raw_rgb(src, &raw_edit, false) {
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
    // NEUTRAL fallback or embedded preview: the renderer applied no adjustment.
    let neutral = labels.get("id") == Some("neutral-preview");
    if mode == "standard" && !neutral {
        labels.set("kind", "prediction");
        labels.set("label", "prediction");
    }
    Ok(Picked {
        rgb,
        from_raw: !neutral,
        labels,
        path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::source::input::parse_shot;
    use crate::source::plan::Camera;

    fn img(height: usize, width: usize, channels: usize, len: usize) -> Image<u8> {
        Image {
            height,
            width,
            channels,
            values: vec![10; len],
        }
    }

    /// Sol r1 P2-3: the public entry refuses inconsistent pictures before it copies them.
    #[test]
    fn decoded_pictures_are_checked_first() {
        let shot =
            parse_shot(br#"{"files":{"jpg":{"relpath":"R1.JPG"}},"lens":null,"iso":200}"#).unwrap();
        let camera = Camera::Missing;
        let ctx = Context {
            camera: &camera,
            pack: None,
            entry: None,
            binding: "",
        };
        let big = 4_294_967_295;
        let cases = [
            (
                img(1, 2, 3, 3),
                "bad_request",
                "相机 JPG的像素字节数和宽高不符",
            ),
            (img(big, big, 3, 3), "value", gxr_color::ARRAY_TOO_BIG),
            (img(1, 2, 4, 8), "bad_request", "相机 JPG要是 RGB 三通道"),
            (img(0, 2, 3, 0), "bad_request", "相机 JPG的宽高要大于 0"),
            (img(2, 0, 3, 0), "bad_request", "相机 JPG的宽高要大于 0"),
        ];
        for (jpg, kind, message) in &cases {
            let src = Sources {
                shot: &shot,
                dng: None,
                jpg: Some(jpg),
                preview: None,
                ctx,
                lut: None,
            };
            let Err(Fail::Core(e)) = color_image(&src, &Edit::empty(), "camera-jpg") else {
                panic!("{}×{}×{} accepted", jpg.height, jpg.width, jpg.channels);
            };
            assert_eq!((e.kind, e.message.as_str()), (*kind, *message));
        }
        let good = img(1, 2, 3, 6);
        let bad_preview = img(1, 2, 3, 5);
        let src = Sources {
            shot: &shot,
            dng: None,
            jpg: Some(&good),
            preview: Some(&bad_preview),
            ctx,
            lut: None,
        };
        let Err(Fail::Core(e)) = color_image(&src, &Edit::empty(), "camera-jpg") else {
            panic!("bad preview accepted");
        };
        assert_eq!(e.message, "内嵌预览的像素字节数和宽高不符");
        let src = Sources {
            preview: None,
            ..src
        };
        let picked = color_image(&src, &Edit::empty(), "camera-jpg").unwrap();
        assert_eq!((picked.path, picked.rgb.values.len()), ("camera-jpg", 6));
    }
}
