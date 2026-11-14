//! Pillow 12.2.0 image resampler (`src/libImaging/Resample.c`, `Convert.c`, `src/PIL/Image.py`).
//!
//! Supports `L` (1ch), `RGB` (3ch), `RGBA` (4ch via premultiplied `RGBa`), and `F` (1ch `f32`),
//! using `gxr_math::sin` for Lanczos and sequential IEEE `f64` arithmetic without FMA.

pub mod filters;
pub mod passes;

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big};

pub use filters::{Coeffs, Filter, PRECISION_BITS, normalize_coeffs_8bpc, precompute_coeffs};
use passes::{
    premul_to_rgba, resample_horizontal_8bpc, resample_horizontal_32bpc, resample_vertical_8bpc,
    resample_vertical_32bpc, rgba_to_premul,
};

/// 8-bit image channel layout matching Pillow's `L`, `RGB`, and `RGBA` modes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// 1-channel grayscale (`mode = "L"`).
    L,
    /// 3-channel RGB (`mode = "RGB"`).
    Rgb,
    /// 4-channel RGBA (`mode = "RGBA"`), resampled through premultiplied `RGBa`.
    Rgba,
}

impl Mode {
    /// Parse an uppercase Pillow mode name (`"L"`, `"RGB"`, `"RGBA"`).
    pub fn parse(name: &str) -> Result<Self, CoreError> {
        match name {
            "L" => Ok(Self::L),
            "RGB" => Ok(Self::Rgb),
            "RGBA" => Ok(Self::Rgba),
            _ => Err(CoreError::new("value_error", "unsupported resampling mode")),
        }
    }

    /// Number of interleaved `u8` channels per pixel.
    #[must_use]
    pub const fn channels(self) -> usize {
        match self {
            Self::L => 1,
            Self::Rgb => 3,
            Self::Rgba => 4,
        }
    }
}

fn bad_dims() -> CoreError {
    CoreError::new("value_error", "height and width must be > 0")
}

fn check_dims_and_len(
    data_len: usize,
    w: u64,
    h: u64,
    channels: usize,
    out_w: u64,
    out_h: u64,
    elem_size: u64,
) -> Result<(usize, usize, usize, usize), CoreError> {
    if w == 0 || h == 0 || out_w == 0 || out_h == 0 {
        return Err(bad_dims());
    }
    let _ = fit_count(w, 1)?;
    let _ = fit_count(h, 1)?;
    let _ = fit_count(out_w, 1)?;
    let _ = fit_count(out_h, 1)?;

    let ch = channels as u64;
    let in_pixels = checked_mul_u64(w, h)?;
    let in_elems = checked_mul_u64(in_pixels, ch)?;
    let expected_in_elems = fit_count(in_elems, elem_size)?;

    let out_pixels = checked_mul_u64(out_w, out_h)?;
    let out_elems = checked_mul_u64(out_pixels, ch)?;
    let _ = fit_count(out_elems, elem_size)?;

    if data_len != expected_in_elems {
        return Err(CoreError::new(
            "value_error",
            "input length does not match image dimensions",
        ));
    }

    Ok((
        usize::try_from(w).map_err(|_| too_big())?,
        usize::try_from(h).map_err(|_| too_big())?,
        usize::try_from(out_w).map_err(|_| too_big())?,
        usize::try_from(out_h).map_err(|_| too_big())?,
    ))
}

fn clone_u8_fallible(data: &[u8]) -> Result<Vec<u8>, CoreError> {
    let len = u64::try_from(data.len()).map_err(|_| too_big())?;
    let _ = fit_count(len, 1)?;
    let mut out = Vec::new();
    out.try_reserve_exact(data.len()).map_err(|_| too_big())?;
    out.extend_from_slice(data);
    Ok(out)
}

fn clone_f32_fallible(data: &[f32]) -> Result<Vec<f32>, CoreError> {
    let len = u64::try_from(data.len()).map_err(|_| too_big())?;
    let _ = fit_count(len, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(data.len()).map_err(|_| too_big())?;
    out.extend_from_slice(data);
    Ok(out)
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:708 ImagingResampleInner` for 8-bpc buffers.
fn resample_inner_u8(
    src: &[u8],
    in_w: usize,
    in_h: usize,
    out_w: usize,
    out_h: usize,
    channels: usize,
    filter: Filter,
) -> Result<Vec<u8>, CoreError> {
    let need_horizontal = out_w != in_w;
    let need_vertical = out_h != in_h;

    if !need_horizontal && !need_vertical {
        return clone_u8_fallible(src);
    }

    let c_horiz = if need_horizontal {
        Some(precompute_coeffs(
            in_w,
            0.0_f32,
            in_w as f32,
            out_w,
            filter,
        )?)
    } else {
        None
    };

    let mut c_vert = if need_vertical {
        Some(precompute_coeffs(
            in_h,
            0.0_f32,
            in_h as f32,
            out_h,
            filter,
        )?)
    } else {
        None
    };

    let (ybox_first, ybox_last) = if let Some(cv) = c_vert.as_ref() {
        let first = cv.bounds[0] as usize;
        let last = (cv.bounds[out_h * 2 - 2] + cv.bounds[out_h * 2 - 1]) as usize;
        (first, last)
    } else {
        (0_usize, in_h)
    };

    let temp_buf: Option<Vec<u8>> = if let Some(ch) = c_horiz {
        if let Some(cv) = c_vert.as_mut() {
            for i in 0..out_h {
                cv.bounds[i * 2] -= ybox_first as u32;
            }
        }
        let kk_h = normalize_coeffs_8bpc(out_w, ch.ksize, &ch.kk)?;
        let temp_h = ybox_last.saturating_sub(ybox_first);
        let temp = resample_horizontal_8bpc(
            src, in_w, out_w, temp_h, channels, ybox_first, ch.ksize, &ch.bounds, &kk_h,
        )?;
        Some(temp)
    } else {
        None
    };

    if let Some(cv) = c_vert {
        let kk_v = normalize_coeffs_8bpc(out_h, cv.ksize, &cv.kk)?;
        let v_in = temp_buf.as_deref().unwrap_or(src);
        let v_in_w = if temp_buf.is_some() { out_w } else { in_w };
        resample_vertical_8bpc(
            v_in, v_in_w, v_in_w, out_h, channels, cv.ksize, &cv.bounds, &kk_v,
        )
    } else {
        Ok(temp_buf.expect("need_horizontal or need_vertical must be true"))
    }
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:708 ImagingResampleInner` for 32-bpc `f32` (`mode = "F"`).
fn resample_inner_f32(
    src: &[f32],
    in_w: usize,
    in_h: usize,
    out_w: usize,
    out_h: usize,
    filter: Filter,
) -> Result<Vec<f32>, CoreError> {
    let need_horizontal = out_w != in_w;
    let need_vertical = out_h != in_h;

    if !need_horizontal && !need_vertical {
        return clone_f32_fallible(src);
    }

    let c_horiz = if need_horizontal {
        Some(precompute_coeffs(
            in_w,
            0.0_f32,
            in_w as f32,
            out_w,
            filter,
        )?)
    } else {
        None
    };

    let mut c_vert = if need_vertical {
        Some(precompute_coeffs(
            in_h,
            0.0_f32,
            in_h as f32,
            out_h,
            filter,
        )?)
    } else {
        None
    };

    let (ybox_first, ybox_last) = if let Some(cv) = c_vert.as_ref() {
        let first = cv.bounds[0] as usize;
        let last = (cv.bounds[out_h * 2 - 2] + cv.bounds[out_h * 2 - 1]) as usize;
        (first, last)
    } else {
        (0_usize, in_h)
    };

    let temp_buf: Option<Vec<f32>> = if let Some(ch) = c_horiz {
        if let Some(cv) = c_vert.as_mut() {
            for i in 0..out_h {
                cv.bounds[i * 2] -= ybox_first as u32;
            }
        }
        let temp_h = ybox_last.saturating_sub(ybox_first);
        let temp = resample_horizontal_32bpc(
            src, in_w, out_w, temp_h, ybox_first, ch.ksize, &ch.bounds, &ch.kk,
        )?;
        Some(temp)
    } else {
        None
    };

    if let Some(cv) = c_vert {
        let v_in = temp_buf.as_deref().unwrap_or(src);
        let v_in_w = if temp_buf.is_some() { out_w } else { in_w };
        resample_vertical_32bpc(v_in, v_in_w, v_in_w, out_h, cv.ksize, &cv.bounds, &cv.kk)
    } else {
        Ok(temp_buf.expect("need_horizontal or need_vertical must be true"))
    }
}

fn resample_pil_u8_core(
    src: &[u8],
    in_w: usize,
    in_h: usize,
    out_w: usize,
    out_h: usize,
    channels: usize,
    filter: Filter,
) -> Result<Vec<u8>, CoreError> {
    // Port of Pillow 12.2.0 `src/PIL/Image.py:2431-2437`: vertical pass first for tall-narrow downscales.
    if in_h > in_w.saturating_mul(100) && out_h < in_h {
        let vert = resample_inner_u8(src, in_w, in_h, in_w, out_h, channels, filter)?;
        resample_inner_u8(&vert, in_w, out_h, out_w, out_h, channels, filter)
    } else {
        resample_inner_u8(src, in_w, in_h, out_w, out_h, channels, filter)
    }
}

/// Port of Pillow 12.2.0 `Image.resize((out_w, out_h), filter)` for 8-bpc images (`L`, `RGB`, `RGBA`),
/// accepting `u64` dimensions so native and WASM enforce identical size limits.
pub fn resample_u8_u64(
    data: &[u8],
    w: u64,
    h: u64,
    mode: Mode,
    filter: Filter,
    out_w: u64,
    out_h: u64,
) -> Result<Vec<u8>, CoreError> {
    let ch = mode.channels();
    let (in_w, in_h, ow, oh) = check_dims_and_len(data.len(), w, h, ch, out_w, out_h, 1)?;

    // Port of `Image.py:2400-2401`: identity resize returns a copy before RGBA->RGBa conversion.
    if in_w == ow && in_h == oh {
        return clone_u8_fallible(data);
    }

    match mode {
        Mode::L | Mode::Rgb => resample_pil_u8_core(data, in_w, in_h, ow, oh, ch, filter),
        Mode::Rgba => {
            let premul = rgba_to_premul(data)?;
            let resized = resample_pil_u8_core(&premul, in_w, in_h, ow, oh, 4, filter)?;
            drop(premul);
            premul_to_rgba(&resized)
        }
    }
}

/// Convenience `usize` wrapper for [`resample_u8_u64`].
pub fn resample_u8(
    data: &[u8],
    w: usize,
    h: usize,
    mode: Mode,
    filter: Filter,
    out_w: usize,
    out_h: usize,
) -> Result<Vec<u8>, CoreError> {
    resample_u8_u64(
        data,
        w as u64,
        h as u64,
        mode,
        filter,
        out_w as u64,
        out_h as u64,
    )
}

/// Port of Pillow 12.2.0 `Image.resize((out_w, out_h), filter)` for 32-bpc float images (`mode = "F"`),
/// accepting `u64` dimensions so native and WASM enforce identical size limits.
pub fn resample_f32_u64(
    data: &[f32],
    w: u64,
    h: u64,
    filter: Filter,
    out_w: u64,
    out_h: u64,
) -> Result<Vec<f32>, CoreError> {
    let (in_w, in_h, ow, oh) = check_dims_and_len(data.len(), w, h, 1, out_w, out_h, 4)?;

    if in_w == ow && in_h == oh {
        return clone_f32_fallible(data);
    }

    if in_h > in_w.saturating_mul(100) && oh < in_h {
        let vert = resample_inner_f32(data, in_w, in_h, in_w, oh, filter)?;
        resample_inner_f32(&vert, in_w, oh, ow, oh, filter)
    } else {
        resample_inner_f32(data, in_w, in_h, ow, oh, filter)
    }
}

/// Convenience `usize` wrapper for [`resample_f32_u64`].
pub fn resample_f32(
    data: &[f32],
    w: usize,
    h: usize,
    filter: Filter,
    out_w: usize,
    out_h: usize,
) -> Result<Vec<f32>, CoreError> {
    resample_f32_u64(data, w as u64, h as u64, filter, out_w as u64, out_h as u64)
}

#[cfg(test)]
mod tests;
