//! Port of gxr_studio/raw/demosaic.py: full-resolution Malvar-He-Cutler 5x5 demosaic.
//!
//! Reflect-pads the white-balanced mosaic, then correlates (no kernel flip) in float64 and stores
//! float32. Strip-wise execution keeps peak memory bounded by `STRIP_ROWS`.

use crate::codes::{Image, STRIP_ROWS, crop_shape, wb_mosaic_block};
use crate::dng::Dng;
use crate::error::CoreError;

/// Port of gxr_studio/raw/demosaic.py:22 PAD.
pub const PAD: usize = 2;
const PAD_I64: i64 = 2;

/// Port of gxr_studio/raw/demosaic.py:24 _K_G.
const K_G: [[f64; 5]; 5] = [
    [0.0, 0.0, -0.125, 0.0, 0.0],
    [0.0, 0.0, 0.25, 0.0, 0.0],
    [-0.125, 0.25, 0.5, 0.25, -0.125],
    [0.0, 0.0, 0.25, 0.0, 0.0],
    [0.0, 0.0, -0.125, 0.0, 0.0],
];

/// Port of gxr_studio/raw/demosaic.py:25 _K_H.
const K_H: [[f64; 5]; 5] = [
    [0.0, 0.0, 0.0625, 0.0, 0.0],
    [0.0, -0.125, 0.0, -0.125, 0.0],
    [-0.125, 0.5, 0.625, 0.5, -0.125],
    [0.0, -0.125, 0.0, -0.125, 0.0],
    [0.0, 0.0, 0.0625, 0.0, 0.0],
];

/// Port of gxr_studio/raw/demosaic.py:28 _K_V.
const K_V: [[f64; 5]; 5] = [
    [0.0, 0.0, -0.125, 0.0, 0.0],
    [0.0, -0.125, 0.5, -0.125, 0.0],
    [0.0625, 0.0, 0.625, 0.0, 0.0625],
    [0.0, -0.125, 0.5, -0.125, 0.0],
    [0.0, 0.0, -0.125, 0.0, 0.0],
];

/// Port of gxr_studio/raw/demosaic.py:29 _K_X.
const K_X: [[f64; 5]; 5] = [
    [0.0, 0.0, -0.1875, 0.0, 0.0],
    [0.0, 0.25, 0.0, 0.25, 0.0],
    [-0.1875, 0.0, 0.75, 0.0, -0.1875],
    [0.0, 0.25, 0.0, 0.25, 0.0],
    [0.0, 0.0, -0.1875, 0.0, 0.0],
];

#[inline]
fn correlate_5x5(padded: &[f32], pw: usize, y: usize, x: usize, kernel: &[[f64; 5]; 5]) -> f32 {
    let mut acc = 0.0_f64;
    for (ky, krow) in kernel.iter().enumerate() {
        let row_start = (y + ky) * pw + x;
        for (kx, &kw) in krow.iter().enumerate() {
            acc += f64::from(padded[row_start + kx]) * kw;
        }
    }
    #[allow(clippy::cast_possible_truncation)]
    {
        acc as f32
    }
}

/// Port of gxr_studio/raw/demosaic.py:34 demosaic_block.
///
/// `padded` is a single-channel `(ph, pw)` mosaic with `PAD` pixels on each side; `phase` is the
/// 2x2 colour pattern starting at `(PAD, PAD)`. Returns unclipped `(h, w, 3)` float32 codes.
pub fn demosaic_block(padded: &Image<f32>, phase: [[i64; 2]; 2]) -> Result<Image<f32>, CoreError> {
    if padded.channels != 1 {
        return Err(CoreError::new(
            "value_error",
            "demosaic_block expects a 1-channel mosaic",
        ));
    }
    let ph = padded.height;
    let pw = padded.width;
    if ph <= 2 * PAD || pw <= 2 * PAD {
        return Err(CoreError::raw_decode("画面太小"));
    }
    let expected = ph.checked_mul(pw).ok_or_else(crate::bounds::too_big_core)?;
    if padded.values.len() != expected {
        return Err(CoreError::new(
            "value_error",
            "padded mosaic length does not match dimensions",
        ));
    }
    for row in phase {
        for c in row {
            if !(0..=2).contains(&c) {
                return Err(CoreError::new(
                    "unexpected",
                    format!("invalid CFA colour code {c}"),
                ));
            }
        }
    }

    let h = ph - 2 * PAD;
    let w = pw - 2 * PAD;
    let count = crate::bounds::mul_u64(
        crate::bounds::mul_u64(
            u64::try_from(h).map_err(|_| crate::bounds::too_big_core())?,
            u64::try_from(w).map_err(|_| crate::bounds::too_big_core())?,
        )?,
        3,
    )?;
    let out_len = crate::bounds::fit_count_core(u128::from(count))?;
    let mut out = Vec::new();
    out.try_reserve_exact(out_len)
        .map_err(|_| crate::bounds::too_big_core())?;

    let data = &padded.values;
    for y in 0..h {
        let py = y + PAD;
        let p_row = phase[y % 2];
        for x in 0..w {
            let px = x + PAD;
            let colour = p_row[x % 2];
            let right = p_row[1 - (x % 2)];
            let m = data[py * pw + px];
            let (r, g, b) = match colour {
                0 => (
                    m,
                    correlate_5x5(data, pw, y, x, &K_G),
                    correlate_5x5(data, pw, y, x, &K_X),
                ),
                2 => (
                    correlate_5x5(data, pw, y, x, &K_X),
                    correlate_5x5(data, pw, y, x, &K_G),
                    m,
                ),
                _ if right == 0 => (
                    correlate_5x5(data, pw, y, x, &K_H),
                    m,
                    correlate_5x5(data, pw, y, x, &K_V),
                ),
                _ => (
                    correlate_5x5(data, pw, y, x, &K_V),
                    m,
                    correlate_5x5(data, pw, y, x, &K_H),
                ),
            };
            out.push(r);
            out.push(g);
            out.push(b);
        }
    }

    Ok(Image {
        height: h,
        width: w,
        channels: 3,
        values: out,
    })
}

fn reflect_idx(k: isize, n: usize) -> usize {
    if n <= 1 {
        return 0;
    }
    let n_isize = isize::try_from(n).unwrap_or(isize::MAX);
    let period = 2 * (n_isize - 1);
    let m = usize::try_from(k.rem_euclid(period)).unwrap_or(0);
    if m < n { m } else { 2 * (n - 1) - m }
}

/// Port of gxr_studio/raw/demosaic.py:57 _reflect_pad.
fn reflect_pad(block: &Image<f32>, top: usize, bottom: usize) -> Result<Image<f32>, CoreError> {
    if block.channels != 1 || block.height == 0 || block.width == 0 {
        return Err(CoreError::raw_decode("画面太小"));
    }
    let bh = block.height;
    let bw = block.width;
    let ph = top
        .checked_add(bh)
        .and_then(|v| v.checked_add(bottom))
        .ok_or_else(crate::bounds::too_big_core)?;
    let pw = bw
        .checked_add(2 * PAD)
        .ok_or_else(crate::bounds::too_big_core)?;
    let total = crate::bounds::mul_u64(
        u64::try_from(ph).map_err(|_| crate::bounds::too_big_core())?,
        u64::try_from(pw).map_err(|_| crate::bounds::too_big_core())?,
    )?;
    let total_us = crate::bounds::fit_count_core(u128::from(total))?;
    let mut padded = Vec::new();
    padded
        .try_reserve_exact(total_us)
        .map_err(|_| crate::bounds::too_big_core())?;

    let top_isize = isize::try_from(top).map_err(|_| crate::bounds::too_big_core())?;
    let pad_isize = isize::try_from(PAD).map_err(|_| crate::bounds::too_big_core())?;
    let col_map: Vec<usize> = (0..pw)
        .map(|px| {
            let x_rel = isize::try_from(px).unwrap_or(0) - pad_isize;
            reflect_idx(x_rel, bw)
        })
        .collect();

    for py in 0..ph {
        let py_isize = isize::try_from(py).map_err(|_| crate::bounds::too_big_core())?;
        let src_y = reflect_idx(py_isize - top_isize, bh);
        let src_row = &block.values[src_y * bw..(src_y + 1) * bw];
        for &src_x in &col_map {
            padded.push(src_row[src_x]);
        }
    }

    Ok(Image {
        height: ph,
        width: pw,
        channels: 1,
        values: padded,
    })
}

/// Validate crop bounds with checked arithmetic before allocating any strip or full-frame
/// buffer. Rejects negative `crop_origin`, out-of-`ActiveArea`, and out-of-plane bounds.
pub(crate) fn check_crop_window(dng: &Dng<'_>) -> Result<(i64, i64, i64, i64), CoreError> {
    let (h, w) = crop_shape(dng);
    if h < 4 || w < 4 {
        return Err(CoreError::raw_decode("画面太小"));
    }
    let cy0 = dng.active[0]
        .checked_add(dng.crop_origin.1)
        .ok_or_else(crate::bounds::too_big_core)?;
    let cx0 = dng.active[1]
        .checked_add(dng.crop_origin.0)
        .ok_or_else(crate::bounds::too_big_core)?;
    let cy1 = cy0.checked_add(h).ok_or_else(crate::bounds::too_big_core)?;
    let cx1 = cx0.checked_add(w).ok_or_else(crate::bounds::too_big_core)?;
    if dng.crop_origin.0 < 0
        || dng.crop_origin.1 < 0
        || cy0 < dng.active[0]
        || cx0 < dng.active[1]
        || cy1 > dng.active[2]
        || cx1 > dng.active[3]
        || cy0 < 0
        || cx0 < 0
        || cy1 > dng.height
        || cx1 > dng.width
    {
        return Err(CoreError::new(
            "index_error",
            format!("block [{cy0}, {cy1}) x [{cx0}, {cx1}) outside the raw plane"),
        ));
    }
    Ok((cy0, cx0, h, w))
}

/// Demosaic one strip `[y0, y1)` in crop coordinates.
///
/// Mirrors one iteration of `demosaic_strips`; supports any valid `[y0, y1)` in `[0, crop_h]`.
pub fn demosaic_strip(dng: &Dng<'_>, y0: i64, y1: i64) -> Result<Image<f32>, CoreError> {
    let (_, _, h, _) = check_crop_window(dng)?;
    if y0 < 0 || y1 > h || y0 >= y1 {
        return Err(CoreError::new(
            "index_error",
            format!("strip [{y0}, {y1}) outside crop height {h}"),
        ));
    }
    let lo = (y0 - PAD_I64).max(0);
    let hi = (y1 + PAD_I64).min(h);
    let block = wb_mosaic_block(dng, lo, hi)?;
    let top = usize::try_from(PAD_I64 - (y0 - lo)).map_err(|_| crate::bounds::too_big_core())?;
    let bottom = usize::try_from(PAD_I64 - (hi - y1)).map_err(|_| crate::bounds::too_big_core())?;
    let padded = reflect_pad(&block, top, bottom)?;
    let mut phase = dng.crop_cfa_phase();
    if y0.rem_euclid(2) != 0 {
        phase.swap(0, 1);
    }
    demosaic_block(&padded, phase)
}

/// Port of gxr_studio/raw/demosaic.py:61 demosaic_strips.
///
/// Calls `visit(y0, strip)` for each strip of up to `rows` rows (default `STRIP_ROWS`).
pub fn demosaic_strips(
    dng: &Dng<'_>,
    rows: i64,
    mut visit: impl FnMut(i64, Image<f32>) -> Result<(), CoreError>,
) -> Result<(), CoreError> {
    if rows < 2 || rows % 2 != 0 {
        return Err(CoreError::new("value_error", "条带行数要是正偶数"));
    }
    let (_, _, h, _) = check_crop_window(dng)?;
    let mut y0 = 0;
    while y0 < h {
        let y1 = h.min(y0 + rows);
        let strip = demosaic_strip(dng, y0, y1)?;
        visit(y0, strip)?;
        y0 += rows;
    }
    Ok(())
}

/// Port of gxr_studio/raw/demosaic.py:77 demosaic_full.
pub fn demosaic_full(dng: &Dng<'_>) -> Result<Image<f32>, CoreError> {
    let (_, _, h, w) = check_crop_window(dng)?;
    crate::codes::wb_gains(dng)?;
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
    let mut out = Vec::new();
    out.try_reserve_exact(out_len)
        .map_err(|_| crate::bounds::too_big_core())?;
    out.resize(out_len, 0.0_f32);

    demosaic_strips(dng, STRIP_ROWS, |y0, strip| {
        let uy0 = usize::try_from(y0).map_err(|_| crate::bounds::too_big_core())?;
        let offset = uy0 * uw * 3;
        out[offset..offset + strip.values.len()].copy_from_slice(&strip.values);
        Ok(())
    })?;

    Ok(Image {
        height: uh,
        width: uw,
        channels: 3,
        values: out,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reflect_pad_matches_numpy_on_short_vector() {
        // [1, 2, 3, 4] reflect-padded by 2 on both sides -> [3, 2, 1, 2, 3, 4, 3, 2]
        let block = Image {
            height: 1,
            width: 4,
            channels: 1,
            values: vec![1.0, 2.0, 3.0, 4.0],
        };
        let padded = reflect_pad(&block, 0, 0).unwrap();
        assert_eq!(padded.height, 1);
        assert_eq!(padded.width, 8);
        assert_eq!(padded.values, vec![3.0, 2.0, 1.0, 2.0, 3.0, 4.0, 3.0, 2.0]);
    }

    #[test]
    fn demosaic_block_rejects_bad_channels_or_size() {
        let small = Image {
            height: 4,
            width: 4,
            channels: 1,
            values: vec![0.0; 16],
        };
        assert_eq!(
            demosaic_block(&small, [[0, 1], [1, 2]]).unwrap_err().kind,
            "raw_decode"
        );
    }

    #[test]
    fn negative_crop_origin_is_rejected_before_allocating() {
        let dng = Dng {
            data: &[],
            name: "neg".into(),
            big_endian: true,
            width: 6,
            height: 4,
            bps: 16,
            strips: (Vec::new(), Vec::new()),
            cfa: [[0, 1], [1, 2]],
            lin_table: None,
            active: [0, 0, 4, 6],
            black_pattern: crate::dng::Grid {
                rows: 1,
                cols: 1,
                shape: vec![1, 1],
                values: vec![0.0],
            },
            black_dh: None,
            black_dv: None,
            white: 4095.0,
            default_scale: vec![1.0, 1.0],
            crop_origin: (-4_000_000, 0),
            crop_size: (4_000_006, 4),
            orientation: 1,
            as_shot_neutral: Some(vec![1.0, 1.0, 1.0]),
            as_shot_white_xy: None,
            analog_balance: vec![1.0, 1.0, 1.0],
            baseline_exposure: 0.0,
            exif: Vec::new(),
            mosaic_cache: std::cell::OnceCell::new(),
        };
        let err_dm = demosaic_full(&dng).unwrap_err();
        assert_eq!(err_dm.kind, "index_error");
        assert_eq!(
            err_dm.message,
            "block [0, 4) x [-4000000, 6) outside the raw plane"
        );
        let err_rn = crate::render::render_full(&dng).unwrap_err();
        assert_eq!(err_rn.kind, "index_error");
        assert_eq!(err_rn.message, err_dm.message);
    }
}
