//! Checked RGB/RGBA dimension and slice bounds helpers for `raster`, built on
//! `crate::geometry::bounds`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{bad_geometry, checked_mask_dims, checked_mul_u64, fit_count};

/// Check RGB dimensions `(w, h)` for allocation and return `(wu, hu, pixels, w*h*3)`.
pub fn checked_rgb_dims(w: u64, h: u64) -> Result<(usize, usize, u64, u64), CoreError> {
    let (wu, hu, pixels) = checked_mask_dims(w, h)?;
    let rgb_len = checked_mul_u64(pixels, 3)?;
    let _ = fit_count(rgb_len, 1)?;
    Ok((wu, hu, pixels, rgb_len))
}

/// Check RGBA dimensions and verify `rgba.len() == w * h * 4`.
pub fn checked_rgba_slice(
    rgba: &[u8],
    w: u64,
    h: u64,
) -> Result<(usize, usize, u64, u64), CoreError> {
    let (wu, hu, pixels) = checked_mask_dims(w, h)?;
    let rgba_len = checked_mul_u64(pixels, 4)?;
    let expected = fit_count(rgba_len, 1)?;
    if rgba.len() != expected {
        return Err(bad_geometry());
    }
    Ok((wu, hu, pixels, rgba_len))
}
