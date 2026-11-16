//! Port of `gxr_studio/finish/fonts.py:draw_run`, `frame_texture.py:text_ink`, and
//! `frame.py:_paint_item` (`"text"` items, including 90°-rotated vertical captions).

use std::collections::BTreeMap;

use gxr_frame::{Fonts as FrameFonts, Type, Val, parse_json};
use gxr_raw::CoreError;

use super::ftcalc::pixel_26_6;
use super::glyf::{FontFace, SizeMetrics};
use super::text::{draw_char_tracked, unpack_font_pack, val_i64, val_str};
use crate::ink::image::{InkImage, InkMode, mean_u8_as_f32};
use crate::ink::paste::paste_image;

fn bad_text(msg: &str) -> CoreError {
    CoreError::new("bad_request", msg)
}

fn is_cjk_char(ch: char) -> bool {
    (ch as u32) >= 0x2E80
}

pub(crate) fn req_field<'a>(obj: &'a Val, key: &str) -> Result<&'a Val, CoreError> {
    obj.get(key)
        .ok_or_else(|| CoreError::new("value_error", format!("missing field {key}")))
}

pub(crate) fn val_f64(v: &Val, field: &str) -> Result<f64, CoreError> {
    let f = match v {
        Val::Int(n) => *n as f64,
        Val::Float(f) => *f,
        _ => {
            return Err(CoreError::new(
                "value_error",
                format!("{field} must be a number"),
            ));
        }
    };
    if !f.is_finite() {
        return Err(CoreError::new(
            "value_error",
            format!("{field} must be finite"),
        ));
    }
    Ok(f)
}

pub(crate) fn val_bool(v: &Val, field: &str) -> Result<bool, CoreError> {
    match v {
        Val::Bool(b) => Ok(*b),
        _ => Err(CoreError::new(
            "value_error",
            format!("{field} must be a boolean"),
        )),
    }
}

pub(crate) fn opt_bool(obj: &Val, key: &str, default: bool) -> Result<bool, CoreError> {
    match obj.get(key) {
        None | Some(Val::Null) => Ok(default),
        Some(v) => val_bool(v, key),
    }
}

pub(crate) fn opt_f64(obj: &Val, key: &str, default: f64) -> Result<f64, CoreError> {
    match obj.get(key) {
        None | Some(Val::Null) => Ok(default),
        Some(v) => val_f64(v, key),
    }
}

pub(crate) fn parse_u8_val(v: &Val, field: &str) -> Result<u8, CoreError> {
    let n = val_i64(v, field)?;
    u8::try_from(n).map_err(|_| CoreError::new("value_error", format!("{field} out of 0..=255")))
}

pub(crate) fn parse_rgb3(v: &Val, field: &str) -> Result<[u8; 3], CoreError> {
    let Val::Arr(arr) = v else {
        return Err(CoreError::new(
            "value_error",
            format!("{field} must be [r, g, b]"),
        ));
    };
    if arr.len() != 3 && arr.len() != 4 {
        return Err(CoreError::new(
            "value_error",
            format!("{field} must have 3 channels"),
        ));
    }
    Ok([
        parse_u8_val(&arr[0], field)?,
        parse_u8_val(&arr[1], field)?,
        parse_u8_val(&arr[2], field)?,
    ])
}

pub(crate) fn parse_fill_for_mode(v: &Val, mode: InkMode) -> Result<[u8; 4], CoreError> {
    match mode {
        InkMode::L => match v {
            Val::Int(_) => Ok([parse_u8_val(v, "fill")?, 0, 0, 255]),
            Val::Arr(arr) if arr.len() == 1 => Ok([parse_u8_val(&arr[0], "fill")?, 0, 0, 255]),
            _ => Err(CoreError::new("value_error", "L fill must be int or [u8]")),
        },
        InkMode::Rgb => {
            let rgb = parse_rgb3(v, "fill")?;
            Ok([rgb[0], rgb[1], rgb[2], 255])
        }
        InkMode::Rgba => {
            let Val::Arr(arr) = v else {
                return Err(CoreError::new(
                    "value_error",
                    "RGBA fill must be [r, g, b, a]",
                ));
            };
            match arr.len() {
                3 => Ok([
                    parse_u8_val(&arr[0], "fill")?,
                    parse_u8_val(&arr[1], "fill")?,
                    parse_u8_val(&arr[2], "fill")?,
                    255,
                ]),
                4 => Ok([
                    parse_u8_val(&arr[0], "fill")?,
                    parse_u8_val(&arr[1], "fill")?,
                    parse_u8_val(&arr[2], "fill")?,
                    parse_u8_val(&arr[3], "fill")?,
                ]),
                _ => Err(CoreError::new(
                    "value_error",
                    "RGBA fill must be [r, g, b, a]",
                )),
            }
        }
    }
}

pub(crate) fn parse_xs(v: &Val, field: &str) -> Result<Vec<i64>, CoreError> {
    let Val::Arr(arr) = v else {
        return Err(CoreError::new(
            "value_error",
            format!("{field} must be an array"),
        ));
    };
    let mut xs = Vec::with_capacity(arr.len());
    for item in arr {
        xs.push(val_i64(item, field)?);
    }
    Ok(xs)
}

/// Decoded font bundle used for both `gxr_frame` metric lookups and FreeType glyph painting.
pub struct FontPack<'a> {
    faces: BTreeMap<String, FontFace<'a>>,
    frame_fonts: FrameFonts,
}

impl<'a> FontPack<'a> {
    /// Parse `font_index_json` (`[[name, len], ...]`) + concatenated font `blob`.
    pub fn from_pack(index_json: &str, blob: &'a [u8]) -> Result<Self, CoreError> {
        let mut faces = BTreeMap::new();
        let mut frame_fonts = FrameFonts::new();
        if index_json.trim().is_empty() && blob.is_empty() {
            return Ok(Self { faces, frame_fonts });
        }
        let map = unpack_font_pack(index_json, blob)?;
        for (name, bytes) in map {
            let face = FontFace::parse(bytes)?;
            if name == "cjk" || gxr_frame::FONT_FILES.iter().any(|&(k, _)| k == name) {
                frame_fonts.add(&name, bytes)?;
            }
            faces.insert(name, face);
        }
        Ok(Self { faces, frame_fonts })
    }

    /// Look up the named font face or the `"cjk"` fallback face.
    pub fn face(&self, font: &str, cjk: bool) -> Result<&FontFace<'a>, CoreError> {
        if cjk {
            self.faces
                .get("cjk")
                .ok_or_else(|| CoreError::new("missing_cjk", "无法显示中文。请改用英文或数字。"))
        } else {
            self.faces
                .get(font)
                .ok_or_else(|| CoreError::new("missing_font", format!("缺少字体：{font}")))
        }
    }

    /// Underlying `gxr_frame::Fonts` table store (used for `text_ink` advance width and `gxr_mark`).
    #[must_use]
    pub fn frame_fonts(&self) -> &FrameFonts {
        &self.frame_fonts
    }
}

pub(crate) fn px_to_u32(px: i64) -> Result<u32, CoreError> {
    u32::try_from(px.max(1)).map_err(|_| bad_text("px out of u32 range"))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn draw_run_fields(
    canvas: &mut InkImage,
    font: &str,
    px: i64,
    cjk: bool,
    text: &str,
    xs: &[i64],
    y: i64,
    fonts: &FontPack<'_>,
    fill: [u8; 4],
) -> Result<bool, CoreError> {
    if text.chars().count() != xs.len() {
        return Err(bad_text("text length does not match xs length"));
    }
    let px_u32 = px_to_u32(px)?;
    let base_face = fonts.face(font, cjk)?;
    let base_metrics = SizeMetrics::from_pixel_size(base_face, px_u32)?;
    let baseline = pixel_26_6(base_metrics.root_ascender);

    let mut exact = true;
    let mut canvas_mut = canvas.as_canvas_mut();
    let mut cjk_asc_cached: Option<i64> = None;

    for (ch, &x) in text.chars().zip(xs.iter()) {
        if ch == ' ' || (ch as u32) < 0x20 {
            continue;
        }
        let ch_cjk = is_cjk_char(ch);
        let glyph_face = if ch_cjk {
            fonts.face(font, true)?
        } else {
            base_face
        };
        let offset = if ch_cjk == cjk {
            0
        } else if ch_cjk {
            if let Some(asc) = cjk_asc_cached {
                baseline - asc
            } else {
                let m = SizeMetrics::from_pixel_size(glyph_face, px_u32)?;
                let asc = pixel_26_6(m.root_ascender);
                cjk_asc_cached = Some(asc);
                baseline - asc
            }
        } else {
            let m = SizeMetrics::from_pixel_size(glyph_face, px_u32)?;
            baseline - pixel_26_6(m.root_ascender)
        };
        let gy = y
            .checked_add(offset)
            .ok_or_else(|| bad_text("y coordinate overflow"))?;
        let hinted = draw_char_tracked(&mut canvas_mut, glyph_face, px_u32, ch, x, gy, fill)?;
        if hinted {
            exact = false;
        }
    }
    Ok(exact)
}

/// Port of `gxr_studio/finish/fonts.py:draw_run`.
///
/// Returns `Ok(exact)` where `exact == false` if any drawn glyph used hinting or CFF outlines.
pub fn draw_run(
    canvas: &mut InkImage,
    item: &Val,
    fonts: &FontPack<'_>,
    fill: [u8; 4],
) -> Result<bool, CoreError> {
    let font = val_str(req_field(item, "font")?, "font")?;
    let px = val_i64(req_field(item, "px")?, "px")?;
    let cjk = opt_bool(item, "cjk", false)?;
    let y = val_i64(req_field(item, "y")?, "y")?;
    let text = val_str(req_field(item, "text")?, "text")?;
    let xs = parse_xs(req_field(item, "xs")?, "xs")?;
    draw_run_fields(canvas, font, px, cjk, text, &xs, y, fonts, fill)
}

/// Port of `gxr_studio/finish/frame_texture.py:text_ink`.
pub fn text_ink(canvas: &InkImage, item: &Val, fonts: &FontPack<'_>) -> Result<[u8; 3], CoreError> {
    let fill = parse_rgb3(req_field(item, "fill")?, "fill")?;
    let Some(auto_val) = item.get("auto_ink") else {
        return Ok(fill);
    };
    let (light, dark) = match auto_val {
        Val::Null => return Ok(fill),
        Val::Arr(pair) if pair.is_empty() => return Ok(fill),
        Val::Arr(pair) if pair.len() == 2 => (
            parse_rgb3(&pair[0], "auto_ink[0]")?,
            parse_rgb3(&pair[1], "auto_ink[1]")?,
        ),
        _ => {
            return Err(CoreError::new(
                "value_error",
                "auto_ink must be [light, dark] or null",
            ));
        }
    };

    let font = val_str(req_field(item, "font")?, "font")?;
    let px = val_i64(req_field(item, "px")?, "px")?;
    let text = val_str(req_field(item, "text")?, "text")?;
    let y = val_i64(req_field(item, "y")?, "y")?;
    let track = opt_f64(item, "track", 0.0)?;
    let xs = parse_xs(req_field(item, "xs")?, "xs")?;

    let width = Type {
        m: fonts.frame_fonts(),
    }
    .advance(font, px, text, track)?
    .ceil() as i64;
    let x0 = xs.first().copied().unwrap_or(0);
    let cw = canvas.width() as i64;
    let ch = canvas.height() as i64;
    let rx0 = x0.saturating_sub(6).clamp(0, cw);
    let ry0 = y.clamp(0, ch);
    let rx1 = x0.saturating_add(width).saturating_add(6).clamp(0, cw);
    let ry1 = y.saturating_add(px).clamp(0, ch);

    let luma = if rx1 > rx0 && ry1 > ry0 {
        let reg = canvas.crop(rx0, ry0, rx1, ry1)?;
        let reg_l = reg.convert(InkMode::L)?;
        mean_u8_as_f32(reg_l.as_slice())
    } else {
        0.0_f32
    };
    Ok(if luma > 150.0_f32 { dark } else { light })
}

/// Port of `gxr_studio/finish/frame.py:_paint_item` for `"text"` (including rotated text).
pub fn draw_text_item(
    canvas: &mut InkImage,
    item: &Val,
    fonts: &FontPack<'_>,
) -> Result<bool, CoreError> {
    if opt_bool(item, "rot", false)? {
        let font = val_str(req_field(item, "font")?, "font")?;
        let px = val_i64(req_field(item, "px")?, "px")?;
        let px_u32 = px_to_u32(px)?;
        let cjk = opt_bool(item, "cjk", false)?;
        let text = val_str(req_field(item, "text")?, "text")?;
        let fill = parse_rgb3(req_field(item, "fill")?, "fill")?;
        let tw_i64 = val_i64(req_field(item, "tw")?, "tw")?;
        let tw = u64::try_from(tw_i64).map_err(|_| bad_text("tw must be non-negative"))?;
        let gx = val_i64(req_field(item, "gx")?, "gx")?;
        let gy = val_i64(req_field(item, "gy")?, "gy")?;
        let xs0 = parse_xs(req_field(item, "xs0")?, "xs0")?;

        let base_face = fonts.face(font, false)?;
        let asc = pixel_26_6(SizeMetrics::from_pixel_size(base_face, px_u32)?.root_ascender);
        let pad_h = (f64::from(px_u32) * 0.4).ceil() as i64;
        let tmp_h_i64 = asc
            .checked_add(pad_h)
            .ok_or_else(|| bad_text("tmp_h overflow"))?;
        let tmp_h = u64::try_from(tmp_h_i64).map_err(|_| bad_text("tmp_h must be non-negative"))?;

        let mut tmp = InkImage::new_filled(InkMode::Rgba, tw, tmp_h, [0, 0, 0, 0])?;
        let exact = draw_run_fields(
            &mut tmp,
            font,
            px,
            cjk,
            text,
            &xs0,
            0,
            fonts,
            [fill[0], fill[1], fill[2], 255],
        )?;
        let rot = tmp.rotate_90()?;
        drop(tmp);
        let alpha = rot.get_alpha()?;
        let rgb = rot.convert(InkMode::Rgb)?;
        let rw = i64::try_from(rgb.width()).map_err(|_| bad_text("rotated width overflow"))?;
        let rh = i64::try_from(rgb.height()).map_err(|_| bad_text("rotated height overflow"))?;
        let gx1 = gx.checked_add(rw).ok_or_else(|| bad_text("gx overflow"))?;
        let gy1 = gy.checked_add(rh).ok_or_else(|| bad_text("gy overflow"))?;
        paste_image(canvas, &rgb, [gx, gy, gx1, gy1], Some(&alpha))?;
        Ok(exact)
    } else {
        let ink = text_ink(canvas, item, fonts)?;
        draw_run(canvas, item, fonts, [ink[0], ink[1], ink[2], 255])
    }
}

/// Entry point for `inktext-run` CLI: draws a run item or array of items onto `pixels`.
pub fn inktext_run_u64(
    pixels: &[u8],
    w: u64,
    h: u64,
    mode: &str,
    run_json: &str,
    font_index_json: &str,
    fonts_blob: &[u8],
) -> Result<(InkImage, bool), CoreError> {
    let ink_mode = InkMode::parse(mode)?;
    let mut canvas = InkImage::from_bytes(ink_mode, w, h, pixels)?;
    let fonts = FontPack::from_pack(font_index_json, fonts_blob)?;
    let parsed = parse_json(run_json.as_bytes())?;
    let mut exact = true;
    match &parsed {
        Val::Arr(items) => {
            for item in items {
                exact &= draw_one_run_item(&mut canvas, item, &fonts)?;
            }
        }
        Val::Obj(_) => {
            exact &= draw_one_run_item(&mut canvas, &parsed, &fonts)?;
        }
        _ => return Err(bad_text("run_json must be an object or array of objects")),
    }
    Ok((canvas, exact))
}

fn draw_one_run_item(
    canvas: &mut InkImage,
    item: &Val,
    fonts: &FontPack<'_>,
) -> Result<bool, CoreError> {
    if opt_bool(item, "rot", false)?
        || item
            .get("auto_ink")
            .is_some_and(|v| !matches!(v, Val::Null))
    {
        return draw_text_item(canvas, item, fonts);
    }
    let fill = parse_fill_for_mode(req_field(item, "fill")?, canvas.mode())?;
    draw_run(canvas, item, fonts, fill)
}
