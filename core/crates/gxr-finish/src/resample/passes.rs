//! Horizontal, vertical, and RGBA premultiply/unpremultiply passes.
//!
//! Port of Pillow 12.2.0 `src/libImaging/Resample.c`, `src/libImaging/Convert.c`,
//! and `src/libImaging/ImagingUtils.h`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big, try_vec_u8};

use super::filters::PRECISION_BITS;

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:177 clip8`.
#[inline]
#[must_use]
pub fn clip8(v: i64) -> u8 {
    (v >> PRECISION_BITS).clamp(0, 255) as u8
}

/// Port of Pillow 12.2.0 `src/libImaging/ImagingUtils.h:17-20 MULDIV255`.
#[inline]
#[must_use]
pub fn mul_div_255(a: u8, b: u8) -> u8 {
    let tmp = u32::from(a) * u32::from(b) + 128;
    (((tmp >> 8) + tmp) >> 8) as u8
}

/// Port of Pillow 12.2.0 `src/libImaging/Convert.c:435 rgbA2rgba` (`RGBA -> RGBa` premultiply).
pub fn rgba_to_premul(rgba: &[u8]) -> Result<Vec<u8>, CoreError> {
    let len = u64::try_from(rgba.len()).map_err(|_| too_big())?;
    let _ = fit_count(len, 1)?;
    let mut out = try_vec_u8(len, 0)?;
    for (src, dst) in rgba.chunks_exact(4).zip(out.chunks_exact_mut(4)) {
        let alpha = src[3];
        dst[0] = mul_div_255(src[0], alpha);
        dst[1] = mul_div_255(src[1], alpha);
        dst[2] = mul_div_255(src[2], alpha);
        dst[3] = alpha;
    }
    Ok(out)
}

/// Port of Pillow 12.2.0 `src/libImaging/Convert.c:450 rgba2rgbA` (`RGBa -> RGBA` unpremultiply).
pub fn premul_to_rgba(premul: &[u8]) -> Result<Vec<u8>, CoreError> {
    let len = u64::try_from(premul.len()).map_err(|_| too_big())?;
    let _ = fit_count(len, 1)?;
    let mut out = try_vec_u8(len, 0)?;
    for (src, dst) in premul.chunks_exact(4).zip(out.chunks_exact_mut(4)) {
        let alpha = src[3];
        if alpha == 255 || alpha == 0 {
            dst[0] = src[0];
            dst[1] = src[1];
            dst[2] = src[2];
        } else {
            let a = u32::from(alpha);
            dst[0] = ((255 * u32::from(src[0])) / a).min(255) as u8;
            dst[1] = ((255 * u32::from(src[1])) / a).min(255) as u8;
            dst[2] = ((255 * u32::from(src[2])) / a).min(255) as u8;
        }
        dst[3] = alpha;
    }
    Ok(out)
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:287 ImagingResampleHorizontal_8bpc`.
#[allow(clippy::too_many_arguments)]
pub fn resample_horizontal_8bpc(
    src: &[u8],
    in_w: usize,
    out_w: usize,
    out_h: usize,
    channels: usize,
    offset: usize,
    ksize: usize,
    bounds: &[u32],
    kk: &[i32],
) -> Result<Vec<u8>, CoreError> {
    let out_bytes = checked_mul_u64(
        checked_mul_u64(
            u64::try_from(out_w).map_err(|_| too_big())?,
            u64::try_from(out_h).map_err(|_| too_big())?,
        )?,
        u64::try_from(channels).map_err(|_| too_big())?,
    )?;
    let mut dst = try_vec_u8(out_bytes, 0)?;
    let half: i64 = 1 << (PRECISION_BITS - 1);

    match channels {
        1 => {
            for yy in 0..out_h {
                let src_row = &src[(yy + offset) * in_w..][..in_w];
                let dst_row = &mut dst[yy * out_w..][..out_w];
                for xx in 0..out_w {
                    let xmin = bounds[xx * 2] as usize;
                    let xmax = bounds[xx * 2 + 1] as usize;
                    let k = &kk[xx * ksize..][..xmax];
                    let mut ss0 = half;
                    for x in 0..xmax {
                        ss0 += i64::from(src_row[x + xmin]) * i64::from(k[x]);
                    }
                    dst_row[xx] = clip8(ss0);
                }
            }
        }
        3 => {
            let in_stride = in_w * 3;
            let out_stride = out_w * 3;
            for yy in 0..out_h {
                let src_row = &src[(yy + offset) * in_stride..][..in_stride];
                let dst_row = &mut dst[yy * out_stride..][..out_stride];
                for xx in 0..out_w {
                    let xmin = bounds[xx * 2] as usize;
                    let xmax = bounds[xx * 2 + 1] as usize;
                    let k = &kk[xx * ksize..][..xmax];
                    let mut ss0 = half;
                    let mut ss1 = half;
                    let mut ss2 = half;
                    for (x, &coeff_i32) in k.iter().enumerate() {
                        let base = (x + xmin) * 3;
                        let coeff = i64::from(coeff_i32);
                        ss0 += i64::from(src_row[base]) * coeff;
                        ss1 += i64::from(src_row[base + 1]) * coeff;
                        ss2 += i64::from(src_row[base + 2]) * coeff;
                    }
                    let out_base = xx * 3;
                    dst_row[out_base] = clip8(ss0);
                    dst_row[out_base + 1] = clip8(ss1);
                    dst_row[out_base + 2] = clip8(ss2);
                }
            }
        }
        4 => {
            let in_stride = in_w * 4;
            let out_stride = out_w * 4;
            for yy in 0..out_h {
                let src_row = &src[(yy + offset) * in_stride..][..in_stride];
                let dst_row = &mut dst[yy * out_stride..][..out_stride];
                for xx in 0..out_w {
                    let xmin = bounds[xx * 2] as usize;
                    let xmax = bounds[xx * 2 + 1] as usize;
                    let k = &kk[xx * ksize..][..xmax];
                    let mut ss0 = half;
                    let mut ss1 = half;
                    let mut ss2 = half;
                    let mut ss3 = half;
                    for (x, &coeff_i32) in k.iter().enumerate() {
                        let base = (x + xmin) * 4;
                        let coeff = i64::from(coeff_i32);
                        ss0 += i64::from(src_row[base]) * coeff;
                        ss1 += i64::from(src_row[base + 1]) * coeff;
                        ss2 += i64::from(src_row[base + 2]) * coeff;
                        ss3 += i64::from(src_row[base + 3]) * coeff;
                    }
                    let out_base = xx * 4;
                    dst_row[out_base] = clip8(ss0);
                    dst_row[out_base + 1] = clip8(ss1);
                    dst_row[out_base + 2] = clip8(ss2);
                    dst_row[out_base + 3] = clip8(ss3);
                }
            }
        }
        _ => return Err(CoreError::new("value_error", "unsupported channel count")),
    }
    Ok(dst)
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:380 ImagingResampleVertical_8bpc`.
#[allow(clippy::too_many_arguments)]
pub fn resample_vertical_8bpc(
    src: &[u8],
    in_w: usize,
    out_w: usize,
    out_h: usize,
    channels: usize,
    ksize: usize,
    bounds: &[u32],
    kk: &[i32],
) -> Result<Vec<u8>, CoreError> {
    let out_bytes = checked_mul_u64(
        checked_mul_u64(
            u64::try_from(out_w).map_err(|_| too_big())?,
            u64::try_from(out_h).map_err(|_| too_big())?,
        )?,
        u64::try_from(channels).map_err(|_| too_big())?,
    )?;
    let mut dst = try_vec_u8(out_bytes, 0)?;
    let half: i64 = 1 << (PRECISION_BITS - 1);

    match channels {
        1 => {
            for yy in 0..out_h {
                let ymin = bounds[yy * 2] as usize;
                let ymax = bounds[yy * 2 + 1] as usize;
                let k = &kk[yy * ksize..][..ymax];
                let dst_row = &mut dst[yy * out_w..][..out_w];
                for (xx, out_px) in dst_row.iter_mut().enumerate() {
                    let mut ss0 = half;
                    for (y, &coeff_i32) in k.iter().enumerate() {
                        ss0 += i64::from(src[(y + ymin) * in_w + xx]) * i64::from(coeff_i32);
                    }
                    *out_px = clip8(ss0);
                }
            }
        }
        3 => {
            let in_stride = in_w * 3;
            let out_stride = out_w * 3;
            for yy in 0..out_h {
                let ymin = bounds[yy * 2] as usize;
                let ymax = bounds[yy * 2 + 1] as usize;
                let k = &kk[yy * ksize..][..ymax];
                let dst_row = &mut dst[yy * out_stride..][..out_stride];
                for xx in 0..out_w {
                    let mut ss0 = half;
                    let mut ss1 = half;
                    let mut ss2 = half;
                    for (y, &coeff_i32) in k.iter().enumerate() {
                        let base = (y + ymin) * in_stride + xx * 3;
                        let coeff = i64::from(coeff_i32);
                        ss0 += i64::from(src[base]) * coeff;
                        ss1 += i64::from(src[base + 1]) * coeff;
                        ss2 += i64::from(src[base + 2]) * coeff;
                    }
                    let out_base = xx * 3;
                    dst_row[out_base] = clip8(ss0);
                    dst_row[out_base + 1] = clip8(ss1);
                    dst_row[out_base + 2] = clip8(ss2);
                }
            }
        }
        4 => {
            let in_stride = in_w * 4;
            let out_stride = out_w * 4;
            for yy in 0..out_h {
                let ymin = bounds[yy * 2] as usize;
                let ymax = bounds[yy * 2 + 1] as usize;
                let k = &kk[yy * ksize..][..ymax];
                let dst_row = &mut dst[yy * out_stride..][..out_stride];
                for xx in 0..out_w {
                    let mut ss0 = half;
                    let mut ss1 = half;
                    let mut ss2 = half;
                    let mut ss3 = half;
                    for (y, &coeff_i32) in k.iter().enumerate() {
                        let base = (y + ymin) * in_stride + xx * 4;
                        let coeff = i64::from(coeff_i32);
                        ss0 += i64::from(src[base]) * coeff;
                        ss1 += i64::from(src[base + 1]) * coeff;
                        ss2 += i64::from(src[base + 2]) * coeff;
                        ss3 += i64::from(src[base + 3]) * coeff;
                    }
                    let out_base = xx * 4;
                    dst_row[out_base] = clip8(ss0);
                    dst_row[out_base + 1] = clip8(ss1);
                    dst_row[out_base + 2] = clip8(ss2);
                    dst_row[out_base + 3] = clip8(ss3);
                }
            }
        }
        _ => return Err(CoreError::new("value_error", "unsupported channel count")),
    }
    Ok(dst)
}

/// Fallibly allocate a `Vec<f32>` of length `n` filled with `0.0_f32`.
pub fn try_vec_f32(n: u64) -> Result<Vec<f32>, CoreError> {
    let len = fit_count(n, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| too_big())?;
    out.resize(len, 0.0_f32);
    Ok(out)
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:568 ImagingResampleHorizontal_32bpc` (`IMAGING_TYPE_FLOAT32`).
#[allow(clippy::too_many_arguments)]
pub fn resample_horizontal_32bpc(
    src: &[f32],
    in_w: usize,
    out_w: usize,
    out_h: usize,
    offset: usize,
    ksize: usize,
    bounds: &[u32],
    kk: &[f64],
) -> Result<Vec<f32>, CoreError> {
    let out_elems = checked_mul_u64(
        u64::try_from(out_w).map_err(|_| too_big())?,
        u64::try_from(out_h).map_err(|_| too_big())?,
    )?;
    let mut dst = try_vec_f32(out_elems)?;

    for yy in 0..out_h {
        let src_row = &src[(yy + offset) * in_w..][..in_w];
        let dst_row = &mut dst[yy * out_w..][..out_w];
        for xx in 0..out_w {
            let xmin = bounds[xx * 2] as usize;
            let xmax = bounds[xx * 2 + 1] as usize;
            let k = &kk[xx * ksize..][..xmax];
            let mut ss = 0.0_f64;
            for x in 0..xmax {
                ss += f64::from(src_row[x + xmin]) * k[x];
            }
            dst_row[xx] = ss as f32;
        }
    }
    Ok(dst)
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:612 ImagingResampleVertical_32bpc` (`IMAGING_TYPE_FLOAT32`).
pub fn resample_vertical_32bpc(
    src: &[f32],
    in_w: usize,
    out_w: usize,
    out_h: usize,
    ksize: usize,
    bounds: &[u32],
    kk: &[f64],
) -> Result<Vec<f32>, CoreError> {
    let out_elems = checked_mul_u64(
        u64::try_from(out_w).map_err(|_| too_big())?,
        u64::try_from(out_h).map_err(|_| too_big())?,
    )?;
    let mut dst = try_vec_f32(out_elems)?;

    for yy in 0..out_h {
        let ymin = bounds[yy * 2] as usize;
        let ymax = bounds[yy * 2 + 1] as usize;
        let k = &kk[yy * ksize..][..ymax];
        let dst_row = &mut dst[yy * out_w..][..out_w];
        for (xx, out_px) in dst_row.iter_mut().enumerate() {
            let mut ss = 0.0_f64;
            for y in 0..ymax {
                ss += f64::from(src[(y + ymin) * in_w + xx]) * k[y];
            }
            *out_px = ss as f32;
        }
    }
    Ok(dst)
}
