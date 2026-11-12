//! Port of gxr_studio/raw/codes.py: white-balanced sensor codes and the half-resolution path.

use crate::dng::Dng;
use crate::error::CoreError;
use crate::mosaic::require_finite;

/// Port of gxr_studio/raw/codes.py:20 CODE_MAX.
pub const CODE_MAX: f64 = 4095.0;
/// Port of gxr_studio/raw/codes.py:21 STRIP_ROWS.
pub const STRIP_ROWS: i64 = 256;

/// A row-major `(height, width, channels)` array, the shape numpy gives these functions.
#[derive(Clone, Debug, PartialEq)]
pub struct Image<T> {
    pub height: usize,
    pub width: usize,
    pub channels: usize,
    pub values: Vec<T>,
}

impl<T: Copy> Image<T> {
    fn at(&self, y: usize, x: usize) -> &[T] {
        let start = (y * self.width + x) * self.channels;
        &self.values[start..start + self.channels]
    }
}

/// Port of gxr_studio/raw/codes.py:24 wb_gains: `4095 / AsShotNeutral` per colour (R, G, B).
pub fn wb_gains(dng: &Dng<'_>) -> Result<[f64; 3], CoreError> {
    let usable = dng.as_shot_neutral.as_ref().filter(|n| {
        n.len() == 3 && n.iter().all(|v| v.is_finite()) && !n.iter().any(|&v| v <= 0.0)
    });
    match usable {
        Some(n) => Ok([CODE_MAX / n[0], CODE_MAX / n[1], CODE_MAX / n[2]]),
        None => Err(CoreError::raw_decode(
            "这份 RAW 没有可用的白平衡信息（AsShotNeutral）",
        )),
    }
}

/// Port of gxr_studio/raw/codes.py:32 crop_shape: DefaultCrop (height, width), not oriented.
pub fn crop_shape(dng: &Dng<'_>) -> (i64, i64) {
    (dng.crop_size.1, dng.crop_size.0)
}

/// Port of gxr_studio/raw/codes.py:37 half_res_codes, default `dtype=np.float32`:
/// `(h//2, w//2, 3)` white-balanced codes, not oriented (see `orient_array`).
pub fn half_res_codes(dng: &Dng<'_>) -> Result<Image<f32>, CoreError> {
    let wide = half_res_codes_f64(dng)?;
    #[allow(clippy::cast_possible_truncation)]
    let values: Vec<f32> = wide.values.iter().map(|&v| v as f32).collect();
    require_finite(values.iter().map(|&v| f64::from(v)), "半分辨率码值")?;
    Ok(Image {
        height: wide.height,
        width: wide.width,
        channels: 3,
        values,
    })
}

/// `half_res_codes` before the store into the float32 `out` array (`dtype=np.float64`).
///
/// Every step runs in float64 exactly as numpy does it: `normalised_block` is float64, Python's
/// `sum` starts from the int 0, and the float32 rounding happens once, when the value is stored.
pub fn half_res_codes_f64(dng: &Dng<'_>) -> Result<Image<f64>, CoreError> {
    wb_gains(dng)?;
    let (y0, x0, h, w) = dng.crop_window();
    let (h2, w2) = (h.div_euclid(2), w.div_euclid(2));
    if h2 < 1 || w2 < 1 {
        return Err(CoreError::raw_decode("画面太小"));
    }
    let phase = dng.crop_cfa_phase();
    let (uh2, uw2) = (
        usize::try_from(h2).unwrap_or(0),
        usize::try_from(w2).unwrap_or(0),
    );
    let mut out = vec![0f64; uh2 * uw2 * 3];
    let neutral = dng
        .as_shot_neutral
        .clone()
        .ok_or_else(|| CoreError::raw_decode("没有 AsShotNeutral，不能做半分辨率"))?;
    let sites: Vec<Vec<(usize, usize)>> = (0..3)
        .map(|c| {
            let mut s = Vec::new();
            for (i, row) in phase.iter().enumerate() {
                for (j, &v) in row.iter().enumerate() {
                    if v == c {
                        s.push((i, j));
                    }
                }
            }
            s
        })
        .collect();
    if sites.iter().any(Vec::is_empty) {
        return Err(CoreError::new(
            "unexpected",
            "ZeroDivisionError: division by zero",
        ));
    }
    let step = STRIP_ROWS / 2;
    let mut r0 = 0;
    while r0 < h2 {
        let r1 = h2.min(r0 + step);
        let norm = dng.normalised_block(y0 + 2 * r0, y0 + 2 * r1, x0, x0 + 2 * w2)?;
        let base = usize::try_from(r0).unwrap_or(0);
        for r in 0..usize::try_from(r1 - r0).unwrap_or(0) {
            for c2 in 0..uw2 {
                for (c, s) in sites.iter().enumerate() {
                    let mut total = 0.0;
                    for &(i, j) in s {
                        total += norm.values[(2 * r + i) * norm.cols + 2 * c2 + j];
                    }
                    #[allow(clippy::cast_precision_loss)]
                    let mean = total / s.len() as f64;
                    out[((base + r) * uw2 + c2) * 3 + c] = mean / neutral[c] * CODE_MAX;
                }
            }
        }
        r0 += step;
    }
    Ok(Image {
        height: uh2,
        width: uw2,
        channels: 3,
        values: out,
    })
}

/// Port of gxr_studio/raw/codes.py:63 wb_mosaic_block, default `dtype=np.float32`: rows
/// `[y0, y1)` of the crop (crop coordinates), each site times its own colour's gain.
pub fn wb_mosaic_block(dng: &Dng<'_>, y0: i64, y1: i64) -> Result<Image<f32>, CoreError> {
    let gains = wb_gains(dng)?;
    let (cy0, cx0, _h, w) = dng.crop_window();
    let norm = dng.normalised_block(cy0 + y0, cy0 + y1, cx0, cx0 + w)?;
    let mut values = Vec::with_capacity(norm.values.len());
    for r in 0..norm.rows {
        let y = cy0 + i64::try_from(r).unwrap_or(0);
        for c in 0..norm.cols {
            let x = cx0 + i64::try_from(c).unwrap_or(0);
            let gain = gains[usize::try_from(dng.cfa_at(y, x)).unwrap_or(0)];
            #[allow(clippy::cast_possible_truncation)]
            values.push((norm.values[r * norm.cols + c] * gain) as f32);
        }
    }
    require_finite(values.iter().map(|&v| f64::from(v)), "白平衡码值")?;
    Ok(Image {
        height: norm.rows,
        width: norm.cols,
        channels: 1,
        values,
    })
}

/// Port of gxr_studio/raw/codes.py:75 orient_array: turn a stored-orientation array upright by
/// TIFF Orientation (1–8); 5–8 swap height and width.
pub fn orient_array<T: Copy>(a: &Image<T>, orientation: i64) -> Result<Image<T>, CoreError> {
    let (h, w) = (a.height, a.width);
    let swap = (5..=8).contains(&orientation);
    if !(1..=8).contains(&orientation) {
        return Err(CoreError::raw_decode(format!(
            "不支持的 Orientation：{orientation}"
        )));
    }
    let (oh, ow) = if swap { (w, h) } else { (h, w) };
    let mut values = Vec::with_capacity(a.values.len());
    for i in 0..oh {
        for j in 0..ow {
            let (y, x) = match orientation {
                1 => (i, j),
                2 => (i, w - 1 - j),
                3 => (h - 1 - i, w - 1 - j),
                4 => (h - 1 - i, j),
                5 => (j, i),
                6 => (h - 1 - j, i),
                7 => (h - 1 - j, w - 1 - i),
                _ => (j, w - 1 - i),
            };
            values.extend_from_slice(a.at(y, x));
        }
    }
    Ok(Image {
        height: oh,
        width: ow,
        channels: a.channels,
        values,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid() -> Image<u16> {
        Image {
            height: 2,
            width: 3,
            channels: 1,
            values: vec![0, 1, 2, 3, 4, 5],
        }
    }

    #[test]
    fn orient_matches_numpy() {
        // np.arange(6).reshape(2, 3) through orient_array for every orientation
        let want: [&[u16]; 8] = [
            &[0, 1, 2, 3, 4, 5],
            &[2, 1, 0, 5, 4, 3],
            &[5, 4, 3, 2, 1, 0],
            &[3, 4, 5, 0, 1, 2],
            &[0, 3, 1, 4, 2, 5],
            &[3, 0, 4, 1, 5, 2],
            &[5, 2, 4, 1, 3, 0],
            &[2, 5, 1, 4, 0, 3],
        ];
        for (k, expected) in want.iter().enumerate() {
            let o = i64::try_from(k).unwrap() + 1;
            let got = orient_array(&grid(), o).unwrap();
            assert_eq!(got.values, *expected, "orientation {o}");
            assert_eq!(
                (got.height, got.width),
                if o >= 5 { (3, 2) } else { (2, 3) }
            );
        }
        assert_eq!(
            orient_array(&grid(), 9).unwrap_err().message,
            "不支持的 Orientation：9"
        );
    }
}
