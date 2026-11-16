//! Pillow 12.2.0 `ImageDraw.text((x, y), ch, font, fill)` port (`anchor="la"`,
//! `fontmode="L"`, BASIC layout, 1 char, `L`/`RGB`/`RGBA`).

use std::collections::BTreeMap;

use gxr_frame::{Val, parse_json};
use gxr_raw::{CoreError, Json};

use super::ftcalc::{FtBBox, FtVector, ft_cbox_pixels, ft_outline_get_cbox, pixel_26_6};
use super::glyf::{FontFace, Outline, SizeMetrics};
use super::gray::{GlyphBitmap, render_glyph_bitmap, render_outline_gray};
use crate::geometry::bounds::{checked_mask_dims, checked_mul_u64, fit_count};

/// Supported Pillow destination canvas modes for text painting.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CanvasMode {
    L,
    Rgb,
    Rgba,
}

impl CanvasMode {
    pub fn parse(name: &str) -> Result<Self, CoreError> {
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
    pub fn channels(self) -> usize {
        match self {
            Self::L => 1,
            Self::Rgb => 3,
            Self::Rgba => 4,
        }
    }
}

/// Mutable raster buffer target matching Pillow's `L`, `RGB`, or `RGBA` image.
pub struct CanvasMut<'a> {
    pub data: &'a mut [u8],
    pub width: usize,
    pub height: usize,
    pub mode: CanvasMode,
}

/// Mask and placement offset returned by Pillow's `_imagingft.c` `font_render` (`mode="L"`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RenderedCharMask {
    pub width: usize,
    pub height: usize,
    pub x_offset: i32,
    pub y_offset: i32,
    pub mask: Vec<u8>,
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

    let mask_w_i64 = x_max - x_min;
    let mask_h_i64 = y_max - y_min;
    let x_offset = i32::try_from(x_min).map_err(|_| CoreError::new("bad_geometry", "x_offset"))?;
    let y_offset = i32::try_from(-(y_max - ascender_px))
        .map_err(|_| CoreError::new("bad_geometry", "y_offset"))?;

    if mask_w_i64 <= 0 || mask_h_i64 <= 0 {
        let width = usize::try_from(mask_w_i64.max(0))
            .map_err(|_| CoreError::new("bad_geometry", "mask_w"))?;
        let height = usize::try_from(mask_h_i64.max(0))
            .map_err(|_| CoreError::new("bad_geometry", "mask_h"))?;
        let _ = fit_count(width as u64, 1)?;
        let _ = fit_count(height as u64, 1)?;
        return Ok(RenderedCharMask {
            width,
            height,
            x_offset,
            y_offset,
            mask: Vec::new(),
        });
    }

    let (mask_w, mask_h, pixels) = checked_mask_dims(mask_w_i64 as u64, mask_h_i64 as u64)?;
    let mask_len = fit_count(pixels, 1)?;
    let mut mask = vec![0_u8; mask_len];

    if !bitmap.buffer.is_empty() && bitmap.width > 0 && bitmap.height > 0 {
        let r_xmin = 0_i32.min(bitmap.left);
        let r_ymax = 0_i32.max(bitmap.top);
        let xx = i64::from(-r_xmin) + i64::from(bitmap.left);
        let yy0 = i64::from(r_ymax) - i64::from(bitmap.top);

        let x0 = usize::try_from(0_i64.max(-xx))
            .unwrap_or(0)
            .min(bitmap.width);
        let x1 = usize::try_from(0_i64.max((mask_w as i64) - xx))
            .unwrap_or(0)
            .min(bitmap.width);
        if x1 > x0 {
            for by in 0..bitmap.height {
                let yy = yy0 + (by as i64);
                if yy >= 0 && yy < (mask_h as i64) {
                    let dst_off = (yy as usize) * mask_w + ((xx + (x0 as i64)) as usize);
                    let src_off = by * bitmap.width + x0;
                    let len = x1 - x0;
                    mask[dst_off..dst_off + len]
                        .copy_from_slice(&bitmap.buffer[src_off..src_off + len]);
                }
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

/// Port of Pillow `_imagingft.c` (`text_layout_fallback` + `bounding_box_and_anchors` + `font_render`)
/// for a single character, horizontal BASIC layout, `anchor="la"`, `mode="L"`.
pub fn render_char_mask(
    face: &FontFace<'_>,
    px: u32,
    ch: char,
) -> Result<RenderedCharMask, CoreError> {
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
    render_bitmap_into_mask(&bitmap, bbox_px, advanced_px, ascender_px)
}

/// Port of Pillow `Paste.c` `fill_mask_L` (`ImageDraw.draw_bitmap((x + x_off, y + y_off), mask, ink)`).
pub fn blend_mask_onto(
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
        return Err(CoreError::new(
            "bad_geometry",
            "canvas buffer length mismatch",
        ));
    }
    if mask.width == 0 || mask.height == 0 || mask.mask.is_empty() {
        return Ok(());
    }

    let mut dx0 = x
        .checked_add(i64::from(mask.x_offset))
        .ok_or_else(|| CoreError::new("bad_geometry", "x coordinate overflow"))?;
    let mut dy0 = y
        .checked_add(i64::from(mask.y_offset))
        .ok_or_else(|| CoreError::new("bad_geometry", "y coordinate overflow"))?;
    let mut xsize = mask.width as i64;
    let mut ysize = mask.height as i64;
    let mut sx0 = 0_i64;
    let mut sy0 = 0_i64;

    if dx0 < 0 {
        xsize += dx0;
        sx0 = -dx0;
        dx0 = 0;
    }
    let cw = canvas.width as i64;
    if dx0 + xsize > cw {
        xsize = cw - dx0;
    }
    if dy0 < 0 {
        ysize += dy0;
        sy0 = -dy0;
        dy0 = 0;
    }
    let ch_h = canvas.height as i64;
    if dy0 + ysize > ch_h {
        ysize = ch_h - dy0;
    }
    if xsize <= 0 || ysize <= 0 {
        return Ok(());
    }

    let dx0 = dx0 as usize;
    let dy0 = dy0 as usize;
    let sx0 = sx0 as usize;
    let sy0 = sy0 as usize;
    let xsize = xsize as usize;
    let ysize = ysize as usize;

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

/// Port of Pillow `ImageDraw.text((x, y), ch, font=..., fill=...)` for one char (`anchor="la"`).
pub fn draw_char(
    canvas: &mut CanvasMut<'_>,
    face: &FontFace<'_>,
    px: u32,
    ch: char,
    x: i64,
    y: i64,
    fill: [u8; 4],
) -> Result<(), CoreError> {
    let mask = render_char_mask(face, px, ch)?;
    blend_mask_onto(canvas, x, y, &mask, fill)
}

fn unpack_font_pack<'a>(
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

fn val_i64(v: &Val, field: &str) -> Result<i64, CoreError> {
    match v {
        Val::Int(n) => Ok(*n),
        _ => Err(CoreError::new(
            "value_error",
            format!("{field} must be an integer"),
        )),
    }
}

fn val_str<'a>(v: &'a Val, field: &str) -> Result<&'a str, CoreError> {
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
                draw_char(&mut canvas, face, px, c, x, y, fill)?;
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
                    let w_out = usize::try_from(
                        op.get("w")
                            .map(|v| val_i64(v, "w"))
                            .transpose()?
                            .unwrap_or(wu as i64),
                    )
                    .map_err(|_| CoreError::new("value_error", "bad w"))?;
                    let h_out = usize::try_from(
                        op.get("h")
                            .map(|v| val_i64(v, "h"))
                            .transpose()?
                            .unwrap_or(hu as i64),
                    )
                    .map_err(|_| CoreError::new("value_error", "bad h"))?;
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blend_mask_rgba_preserves_transparent_and_opaque_dst_rules() {
        // dst pixel 0: alpha=0 (transparent) -> RGB uses channel_mask = 255, alpha uses mask.
        // dst pixel 1: alpha=100 (>0) -> RGB & alpha both use channel_mask = mask.
        let mut buf = vec![10, 20, 30, 0, 10, 20, 30, 100];
        let mut canvas = CanvasMut {
            data: &mut buf,
            width: 2,
            height: 1,
            mode: CanvasMode::Rgba,
        };
        let mask = RenderedCharMask {
            width: 2,
            height: 1,
            x_offset: 0,
            y_offset: 0,
            mask: vec![128, 128],
        };
        blend_mask_onto(&mut canvas, 0, 0, &mask, [200, 100, 50, 255]).unwrap();
        assert_eq!(&buf[0..4], &[200, 100, 50, 128]);
        assert_eq!(
            &buf[4..8],
            &[
                blend_u8(128, 10, 200),
                blend_u8(128, 20, 100),
                blend_u8(128, 30, 50),
                blend_u8(128, 100, 255),
            ]
        );
    }

    #[test]
    fn json_nesting_over_128_is_rejected() {
        let deep = "[".repeat(130) + &"]".repeat(130);
        let err = inktext_draw_u64(&[0], 1, 1, "L", &deep, "[]", &[]).unwrap_err();
        assert_eq!(err.kind, "json");
    }

    #[test]
    fn draw_ops_renders_even_odd_and_rejects_huge_cbox() {
        let ops = r#"[{"op":"outline","points":[[0,0],[0,384],[384,384],[384,0],[128,128],[128,256],[256,256],[256,128]],"tags":[1,1,1,1,1,1,1,1],"contours":[3,7],"flags":2,"x":0,"y":6,"fill":255,"ppem":[16,16]}]"#;
        let res = inktext_draw_u64(&[0_u8; 6 * 6], 6, 6, "L", ops, "[]", &[]).unwrap();
        assert_eq!(res.pixels[3 * 6 + 3], 0);
        assert_eq!(res.pixels[0], 255);

        let huge_ops = r#"[{"op":"outline","points":[[0,0],[0,64],[16777217,64],[16777217,0]],"tags":[1,1,1,1],"contours":[3],"raw":true,"w":4,"h":4,"fill":255}]"#;
        assert!(inktext_draw_u64(&[0_u8; 16], 4, 4, "L", huge_ops, "[]", &[]).is_err());
    }
}
