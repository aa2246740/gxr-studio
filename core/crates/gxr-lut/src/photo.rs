//! Port of `gxr_studio/lut/photo.py`: quantise `Target.render` to uint8 in 65536-pixel chunks.

use gxr_raw::CoreError;

use crate::bounds::try_vec_cap;
use crate::spaces::Target;

/// Chunk size used by `lut/photo.py:82`.
pub const CHUNK_PIXELS: usize = 65536;

/// Port of gxr_studio/lut/photo.py:76 _apply.
pub fn apply_u8(
    target: &Target<'_>,
    rgb: &[[f64; 3]],
    xyz: Option<&[[f64; 3]]>,
) -> Result<Vec<[u8; 3]>, CoreError> {
    if let Some(xyz_slice) = xyz
        && xyz_slice.len() != rgb.len()
    {
        return Err(CoreError::new(
            "value_error",
            format!(
                "xyz length {} does not match rgb length {}",
                xyz_slice.len(),
                rgb.len()
            ),
        ));
    }
    let mut out = try_vec_cap(rgb.len())?;
    let mut start = 0;
    while start < rgb.len() {
        let end = (start + CHUNK_PIXELS).min(rgb.len());
        let xyz_chunk = xyz.map(|s| &s[start..end]);
        let (rendered, _) = target.render(&rgb[start..end], xyz_chunk)?;
        for px in rendered {
            if !px.iter().all(|v| v.is_finite()) {
                return Err(CoreError::new(
                    "lut_nonfinite",
                    "无法套用 LUT。请检查输入色彩空间。",
                ));
            }
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            let q = [
                (px[0].clamp(0.0, 255.0) + 0.5).floor() as u8,
                (px[1].clamp(0.0, 255.0) + 0.5).floor() as u8,
                (px[2].clamp(0.0, 255.0) + 0.5).floor() as u8,
            ];
            out.push(q);
        }
        start = end;
    }
    Ok(out)
}
