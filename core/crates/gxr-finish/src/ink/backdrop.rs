//! Port of `frame_texture.py` (`blurred_background`, `paper_grain`) and `frame_paint.py:background`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big};
use crate::ink::blur::gaussian_blur;
use crate::ink::draw::bad_coords;
use crate::ink::image::{InkImage, InkMode, bad_mismatch};
use crate::ink::rng::Pcg64;
use crate::resample::{Filter, Mode, resample_u8_u64};

/// Port of `frame_texture.py:blurred_background`.
pub fn blurred_background(
    photo: &InkImage,
    width: u64,
    height: u64,
    sigma: f64,
    sat: f64,
    veil: f64,
) -> Result<InkImage, CoreError> {
    if photo.mode != InkMode::Rgb {
        return Err(bad_mismatch("blurred_background requires RGB photo"));
    }
    if photo.width == 0 || photo.height == 0 || width == 0 || height == 0 {
        return Err(bad_coords("blurred_background dimensions must be positive"));
    }
    if !sigma.is_finite() || sigma < 0.0_f64 || !sat.is_finite() || !veil.is_finite() {
        return Err(bad_coords("blurred_background parameters must be finite"));
    }
    let _ = fit_count(width, 1)?;
    let _ = fit_count(height, 1)?;
    let out_bytes_u64 = checked_mul_u64(checked_mul_u64(width, height)?, 3)?;
    let out_bytes = fit_count(out_bytes_u64, 1)?;

    let scale = (width as f64 / photo.width as f64).max(height as f64 / photo.height as f64);
    let bw_f = ((photo.width as f64) * scale).round_ties_even();
    let bh_f = ((photo.height as f64) * scale).round_ties_even();
    if !bw_f.is_finite()
        || !bh_f.is_finite()
        || bw_f > (u64::MAX as f64)
        || bh_f > (u64::MAX as f64)
    {
        return Err(too_big());
    }
    let bw = width.max(bw_f.max(0.0) as u64);
    let bh = height.max(bh_f.max(0.0) as u64);
    let _ = fit_count(bw, 1)?;
    let _ = fit_count(bh, 1)?;
    let _ = fit_count(checked_mul_u64(bw, bh)?, 3)?;

    let sw = (bw / 8).max(8);
    let sh = (bh / 8).max(8);
    let small_bytes = resample_u8_u64(
        &photo.data,
        photo.width as u64,
        photo.height as u64,
        Mode::Rgb,
        Filter::Bilinear,
        sw,
        sh,
    )?;
    let small = InkImage::from_bytes(InkMode::Rgb, sw, sh, &small_bytes)?;
    drop(small_bytes);

    let blur_r = (sigma / 8.0_f64).max(2.0_f64);
    let blurred = gaussian_blur(&small, blur_r, blur_r)?;
    drop(small);

    let big = resample_u8_u64(&blurred.data, sw, sh, Mode::Rgb, Filter::Bicubic, bw, bh)?;
    drop(blurred);

    let mut out = Vec::new();
    out.try_reserve_exact(out_bytes).map_err(|_| too_big())?;
    out.resize(out_bytes, 0);

    let w_usize = fit_count(width, 1)?;
    let h_usize = fit_count(height, 1)?;
    let bw_usize = fit_count(bw, 1)?;
    let left = fit_count((bw - width) / 2, 1)?;
    let top = fit_count((bh - height) / 2, 1)?;
    let sat_f = sat as f32;
    let veil_f = veil as f32;

    for y in 0..h_usize {
        let src_row = (top + y) * bw_usize * 3 + left * 3;
        let dst_row = y * w_usize * 3;
        for x in 0..w_usize {
            let s = src_row + x * 3;
            let d = dst_row + x * 3;
            let r = f32::from(big[s]);
            let g = f32::from(big[s + 1]);
            let b = f32::from(big[s + 2]);
            let luma = (r * 0.299_f32 + g * 0.587_f32) + b * 0.114_f32;
            out[d] = ((luma + (r - luma) * sat_f) * veil_f).clamp(0.0_f32, 255.0_f32) as u8;
            out[d + 1] = ((luma + (g - luma) * sat_f) * veil_f).clamp(0.0_f32, 255.0_f32) as u8;
            out[d + 2] = ((luma + (b - luma) * sat_f) * veil_f).clamp(0.0_f32, 255.0_f32) as u8;
        }
    }

    Ok(InkImage {
        mode: InkMode::Rgb,
        width: w_usize,
        height: h_usize,
        data: out,
    })
}

/// Port of `frame_texture.py:paper_grain`, mutating `canvas` in place without allocating a
/// second RGB output buffer. Holds at most one `(h, w)` `f32` plane (`fine`) and one `(h, w)`
/// `u8` plane (`low_big`).
pub fn paper_grain_in_place(canvas: &mut InkImage, amount: f64) -> Result<(), CoreError> {
    if canvas.mode != InkMode::Rgb {
        return Err(bad_mismatch("paper_grain requires RGB canvas"));
    }
    if canvas.width == 0 || canvas.height == 0 {
        return Err(bad_coords("paper_grain dimensions must be positive"));
    }
    if !amount.is_finite() || amount < 0.0_f64 {
        return Err(bad_coords("paper_grain amount must be non-negative finite"));
    }
    let w = canvas.width as u64;
    let h = canvas.height as u64;
    let pixels = checked_mul_u64(w, h)?;
    let n_px = fit_count(pixels, 4)?;
    let expected_bytes = fit_count(checked_mul_u64(pixels, 3)?, 1)?;
    if canvas.data.len() != expected_bytes {
        return Err(bad_mismatch(
            "canvas length does not match image dimensions",
        ));
    }

    let mut rng = Pcg64::from_seed(42);
    let scale = 255.0_f64 * amount;
    let mut fine = Vec::new();
    fine.try_reserve_exact(n_px).map_err(|_| too_big())?;
    for _ in 0..n_px {
        fine.push(rng.normal(0.0_f64, scale) as f32);
    }

    let lw = (w / 6).max(4);
    let lh = (h / 6).max(4);
    let low_n = fit_count(checked_mul_u64(lw, lh)?, 1)?;
    let mut low_u8 = Vec::new();
    low_u8.try_reserve_exact(low_n).map_err(|_| too_big())?;
    for _ in 0..low_n {
        low_u8.push(rng.normal(128.0_f64, 40.0_f64).clamp(0.0_f64, 255.0_f64) as u8);
    }
    let low_big = resample_u8_u64(&low_u8, lw, lh, Mode::L, Filter::Bicubic, w, h)?;
    drop(low_u8);

    let amt_f = amount as f32;
    for i in 0..n_px {
        let f_i = fine[i];
        let low_f = (f32::from(low_big[i]) - 128.0_f32) * amt_f * 0.8_f32;
        let base = i * 3;
        for c in 0..3 {
            let s = f32::from(canvas.data[base + c]) + f_i + low_f;
            canvas.data[base + c] = s.clamp(0.0_f32, 255.0_f32) as u8;
        }
    }

    Ok(())
}

/// Port of `frame_texture.py:paper_grain`. Validates upfront before cloning the canvas.
pub fn paper_grain(canvas: &InkImage, amount: f64) -> Result<InkImage, CoreError> {
    if canvas.mode != InkMode::Rgb {
        return Err(bad_mismatch("paper_grain requires RGB canvas"));
    }
    if canvas.width == 0 || canvas.height == 0 {
        return Err(bad_coords("paper_grain dimensions must be positive"));
    }
    if !amount.is_finite() || amount < 0.0_f64 {
        return Err(bad_coords("paper_grain amount must be non-negative finite"));
    }
    let pixels = checked_mul_u64(canvas.width as u64, canvas.height as u64)?;
    let _ = fit_count(pixels, 4)?;
    let expected_bytes = fit_count(checked_mul_u64(pixels, 3)?, 1)?;
    if canvas.data.len() != expected_bytes {
        return Err(bad_mismatch(
            "canvas length does not match image dimensions",
        ));
    }
    let mut out = InkImage::from_bytes(
        InkMode::Rgb,
        canvas.width as u64,
        canvas.height as u64,
        &canvas.data,
    )?;
    paper_grain_in_place(&mut out, amount)?;
    Ok(out)
}

/// Port of `frame_paint.py:background` (solid or glow), delegating to `crate::raster::background`.
pub fn background(
    width: u64,
    height: u64,
    bg: [u8; 3],
    glow_falloff: Option<f64>,
) -> Result<InkImage, CoreError> {
    if let Some(falloff) = glow_falloff
        && !falloff.is_finite()
    {
        return Err(bad_coords("glow falloff must be finite"));
    }
    if width == 0 || height == 0 {
        return InkImage::new_filled(InkMode::Rgb, width, height, [bg[0], bg[1], bg[2], 255]);
    }
    let data = crate::raster::background(width, height, bg, glow_falloff)?;
    Ok(InkImage {
        mode: InkMode::Rgb,
        width: width as usize,
        height: height as usize,
        data,
    })
}
