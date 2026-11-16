//! Pillow 12.2.0 `ImageDraw.text((x, y), ch, font, fill)` port (`anchor="la"`,
//! `fontmode="L"`, BASIC layout, 1 char, `L`/`RGB`/`RGBA`).

use std::collections::BTreeMap;

use gxr_frame::{Val, parse_json};
use gxr_raw::{CoreError, Json};

use super::ftcalc::{FtBBox, FtVector, ft_cbox_pixels, ft_outline_get_cbox, pixel_26_6};
use super::glyf::{FontFace, Outline, SizeMetrics};
use super::gray::{GlyphBitmap, render_glyph_bitmap, render_outline_gray};
use crate::geometry::bounds::{checked_mask_dims, checked_mul_u64, fit_count, try_vec_u8};

/// Supported Pillow destination canvas modes for text painting.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CanvasMode {
    L,
    Rgb,
    Rgba,
}

impl CanvasMode {
    pub(crate) fn parse(name: &str) -> Result<Self, CoreError> {
        match name {
            "L" => Ok(Self::L),
            "RGB" => Ok(Self::Rgb),
            "RGBA" => Ok(Self::Rgba),
            _ => Err(CoreError::new(
                "value_error",
                format!("unsupported canvas mode: {name}"),
            )),
        }
    }

    #[inline]
    pub(crate) fn channels(self) -> usize {
        match self {
            Self::L => 1,
            Self::Rgb => 3,
            Self::Rgba => 4,
        }
    }
}

/// Mutable raster buffer target matching Pillow's `L`, `RGB`, or `RGBA` image.
pub(crate) struct CanvasMut<'a> {
    pub(crate) data: &'a mut [u8],
    pub(crate) width: usize,
    pub(crate) height: usize,
    pub(crate) mode: CanvasMode,
}

/// Mask and placement offset returned by Pillow's `_imagingft.c` `font_render` (`mode="L"`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct RenderedCharMask {
    pub(crate) width: usize,
    pub(crate) height: usize,
    pub(crate) x_offset: i32,
    pub(crate) y_offset: i32,
    pub(crate) mask: Vec<u8>,
}

/// Output of `inktext_draw_u64`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InkTextDrawResult {
    pub width: usize,
    pub height: usize,
    pub channels: usize,
    pub pixels: Vec<u8>,
    pub drawn: usize,
}

impl InkTextDrawResult {
    pub fn report_json(&self) -> Json {
        Json::object([
            ("width", Json::Int(self.width as i64)),
            ("height", Json::Int(self.height as i64)),
            ("channels", Json::Int(self.channels as i64)),
            ("drawn", Json::Int(self.drawn as i64)),
        ])
    }
}

#[inline]
pub fn div255(a: u32) -> u8 {
    let tmp = a + 128;
    (((tmp >> 8) + tmp) >> 8) as u8
}

#[inline]
pub fn blend_u8(mask: u8, dst: u8, ink: u8) -> u8 {
    div255(u32::from(dst) * u32::from(255 - mask) + u32::from(ink) * u32::from(mask))
}

fn bad_geom(msg: &str) -> CoreError {
    CoreError::new("bad_geometry", msg)
}

fn render_bitmap_into_mask(
    bitmap: &GlyphBitmap,
    bbox_px: FtBBox,
    advanced_px: i64,
    ascender_px: i64,
) -> Result<RenderedCharMask, CoreError> {
    let x_min = 0_i64.min(bbox_px.x_min);
    let x_max = 0_i64.max(advanced_px).max(bbox_px.x_max);
    let y_min = 0_i64.min(bbox_px.y_min);
    let y_max = 0_i64.max(bbox_px.y_max);

    let mask_w_i64 = x_max
        .checked_sub(x_min)
        .ok_or_else(|| bad_geom("mask width overflow"))?;
    let mask_h_i64 = y_max
        .checked_sub(y_min)
        .ok_or_else(|| bad_geom("mask height overflow"))?;
    let x_offset = i32::try_from(x_min).map_err(|_| bad_geom("x_offset"))?;
    let y_offset = y_max
        .checked_sub(ascender_px)
        .and_then(i64::checked_neg)
        .and_then(|v| i32::try_from(v).ok())
        .ok_or_else(|| bad_geom("y_offset"))?;

    if mask_w_i64 <= 0 || mask_h_i64 <= 0 {
        let width = fit_count(u64::try_from(mask_w_i64.max(0)).unwrap_or(0), 1)?;
        let height = fit_count(u64::try_from(mask_h_i64.max(0)).unwrap_or(0), 1)?;
        return Ok(RenderedCharMask {
            width,
            height,
            x_offset,
            y_offset,
            mask: Vec::new(),
        });
    }

    let (mask_w, mask_h, pixels) = checked_mask_dims(mask_w_i64 as u64, mask_h_i64 as u64)?;
    let mut mask = try_vec_u8(pixels, 0)?;

    if !bitmap.buffer.is_empty() && bitmap.width > 0 && bitmap.height > 0 {
        let bmp_pixels = checked_mul_u64(bitmap.width as u64, bitmap.height as u64)?;
        let expected_bmp = fit_count(bmp_pixels, 1)?;
        if bitmap.buffer.len() != expected_bmp {
            return Err(bad_geom("bitmap buffer length mismatch"));
        }
        let mw = i64::try_from(mask_w).map_err(|_| bad_geom("mask_w"))?;
        let mh = i64::try_from(mask_h).map_err(|_| bad_geom("mask_h"))?;
        let bw = i64::try_from(bitmap.width).map_err(|_| bad_geom("bmp_w"))?;
        let bh = i64::try_from(bitmap.height).map_err(|_| bad_geom("bmp_h"))?;
        let r_xmin = 0_i64.min(i64::from(bitmap.left));
        let r_ymax = 0_i64.max(i64::from(bitmap.top));
        let xx = i64::from(bitmap.left)
            .checked_sub(r_xmin)
            .ok_or_else(|| bad_geom("bitmap x overflow"))?;
        let yy0 = r_ymax
            .checked_sub(i64::from(bitmap.top))
            .ok_or_else(|| bad_geom("bitmap y overflow"))?;
        let xx1 = xx
            .checked_add(bw)
            .ok_or_else(|| bad_geom("bitmap x overflow"))?;
        let yy1 = yy0
            .checked_add(bh)
            .ok_or_else(|| bad_geom("bitmap y overflow"))?;

        let ix0 = xx.max(0);
        let ix1 = xx1.min(mw);
        let iy0 = yy0.max(0);
        let iy1 = yy1.min(mh);
        if ix0 < ix1 && iy0 < iy1 {
            let dst_x0 = usize::try_from(ix0).map_err(|_| bad_geom("dst_x0"))?;
            let src_x0 = ix0
                .checked_sub(xx)
                .and_then(|v| usize::try_from(v).ok())
                .ok_or_else(|| bad_geom("src_x0"))?;
            let len = ix1
                .checked_sub(ix0)
                .and_then(|v| usize::try_from(v).ok())
                .ok_or_else(|| bad_geom("copy len"))?;
            let dst_y0 = usize::try_from(iy0).map_err(|_| bad_geom("dst_y0"))?;
            let src_y0 = iy0
                .checked_sub(yy0)
                .and_then(|v| usize::try_from(v).ok())
                .ok_or_else(|| bad_geom("src_y0"))?;
            let rows = iy1
                .checked_sub(iy0)
                .and_then(|v| usize::try_from(v).ok())
                .ok_or_else(|| bad_geom("rows"))?;
            for r in 0..rows {
                let dst_off = (dst_y0 + r) * mask_w + dst_x0;
                let src_off = (src_y0 + r) * bitmap.width + src_x0;
                mask[dst_off..dst_off + len]
                    .copy_from_slice(&bitmap.buffer[src_off..src_off + len]);
            }
        }
    }

    Ok(RenderedCharMask {
        width: mask_w,
        height: mask_h,
        x_offset,
        y_offset,
        mask,
    })
}

fn render_char_mask_tracked(
    face: &FontFace<'_>,
    px: u32,
    ch: char,
) -> Result<(RenderedCharMask, bool), CoreError> {
    let metrics = SizeMetrics::from_pixel_size(face, px)?;
    let glyph = face.load_char(&metrics, ch)?;
    let bbox_px = if glyph.outline.points.is_empty() {
        FtBBox::default()
    } else {
        ft_cbox_pixels(ft_outline_get_cbox(&glyph.outline.points))
    };
    let advanced_px = pixel_26_6(glyph.hori_advance);
    let ascender_px = pixel_26_6(metrics.root_ascender);
    let bitmap = render_glyph_bitmap(&glyph.outline, Some((metrics.x_ppem, metrics.y_ppem)))?;
    let mask = render_bitmap_into_mask(&bitmap, bbox_px, advanced_px, ascender_px)?;
    Ok((mask, glyph.hinted))
}

/// Port of Pillow `Paste.c` `fill_mask_L` (`ImageDraw.draw_bitmap((x + x_off, y + y_off), mask, ink)`).
pub(crate) fn blend_mask_onto(
    canvas: &mut CanvasMut<'_>,
    x: i64,
    y: i64,
    mask: &RenderedCharMask,
    fill: [u8; 4],
) -> Result<(), CoreError> {
    let (_, _, pixels) = checked_mask_dims(canvas.width as u64, canvas.height as u64)?;
    let ch = canvas.mode.channels();
    let expected = fit_count(checked_mul_u64(pixels, ch as u64)?, 1)?;
    if canvas.data.len() != expected {
        return Err(bad_geom("canvas buffer length mismatch"));
    }

    let mw_u64 = u64::try_from(mask.width).map_err(|_| bad_geom("mask width overflow"))?;
    let mh_u64 = u64::try_from(mask.height).map_err(|_| bad_geom("mask height overflow"))?;
    let _ = fit_count(mw_u64, 1)?;
    let _ = fit_count(mh_u64, 1)?;
    let mask_pixels = checked_mul_u64(mw_u64, mh_u64)?;
    let expected_mask = fit_count(mask_pixels, 1)?;
    if mask.mask.len() != expected_mask {
        return Err(bad_geom("mask buffer length mismatch"));
    }
    if mask.width == 0 || mask.height == 0 {
        return Ok(());
    }

    let cw = i64::try_from(canvas.width).map_err(|_| bad_geom("canvas width overflow"))?;
    let ch_h = i64::try_from(canvas.height).map_err(|_| bad_geom("canvas height overflow"))?;
    let mw = i64::try_from(mask.width).map_err(|_| bad_geom("mask width overflow"))?;
    let mh = i64::try_from(mask.height).map_err(|_| bad_geom("mask height overflow"))?;

    let dx0 = x
        .checked_add(i64::from(mask.x_offset))
        .ok_or_else(|| bad_geom("x coordinate overflow"))?;
    let dy0 = y
        .checked_add(i64::from(mask.y_offset))
        .ok_or_else(|| bad_geom("y coordinate overflow"))?;
    let dx1 = dx0
        .checked_add(mw)
        .ok_or_else(|| bad_geom("x coordinate overflow"))?;
    let dy1 = dy0
        .checked_add(mh)
        .ok_or_else(|| bad_geom("y coordinate overflow"))?;

    let ix0 = dx0.max(0);
    let ix1 = dx1.min(cw);
    let iy0 = dy0.max(0);
    let iy1 = dy1.min(ch_h);
    if ix0 >= ix1 || iy0 >= iy1 {
        return Ok(());
    }

    let sx0_i64 = ix0
        .checked_sub(dx0)
        .ok_or_else(|| bad_geom("x coordinate overflow"))?;
    let sy0_i64 = iy0
        .checked_sub(dy0)
        .ok_or_else(|| bad_geom("y coordinate overflow"))?;
    let xsize_i64 = ix1
        .checked_sub(ix0)
        .ok_or_else(|| bad_geom("x coordinate overflow"))?;
    let ysize_i64 = iy1
        .checked_sub(iy0)
        .ok_or_else(|| bad_geom("y coordinate overflow"))?;

    let dx0 = usize::try_from(ix0).map_err(|_| bad_geom("x index overflow"))?;
    let dy0 = usize::try_from(iy0).map_err(|_| bad_geom("y index overflow"))?;
    let sx0 = usize::try_from(sx0_i64).map_err(|_| bad_geom("sx index overflow"))?;
    let sy0 = usize::try_from(sy0_i64).map_err(|_| bad_geom("sy index overflow"))?;
    let xsize = usize::try_from(xsize_i64).map_err(|_| bad_geom("xsize overflow"))?;
    let ysize = usize::try_from(ysize_i64).map_err(|_| bad_geom("ysize overflow"))?;

    for row in 0..ysize {
        let mask_row =
            &mask.mask[(sy0 + row) * mask.width + sx0..(sy0 + row) * mask.width + sx0 + xsize];
        let dst_row_start = ((dy0 + row) * canvas.width + dx0) * ch;
        let dst_row = &mut canvas.data[dst_row_start..dst_row_start + xsize * ch];
        match canvas.mode {
            CanvasMode::L => {
                for (px, &m) in dst_row.iter_mut().zip(mask_row.iter()) {
                    if m != 0 {
                        *px = blend_u8(m, *px, fill[0]);
                    }
                }
            }
            CanvasMode::Rgb => {
                for (px, &m) in dst_row.chunks_exact_mut(3).zip(mask_row.iter()) {
                    if m != 0 {
                        px[0] = blend_u8(m, px[0], fill[0]);
                        px[1] = blend_u8(m, px[1], fill[1]);
                        px[2] = blend_u8(m, px[2], fill[2]);
                    }
                }
            }
            CanvasMode::Rgba => {
                for (px, &m) in dst_row.chunks_exact_mut(4).zip(mask_row.iter()) {
                    if m != 0 {
                        let rgb_m = if px[3] == 0 { 255 } else { m };
                        px[0] = blend_u8(rgb_m, px[0], fill[0]);
                        px[1] = blend_u8(rgb_m, px[1], fill[1]);
                        px[2] = blend_u8(rgb_m, px[2], fill[2]);
                        px[3] = blend_u8(m, px[3], fill[3]);
                    }
                }
            }
        }
    }
    Ok(())
}

/// Like [`draw_char`], also reporting whether the glyph used hinting/auto-hinter/CFF.
pub(crate) fn draw_char_tracked(
    canvas: &mut CanvasMut<'_>,
    face: &FontFace<'_>,
    px: u32,
    ch: char,
    x: i64,
    y: i64,
    fill: [u8; 4],
) -> Result<bool, CoreError> {
    let (mask, hinted) = render_char_mask_tracked(face, px, ch)?;
    blend_mask_onto(canvas, x, y, &mask, fill)?;
    Ok(hinted)
}

pub(crate) fn unpack_font_pack<'a>(
    index_json: &str,
    blob: &'a [u8],
) -> Result<BTreeMap<String, &'a [u8]>, CoreError> {
    let idx = parse_json(index_json.as_bytes())?;
    let Val::Arr(entries) = idx else {
        return Err(CoreError::new("value_error", "font index must be an array"));
    };
    let mut map = BTreeMap::new();
    let mut cursor = 0_usize;
    for item in entries {
        let Val::Arr(pair) = item else {
            return Err(CoreError::new(
                "value_error",
                "font index entry must be [name, len]",
            ));
        };
        if pair.len() != 2 {
            return Err(CoreError::new(
                "value_error",
                "font index entry must have 2 elements",
            ));
        }
        let Val::Str(name) = &pair[0] else {
            return Err(CoreError::new("value_error", "font name must be a string"));
        };
        let Val::Int(len_i64) = pair[1] else {
            return Err(CoreError::new(
                "value_error",
                "font length must be an integer",
            ));
        };
        let len = usize::try_from(len_i64)
            .map_err(|_| CoreError::new("value_error", "negative font length"))?;
        let end = cursor
            .checked_add(len)
            .ok_or_else(|| CoreError::new("value_error", "font pack offset overflow"))?;
        if end > blob.len() {
            return Err(CoreError::new("value_error", "font pack truncated"));
        }
        map.insert(name.clone(), &blob[cursor..end]);
        cursor = end;
    }
    if cursor != blob.len() {
        return Err(CoreError::new("value_error", "font pack trailing bytes"));
    }
    Ok(map)
}

pub(crate) fn val_i64(v: &Val, field: &str) -> Result<i64, CoreError> {
    match v {
        Val::Int(n) => Ok(*n),
        _ => Err(CoreError::new(
            "value_error",
            format!("{field} must be an integer"),
        )),
    }
}

pub(crate) fn val_str<'a>(v: &'a Val, field: &str) -> Result<&'a str, CoreError> {
    match v {
        Val::Str(s) => Ok(s.as_str()),
        _ => Err(CoreError::new(
            "value_error",
            format!("{field} must be a string"),
        )),
    }
}

fn parse_fill(val: &Val, mode: CanvasMode) -> Result<[u8; 4], CoreError> {
    let byte = |v: &Val| -> Result<u8, CoreError> {
        let n = val_i64(v, "fill")?;
        u8::try_from(n).map_err(|_| CoreError::new("value_error", "fill channel out of 0..=255"))
    };
    match mode {
        CanvasMode::L => match val {
            Val::Int(_) => Ok([byte(val)?, 0, 0, 255]),
            Val::Arr(arr) if arr.len() == 1 => Ok([byte(&arr[0])?, 0, 0, 255]),
            _ => Err(CoreError::new("value_error", "L fill must be int or [u8]")),
        },
        CanvasMode::Rgb => match val {
            Val::Arr(arr) if arr.len() == 3 => {
                Ok([byte(&arr[0])?, byte(&arr[1])?, byte(&arr[2])?, 255])
            }
            _ => Err(CoreError::new("value_error", "RGB fill must be [r, g, b]")),
        },
        CanvasMode::Rgba => match val {
            Val::Arr(arr) if arr.len() == 4 => Ok([
                byte(&arr[0])?,
                byte(&arr[1])?,
                byte(&arr[2])?,
                byte(&arr[3])?,
            ]),
            Val::Arr(arr) if arr.len() == 3 => {
                Ok([byte(&arr[0])?, byte(&arr[1])?, byte(&arr[2])?, 255])
            }
            _ => Err(CoreError::new(
                "value_error",
                "RGBA fill must be [r, g, b, a]",
            )),
        },
    }
}

fn parse_outline(obj: &Val) -> Result<Outline, CoreError> {
    let Some(Val::Arr(pts_v)) = obj.get("points") else {
        return Err(CoreError::new(
            "value_error",
            "outline points must be array",
        ));
    };
    let Some(Val::Arr(tags_v)) = obj.get("tags") else {
        return Err(CoreError::new("value_error", "outline tags must be array"));
    };
    let Some(Val::Arr(contours_v)) = obj.get("contours") else {
        return Err(CoreError::new(
            "value_error",
            "outline contours must be array",
        ));
    };
    let mut points = Vec::with_capacity(pts_v.len());
    for p in pts_v {
        let Val::Arr(pair) = p else {
            return Err(CoreError::new("value_error", "point must be [x, y]"));
        };
        if pair.len() != 2 {
            return Err(CoreError::new("value_error", "point must have 2 elements"));
        }
        points.push(FtVector {
            x: val_i64(&pair[0], "point.x")?,
            y: val_i64(&pair[1], "point.y")?,
        });
    }
    let mut tags = Vec::with_capacity(tags_v.len());
    for t in tags_v {
        let n = val_i64(t, "tag")?;
        tags.push(u8::try_from(n).map_err(|_| CoreError::new("value_error", "bad tag"))?);
    }
    let mut contours = Vec::with_capacity(contours_v.len());
    for c in contours_v {
        let n = val_i64(c, "contour")?;
        contours.push(u16::try_from(n).map_err(|_| CoreError::new("value_error", "bad contour"))?);
    }
    let flags = match obj.get("flags") {
        Some(v) => u32::try_from(val_i64(v, "flags")?)
            .map_err(|_| CoreError::new("value_error", "bad outline flags"))?,
        None => 0,
    };
    Ok(Outline {
        points,
        tags,
        contours,
        flags,
    })
}

/// Test-facing op interpreter for text & outline rasterization across native CLI and WASM.
pub fn inktext_draw_u64(
    pixels: &[u8],
    w: u64,
    h: u64,
    mode_str: &str,
    ops_json: &str,
    font_index_json: &str,
    fonts_blob: &[u8],
) -> Result<InkTextDrawResult, CoreError> {
    let mode = CanvasMode::parse(mode_str)?;
    let (wu, hu, count) = checked_mask_dims(w, h)?;
    let ch = mode.channels();
    let expected = fit_count(checked_mul_u64(count, ch as u64)?, 1)?;
    if pixels.len() != expected {
        return Err(CoreError::new(
            "bad_geometry",
            "canvas buffer length mismatch",
        ));
    }

    let font_blobs = unpack_font_pack(font_index_json, fonts_blob)?;
    let mut faces: BTreeMap<String, FontFace<'_>> = BTreeMap::new();

    let ops_val = parse_json(ops_json.as_bytes())?;
    let Val::Arr(ops) = ops_val else {
        return Err(CoreError::new("value_error", "ops must be an array"));
    };

    let mut out = pixels.to_vec();
    let mut canvas = CanvasMut {
        data: &mut out,
        width: wu,
        height: hu,
        mode,
    };
    let mut drawn = 0_usize;

    for op in ops {
        let kind = op
            .get("op")
            .ok_or_else(|| CoreError::new("value_error", "missing op"))?;
        match val_str(kind, "op")? {
            "char" => {
                let font_name = val_str(
                    op.get("font")
                        .ok_or_else(|| CoreError::new("value_error", "missing font"))?,
                    "font",
                )?;
                let px_i64 = val_i64(
                    op.get("px")
                        .ok_or_else(|| CoreError::new("value_error", "missing px"))?,
                    "px",
                )?;
                let px = u32::try_from(px_i64)
                    .map_err(|_| CoreError::new("value_error", "invalid pixel size"))?;
                let ch_str = val_str(
                    op.get("ch")
                        .ok_or_else(|| CoreError::new("value_error", "missing ch"))?,
                    "ch",
                )?;
                let mut chars = ch_str.chars();
                let c = chars
                    .next()
                    .ok_or_else(|| CoreError::new("value_error", "ch must be non-empty"))?;
                if chars.next().is_some() {
                    return Err(CoreError::new("value_error", "ch must be 1 character"));
                }
                let x = val_i64(
                    op.get("x")
                        .ok_or_else(|| CoreError::new("value_error", "missing x"))?,
                    "x",
                )?;
                let y = val_i64(
                    op.get("y")
                        .ok_or_else(|| CoreError::new("value_error", "missing y"))?,
                    "y",
                )?;
                let fill = parse_fill(
                    op.get("fill")
                        .ok_or_else(|| CoreError::new("value_error", "missing fill"))?,
                    mode,
                )?;
                if !faces.contains_key(font_name) {
                    let bytes = font_blobs.get(font_name).ok_or_else(|| {
                        CoreError::new("value_error", format!("unknown font {font_name}"))
                    })?;
                    faces.insert(font_name.to_string(), FontFace::parse(bytes)?);
                }
                let face = &faces[font_name];
                draw_char_tracked(&mut canvas, face, px, c, x, y, fill)?;
                drawn += 1;
            }
            "outline" => {
                let outline = parse_outline(&op)?;
                let x = op
                    .get("x")
                    .map(|v| val_i64(v, "x"))
                    .transpose()?
                    .unwrap_or(0);
                let y = op
                    .get("y")
                    .map(|v| val_i64(v, "y"))
                    .transpose()?
                    .unwrap_or(0);
                let fill = parse_fill(
                    op.get("fill")
                        .ok_or_else(|| CoreError::new("value_error", "missing fill"))?,
                    mode,
                )?;
                let raw = matches!(op.get("raw"), Some(Val::Bool(true)));
                if raw {
                    let w_i64 = op
                        .get("w")
                        .map(|v| val_i64(v, "w"))
                        .transpose()?
                        .unwrap_or(wu as i64);
                    let h_i64 = op
                        .get("h")
                        .map(|v| val_i64(v, "h"))
                        .transpose()?
                        .unwrap_or(hu as i64);
                    let w_u64 = u64::try_from(w_i64).map_err(|_| bad_geom("bad w"))?;
                    let h_u64 = u64::try_from(h_i64).map_err(|_| bad_geom("bad h"))?;
                    let w_out = fit_count(w_u64, 1)?;
                    let h_out = fit_count(h_u64, 1)?;
                    let buf = render_outline_gray(&outline, w_out, h_out)?;
                    let mask = RenderedCharMask {
                        width: w_out,
                        height: h_out,
                        x_offset: 0,
                        y_offset: 0,
                        mask: buf,
                    };
                    blend_mask_onto(&mut canvas, x, y, &mask, fill)?;
                } else {
                    let ppem = match op.get("ppem") {
                        Some(Val::Arr(pair)) if pair.len() == 2 => {
                            let xp = u16::try_from(val_i64(&pair[0], "ppem[0]")?)
                                .map_err(|_| CoreError::new("value_error", "bad ppem"))?;
                            let yp = u16::try_from(val_i64(&pair[1], "ppem[1]")?)
                                .map_err(|_| CoreError::new("value_error", "bad ppem"))?;
                            Some((xp, yp))
                        }
                        Some(Val::Null) | None => None,
                        _ => return Err(CoreError::new("value_error", "bad ppem")),
                    };
                    let bmp = render_glyph_bitmap(&outline, ppem)?;
                    let mask = RenderedCharMask {
                        width: bmp.width,
                        height: bmp.height,
                        x_offset: bmp.left,
                        y_offset: -bmp.top,
                        mask: bmp.buffer,
                    };
                    blend_mask_onto(&mut canvas, x, y, &mask, fill)?;
                }
                drawn += 1;
            }
            other => {
                return Err(CoreError::new(
                    "value_error",
                    format!("unknown inktext op: {other}"),
                ));
            }
        }
    }

    Ok(InkTextDrawResult {
        width: wu,
        height: hu,
        channels: ch,
        pixels: out,
        drawn,
    })
}

/// Render a batch of `(px, char)` masks from one font, returning `(meta_json, mask_blob)`.
pub fn inktext_batch_masks(
    font_bytes: &[u8],
    cases_json: &str,
) -> Result<(Json, Vec<u8>), CoreError> {
    let face = FontFace::parse(font_bytes)?;
    let spec = parse_json(cases_json.as_bytes())?;
    let Some(Val::Arr(pxs_v)) = spec.get("pxs") else {
        return Err(CoreError::new("value_error", "cases.pxs must be array"));
    };
    let Some(Val::Arr(chars_v)) = spec.get("chars") else {
        return Err(CoreError::new("value_error", "cases.chars must be array"));
    };

    let mut pxs = Vec::with_capacity(pxs_v.len());
    for v in pxs_v {
        let px_i = val_i64(v, "px")?;
        let px =
            u32::try_from(px_i).map_err(|_| CoreError::new("value_error", "invalid pixel size"))?;
        pxs.push(px);
    }
    let mut chars = Vec::with_capacity(chars_v.len());
    for v in chars_v {
        let s = val_str(v, "char")?;
        let mut it = s.chars();
        let c = it
            .next()
            .ok_or_else(|| CoreError::new("value_error", "empty char"))?;
        if it.next().is_some() {
            return Err(CoreError::new("value_error", "char must be 1 scalar"));
        }
        chars.push(c);
    }

    let mut items = Vec::with_capacity(pxs.len() * chars.len());
    let mut blob = Vec::new();
    for px in pxs {
        let metrics = SizeMetrics::from_pixel_size(&face, px)?;
        let ascender_px = pixel_26_6(metrics.root_ascender);
        for &c in &chars {
            let glyph = face.load_char(&metrics, c)?;
            let bbox_px = if glyph.outline.points.is_empty() {
                FtBBox::default()
            } else {
                ft_cbox_pixels(ft_outline_get_cbox(&glyph.outline.points))
            };
            let advanced_px = pixel_26_6(glyph.hori_advance);
            let bitmap =
                render_glyph_bitmap(&glyph.outline, Some((metrics.x_ppem, metrics.y_ppem)))?;
            let rendered = render_bitmap_into_mask(&bitmap, bbox_px, advanced_px, ascender_px)?;
            let len = rendered.mask.len() as i64;
            blob.extend_from_slice(&rendered.mask);
            items.push(Json::ints(&[
                i64::from(px),
                i64::from(u32::from(c)),
                rendered.width as i64,
                rendered.height as i64,
                i64::from(rendered.x_offset),
                i64::from(rendered.y_offset),
                len,
            ]));
        }
    }

    Ok((Json::object([("items", Json::Array(items))]), blob))
}
