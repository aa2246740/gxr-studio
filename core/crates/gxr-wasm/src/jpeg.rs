//! JPEG exports. Owner: wave-1 unit jpeg.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_raw::{CoreError, error_json};

fn checked_quality_i32(quality: f64) -> i32 {
    if quality.is_finite() && quality.fract() == 0.0 && (1.0..=100.0).contains(&quality) {
        #[allow(clippy::cast_possible_truncation)]
        {
            quality as i32
        }
    } else {
        -1
    }
}

fn checked_jpeg_dim_u32(dim: f64) -> u32 {
    if !dim.is_finite() || dim.fract() != 0.0 || dim <= 0.0 {
        0
    } else if dim > f64::from(u16::MAX) {
        65536
    } else {
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        {
            dim as u32
        }
    }
}

/// Port of `gxr_studio/export.py:_finish` single JPEG encode with EXIF and sRGB ICC.
/// `optimize = true` is what `_finish` writes (Pillow `optimize=True`); `false` keeps the
/// Annex K tables (`gxr_jpeg::encode_jpeg_with`).
#[wasm_bindgen]
pub fn jpeg_encode(
    rgb_u8: &[u8],
    w: f64,
    h: f64,
    quality: f64,
    exif_json: &str,
    optimize: bool,
) -> Result<Vec<u8>, JsValue> {
    let q = checked_quality_i32(quality);
    if !(1..=100).contains(&q) {
        return Err(JsValue::from_str(
            &error_json(&CoreError::new("bad_quality", "品质要在 1 到 100 之间。")).render(),
        ));
    }
    let w_u32 = checked_jpeg_dim_u32(w);
    let h_u32 = checked_jpeg_dim_u32(h);
    gxr_jpeg::encode_jpeg_with(rgb_u8, w_u32, h_u32, q, exif_json, optimize)
        .map_err(|err| JsValue::from_str(&error_json(&err).render()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jpeg_quality_and_dim_reject_wraps_and_bad_floats() {
        assert_eq!(checked_quality_i32(90.0), 90);
        assert_eq!(checked_quality_i32(1.0), 1);
        assert_eq!(checked_quality_i32(100.0), 100);
        for bad_q in [
            0.0,
            -1.0,
            101.0,
            4_294_967_386.0,
            -4_294_967_206.0,
            9_007_199_254_740_992.0,
            1.5,
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
        ] {
            assert_eq!(checked_quality_i32(bad_q), -1);
        }

        assert_eq!(checked_jpeg_dim_u32(2.0), 2);
        assert_eq!(checked_jpeg_dim_u32(65535.0), 65535);
        assert_eq!(checked_jpeg_dim_u32(65536.0), 65536);
        assert_eq!(checked_jpeg_dim_u32(4_294_967_298.0), 65536);
        assert_eq!(checked_jpeg_dim_u32(9_007_199_254_740_992.0), 65536);
        for bad_d in [
            0.0,
            -1.0,
            -4_294_967_294.0,
            1.5,
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
        ] {
            assert_eq!(checked_jpeg_dim_u32(bad_d), 0);
        }
    }
}
