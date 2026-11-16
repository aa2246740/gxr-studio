//! Ink stage of the phone export pipeline (`draw_frame`, `apply_stamp`, `stamp_hdr_mask`),
//! bridging `export_prepare` and `export_complete` entirely inside Rust/WASM.

use gxr_finish::ink::{InkImage, InkMode, LogoInput, decode_logo_bundle};
use gxr_finish::{FontPack, apply_stamp, draw_frame, stamp_hdr_mask};
use gxr_frame::{Val, parse_json};
use gxr_raw::CoreError;

use super::api::dim_from_f64;
use super::complete::checked_rgb;
use super::result::ExportFail;

fn bad_prepared(what: &str) -> CoreError {
    CoreError::new("bad_state", format!("导出预处理状态无效：{what}"))
}

/// Output of [`export_ink`]: the inked RGB canvas and (when HDR photo stamp applies) its mask.
#[derive(Clone, Debug, PartialEq)]
pub struct Inked {
    pub width: usize,
    pub height: usize,
    pub canvas: Vec<u8>,
    pub stamp_mask: Vec<u8>,
    pub mask_width: usize,
    pub mask_height: usize,
    pub exact: bool,
}

fn parse_photo_rect(v: &Val) -> Result<(i64, i64, u64, u64), CoreError> {
    let px = v
        .get("x")
        .and_then(|x| match x {
            Val::Int(n) => Some(*n),
            _ => None,
        })
        .ok_or_else(|| bad_prepared("photo.x"))?;
    let py = v
        .get("y")
        .and_then(|y| match y {
            Val::Int(n) => Some(*n),
            _ => None,
        })
        .ok_or_else(|| bad_prepared("photo.y"))?;
    let pw = v
        .get("w")
        .and_then(|w| match w {
            Val::Int(n) if *n > 0 => u64::try_from(*n).ok(),
            _ => None,
        })
        .ok_or_else(|| bad_prepared("photo.w"))?;
    let ph = v
        .get("h")
        .and_then(|h| match h {
            Val::Int(n) if *n > 0 => u64::try_from(*n).ok(),
            _ => None,
        })
        .ok_or_else(|| bad_prepared("photo.h"))?;
    Ok((px, py, pw, ph))
}

fn hdr_track_active(prep: &Val) -> bool {
    let Some(hdr) = prep.get("state").and_then(|s| s.get("hdr")) else {
        return false;
    };
    if matches!(hdr, Val::Null) {
        return false;
    }
    if let Some(Val::Bool(active)) = hdr.get("active") {
        return *active;
    }
    matches!(hdr.get("asked"), Some(Val::Bool(true)))
}

/// Ink the canvas produced by `export_prepare`, applying `stamp` (on photo or canvas),
/// `draw_frame`, and computing `stamp_hdr_mask` when HDR is active and `stamp_on == "photo"`.
#[allow(clippy::too_many_arguments)]
pub fn export_ink(
    canvas: Vec<u8>,
    width: f64,
    height: f64,
    prepared_json: &[u8],
    font_index_json: &str,
    fonts_blob: &[u8],
    logo_index_json: &str,
    logo_blob: &[u8],
) -> Result<Inked, ExportFail> {
    let w = dim_from_f64(width, "宽")?;
    let h = dim_from_f64(height, "高")?;
    let (wu, hu) = checked_rgb(&canvas, w, h)?;

    let root = parse_json(prepared_json)?;
    if matches!(root.get("original"), Some(Val::Bool(true))) {
        return Ok(Inked {
            width: wu,
            height: hu,
            canvas,
            stamp_mask: Vec::new(),
            mask_width: 0,
            mask_height: 0,
            exact: true,
        });
    }

    let prep = root.get("prepared").unwrap_or(&root);
    if !matches!(prep, Val::Obj(_)) {
        return Err(bad_prepared("prepared").into());
    }

    let canvas_kind = match prep.get("canvas") {
        None | Some(Val::Null) => "full",
        Some(Val::Str(s)) if s == "full" => "full",
        Some(Val::Str(s)) if s == "photo" => "photo",
        Some(_) => return Err(bad_prepared("canvas").into()),
    };
    let frame = prep.get("frame").unwrap_or(&Val::Null);
    let stamp = prep.get("stamp").unwrap_or(&Val::Null);
    let stamp_on = match prep.get("stamp_on") {
        None | Some(Val::Null) => None,
        Some(Val::Str(s)) if s == "photo" => Some("photo"),
        Some(Val::Str(s)) if s == "canvas" => Some("canvas"),
        Some(_) => return Err(bad_prepared("stamp_on").into()),
    };

    let fonts = FontPack::from_pack(font_index_json, fonts_blob)?;
    let logos: Vec<LogoInput> = decode_logo_bundle(logo_index_json, logo_blob)?;

    if matches!(frame, Val::Null) && matches!(stamp, Val::Null) {
        return Ok(Inked {
            width: wu,
            height: hu,
            canvas,
            stamp_mask: Vec::new(),
            mask_width: 0,
            mask_height: 0,
            exact: true,
        });
    }

    let want_hdr_mask = hdr_track_active(prep) && !matches!(stamp, Val::Null);
    let mut stamp_mask = Vec::new();
    let mut mask_width = 0_usize;
    let mut mask_height = 0_usize;
    let mut exact = true;

    if matches!(frame, Val::Null) {
        let mut img = InkImage::from_vec(InkMode::Rgb, w, h, canvas)?;
        if !matches!(stamp, Val::Null) {
            if want_hdr_mask && stamp_on != Some("canvas") {
                stamp_mask = stamp_hdr_mask(w, h, &fonts, stamp)?;
                mask_width = wu;
                mask_height = hu;
            }
            exact &= apply_stamp(&mut img, &fonts, stamp)?;
        }
        let width = img.width();
        let height = img.height();
        return Ok(Inked {
            width,
            height,
            canvas: img.into_bytes(),
            stamp_mask,
            mask_width,
            mask_height,
            exact,
        });
    }

    let (px, py, pw, ph) = match prep.get("photo") {
        Some(v) if !matches!(v, Val::Null) => parse_photo_rect(v)?,
        _ => return Err(bad_prepared("photo").into()),
    };

    let mut photo = if canvas_kind == "photo" {
        if w != pw || h != ph {
            return Err(bad_prepared("photo dimensions").into());
        }
        InkImage::from_vec(InkMode::Rgb, w, h, canvas)?
    } else {
        let pw_i64 = i64::try_from(pw).map_err(|_| bad_prepared("photo.w"))?;
        let ph_i64 = i64::try_from(ph).map_err(|_| bad_prepared("photo.h"))?;
        let px1 = px
            .checked_add(pw_i64)
            .ok_or_else(|| bad_prepared("photo.w"))?;
        let py1 = py
            .checked_add(ph_i64)
            .ok_or_else(|| bad_prepared("photo.h"))?;
        if px < 0 || py < 0 || px1 > wu as i64 || py1 > hu as i64 {
            return Err(bad_prepared("photo rect out of canvas bounds").into());
        }
        let full = InkImage::from_vec(InkMode::Rgb, w, h, canvas)?;
        let cropped = full.crop(px, py, px1, py1)?;
        drop(full);
        cropped
    };

    if !matches!(stamp, Val::Null) && stamp_on == Some("photo") {
        if want_hdr_mask {
            stamp_mask = stamp_hdr_mask(pw, ph, &fonts, stamp)?;
            mask_width = pw as usize;
            mask_height = ph as usize;
        }
        exact &= apply_stamp(&mut photo, &fonts, stamp)?;
    }

    let (mut framed, frame_exact) = draw_frame(&photo, frame, &fonts, &logos)?;
    drop(photo);
    exact &= frame_exact;

    if !matches!(stamp, Val::Null) && stamp_on == Some("canvas") {
        exact &= apply_stamp(&mut framed, &fonts, stamp)?;
    }

    let width = framed.width();
    let height = framed.height();
    Ok(Inked {
        width,
        height,
        canvas: framed.into_bytes(),
        stamp_mask,
        mask_width,
        mask_height,
        exact,
    })
}
