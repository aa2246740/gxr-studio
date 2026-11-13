//! Port of gxr_studio/raw/render.py: neutral full-resolution RAW -> sRGB uint8 rendering.

use crate::codes::{CODE_MAX, Image, STRIP_ROWS, crop_shape, orient_array, wb_gains};
use crate::demosaic::demosaic_strips;
use crate::dng::Dng;
use crate::error::CoreError;
use crate::mosaic::require_finite;

/// Port of gxr_studio/raw/render.py:27 srgb_encode.
///
/// IEC 61966-2-1 transfer function in float32, matching NumPy 2.4 NEP 50 scalar promotion.
pub fn srgb_encode(linear: f32) -> f32 {
    let a = linear.clamp(0.0_f32, 1.0_f32);
    if a <= 0.003_130_8_f32 {
        a * 12.92_f32
    } else {
        // NumPy 2.4 NEP 50 promotes `1 / 2.4` to float32 (`(1.0_f64 / 2.4_f64) as f32`) and calls
        // `powf(a, exp)` via float64 intermediate (`libm::pow(a as f64, exp as f64) as f32`),
        // matching macOS libSystem `powf` to the exact float32 bit on every boundary crossing.
        #[allow(clippy::cast_possible_truncation)]
        let inv_gamma_f32 = (1.0_f64 / 2.4_f64) as f32;
        #[allow(clippy::cast_possible_truncation)]
        let powered = gxr_math::powf(f64::from(a), f64::from(inv_gamma_f32)) as f32;
        1.055_f32 * powered - 0.055_f32
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
    let (h, w) = crop_shape(dng);
    if h < 4 || w < 4 {
        return Err(CoreError::raw_decode("画面太小"));
    }
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
        let uy0 = usize::try_from(y0).map_err(|_| crate::bounds::too_big_core())?;
        let mut idx = uy0 * uw * 3;
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
    fn neutral_display_correctly_rounds_0x3eadd76b_boundary() {
        // R8051302.DNG boundary sample: code = 1390.392333984375 -> lin = 0x3eadd76b.
        // Correctly rounded powf is 0x3f233874, which encodes to 157.50003 -> 158.
        let code = f32::from_bits(0x44ad_cc8e);
        assert_eq!((code / 4095.0_f32).to_bits(), 0x3ead_d76b);
        assert_eq!(neutral_display_sample(code), 158);
    }
}
