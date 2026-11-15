//! Resampling filter kernels and coefficient precomputation.
//!
//! Port of Pillow 12.2.0 `src/libImaging/Resample.c`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big};

/// Fixed-point fractional bits for 8-bpc resampling (`Resample.c:92 PRECISION_BITS`).
pub const PRECISION_BITS: u32 = 32 - 8 - 2;

const SCALE_8BPC: f64 = (1_u32 << PRECISION_BITS) as f64;

/// Resampling filter matching Pillow 12.2.0 `Image.Resampling`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Filter {
    /// Box filter (support 0.5).
    Box,
    /// Bilinear / triangle filter (support 1.0).
    Bilinear,
    /// Bicubic / Catmull-Rom (`a = -0.5`) filter (support 2.0).
    Bicubic,
    /// Lanczos-3 windowed sinc filter (support 3.0).
    Lanczos,
}

impl Filter {
    /// Parse a lowercase filter name (`"box"`, `"bilinear"`, `"bicubic"`, `"lanczos"`).
    pub fn parse(name: &str) -> Result<Self, CoreError> {
        match name {
            "box" => Ok(Self::Box),
            "bilinear" => Ok(Self::Bilinear),
            "bicubic" => Ok(Self::Bicubic),
            "lanczos" => Ok(Self::Lanczos),
            _ => Err(CoreError::new(
                "value_error",
                "unsupported resampling filter",
            )),
        }
    }

    /// Filter support radius in destination-aligned source units (`Resample.c:82-86`).
    #[must_use]
    pub const fn support(self) -> f64 {
        match self {
            Self::Box => 0.5,
            Self::Bilinear => 1.0,
            Self::Bicubic => 2.0,
            Self::Lanczos => 3.0,
        }
    }

    /// Evaluate the filter kernel at `x`.
    #[inline]
    #[must_use]
    pub fn eval(self, x: f64) -> f64 {
        match self {
            Self::Box => box_filter(x),
            Self::Bilinear => bilinear_filter(x),
            Self::Bicubic => bicubic_filter(x),
            Self::Lanczos => lanczos_filter(x),
        }
    }
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:13 box_filter`.
#[inline]
#[must_use]
pub fn box_filter(x: f64) -> f64 {
    if x > -0.5 && x <= 0.5 { 1.0 } else { 0.0 }
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:21 bilinear_filter`.
#[inline]
#[must_use]
pub fn bilinear_filter(x: f64) -> f64 {
    let x = if x < 0.0 { -x } else { x };
    if x < 1.0 { 1.0 - x } else { 0.0 }
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:47 bicubic_filter`.
#[inline]
#[must_use]
pub fn bicubic_filter(x: f64) -> f64 {
    const A: f64 = -0.5;
    let x = if x < 0.0 { -x } else { x };
    if x < 1.0 {
        ((A + 2.0) * x - (A + 3.0)) * x * x + 1.0
    } else if x < 2.0 {
        (((x - 5.0) * x + 8.0) * x - 4.0) * A
    } else {
        0.0
    }
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:65 sinc_filter`.
#[inline]
#[must_use]
pub fn sinc_filter(x: f64) -> f64 {
    if x == 0.0 {
        return 1.0;
    }
    let x = x * std::f64::consts::PI;
    gxr_math::sin(x) / x
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:74 lanczos_filter`.
#[inline]
#[must_use]
pub fn lanczos_filter(x: f64) -> f64 {
    if (-3.0..3.0).contains(&x) {
        sinc_filter(x) * sinc_filter(x / 3.0)
    } else {
        0.0
    }
}

/// Precomputed 1-D resampling window bounds (`[xmin, xmax]` per output coordinate) and weights.
pub struct Coeffs {
    /// Maximum taps per output coordinate (`ceil(support) * 2 + 1`).
    pub ksize: usize,
    /// Interleaved `(xmin, xmax)` pairs of length `out_size * 2`.
    pub bounds: Vec<u32>,
    /// Row-major `out_size * ksize` normalized `f64` filter coefficients.
    pub kk: Vec<f64>,
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:182 precompute_coeffs`.
pub fn precompute_coeffs(
    in_size: usize,
    in0: f32,
    in1: f32,
    out_size: usize,
    filter: Filter,
) -> Result<Coeffs, CoreError> {
    if in_size == 0 || out_size == 0 {
        return Err(CoreError::new(
            "value_error",
            "height and width must be > 0",
        ));
    }

    let scale = f64::from(in1 - in0) / (out_size as f64);
    let filterscale = if scale < 1.0 { 1.0 } else { scale };
    let support = filter.support() * filterscale;
    let ceil_support = support.ceil();
    if !ceil_support.is_finite() || ceil_support < 0.0 || ceil_support > f64::from(i32::MAX / 2) {
        return Err(too_big());
    }
    let ksize_u64 = (ceil_support as u64)
        .checked_mul(2)
        .and_then(|v| v.checked_add(1))
        .ok_or_else(too_big)?;
    let ksize = fit_count(ksize_u64, 1)?;

    let out_u64 = u64::try_from(out_size).map_err(|_| too_big())?;
    let coeffs_count = checked_mul_u64(out_u64, ksize_u64)?;
    let coeffs_len = fit_count(coeffs_count, 8)?;
    let bounds_count = checked_mul_u64(out_u64, 2)?;
    let bounds_len = fit_count(bounds_count, 4)?;

    let mut kk = Vec::new();
    kk.try_reserve_exact(coeffs_len).map_err(|_| too_big())?;
    kk.resize(coeffs_len, 0.0_f64);

    let mut bounds = Vec::new();
    bounds
        .try_reserve_exact(bounds_len)
        .map_err(|_| too_big())?;
    bounds.resize(bounds_len, 0_u32);

    let ss = 1.0 / filterscale;
    for xx in 0..out_size {
        let center = f64::from(in0) + (xx as f64 + 0.5) * scale;
        let mut ww = 0.0_f64;

        let xmin_raw = (center - support + 0.5) as i64;
        let xmin = if xmin_raw < 0 {
            0_usize
        } else {
            usize::try_from(xmin_raw).unwrap_or(in_size)
        };

        let xmax_raw = (center + support + 0.5) as i64;
        let xmax_end = if xmax_raw < 0 {
            0_usize
        } else {
            usize::try_from(xmax_raw).unwrap_or(in_size).min(in_size)
        };
        let xmax = xmax_end.saturating_sub(xmin).min(ksize);

        let k = &mut kk[xx * ksize..(xx + 1) * ksize];
        for (x, slot) in k[..xmax].iter_mut().enumerate() {
            let w = filter.eval((((x + xmin) as f64) - center + 0.5) * ss);
            *slot = w;
            ww += w;
        }
        if ww != 0.0 {
            for slot in &mut k[..xmax] {
                *slot /= ww;
            }
        }
        for slot in &mut k[xmax..ksize] {
            *slot = 0.0;
        }
        bounds[xx * 2] = u32::try_from(xmin).map_err(|_| too_big())?;
        bounds[xx * 2 + 1] = u32::try_from(xmax).map_err(|_| too_big())?;
    }

    Ok(Coeffs { ksize, bounds, kk })
}

/// Port of Pillow 12.2.0 `src/libImaging/Resample.c:270 normalize_coeffs_8bpc`.
///
/// Rounds negative coefficients toward negative infinity (`-0.5 + v * 2^22`) and non-negative
/// coefficients toward positive infinity (`+0.5 + v * 2^22`) before truncating to `i32`.
pub fn normalize_coeffs_8bpc(
    out_size: usize,
    ksize: usize,
    kk: &[f64],
) -> Result<Vec<i32>, CoreError> {
    let count = checked_mul_u64(
        u64::try_from(out_size).map_err(|_| too_big())?,
        u64::try_from(ksize).map_err(|_| too_big())?,
    )?;
    let len = fit_count(count, 4)?;
    if kk.len() < len {
        return Err(too_big());
    }
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| too_big())?;
    for &v in &kk[..len] {
        let q = if v < 0.0 {
            (-0.5 + v * SCALE_8BPC) as i32
        } else {
            (0.5 + v * SCALE_8BPC) as i32
        };
        out.push(q);
    }
    Ok(out)
}
