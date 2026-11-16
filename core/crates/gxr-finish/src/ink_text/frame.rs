//! Port of `gxr_studio/finish/frame.py:draw_frame` (`_paint_item` across all 11 item kinds).

use gxr_frame::{Val, parse_json};
use gxr_raw::CoreError;

use super::mark::{GxrMark, gxr_mark, paint_logo_with_mark};
use super::run::{FontPack, draw_text_item, opt_bool, opt_f64, parse_rgb3, req_field, val_f64};
use super::text::{val_i64, val_str};
use crate::ink::backdrop::{background, blurred_background, paper_grain_in_place};
use crate::ink::carrier::paint_carrier;
use crate::ink::draw::{bad_coords, draw_line};
use crate::ink::image::{InkImage, InkMode, bad_mismatch};
use crate::ink::logo::{ClogoItem, decode_logo_bundle, paint_clogo};
use crate::ink::ops::LogoInput;
use crate::ink::paste::paste_image_at;
use crate::ink::shapes::{
    RectItem, ShadowItem, paint_ellipse, paint_hole, paint_poly, paint_rect, paint_shadow,
};

fn bad_frame(msg: &str) -> CoreError {
    CoreError::new("bad_request", msg)
}

fn opt_i64(obj: &Val, key: &str, default: i64) -> Result<i64, CoreError> {
    match obj.get(key) {
        None | Some(Val::Null) => Ok(default),
        Some(v) => val_i64(v, key),
    }
}

fn opt_str<'a>(obj: &'a Val, key: &str, default: &'a str) -> Result<&'a str, CoreError> {
    match obj.get(key) {
        None | Some(Val::Null) => Ok(default),
        Some(v) => val_str(v, key),
    }
}

fn parse_opt_rgb3(val: Option<&Val>, field: &str) -> Result<Option<[u8; 3]>, CoreError> {
    match val {
        None | Some(Val::Null) => Ok(None),
        Some(v) => Ok(Some(parse_rgb3(v, field)?)),
    }
}

fn parse_pts(v: &Val) -> Result<Vec<[f64; 2]>, CoreError> {
    let Val::Arr(arr) = v else {
        return Err(bad_frame("poly pts must be array"));
    };
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let Val::Arr(pair) = item else {
            return Err(bad_frame("poly point must be [x, y]"));
        };
        if pair.len() != 2 {
            return Err(bad_frame("poly point must have 2 coordinates"));
        }
        out.push([val_f64(&pair[0], "pts.x")?, val_f64(&pair[1], "pts.y")?]);
    }
    Ok(out)
}

fn is_kind(it: &Val, expected: &str) -> bool {
    matches!(it.get("k"), Some(Val::Str(s)) if s == expected)
}

fn precheck_clogos(items: &[Val], logos: &[LogoInput]) -> Result<(), CoreError> {
    for item in items {
        if is_kind(item, "clogo") {
            let id = match item.get("id") {
                None | Some(Val::Null) => "",
                Some(v) => val_str(v, "clogo.id")?,
            };
            if id.is_empty() || !logos.iter().any(|l| l.id == id) {
                return Err(CoreError::new("unknown_logo", format!("找不到标志：{id}")));
            }
        }
    }
    Ok(())
}

/// Port of `gxr_studio/finish/frame.py:draw_frame`.
///
/// Returns `(canvas, exact)` where `exact == false` if any `"text"` item or built-in `"logo"`
/// mark item relied on hinting or CFF glyphs.
pub fn draw_frame(
    photo: &InkImage,
    layout: &Val,
    fonts: &FontPack<'_>,
    logos: &[LogoInput],
) -> Result<(InkImage, bool), CoreError> {
    if photo.mode() != InkMode::Rgb {
        return Err(bad_mismatch("draw_frame requires RGB photo"));
    }
    if !matches!(layout, Val::Obj(_)) {
        return Err(bad_frame("frame layout must be an object"));
    }

    let width_i64 = val_i64(req_field(layout, "width")?, "width")?;
    let height_i64 = val_i64(req_field(layout, "height")?, "height")?;
    let width = u64::try_from(width_i64).map_err(|_| bad_coords("invalid frame width"))?;
    let height = u64::try_from(height_i64).map_err(|_| bad_coords("invalid frame height"))?;
    let bg = parse_rgb3(req_field(layout, "bg")?, "bg")?;

    let Val::Arr(items) = req_field(layout, "items")? else {
        return Err(bad_frame("frame items must be an array"));
    };
    precheck_clogos(items, logos)?;

    let mut canvas = match layout.get("bgfx") {
        None | Some(Val::Null) => background(width, height, bg, None)?,
        Some(bgfx) => {
            if !matches!(bgfx, Val::Obj(_)) {
                return Err(bad_frame("bgfx must be object or null"));
            }
            let kind = opt_str(bgfx, "kind", "")?;
            if kind == "blur" {
                let sigma = val_f64(req_field(bgfx, "sigma")?, "bgfx.sigma")?;
                let sat = opt_f64(bgfx, "sat", 1.0)?;
                let veil = val_f64(req_field(bgfx, "veil")?, "bgfx.veil")?;
                blurred_background(photo, width, height, sigma, sat, veil)?
            } else if kind == "glow" {
                let falloff = opt_f64(bgfx, "falloff", 0.05)?;
                background(width, height, bg, Some(falloff))?
            } else {
                background(width, height, bg, None)?
            }
        }
    };

    let grain = opt_f64(layout, "grain", 0.0)?;
    if grain != 0.0_f64 {
        paper_grain_in_place(&mut canvas, grain)?;
    }

    let has_hole = items.iter().any(|it| is_kind(it, "hole"));
    let base = has_hole.then(|| canvas.clone());

    let mut mark_cache: Option<GxrMark> = None;
    let mut exact = true;

    for item in items {
        let kind = val_str(req_field(item, "k")?, "item.k")?;
        match kind {
            "photo" => {
                let x = val_i64(req_field(item, "x")?, "photo.x")?;
                let y = val_i64(req_field(item, "y")?, "photo.y")?;
                paste_image_at(&mut canvas, photo, x, y)?;
            }
            "text" => {
                exact &= draw_text_item(&mut canvas, item, fonts)?;
            }
            "line" => {
                let x1 = val_f64(req_field(item, "x1")?, "line.x1")?;
                let y1 = val_f64(req_field(item, "y1")?, "line.y1")?;
                let x2 = val_f64(req_field(item, "x2")?, "line.x2")?;
                let y2 = val_f64(req_field(item, "y2")?, "line.y2")?;
                let fill = parse_rgb3(req_field(item, "fill")?, "line.fill")?;
                let w = i32::try_from(opt_i64(item, "w", 1)?)
                    .map_err(|_| bad_coords("line width out of i32 range"))?;
                draw_line(&mut canvas, &[[x1, y1], [x2, y2]], fill, w)?;
            }
            "rect" => {
                let spec = RectItem {
                    x: val_i64(req_field(item, "x")?, "rect.x")?,
                    y: val_i64(req_field(item, "y")?, "rect.y")?,
                    w: val_i64(req_field(item, "w")?, "rect.w")?,
                    h: val_i64(req_field(item, "h")?, "rect.h")?,
                    radius: opt_f64(item, "radius", 0.0)?,
                    fill: parse_opt_rgb3(item.get("fill"), "rect.fill")?,
                    outline: parse_opt_rgb3(item.get("outline"), "rect.outline")?,
                    stroke_w: opt_f64(item, "stroke_w", 1.0)?,
                    alpha: opt_f64(item, "alpha", 1.0)?,
                };
                paint_rect(&mut canvas, &spec)?;
            }
            "shadow" => {
                let spec = ShadowItem {
                    x: val_i64(req_field(item, "x")?, "shadow.x")?,
                    y: val_i64(req_field(item, "y")?, "shadow.y")?,
                    w: val_i64(req_field(item, "w")?, "shadow.w")?,
                    h: val_i64(req_field(item, "h")?, "shadow.h")?,
                    blur: val_f64(req_field(item, "blur")?, "shadow.blur")?,
                    dx: opt_i64(item, "dx", 0)?,
                    dy: opt_i64(item, "dy", 0)?,
                    radius: opt_f64(item, "radius", 0.0)?,
                    alpha: opt_f64(item, "alpha", 0.3)?,
                    color: parse_opt_rgb3(item.get("color"), "shadow.color")?.unwrap_or([0, 0, 0]),
                };
                paint_shadow(&mut canvas, &spec)?;
            }
            "hole" => {
                let base_img = base
                    .as_ref()
                    .ok_or_else(|| bad_frame("missing base canvas for hole"))?;
                paint_hole(
                    &mut canvas,
                    val_i64(req_field(item, "x")?, "hole.x")?,
                    val_i64(req_field(item, "y")?, "hole.y")?,
                    val_i64(req_field(item, "w")?, "hole.w")?,
                    val_i64(req_field(item, "h")?, "hole.h")?,
                    opt_f64(item, "radius", 0.0)?,
                    base_img,
                )?;
            }
            "poly" => {
                let pts = parse_pts(req_field(item, "pts")?)?;
                let fill = parse_rgb3(req_field(item, "fill")?, "poly.fill")?;
                let alpha = opt_f64(item, "alpha", 1.0)?;
                paint_poly(&mut canvas, &pts, fill, alpha)?;
            }
            "ellipse" => {
                let cx = val_f64(req_field(item, "cx")?, "ellipse.cx")?;
                let cy = val_f64(req_field(item, "cy")?, "ellipse.cy")?;
                let r = val_f64(req_field(item, "r")?, "ellipse.r")?;
                let fill = parse_rgb3(req_field(item, "fill")?, "ellipse.fill")?;
                let alpha = opt_f64(item, "alpha", 1.0)?;
                paint_ellipse(&mut canvas, cx, cy, r, fill, alpha)?;
            }
            "carrier" => {
                let seed_i64 = val_i64(req_field(item, "seed")?, "carrier.seed")?;
                let seed = u64::try_from(seed_i64)
                    .map_err(|_| bad_frame("carrier seed must be non-negative"))?;
                paint_carrier(
                    &mut canvas,
                    val_i64(req_field(item, "x")?, "carrier.x")?,
                    val_i64(req_field(item, "y")?, "carrier.y")?,
                    val_i64(req_field(item, "w")?, "carrier.w")?,
                    val_i64(req_field(item, "h")?, "carrier.h")?,
                    val_i64(req_field(item, "c")?, "carrier.c")?,
                    val_i64(req_field(item, "jit")?, "carrier.jit")?,
                    seed,
                    parse_rgb3(req_field(item, "fill")?, "carrier.fill")?,
                    parse_rgb3(req_field(item, "paper")?, "carrier.paper")?,
                )?;
            }
            "logo" => {
                let x = val_i64(req_field(item, "x")?, "logo.x")?;
                let y = val_i64(req_field(item, "y")?, "logo.y")?;
                let w_i64 = val_i64(req_field(item, "w")?, "logo.w")?;
                let h_i64 = val_i64(req_field(item, "h")?, "logo.h")?;
                if w_i64 <= 0 || h_i64 <= 0 {
                    return Err(CoreError::new(
                        "value_error",
                        "height and width must be > 0",
                    ));
                }
                let logo_fill = match parse_opt_rgb3(item.get("fill"), "logo.fill")? {
                    Some(rgb) => rgb,
                    None => match parse_opt_rgb3(layout.get("ink"), "layout.ink")? {
                        Some(rgb) => rgb,
                        None => {
                            let luma = 0.299 * f64::from(bg[0])
                                + 0.587 * f64::from(bg[1])
                                + 0.114 * f64::from(bg[2]);
                            if luma < 128.0 {
                                [244, 244, 246]
                            } else {
                                [22, 22, 26]
                            }
                        }
                    },
                };
                if mark_cache.is_none() {
                    mark_cache = Some(gxr_mark(fonts)?);
                }
                let mark = mark_cache
                    .as_ref()
                    .ok_or_else(|| bad_frame("mark cache uninitialized"))?;
                exact &= paint_logo_with_mark(
                    &mut canvas,
                    x,
                    y,
                    w_i64 as u64,
                    h_i64 as u64,
                    logo_fill,
                    mark,
                    fonts,
                )?;
            }
            "clogo" => {
                let id = opt_str(item, "id", "")?;
                let x = val_i64(req_field(item, "x")?, "clogo.x")?;
                let y = val_i64(req_field(item, "y")?, "clogo.y")?;
                let w_i64 = val_i64(req_field(item, "w")?, "clogo.w")?;
                let h_i64 = val_i64(req_field(item, "h")?, "clogo.h")?;
                if w_i64 <= 0 || h_i64 <= 0 {
                    return Err(CoreError::new(
                        "value_error",
                        "height and width must be > 0",
                    ));
                }
                let mode = opt_str(item, "mode", "orig")?;
                let ink = match item.get("ink") {
                    None | Some(Val::Null) => [22, 22, 26],
                    Some(Val::Arr(arr)) if arr.is_empty() => [22, 22, 26],
                    Some(v) => parse_rgb3(v, "clogo.ink")?,
                };
                let auto = opt_bool(item, "auto", false)?;
                let auto_ink = match item.get("auto_ink") {
                    None | Some(Val::Null) => None,
                    Some(Val::Arr(pair)) if pair.is_empty() => None,
                    Some(Val::Arr(pair)) if pair.len() == 2 => Some([
                        parse_rgb3(&pair[0], "clogo.auto_ink[0]")?,
                        parse_rgb3(&pair[1], "clogo.auto_ink[1]")?,
                    ]),
                    Some(_) => return Err(bad_frame("clogo auto_ink must be 2-element array")),
                };
                let clogo = ClogoItem {
                    id,
                    x,
                    y,
                    w: w_i64 as u64,
                    h: h_i64 as u64,
                    mode,
                    ink,
                    auto,
                    auto_ink,
                };
                paint_clogo(&mut canvas, &clogo, logos)?;
            }
            _ => return Err(bad_frame("unsupported frame item kind")),
        }
    }

    Ok((canvas, exact))
}

/// Entry point for `inktext-frame` CLI.
#[allow(clippy::too_many_arguments)]
pub fn inktext_frame_u64(
    photo_pixels: &[u8],
    pw: u64,
    ph: u64,
    layout_json: &str,
    font_index_json: &str,
    fonts_blob: &[u8],
    logo_index_json: &str,
    logo_blob: &[u8],
) -> Result<(InkImage, bool), CoreError> {
    let photo = InkImage::from_bytes(InkMode::Rgb, pw, ph, photo_pixels)?;
    let fonts = FontPack::from_pack(font_index_json, fonts_blob)?;
    let logos = decode_logo_bundle(logo_index_json, logo_blob)?;
    let layout = parse_json(layout_json.as_bytes())?;
    draw_frame(&photo, &layout, &fonts, &logos)
}
