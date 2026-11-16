//! Anti-aliased outline rasterizer (`ftgrays.c` + `ftsmooth.c` + `FT_Outline_Decompose`).
//!
//! Safe Rust port of FreeType 2.14.3 (`FT_INT64` scanline & conic DDA paths),
//! including 4× oversampling for outlines flagged with `FT_OUTLINE_OVERLAP`.

use gxr_raw::CoreError;

use super::ftcalc::{FtBBox, FtVector, ft_outline_get_cbox};
use super::glyf::{
    FT_CURVE_TAG_CONIC, FT_CURVE_TAG_CUBIC, FT_CURVE_TAG_ON, FT_OUTLINE_OVERLAP, Outline,
};
use super::gray_worker::{ClipBox, GrayTarget, GrayWorker, OVERLAP_SCALE};
use crate::geometry::bounds::{checked_mask_dims, try_vec_u8};

/// Grayscale coverage bitmap produced by `ft_smooth_render`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct GlyphBitmap {
    pub left: i32,
    pub top: i32,
    pub width: usize,
    pub height: usize,
    /// Top-down row-major 8-bit coverage (`width * height` bytes).
    pub buffer: Vec<u8>,
}

fn validate_outline(outline: &Outline) -> Result<(), CoreError> {
    if outline.points.len() != outline.tags.len() {
        return Err(CoreError::new("value_error", "invalid outline tag count"));
    }
    if outline.points.is_empty() || outline.contours.is_empty() {
        if outline.points.is_empty() && outline.contours.is_empty() {
            return Ok(());
        }
        return Err(CoreError::new("value_error", "invalid empty outline"));
    }
    let last = *outline
        .contours
        .last()
        .ok_or_else(|| CoreError::new("value_error", "invalid outline"))?;
    if usize::from(last) + 1 != outline.points.len() {
        return Err(CoreError::new(
            "value_error",
            "invalid outline contour endpoint",
        ));
    }
    let mut prev_end: Option<usize> = None;
    for &end in &outline.contours {
        let end_u = usize::from(end);
        if let Some(prev) = prev_end
            && end_u <= prev
        {
            return Err(CoreError::new("value_error", "invalid contour order"));
        }
        prev_end = Some(end_u);
    }
    Ok(())
}

/// Port of FreeType `ft_glyphslot_preset_bitmap` for `FT_RENDER_MODE_NORMAL`.
pub fn preset_bitmap(
    outline: &Outline,
    ppem: Option<(u16, u16)>,
) -> Result<(i32, i32, usize, usize), CoreError> {
    validate_outline(outline)?;
    let cbox = ft_outline_get_cbox(&outline.points);
    let mut pbox = FtBBox {
        x_min: cbox.x_min >> 6,
        y_min: cbox.y_min >> 6,
        x_max: cbox.x_max >> 6,
        y_max: cbox.y_max >> 6,
    };
    pbox.x_min += (cbox.x_min & 63) >> 6;
    pbox.y_min += (cbox.y_min & 63) >> 6;
    pbox.x_max += ((cbox.x_max & 63) + 63) >> 6;
    pbox.y_max += ((cbox.y_max & 63) + 63) >> 6;

    let x_left = pbox.x_min;
    let y_top = pbox.y_max;
    let width = pbox.x_max - pbox.x_min;
    let height = pbox.y_max - pbox.y_min;

    let ppem_overflow = if let Some((x_ppem, y_ppem)) = ppem {
        width > 10 * i64::from(x_ppem) || height > 10 * i64::from(y_ppem)
    } else {
        false
    };
    if !(0..0x10000).contains(&width)
        || !(0..0x10000).contains(&height)
        || pbox.x_min < -0x1000000
        || pbox.x_max >= 0x1000000
        || pbox.y_min < -0x1000000
        || pbox.y_max >= 0x1000000
        || ppem_overflow
    {
        return Err(CoreError::new(
            "bad_geometry",
            "glyph bitmap raster overflow",
        ));
    }

    Ok((x_left as i32, y_top as i32, width as usize, height as usize))
}

/// Render a glyph outline matching FreeType's `ft_smooth_render` (`FT_RENDER_MODE_NORMAL`).
pub fn render_glyph_bitmap(
    outline: &Outline,
    ppem: Option<(u16, u16)>,
) -> Result<GlyphBitmap, CoreError> {
    let (left, top, width, height) = preset_bitmap(outline, ppem)?;
    if width == 0 || height == 0 || outline.points.is_empty() {
        return Ok(GlyphBitmap {
            left,
            top,
            width,
            height,
            buffer: Vec::new(),
        });
    }

    let (_, _, pixels) = checked_mask_dims(width as u64, height as u64)?;
    let mut buffer = try_vec_u8(pixels, 0)?;

    let x_shift = 64 * -i64::from(left);
    let y_shift = 64 * (-i64::from(top) + height as i64);
    let overlap = (outline.flags & FT_OUTLINE_OVERLAP) != 0;
    let shifted_points: Vec<FtVector> = if overlap {
        if width * (OVERLAP_SCALE as usize) > 0x7FFF {
            return Err(CoreError::new("bad_geometry", "overlap glyph too wide"));
        }
        outline
            .points
            .iter()
            .map(|p| FtVector {
                x: (p.x.wrapping_add(x_shift)).wrapping_mul(i64::from(OVERLAP_SCALE)),
                y: (p.y.wrapping_add(y_shift)).wrapping_mul(i64::from(OVERLAP_SCALE)),
            })
            .collect()
    } else {
        outline
            .points
            .iter()
            .map(|p| FtVector {
                x: p.x.wrapping_add(x_shift),
                y: p.y.wrapping_add(y_shift),
            })
            .collect()
    };

    let shifted = Outline {
        points: shifted_points,
        tags: outline.tags.clone(),
        contours: outline.contours.clone(),
        flags: outline.flags,
    };

    let mut worker = GrayWorker::new();
    let clip = if overlap {
        ClipBox {
            x_min: 0,
            y_min: 0,
            x_max: (width as i32) * OVERLAP_SCALE,
            y_max: (height as i32) * OVERLAP_SCALE,
        }
    } else {
        ClipBox {
            x_min: 0,
            y_min: 0,
            x_max: width as i32,
            y_max: height as i32,
        }
    };
    let mut target = GrayTarget {
        clip,
        overlap,
        width,
        height,
        dst: &mut buffer,
    };
    worker.convert(&shifted, &mut target, decompose)?;

    Ok(GlyphBitmap {
        left,
        top,
        width,
        height,
        buffer,
    })
}

/// Render a raw 26.6 outline matching FreeType `FT_Outline_Render` (`cbox` <= `0x1000000`).
pub fn render_outline_gray(
    outline: &Outline,
    width: usize,
    height: usize,
) -> Result<Vec<u8>, CoreError> {
    validate_outline(outline)?;
    let cbox = ft_outline_get_cbox(&outline.points);
    if cbox.x_min < -0x1000000
        || cbox.y_min < -0x1000000
        || cbox.x_max > 0x1000000
        || cbox.y_max > 0x1000000
    {
        return Err(CoreError::new("value_error", "outline cbox out of range"));
    }
    if outline.points.is_empty() || width == 0 || height == 0 {
        let total = u64::try_from(width)
            .and_then(|w| u64::try_from(height).map(|h| w.saturating_mul(h)))
            .unwrap_or(u64::MAX);
        return if total == 0 {
            Ok(Vec::new())
        } else {
            try_vec_u8(total, 0)
        };
    }
    let (_, _, pixels) = checked_mask_dims(width as u64, height as u64)?;
    let w_i32 = i32::try_from(width).map_err(|_| CoreError::new("bad_geometry", "too wide"))?;
    let h_i32 = i32::try_from(height).map_err(|_| CoreError::new("bad_geometry", "too tall"))?;
    let mut buffer = try_vec_u8(pixels, 0)?;

    let mut worker = GrayWorker::new();
    let overlap = (outline.flags & FT_OUTLINE_OVERLAP) != 0;
    if overlap {
        if width * (OVERLAP_SCALE as usize) > 0x7FFF {
            return Err(CoreError::new("bad_geometry", "overlap glyph too wide"));
        }
        let inflated = Outline {
            points: outline
                .points
                .iter()
                .map(|p| FtVector {
                    x: p.x.wrapping_mul(i64::from(OVERLAP_SCALE)),
                    y: p.y.wrapping_mul(i64::from(OVERLAP_SCALE)),
                })
                .collect(),
            tags: outline.tags.clone(),
            contours: outline.contours.clone(),
            flags: outline.flags,
        };
        let mut target = GrayTarget {
            clip: ClipBox {
                x_min: 0,
                y_min: 0,
                x_max: w_i32 * OVERLAP_SCALE,
                y_max: h_i32 * OVERLAP_SCALE,
            },
            overlap: true,
            width,
            height,
            dst: &mut buffer,
        };
        worker.convert(&inflated, &mut target, decompose)?;
    } else {
        let mut target = GrayTarget {
            clip: ClipBox {
                x_min: 0,
                y_min: 0,
                x_max: w_i32,
                y_max: h_i32,
            },
            overlap: false,
            width,
            height,
            dst: &mut buffer,
        };
        worker.convert(outline, &mut target, decompose)?;
    }
    Ok(buffer)
}

fn decompose(worker: &mut GrayWorker, outline: &Outline) -> Result<(), CoreError> {
    let mut first = 0_usize;
    for &contour_end in &outline.contours {
        let last = usize::from(contour_end);
        if last < first || last >= outline.points.len() {
            return Err(CoreError::new("value_error", "invalid contour range"));
        }

        let mut limit = last;
        let mut v_start = outline.points[first];
        let v_last = outline.points[last];
        let first_tag = outline.tags[first] & 3;
        if first_tag == FT_CURVE_TAG_CUBIC {
            return Err(CoreError::new("value_error", "contour starts with cubic"));
        }

        let mut idx: isize = first as isize;
        if first_tag == FT_CURVE_TAG_CONIC {
            if (outline.tags[last] & 3) == FT_CURVE_TAG_ON {
                v_start = v_last;
                limit -= 1;
            } else {
                v_start.x = (v_start.x + v_last.x) / 2;
                v_start.y = (v_start.y + v_last.y) / 2;
            }
            idx -= 1;
        }

        if worker.move_to(v_start) {
            return Ok(());
        }

        let mut closed = false;
        while idx < (limit as isize) {
            idx += 1;
            let u = idx as usize;
            let tag = outline.tags[u] & 3;
            match tag {
                FT_CURVE_TAG_ON => {
                    if worker.line_to(outline.points[u]) {
                        return Ok(());
                    }
                }
                FT_CURVE_TAG_CONIC => {
                    let mut v_control = outline.points[u];
                    loop {
                        if idx < (limit as isize) {
                            idx += 1;
                            let u2 = idx as usize;
                            let tag2 = outline.tags[u2] & 3;
                            let vec = outline.points[u2];
                            if tag2 == FT_CURVE_TAG_ON {
                                if worker.conic_to(v_control, vec) {
                                    return Ok(());
                                }
                                break;
                            }
                            if tag2 != FT_CURVE_TAG_CONIC {
                                return Err(CoreError::new("value_error", "invalid conic tag"));
                            }
                            let v_middle = FtVector {
                                x: (v_control.x + vec.x) / 2,
                                y: (v_control.y + vec.y) / 2,
                            };
                            if worker.conic_to(v_control, v_middle) {
                                return Ok(());
                            }
                            v_control = vec;
                        } else {
                            if worker.conic_to(v_control, v_start) {
                                return Ok(());
                            }
                            closed = true;
                            break;
                        }
                    }
                }
                _ => {
                    let u1 = (idx + 1) as usize;
                    if u1 > limit || (outline.tags[u1] & 3) != FT_CURVE_TAG_CUBIC {
                        return Err(CoreError::new("value_error", "unpaired cubic point"));
                    }
                    idx += 2;
                    let vec1 = outline.points[u];
                    let vec2 = outline.points[u1];
                    if idx <= (limit as isize) {
                        let vec = outline.points[idx as usize];
                        if worker.cubic_to(vec1, vec2, vec) {
                            return Ok(());
                        }
                    } else {
                        if worker.cubic_to(vec1, vec2, v_start) {
                            return Ok(());
                        }
                        closed = true;
                        break;
                    }
                }
            }
        }

        if !closed && worker.line_to(v_start) {
            return Ok(());
        }
        first = last + 1;
    }
    Ok(())
}
