//! Built-in `"GXR"` wordmark metrics (`gxr_mark`, `gxr_mark_json`) and frame `"logo"` painter.
//!
//! Measures `"GXR"` at 1000 px using `"atkinson"` (`MARK_FONT`) on the untrimmed `w × (asc + desc)`
//! box (`{"w", "h", "ink_top", "ink_bottom"}`), and paints frame `"logo"` items by redrawing
//! `"GXR"` via `draw_run_fields` at `px = round(1000 * h / h_ref)` in `item.fill` (or frame ink).
//! No Ricoh logo assets or hardcoded `ricoh.png` dimensions exist in Rust.

use gxr_raw::{CoreError, Json};

use super::ftcalc::pixel_26_6;
use super::glyf::SizeMetrics;
use super::run::{FontPack, draw_run_fields};
use crate::ink::draw::bad_coords;
use crate::ink::image::{InkImage, InkMode};

/// Built-in wordmark text.
pub const MARK_TEXT: &str = "GXR";
/// Font key used to render the built-in wordmark.
pub const MARK_FONT: &str = "atkinson";
/// Reference pixel size for computing the `"GXR"` mark and JSON metrics.
pub const MARK_PX: u32 = 1000;

/// Untrimmed `"GXR"` metrics for `gxr-frame`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GxrMark {
    pub width: i64,
    pub height: i64,
    pub ink_top: i64,
    pub ink_bottom: i64,
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

fn mark_glyph_xs(
    fonts: &FontPack<'_>,
    px: i64,
    x0: i64,
) -> Result<(Vec<i64>, i64, i64), CoreError> {
    let frame_fonts = fonts.frame_fonts();
    let upm = frame_fonts.upm(MARK_FONT, false)?;
    let mut pens = Vec::with_capacity(MARK_TEXT.len() + 1);
    pens.push(0_i64);
    for ch in MARK_TEXT.chars() {
        let (adv, _) = frame_fonts.glyph(MARK_FONT, ch, false)?;
        let next = pens
            .last()
            .copied()
            .unwrap_or(0)
            .checked_add(adv)
            .ok_or_else(|| bad_coords("mark advance overflow"))?;
        pens.push(next);
    }
    let total_pen = *pens.last().unwrap_or(&0);
    let mut xs = Vec::with_capacity(MARK_TEXT.len());
    for &pen in &pens[..pens.len() - 1] {
        let scaled = pen
            .checked_mul(px)
            .ok_or_else(|| bad_coords("mark pen overflow"))?;
        let pos = ((x0 as f64) + (scaled as f64) / (upm as f64) + 0.5_f64).floor();
        if !pos.is_finite() {
            return Err(bad_coords("mark x overflow"));
        }
        xs.push(pos as i64);
    }
    Ok((xs, total_pen, upm))
}

/// Measure the built-in `"GXR"` mark metrics (`w`, `h = asc + desc`, untrimmed `ink_top`/`ink_bottom`).
pub fn gxr_mark(fonts: &FontPack<'_>) -> Result<GxrMark, CoreError> {
    let face = fonts.face(MARK_FONT, false)?;
    let (xs, total_pen, upm) = mark_glyph_xs(fonts, i64::from(MARK_PX), 0)?;
    let total_scaled = total_pen
        .checked_mul(i64::from(MARK_PX))
        .ok_or_else(|| bad_coords("mark advance overflow"))?;
    let w_f = ((total_scaled as f64) / (upm as f64)).round_ties_even();
    if !w_f.is_finite() || w_f <= 0.0 {
        return Err(bad_coords("invalid mark advance width"));
    }
    let width = w_f as i64;

    let metrics = SizeMetrics::from_pixel_size(face, MARK_PX)?;
    let asc = pixel_26_6(metrics.root_ascender);
    let desc = -pixel_26_6(metrics.root_descender);
    let height = asc
        .checked_add(desc)
        .ok_or_else(|| bad_coords("mark height overflow"))?;
    if height <= 0 {
        return Err(bad_coords("invalid mark height"));
    }

    let cw = u64::try_from(width).map_err(|_| bad_coords("mark width overflow"))?;
    let ch = u64::try_from(height).map_err(|_| bad_coords("mark height overflow"))?;
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

    let w_u = tmp.width();
    let h_u = tmp.height();
    let data = tmp.as_slice();
    let mut ink_top: Option<i64> = None;
    let mut ink_bottom = 0_i64;
    for y in 0..h_u {
        let row = &data[y * w_u..(y + 1) * w_u];
        if row.iter().any(|&v| v != 0) {
            let yi = y as i64;
            if ink_top.is_none() {
                ink_top = Some(yi);
            }
            ink_bottom = yi;
        }
    }
    let Some(ink_top) = ink_top else {
        return Err(CoreError::new(
            "value_error",
            "atkinson font produced empty GXR mark",
        ));
    };

    Ok(GxrMark {
        width,
        height,
        ink_top,
        ink_bottom,
    })
}

/// JSON metrics helper for `gxr_mark` WASM and `inktext-mark` CLI.
pub fn gxr_mark_json(font_index_json: &str, fonts_blob: &[u8]) -> Result<Json, CoreError> {
    let fonts = FontPack::from_pack(font_index_json, fonts_blob)?;
    let mark = gxr_mark(&fonts)?;
    Ok(mark.to_mark_json())
}

/// Paint a frame `"logo"` item by redrawing `"GXR"` with `draw_run` at `round(R * h / h_ref)`
/// in the given `[r, g, b]` fill color, using a precomputed [`GxrMark`].
#[allow(clippy::too_many_arguments)]
pub fn paint_logo_with_mark(
    canvas: &mut InkImage,
    x: i64,
    y: i64,
    w: u64,
    h: u64,
    fill: [u8; 3],
    mark: &GxrMark,
    fonts: &FontPack<'_>,
) -> Result<bool, CoreError> {
    if w == 0 || h == 0 || mark.height <= 0 {
        return Err(CoreError::new(
            "value_error",
            "height and width must be > 0",
        ));
    }
    let _ = i64::try_from(w).map_err(|_| bad_coords("logo box overflow"))?;
    let h_i64 = i64::try_from(h).map_err(|_| bad_coords("logo box overflow"))?;
    let px_f = (f64::from(MARK_PX) * (h_i64 as f64) / (mark.height as f64)).round_ties_even();
    if !px_f.is_finite() || px_f > (u32::MAX as f64) {
        return Err(bad_coords("logo px overflow"));
    }
    let px = (px_f as i64).max(1);
    let (xs, _, _) = mark_glyph_xs(fonts, px, x)?;
    draw_run_fields(
        canvas,
        MARK_FONT,
        px,
        false,
        MARK_TEXT,
        &xs,
        y,
        fonts,
        [fill[0], fill[1], fill[2], 255],
    )
}

/// Paint a frame `"logo"` item (`{x, y, w, h}`) onto a canvas in `fill`.
pub fn paint_logo_item(
    canvas: &mut InkImage,
    x: i64,
    y: i64,
    w: u64,
    h: u64,
    fill: [u8; 3],
    fonts: &FontPack<'_>,
) -> Result<bool, CoreError> {
    let mark = gxr_mark(fonts)?;
    paint_logo_with_mark(canvas, x, y, w, h, fill, &mark, fonts)
}
