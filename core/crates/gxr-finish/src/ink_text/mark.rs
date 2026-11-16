//! Built-in `"GXR"` wordmark mask (`gxr_mark`, `gxr_mark_json`) and frame `"logo"` painter.
//!
//! Renders `"GXR"` at 1000 px using `"atkinson"` (`MARK_FONT`), crops tightly to ink bounds, and
//! reports `{"w", "h", "ink_top", "ink_bottom"}` matching `gxr-frame`'s `mark` contract
//! (`docs/mobile.md`). No Ricoh logo assets or hardcoded `ricoh.png` dimensions exist in Rust.

use gxr_raw::{CoreError, Json};

use super::ftcalc::pixel_26_6;
use super::glyf::SizeMetrics;
use super::run::{FontPack, draw_run_fields};
use crate::ink::draw::bad_coords;
use crate::ink::image::{InkImage, InkMode, mean_u8_as_f32};
use crate::ink::paste::paste_color;
use crate::resample::{Filter, Mode, resample_u8_u64};

/// Built-in wordmark text.
pub const MARK_TEXT: &str = "GXR";
/// Font key used to render the built-in wordmark.
pub const MARK_FONT: &str = "atkinson";
/// Reference pixel size for computing the tight `"GXR"` mark and JSON metrics.
pub const MARK_PX: u32 = 1000;

/// Cropped `"GXR"` alpha mask (`InkMode::L`) and metrics for `gxr-frame`.
#[derive(Clone, Debug)]
pub struct GxrMark {
    pub width: i64,
    pub height: i64,
    pub ink_top: i64,
    pub ink_bottom: i64,
    pub mask: InkImage,
}

impl GxrMark {
    /// Metrics object expected by `gxr-frame`'s `mark` field: `{"w", "h", "ink_top", "ink_bottom"}`.
    #[must_use]
    pub fn to_mark_json(&self) -> Json {
        Json::object([
            ("w", Json::Int(self.width)),
            ("h", Json::Int(self.height)),
            ("ink_top", Json::Int(self.ink_top)),
            ("ink_bottom", Json::Int(self.ink_bottom)),
        ])
    }
}

/// Render the built-in `"GXR"` mark mask and compute tight ink metrics.
pub fn gxr_mark(fonts: &FontPack<'_>) -> Result<GxrMark, CoreError> {
    let face = fonts.face(MARK_FONT, false)?;
    let frame_fonts = fonts.frame_fonts();
    let upm = frame_fonts.upm(MARK_FONT, false)?;
    let scale = f64::from(MARK_PX) / (upm as f64);
    let pad_x = 128_i64;
    let mut pen = 0.0_f64;
    let mut xs = Vec::with_capacity(MARK_TEXT.len());
    for ch in MARK_TEXT.chars() {
        xs.push((pen * scale + 0.5_f64).floor() as i64 + pad_x);
        let (adv, _) = frame_fonts.glyph(MARK_FONT, ch, false)?;
        pen += adv as f64;
    }
    let advance = (pen * scale).ceil() as i64;
    let cw = u64::try_from((advance + pad_x * 2).max(16))
        .map_err(|_| bad_coords("mark width overflow"))?;

    let metrics = SizeMetrics::from_pixel_size(face, MARK_PX)?;
    let asc = pixel_26_6(metrics.root_ascender);
    let ch = u64::try_from((asc + i64::from(MARK_PX)).max(16))
        .map_err(|_| bad_coords("mark height overflow"))?;

    let mut tmp = InkImage::new_filled(InkMode::L, cw, ch, [0, 0, 0, 0])?;
    draw_run_fields(
        &mut tmp,
        MARK_FONT,
        i64::from(MARK_PX),
        false,
        MARK_TEXT,
        &xs,
        0,
        fonts,
        [255, 0, 0, 255],
    )?;

    let w = tmp.width();
    let h = tmp.height();
    let mut min_x = w;
    let mut min_y = h;
    let mut max_x = 0_usize;
    let mut max_y = 0_usize;
    let mut any = false;
    let data = tmp.as_slice();
    for y in 0..h {
        let row = &data[y * w..(y + 1) * w];
        for (x, &v) in row.iter().enumerate() {
            if v != 0 {
                any = true;
                min_x = min_x.min(x);
                max_x = max_x.max(x);
                min_y = min_y.min(y);
                max_y = max_y.max(y);
            }
        }
    }
    if !any {
        return Err(CoreError::new(
            "value_error",
            "atkinson font produced empty GXR mark",
        ));
    }

    let mask = tmp.crop(
        min_x as i64,
        min_y as i64,
        (max_x + 1) as i64,
        (max_y + 1) as i64,
    )?;
    let width = mask.width() as i64;
    let height = mask.height() as i64;
    Ok(GxrMark {
        width,
        height,
        ink_top: 0,
        ink_bottom: height - 1,
        mask,
    })
}

/// JSON metrics helper for `gxr_mark` WASM and `inktext-mark` CLI.
pub fn gxr_mark_json(font_index_json: &str, fonts_blob: &[u8]) -> Result<Json, CoreError> {
    let fonts = FontPack::from_pack(font_index_json, fonts_blob)?;
    let mark = gxr_mark(&fonts)?;
    Ok(mark.to_mark_json())
}

/// Paint a frame `"logo"` item using a precomputed [`GxrMark`].
pub fn paint_logo_with_mark(
    canvas: &mut InkImage,
    x: i64,
    y: i64,
    w: u64,
    h: u64,
    mark: &GxrMark,
) -> Result<(), CoreError> {
    if w == 0 || h == 0 {
        return Err(CoreError::new(
            "value_error",
            "height and width must be > 0",
        ));
    }
    let w_i64 = i64::try_from(w).map_err(|_| bad_coords("logo box overflow"))?;
    let h_i64 = i64::try_from(h).map_err(|_| bad_coords("logo box overflow"))?;
    let x1 = x
        .checked_add(w_i64)
        .ok_or_else(|| bad_coords("logo box overflow"))?;
    let y1 = y
        .checked_add(h_i64)
        .ok_or_else(|| bad_coords("logo box overflow"))?;

    let cw = canvas.width() as i64;
    let ch = canvas.height() as i64;
    let cx0 = x.clamp(0, cw);
    let cy0 = y.clamp(0, ch);
    let cx1 = x1.clamp(0, cw);
    let cy1 = y1.clamp(0, ch);
    let bg_luma = if cx1 > cx0 && cy1 > cy0 {
        let reg = canvas.crop(cx0, cy0, cx1, cy1)?;
        let reg_l = reg.convert(InkMode::L)?;
        f64::from(mean_u8_as_f32(reg_l.as_slice()))
    } else {
        245.0_f64
    };
    let ink = if bg_luma > 150.0_f64 {
        [22, 22, 26]
    } else {
        [244, 244, 246]
    };

    let resized = resample_u8_u64(
        mark.mask.as_slice(),
        mark.mask.width() as u64,
        mark.mask.height() as u64,
        Mode::L,
        Filter::Lanczos,
        w,
        h,
    )?;
    let mask = InkImage::from_vec(InkMode::L, w, h, resized)?;
    paste_color(
        canvas,
        [ink[0], ink[1], ink[2], 255],
        [x, y, x1, y1],
        Some(&mask),
    )
}

/// Paint a frame `"logo"` item (`{x, y, w, h}`) onto an RGB canvas.
pub fn paint_logo_item(
    canvas: &mut InkImage,
    x: i64,
    y: i64,
    w: u64,
    h: u64,
    fonts: &FontPack<'_>,
) -> Result<(), CoreError> {
    let mark = gxr_mark(fonts)?;
    paint_logo_with_mark(canvas, x, y, w, h, &mark)
}
