//! CFF outline construction and 16.16 -> 26.6 scaling for [`FontFace`].

use gxr_frame::{Cff, CffSeg, Fixed};
use gxr_raw::CoreError;

use super::ftcalc::{FtVector, ft_mul_fix, ft_pix_round};
use super::glyf::{FT_CURVE_TAG_CUBIC, FT_CURVE_TAG_ON, LoadedGlyph, Outline};

const MAX_GLYPH_POINTS: usize = 65_535;
const MAX_GLYPH_CONTOURS: usize = 16_384;

#[inline]
fn scale_fixed_to_26_6(v: Fixed, scale: i64) -> i64 {
    let neg = (v < 0) ^ (scale < 0);
    let mag =
        (i128::from(v.unsigned_abs()) * i128::from(scale.unsigned_abs()) + (1_i128 << 31)) >> 32;
    let mag_i64 = mag as i64;
    if neg { -mag_i64 } else { mag_i64 }
}

fn close_contour(outline: &mut Outline, contour_start: usize) -> Result<(), CoreError> {
    let len = outline.points.len();
    if len <= contour_start {
        return Ok(());
    }
    if len - contour_start > 1
        && outline.points[len - 1] == outline.points[contour_start]
        && outline.tags[len - 1] == FT_CURVE_TAG_ON
        && outline.tags[contour_start] == FT_CURVE_TAG_ON
    {
        outline.points.pop();
        outline.tags.pop();
    }
    if outline.contours.len() >= MAX_GLYPH_CONTOURS {
        return Err(CoreError::new("value_error", "too many CFF contours"));
    }
    let end_idx = u16::try_from(outline.points.len() - 1)
        .map_err(|_| CoreError::new("value_error", "too many CFF points"))?;
    outline.contours.push(end_idx);
    Ok(())
}

fn push_point(outline: &mut Outline, x: i64, y: i64, tag: u8) -> Result<(), CoreError> {
    if outline.points.len() >= MAX_GLYPH_POINTS {
        return Err(CoreError::new("value_error", "too many CFF points"));
    }
    outline.points.push(FtVector { x, y });
    outline.tags.push(tag);
    Ok(())
}

pub(crate) fn load_cff_glyph(
    cff: &Cff,
    gid: u32,
    advance: u16,
    x_scale: i64,
    y_scale: i64,
) -> Result<LoadedGlyph, CoreError> {
    let gid_usize =
        usize::try_from(gid).map_err(|_| CoreError::new("value_error", "invalid glyph index"))?;
    let segs = cff.segments(gid_usize)?;
    let mut outline = Outline::default();
    let mut contour_start = 0_usize;

    for seg in segs {
        match seg {
            CffSeg::Move(x, y) => {
                close_contour(&mut outline, contour_start)?;
                contour_start = outline.points.len();
                push_point(
                    &mut outline,
                    scale_fixed_to_26_6(x, x_scale),
                    scale_fixed_to_26_6(y, y_scale),
                    FT_CURVE_TAG_ON,
                )?;
            }
            CffSeg::Line(x, y) => {
                push_point(
                    &mut outline,
                    scale_fixed_to_26_6(x, x_scale),
                    scale_fixed_to_26_6(y, y_scale),
                    FT_CURVE_TAG_ON,
                )?;
            }
            CffSeg::Curve([x1, y1, x2, y2, x3, y3]) => {
                push_point(
                    &mut outline,
                    scale_fixed_to_26_6(x1, x_scale),
                    scale_fixed_to_26_6(y1, y_scale),
                    FT_CURVE_TAG_CUBIC,
                )?;
                push_point(
                    &mut outline,
                    scale_fixed_to_26_6(x2, x_scale),
                    scale_fixed_to_26_6(y2, y_scale),
                    FT_CURVE_TAG_CUBIC,
                )?;
                push_point(
                    &mut outline,
                    scale_fixed_to_26_6(x3, x_scale),
                    scale_fixed_to_26_6(y3, y_scale),
                    FT_CURVE_TAG_ON,
                )?;
            }
        }
    }
    close_contour(&mut outline, contour_start)?;

    let linear_hori_advance = i64::from(advance);
    let raw_hori_advance = ft_mul_fix(linear_hori_advance, x_scale);
    Ok(LoadedGlyph {
        outline,
        hori_advance: ft_pix_round(raw_hori_advance),
        raw_hori_advance,
        linear_hori_advance,
        hinted: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scales_16_16_to_26_6_with_symmetric_rounding() {
        let scale = 65536_i64; // 1.0 in 16.16
        assert_eq!(scale_fixed_to_26_6(100 << 16, scale), 100);
        assert_eq!(scale_fixed_to_26_6(-(100 << 16), scale), -100);
        assert_eq!(scale_fixed_to_26_6(1 << 15, scale), 1);
        assert_eq!(scale_fixed_to_26_6(-(1 << 15), scale), -1);
    }
}
