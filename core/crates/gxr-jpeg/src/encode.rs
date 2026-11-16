//! Port of `gxr_studio/export.py:245 _finish` JPEG encoding (single encode with EXIF + sRGB ICC).

use crate::jpeg_enc::{ColorType, Encoder, SamplingFactor};

use gxr_raw::CoreError;

use crate::exif::{build_exif_once, build_exif_with, exif_attempts, parse_exif_json};
use crate::icc::srgb_icc;
use crate::pillow_buffer::pillow_fits;

/// Encode `rgb` (`height * width * 3` bytes, row-major RGB uint8) once to JPEG, with Pillow's
/// `optimize=False` tables: [`encode_jpeg_with`] with `optimize = false`.
pub fn encode_jpeg(
    rgb: &[u8],
    width: u32,
    height: u32,
    quality: i32,
    exif_json: &str,
) -> Result<Vec<u8>, CoreError> {
    encode_jpeg_with(rgb, width, height, quality, exif_json, false)
}

/// Port of `gxr_studio/export.py:246 _finish` (JPEG): encode `rgb` (`height * width * 3`
/// bytes, row-major RGB uint8) once, with libjpeg-turbo's arithmetic:
/// - `quality` validated in `1..=100`;
/// - `4:4:4` chroma subsampling when `quality >= 90`, else `4:2:0` (`h2v2_downsample`);
/// - one interleaved baseline scan;
/// - APP1 EXIF built via `build_exif`, then APP2 with the 588-byte sRGB ICC profile.
///
/// `optimize = true` is what `_finish` writes (Pillow `optimize=True`), byte for byte: optimal
/// Huffman tables (`jchuff.c jpeg_gen_optimal_table`), JFIF 1.01, libjpeg's marker order (DQT
/// before SOF0) and every EXIF rational as Pillow writes it (`_limit_rational`,
/// `_limit_signed_rational`, zero-denominator values inferred as Pillow does). It also fails
/// where `_finish` fails: Pillow's fixed `optimize=True` output buffer (`pillow_buffer.rs`)
/// can't hold the file with the full EXIF, then with the reduced one, so `encode_failed`.
///
/// `optimize = false` is the earlier output, unchanged: the Annex K.3 tables, whose SOF0, DQT,
/// DHT and scan bytes equal Pillow `optimize=False`; JFIF 1.02, SOF0 before DQT, and rationals
/// written as given.
pub fn encode_jpeg_with(
    rgb: &[u8],
    width: u32,
    height: u32,
    quality: i32,
    exif_json: &str,
    optimize: bool,
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
    let q_u8 = u8::try_from(quality)
        .map_err(|_| CoreError::new("bad_quality", "品质要在 1 到 100 之间。"))?;
    if !optimize {
        let exif_payload = build_exif_with(&spec, width, height, false)?;
        return encode_once(rgb, w16, h16, q_u8, &exif_payload, false);
    }
    // `_finish`: `for full in (True, False)`, each try failing on the EXIF or on Pillow's buffer.
    let mut last_err = None;
    for &full in exif_attempts(&spec) {
        let exif_payload = match build_exif_once(&spec, width, height, full, true) {
            Ok(p) => p,
            Err(e) => {
                last_err = Some(e);
                continue;
            }
        };
        let out = encode_once(rgb, w16, h16, q_u8, &exif_payload, true)?;
        let exif_len = u64::try_from(exif_payload.len()).unwrap_or(u64::MAX);
        let extra = icc_segments_len();
        let header = 20_u64.saturating_add(exif_len + 4).saturating_add(extra);
        let finish = u64::try_from(out.len())
            .unwrap_or(u64::MAX)
            .saturating_sub(header);
        if pillow_fits(width, height, quality, exif_len, extra, finish) {
            return Ok(out);
        }
        last_err = Some(CoreError::new(
            "encode_failed",
            "无法导出。请重试。（Suspension not allowed here：文件超出 Pillow 的输出缓冲区）",
        ));
    }
    Err(last_err.unwrap_or_else(|| CoreError::new("encode_failed", "无法导出。请重试。")))
}

/// The APP2 `ICC_PROFILE` segments `add_icc_profile` writes for `srgb_icc()`: one marker,
/// length and 14-byte header per 65519-byte chunk.
fn icc_segments_len() -> u64 {
    let n = srgb_icc().len() as u64;
    let chunks = n.div_ceil(65_519).max(1);
    n + chunks * (2 + 2 + 14)
}

/// One encode of the validated picture with this EXIF payload and the sRGB ICC profile.
fn encode_once(
    rgb: &[u8],
    w16: u16,
    h16: u16,
    q_u8: u8,
    exif_payload: &[u8],
    optimize: bool,
) -> Result<Vec<u8>, CoreError> {
    let mut out = Vec::new();
    let mut encoder = Encoder::new(&mut out, q_u8);
    encoder.set_sampling_factor(if q_u8 >= 90 {
        SamplingFactor::R_4_4_4
    } else {
        SamplingFactor::R_4_2_0
    });
    encoder.set_optimized_huffman_tables(false);
    encoder.set_libjpeg_optimize(optimize);
    encoder
        .add_app_segment(1, exif_payload)
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
