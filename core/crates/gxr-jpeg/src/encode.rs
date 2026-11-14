//! Port of `gxr_studio/export.py:245 _finish` JPEG encoding (single encode with EXIF + sRGB ICC).

use crate::jpeg_enc::{ColorType, Encoder, SamplingFactor};

use gxr_raw::CoreError;

use crate::exif::{build_exif, parse_exif_json};
use crate::icc::srgb_icc;

/// Encode `rgb` (`height * width * 3` bytes, row-major RGB uint8) once to JPEG.
///
/// Matches `_finish` (`export.py:277-286`) with `libjpeg-turbo`-equivalent
/// `optimize=False` encoding:
/// - `quality` validated in `1..=100`
/// - `4:4:4` chroma subsampling when `quality >= 90`, else `4:2:0` (`h2v2_downsample`)
/// - standard Annex K.3 Huffman tables (`optimize_huffman_table = false`, single interleaved scan)
/// - APP1 EXIF built via `build_exif`
/// - APP2 588-byte sRGB ICC profile
pub fn encode_jpeg(
    rgb: &[u8],
    width: u32,
    height: u32,
    quality: i32,
    exif_json: &str,
) -> Result<Vec<u8>, CoreError> {
    if !(1..=100).contains(&quality) {
        return Err(CoreError::new("bad_quality", "品质要在 1 到 100 之间。"));
    }
    if width == 0 || height == 0 {
        return Err(CoreError::new("bad_image", "图像尺寸必须大于 0"));
    }
    let w16 = u16::try_from(width)
        .map_err(|_| CoreError::new("bad_image", "图像宽度超过 JPEG 上限 65535"))?;
    let h16 = u16::try_from(height)
        .map_err(|_| CoreError::new("bad_image", "图像高度超过 JPEG 上限 65535"))?;
    let expected_u64 = u64::from(width)
        .checked_mul(u64::from(height))
        .and_then(|px| px.checked_mul(3))
        .ok_or_else(|| CoreError::new("bad_image", "图像字节数溢出"))?;
    let actual_u64 =
        u64::try_from(rgb.len()).map_err(|_| CoreError::new("bad_image", "图像字节数溢出"))?;
    if actual_u64 != expected_u64 {
        return Err(CoreError::new(
            "bad_image",
            format!("RGB 字节长度不符：需要 {expected_u64}，实际 {actual_u64}"),
        ));
    }

    let spec = parse_exif_json(exif_json)?;
    let exif_payload = build_exif(&spec, width, height)?;

    let q_u8 = u8::try_from(quality)
        .map_err(|_| CoreError::new("bad_quality", "品质要在 1 到 100 之间。"))?;
    let mut out = Vec::new();
    let mut encoder = Encoder::new(&mut out, q_u8);
    encoder.set_sampling_factor(if q_u8 >= 90 {
        SamplingFactor::R_4_4_4
    } else {
        SamplingFactor::R_4_2_0
    });
    encoder.set_optimized_huffman_tables(false);
    encoder
        .add_app_segment(1, &exif_payload)
        .map_err(|e| CoreError::new("encode_failed", format!("EXIF segment error: {e}")))?;
    encoder
        .add_icc_profile(srgb_icc())
        .map_err(|e| CoreError::new("encode_failed", format!("ICC profile error: {e}")))?;
    encoder
        .encode(rgb, w16, h16, ColorType::Rgb)
        .map_err(|e| CoreError::new("encode_failed", format!("无法导出。请重试。（{e}）")))?;
    Ok(out)
}
