//! Port of `gxr_studio/export.py:245 _finish` JPEG encoding (single encode with EXIF + sRGB ICC).

use jpeg_encoder::{ColorType, Encoder, SamplingFactor, rgb_to_ycbcr};

use gxr_raw::CoreError;

use crate::exif::{build_exif, parse_exif_json};
use crate::icc::srgb_icc;

/// Convert RGB to YCbCr with IJG/libjpeg `h2v2_downsample` 2x2 box averaging on chroma so that
/// `jpeg-encoder`'s `2x2` strided subsampling (`get_block` with `h_scale=2, v_scale=2`) samples
/// the exact 2x2-averaged chroma values instead of point-sampling top-left pixels.
fn rgb_to_ycbcr_h2v2(rgb: &[u8], width: usize, height: usize) -> Vec<u8> {
    let n_pixels = width * height;
    let mut ycc = vec![0u8; n_pixels * 3];
    let mut cb_plane = vec![0i32; n_pixels];
    let mut cr_plane = vec![0i32; n_pixels];

    for (idx, px) in rgb.chunks_exact(3).enumerate() {
        let (y, cb, cr) = rgb_to_ycbcr(px[0], px[1], px[2]);
        ycc[idx * 3] = y;
        cb_plane[idx] = i32::from(cb);
        cr_plane[idx] = i32::from(cr);
    }

    let mut row = 0usize;
    while row < height {
        let r0 = row;
        let r1 = (row + 1).min(height - 1);
        let row0 = r0 * width;
        let row1 = r1 * width;
        let mut col = 0usize;
        let mut bias = 1i32;
        while col < width {
            let c0 = col;
            let c1 = (col + 1).min(width - 1);
            let cb_sum = cb_plane[row0 + c0]
                + cb_plane[row0 + c1]
                + cb_plane[row1 + c0]
                + cb_plane[row1 + c1];
            let cr_sum = cr_plane[row0 + c0]
                + cr_plane[row0 + c1]
                + cr_plane[row1 + c0]
                + cr_plane[row1 + c1];
            let cb_avg = ((cb_sum + bias) >> 2) as u8;
            let cr_avg = ((cr_sum + bias) >> 2) as u8;
            for &r in &[row0, row1] {
                for &c in &[c0, c1] {
                    let dst = (r + c) * 3;
                    ycc[dst + 1] = cb_avg;
                    ycc[dst + 2] = cr_avg;
                }
            }
            bias ^= 3; // alternates 1, 2, 1, 2 across columns like libjpeg h2v2_downsample
            col += 2;
        }
        row += 2;
    }
    ycc
}

/// Encode `rgb` (`height * width * 3` bytes, row-major RGB uint8) once to JPEG.
///
/// Matches `_finish` (`export.py:277-286`) and PORT-WAVE-1 decision 1:
/// - `quality` validated in `1..=100`
/// - `4:4:4` chroma subsampling when `quality >= 90`, else `4:2:0`
/// - optimized Huffman tables
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
    let w_usize = usize::from(w16);
    let h_usize = usize::from(h16);

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
    encoder.set_optimized_huffman_tables(true);
    encoder
        .add_app_segment(1, &exif_payload)
        .map_err(|e| CoreError::new("encode_failed", format!("EXIF segment error: {e}")))?;
    encoder
        .add_icc_profile(srgb_icc())
        .map_err(|e| CoreError::new("encode_failed", format!("ICC profile error: {e}")))?;
    if q_u8 >= 90 {
        encoder
            .encode(rgb, w16, h16, ColorType::Rgb)
            .map_err(|e| CoreError::new("encode_failed", format!("无法导出。请重试。（{e}）")))?;
    } else {
        let ycc = rgb_to_ycbcr_h2v2(rgb, w_usize, h_usize);
        encoder
            .encode(&ycc, w16, h16, ColorType::Ycbcr)
            .map_err(|e| CoreError::new("encode_failed", format!("无法导出。请重试。（{e}）")))?;
    }
    Ok(out)
}
