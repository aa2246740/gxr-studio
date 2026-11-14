//! Port of gxr_studio/lut/photo.py:18 color_image (`lut_image`) with `full=True`, and its
//! `_apply` (photo.py:76) over a full-size picture.
//!
//! The desktop takes the cube and its spaces from the LUT library (`render.luts.target`);
//! the phone passes the parsed cube and the item's spaces. Adjustments run after the LUT, in
//! `finish_array`, so they are not applied here (`from_raw` is False).

use gxr_lut::{Cube, RenderSpec, Target, apply_u8, scene_colour};
use gxr_raw::render::neutral_display_sample;
use gxr_raw::{CoreError, Image};

use super::fail::{Fail, Labels};
use super::input::Edit;
use super::render::render_full_with;
use super::select::{Picked, Sources, color_image, open_dng};

/// `lut/photo.py:82`: `_apply` renders this many pixels at a time.
const CHUNK: usize = 65536;
/// `SceneError.code` values (`lut/scene.py`).
const SCENE_CODES: [&str; 4] = ["no_neutral", "no_matrix", "bad_matrix", "bad_colour"];

/// The LUT the edit names: the cube, and the item's spaces (or the `LutError` reading them gave).
pub struct LutSource {
    pub cube: Result<Cube, CoreError>,
    pub spec: Result<RenderSpec, CoreError>,
}

/// `except LutError as exc: raise EditError(exc.code, exc.message)`.
fn lut_error(e: &CoreError) -> Fail {
    Fail::edit(e.kind, &e.message)
}

/// `_apply` raises `EditError("lut_nonfinite", ...)` itself; `SceneError` from `colour.xyz`
/// becomes `EditError(code, "无法换算输入色彩空间。请检查 RAW 文件。")`. Everything else passes.
fn render_error(e: CoreError) -> Fail {
    if e.kind == "lut_nonfinite" {
        return Fail::edit(e.kind, &e.message);
    }
    if SCENE_CODES.contains(&e.kind) {
        return Fail::edit(e.kind, "无法换算输入色彩空间。请检查 RAW 文件。");
    }
    Fail::Core(e)
}

/// Port of gxr_studio/lut/photo.py:76 _apply on a whole uint8 picture, display input.
fn apply_display(target: &Target<'_>, rgb: &Image<u8>) -> Result<Image<u8>, CoreError> {
    let mut out = gxr_lut::try_vec_cap(rgb.values.len())?;
    let mut flat: Vec<[f64; 3]> = Vec::with_capacity(CHUNK);
    for chunk in rgb.values.chunks(CHUNK * 3) {
        flat.clear();
        flat.extend(
            chunk
                .chunks_exact(3)
                .map(|p| [f64::from(p[0]), f64::from(p[1]), f64::from(p[2])]),
        );
        for px in apply_u8(target, &flat, None)? {
            out.extend_from_slice(&px);
        }
    }
    Ok(Image {
        height: rgb.height,
        width: rgb.width,
        channels: rgb.channels,
        values: out,
    })
}

/// The log renderer's `display`: `_apply(target, NEUTRAL.display(codes), colour.xyz(codes))`
/// on one strip. `colour.xyz` checks the whole strip before any pixel is rendered.
fn log_display(
    target: &Target<'_>,
    colour: &gxr_raw::scene::SceneColour,
    strip: &Image<f32>,
    out: &mut [u8],
) -> Result<(), CoreError> {
    let neutral: Vec<f64> = strip
        .values
        .iter()
        .map(|&c| f64::from(neutral_display_sample(c)))
        .collect();
    let mut xyz: Vec<[f64; 3]> = Vec::with_capacity(strip.values.len() / 3);
    for px in strip.values.chunks_exact(3) {
        let codes = [f64::from(px[0]), f64::from(px[1]), f64::from(px[2])];
        xyz.push(
            colour
                .xyz(&codes)
                .map_err(gxr_lut::scene::scene_error_to_core)?,
        );
    }
    let mut rgb: Vec<[f64; 3]> = Vec::with_capacity(CHUNK);
    let mut at = 0;
    for (n, chunk) in neutral.chunks(CHUNK * 3).enumerate() {
        rgb.clear();
        rgb.extend(chunk.chunks_exact(3).map(|p| [p[0], p[1], p[2]]));
        let start = n * CHUNK;
        let scene = &xyz[start..start + rgb.len()];
        for px in apply_u8(target, &rgb, Some(scene))? {
            out[at..at + 3].copy_from_slice(&px);
            at += 3;
        }
    }
    Ok(())
}

fn lut_labels(edit: &Edit, spec: &RenderSpec) -> Labels {
    let description = format!(
        "GXR Studio computer prediction: LUT {}, {} to {}",
        edit.lut.as_deref().unwrap_or(""),
        spec.input_space,
        spec.output_space
    );
    Labels::of(&[
        ("kind", "prediction"),
        ("label", "prediction"),
        ("text", "电脑预测"),
        ("id", "lut-prediction"),
        ("tag", ""),
        ("description", &description),
        ("detail", ""),
        ("key", ""),
        ("cache", "miss"),
        ("calibrated", "0"),
    ])
}

/// Port of gxr_studio/lut/photo.py:18 color_image, `full=True`.
pub fn lut_image(src: &Sources<'_>, edit: &Edit, base: &str) -> Result<Picked, Fail> {
    let lut = src
        .lut
        .ok_or_else(|| Fail::Core(CoreError::new("bad_request", "需要 LUT 文件")))?;
    let cube = lut.cube.as_ref().map_err(lut_error)?;
    let spec = lut.spec.as_ref().map_err(lut_error)?;
    let target = Target::from_spec(cube, spec);
    let (rgb, path) = if gxr_lut::spaces::is_display(&spec.input_space) {
        let stripped = Edit {
            recipe: edit.recipe.clone(),
            ..Edit::empty()
        };
        let rgb = match color_image(src, &stripped, base) {
            Ok(picked) => picked.rgb,
            // Unreachable today: `color_image` only lets `RenderUnavailable` out with a recipe.
            Err(Fail::Unavailable { .. }) if edit.recipe.is_none() => {
                gxr_raw::render_full(&open_dng(src)?)?
            }
            Err(e) => return Err(e),
        };
        (
            apply_display(&target, &rgb).map_err(render_error)?,
            "lut-display",
        )
    } else {
        if !src.shot.has_dng {
            return Err(Fail::edit("lut_needs_raw", "这个 LUT 需要 RAW 文件"));
        }
        let dng = open_dng(src)?;
        let colour = scene_colour(&dng).map_err(render_error)?;
        let rgb = render_full_with(&dng, |strip, out| log_display(&target, &colour, strip, out))
            .map_err(render_error)?;
        (rgb, "lut-log")
    };
    Ok(Picked {
        rgb,
        from_raw: false,
        labels: lut_labels(edit, spec),
        path,
    })
}
