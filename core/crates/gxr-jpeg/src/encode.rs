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

/// Encode `luma` (`height * width` bytes, row-major `"L"` uint8) once to grayscale JPEG.
///
/// Matches Pillow `Image.fromarray(arr, "L").save(buf, "JPEG", quality=quality)`:
/// - JFIF 1.01 APP0, single luma DQT (table 0), component ID `1` (`1x1` MCU)
/// - standard Annex K.3 luma Huffman tables (`optimize=False`)
pub fn encode_luma_jpeg(
    luma: &[u8],
    width: u32,
    height: u32,
    quality: i32,
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
        .ok_or_else(|| CoreError::new("bad_image", "图像字节数溢出"))?;
    let actual_u64 =
        u64::try_from(luma.len()).map_err(|_| CoreError::new("bad_image", "图像字节数溢出"))?;
    if actual_u64 != expected_u64 {
        return Err(CoreError::new(
            "bad_image",
            format!("Luma 字节长度不符：需要 {expected_u64}，实际 {actual_u64}"),
        ));
    }

    let q_u8 = u8::try_from(quality)
        .map_err(|_| CoreError::new("bad_quality", "品质要在 1 到 100 之间。"))?;
    let mut out = Vec::new();
    let mut encoder = Encoder::new(&mut out, q_u8);
    encoder.set_optimized_huffman_tables(false);
    encoder
        .encode(luma, w16, h16, ColorType::Luma)
        .map_err(|e| CoreError::new("encode_failed", format!("无法导出。请重试。（{e}）")))?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::segments::jpeg_segments;

    #[test]
    fn encode_luma_jpeg_writes_libjpeg_grayscale_structure() {
        let mut luma = vec![0_u8; 8 * 8];
        for (i, slot) in luma.iter_mut().enumerate() {
            *slot = i as u8;
        }
        let jpg = encode_luma_jpeg(&luma, 8, 8, 90).unwrap();
        let (segs, sos, eoi) = jpeg_segments(&jpg, 0).unwrap();
        let markers: Vec<u8> = segs.iter().map(|s| s.marker).collect();
        // SOI -> APP0 -> DQT(0) -> SOF0 -> DHT(DC0) -> DHT(AC0) -> SOS .. EOI
        assert_eq!(markers, vec![0xE0, 0xDB, 0xC0, 0xC4, 0xC4]);
        // APP0 JFIF 1.01
        let app0 = &jpg[segs[0].start + 4..segs[0].end];
        assert_eq!(&app0[..7], b"JFIF\0\x01\x01");
        // SOF0: 1 component, ID=1, 1x1, DQT=0
        let sof0 = &jpg[segs[2].start + 4..segs[2].end];
        assert_eq!(sof0, &[8, 0, 8, 0, 8, 1, 1, 0x11, 0]);
        // SOS: 1 component, ID=1
        assert_eq!(&jpg[sos + 4..sos + 10], &[1, 1, 0, 0, 63, 0]);
        assert_eq!(&jpg[eoi - 2..eoi], b"\xFF\xD9");
    }

    #[test]
    fn encode_luma_jpeg_validates_quality_and_dimensions() {
        assert_eq!(
            encode_luma_jpeg(&[0], 1, 1, 0).unwrap_err().kind,
            "bad_quality"
        );
        assert_eq!(
            encode_luma_jpeg(&[0], 1, 1, 101).unwrap_err().kind,
            "bad_quality"
        );
        assert_eq!(
            encode_luma_jpeg(&[], 0, 1, 90).unwrap_err().kind,
            "bad_image"
        );
        assert_eq!(
            encode_luma_jpeg(&[0], 2, 2, 90).unwrap_err().kind,
            "bad_image"
        );
        // Odd non-8-aligned dimensions at <90 quality must succeed with 1x1 sampling
        let jpg = encode_luma_jpeg(&[42_u8; 7 * 9], 7, 9, 75).unwrap();
        let (segs, _, _) = jpeg_segments(&jpg, 0).unwrap();
        assert_eq!(
            segs.iter().map(|s| s.marker).collect::<Vec<_>>(),
            vec![0xE0, 0xDB, 0xC0, 0xC4, 0xC4]
        );
    }
}
