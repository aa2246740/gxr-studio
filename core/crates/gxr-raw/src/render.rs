//! Port of gxr_studio/raw/render.py: neutral full-resolution RAW -> sRGB uint8 rendering.

use crate::codes::{CODE_MAX, Image, STRIP_ROWS, orient_array, wb_gains};
use crate::demosaic::{check_crop_window, demosaic_strips};
use crate::dng::Dng;
use crate::error::CoreError;
use crate::mosaic::require_finite;

/// Power term `a ** f32(1/2.4)` used by `srgb_encode` above the linear segment.
///
/// NumPy 2.4 NEP 50 promotes `1 / 2.4` to float32 (`(1.0_f64 / 2.4_f64) as f32`). Evaluating
/// via double precision (`libm::pow(f64::from(a), f64::from(inv_gamma_f32)) as f32`) produces
/// the correctly rounded float32 power, avoiding macOS `libSystem powf` 1-ULP errors.
#[inline]
pub fn srgb_gamma_pow(a: f32) -> f32 {
    #[allow(clippy::cast_possible_truncation)]
    let inv_gamma_f32 = (1.0_f64 / 2.4_f64) as f32;
    #[allow(clippy::cast_possible_truncation)]
    {
        gxr_math::powf(f64::from(a), f64::from(inv_gamma_f32)) as f32
    }
}

/// Port of gxr_studio/raw/render.py:27 srgb_encode.
///
/// IEC 61966-2-1 transfer function in float32, matching NumPy 2.4 NEP 50 scalar promotion.
pub fn srgb_encode(linear: f32) -> f32 {
    let a = linear.clamp(0.0_f32, 1.0_f32);
    if a <= 0.003_130_8_f32 {
        a * 12.92_f32
    } else {
        1.055_f32 * srgb_gamma_pow(a) - 0.055_f32
    }
}

/// Port of gxr_studio/raw/render.py:33 _neutral_display (single sample).
#[inline]
pub fn neutral_display_sample(code: f32) -> u8 {
    #[allow(clippy::cast_possible_truncation)]
    let code_max = CODE_MAX as f32;
    let scaled = srgb_encode(code / code_max) * 255.0_f32;
    let rounded = scaled.round_ties_even();
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    {
        rounded as u8
    }
}

/// Port of gxr_studio/raw/render.py:33 _neutral_display.
pub fn neutral_display(codes: &Image<f32>) -> Image<u8> {
    let values = codes
        .values
        .iter()
        .map(|&c| neutral_display_sample(c))
        .collect();
    Image {
        height: codes.height,
        width: codes.width,
        channels: codes.channels,
        values,
    }
}

/// Port of gxr_studio/raw/render.py:57 render_full (neutral renderer).
///
/// Runs strip-wise (`STRIP_ROWS = 256`) white-balanced demosaic -> `_neutral_display` ->
/// `orient_array`, keeping peak memory bounded without allocating a full-frame float buffer.
pub fn render_full(dng: &Dng<'_>) -> Result<Image<u8>, CoreError> {
    let (_, _, h, w) = check_crop_window(dng)?;
    // Check neutral & decode strips before allocating the full output buffer so a huge synthetic
    // crop on a truncated file reports the missing strips rather than trying to reserve gigabytes.
    wb_gains(dng)?;
    dng.mosaic()?;

    let uh = crate::bounds::fit_count_core(
        u128::try_from(h).map_err(|_| crate::bounds::too_big_core())?,
    )?;
    let uw = crate::bounds::fit_count_core(
        u128::try_from(w).map_err(|_| crate::bounds::too_big_core())?,
    )?;
    let count = crate::bounds::mul_u64(
        crate::bounds::mul_u64(
            u64::try_from(uh).map_err(|_| crate::bounds::too_big_core())?,
            u64::try_from(uw).map_err(|_| crate::bounds::too_big_core())?,
        )?,
        3,
    )?;
    let out_len = crate::bounds::fit_count_core(u128::from(count))?;
    let mut out_values = Vec::new();
    out_values
        .try_reserve_exact(out_len)
        .map_err(|_| crate::bounds::too_big_core())?;
    out_values.resize(out_len, 0_u8);

    demosaic_strips(dng, STRIP_ROWS, |y0, strip| {
        require_finite(strip.values.iter().map(|&v| f64::from(v)), "去马赛克结果")?;
        let y0_u64 = u64::try_from(y0).map_err(|_| crate::bounds::too_big_core())?;
        let row_elems = crate::bounds::mul_u64(
            u64::try_from(uw).map_err(|_| crate::bounds::too_big_core())?,
            3,
        )?;
        let mut idx =
            crate::bounds::fit_count_core(u128::from(crate::bounds::mul_u64(y0_u64, row_elems)?))?;
        for &code in &strip.values {
            out_values[idx] = neutral_display_sample(code);
            idx += 1;
        }
        Ok(())
    })?;

    let unoriented = Image {
        height: uh,
        width: uw,
        channels: 3,
        values: out_values,
    };
    if dng.orientation == 1 {
        Ok(unoriented)
    } else {
        orient_array(&unoriented, dng.orientation)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn neutral_display_matches_measured_samples() {
        let codes = Image {
            height: 1,
            width: 2,
            channels: 3,
            values: vec![
                -50.0,
                0.0,
                0.003_130_8 * 4095.0,
                0.18 * 4095.0,
                4095.0,
                9000.0,
            ],
        };
        let got = neutral_display(&codes);
        assert_eq!(got.values, vec![0, 0, 10, 118, 255, 255]);
    }

    #[test]
    fn neutral_display_correctly_rounds_known_powf_boundaries() {
        // Known inputs where macOS libSystem powf differs by 1 ULP from correctly rounded f32:
        // (linear_bits, expected_power_bits, expected_code_bits, expected_u8)
        let cases: [(u32, u32, u32, u8); 3] = [
            (0x3cc1_c2a9, 0x3e57_273c, 0x42c1_b68d, 43),
            (0x3da2_3384, 0x3eb1_fe6f, 0x43a2_2961, 80),
            (0x3ead_d76b, 0x3f23_3874, 0x44ad_cc8e, 158),
        ];
        for (lin_bits, pow_bits, code_bits, want_u8) in cases {
            let code = f32::from_bits(code_bits);
            let lin = code / 4095.0_f32;
            assert_eq!(lin.to_bits(), lin_bits);
            assert_eq!(srgb_gamma_pow(lin).to_bits(), pow_bits);
            assert_eq!(neutral_display_sample(code), want_u8);
        }
    }
}
