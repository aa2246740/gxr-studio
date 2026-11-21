//! 相机关机图（`GXROFF.JPG`）编码、重封装与严格校验。
//!
//! 与 Python `gxr_studio/off_image.py` 共享完全一致的几何铺满、居中裁切、
//! 4:2:2 基线 + DRI 40 编码、段重封装与 DAT 260 严格校验规则。
//! 不修改现有 `gxr-jpeg` 常规导出路径，保证常规导出字节零变化。

pub mod check;
pub mod encode;

use gxr_finish::{Filter, Mode, resample_u8_u64};
use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

pub use check::{
    ANNEX_K, CheckIssue, DRI_VALUE, HEADER_BYTES, HEIGHT, MAX_BYTES, OffImageCheck, RESTARTS,
    WIDTH, check_off_image, dat_state, fixed_tail, header_crc, remux_off_image,
};

/// 品质阶梯（与桌面 `off_image.py` 及参考脚本完全一致）。
pub const QUALITIES: [u8; 18] = [
    92, 88, 84, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10,
];

/// 关机图生成结果。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OffImageResult {
    pub data: Vec<u8>,
    pub quality: u8,
    pub width: u32,
    pub height: u32,
    pub check: OffImageCheck,
    pub sha256: String,
}

impl OffImageResult {
    pub fn meta_json(&self) -> Json {
        Json::object([
            ("ok", Json::Bool(self.check.ok)),
            ("bytes", Json::Int(self.data.len() as i64)),
            ("quality", Json::Int(i64::from(self.quality))),
            ("width", Json::Int(i64::from(self.width))),
            ("height", Json::Int(i64::from(self.height))),
            ("dat_state", Json::Int(i64::from(self.check.dat_state))),
            ("errors", self.check.errors_json()),
            ("sha256", Json::str(&self.sha256)),
        ])
    }
}

/// 纯整数铺满 640×480 目标尺寸，与 Python `off_image.cover_size` 逐位一致。
pub fn cover_size(width: u32, height: u32) -> Result<(u32, u32), CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new("bad_image", "图像尺寸无效。"));
    }
    if width == WIDTH && height == HEIGHT {
        return Ok((WIDTH, HEIGHT));
    }
    let w = u64::from(width);
    let h = u64::from(height);
    let tw = u64::from(WIDTH);
    let th = u64::from(HEIGHT);
    if w * th >= h * tw {
        let nw = ((w * th + h / 2) / h).max(tw);
        let nw32 = u32::try_from(nw).map_err(|_| CoreError::new("bad_image", "图像比例过大。"))?;
        Ok((nw32, HEIGHT))
    } else {
        let nh = ((h * tw + w / 2) / w).max(th);
        let nh32 = u32::try_from(nh).map_err(|_| CoreError::new("bad_image", "图像比例过大。"))?;
        Ok((WIDTH, nh32))
    }
}

/// 将转正后的 RGB uint8 按 Lanczos 缩放铺满 640×480 并居中裁切。
pub fn cover_crop_rgb(rgb: &[u8], width: u32, height: u32) -> Result<Vec<u8>, CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new("bad_image", "图像尺寸无效。"));
    }
    let expected = usize::try_from(width)
        .ok()
        .and_then(|w| w.checked_mul(usize::try_from(height).ok()?))
        .and_then(|px| px.checked_mul(3))
        .ok_or_else(|| CoreError::new("bad_image", "图像字节数溢出。"))?;
    if rgb.len() != expected {
        return Err(CoreError::new(
            "bad_image",
            format!("RGB 字节长度不符：需要 {expected}，实际 {}", rgb.len()),
        ));
    }
    if width == WIDTH && height == HEIGHT {
        return Ok(rgb.to_vec());
    }
    let (nw, nh) = cover_size(width, height)?;
    let scaled = if nw == width && nh == height {
        rgb.to_vec()
    } else {
        resample_u8_u64(
            rgb,
            u64::from(width),
            u64::from(height),
            Mode::Rgb,
            Filter::Lanczos,
            u64::from(nw),
            u64::from(nh),
        )?
    };
    let nw_usize = nw as usize;
    let nh_usize = nh as usize;
    let w_out = WIDTH as usize;
    let h_out = HEIGHT as usize;
    if nw_usize == w_out && nh_usize == h_out {
        return Ok(scaled);
    }
    let x0 = (nw_usize - w_out) / 2;
    let y0 = (nh_usize - h_out) / 2;
    let mut out = Vec::with_capacity(w_out * h_out * 3);
    for y in 0..h_out {
        let row_start = ((y0 + y) * nw_usize + x0) * 3;
        out.extend_from_slice(&scaled[row_start..row_start + w_out * 3]);
    }
    Ok(out)
}

/// 从转正后的 RGB 像素生成 640×480 `GXROFF.JPG`，品质从 92 递减直到不超过 32,768 B 并通过严格校验。
pub fn make_off_image(rgb: &[u8], width: u32, height: u32) -> Result<OffImageResult, CoreError> {
    let frame = cover_crop_rgb(rgb, width, height)?;
    let mut chosen_data = Vec::new();
    let mut chosen_q = QUALITIES[QUALITIES.len() - 1];
    for &q in &QUALITIES {
        let raw = encode::encode_baseline_422_dri40(&frame, q)?;
        let remuxed = remux_off_image(&raw)?;
        chosen_data = remuxed;
        chosen_q = q;
        if chosen_data.len() <= MAX_BYTES {
            break;
        }
    }
    let check = check_off_image(&chosen_data);
    let sha256 = sha256_hex(&chosen_data);
    Ok(OffImageResult {
        data: chosen_data,
        quality: chosen_q,
        width: WIDTH,
        height: HEIGHT,
        check,
        sha256,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn smooth_rgb(w: u32, h: u32) -> Vec<u8> {
        let mut out = Vec::with_capacity((w * h * 3) as usize);
        for y in 0..h {
            for x in 0..w {
                out.push(((x * 255) / w) as u8);
                out.push(((y * 255) / h) as u8);
                out.push((((x + y) * 128) / (w + h)) as u8);
            }
        }
        out
    }

    fn busy_rgb(w: u32, h: u32) -> Vec<u8> {
        let mut out = Vec::with_capacity((w * h * 3) as usize);
        for y in 0..h {
            for x in 0..w {
                out.push(((x * 5) & 255) as u8);
                out.push(((y * 3) & 255) as u8);
                out.push(((x ^ y) & 255) as u8);
            }
        }
        out
    }

    #[test]
    fn make_off_image_passes_strict_check() {
        let rgb = smooth_rgb(640, 480);
        let res = make_off_image(&rgb, 640, 480).unwrap();
        assert!(res.check.ok, "errors: {:?}", res.check.errors);
        assert_eq!(res.check.dat_state, 7);
        assert!(res.data.len() <= MAX_BYTES);
        assert_eq!(res.quality, 92);
    }

    #[test]
    fn make_off_image_steps_quality_down_for_busy_frame() {
        let rgb = busy_rgb(640, 480);
        let res = make_off_image(&rgb, 640, 480).unwrap();
        assert!(res.check.ok, "errors: {:?}", res.check.errors);
        assert_eq!(res.check.dat_state, 7);
        assert!(res.data.len() <= MAX_BYTES);
        assert!(res.quality < 92);
    }

    #[test]
    fn make_off_image_flags_incompressible_noise_over_cap() {
        let mut state: u32 = 0x1234_5678;
        let mut rgb = vec![0u8; (640 * 480 * 3) as usize];
        for b in &mut rgb {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            *b = (state >> 24) as u8;
        }
        let res = make_off_image(&rgb, 640, 480).unwrap();
        assert!(!res.check.ok);
        assert_eq!(res.quality, 10);
        assert!(res.check.errors.iter().any(|e| e.code == "over_max_bytes"));
    }

    #[test]
    fn header_crc_matches_dat_constant() {
        assert_eq!(fixed_tail().len(), HEADER_BYTES - 136);
        assert_eq!(header_crc(), 0xdd1d_ff77);
    }
}
