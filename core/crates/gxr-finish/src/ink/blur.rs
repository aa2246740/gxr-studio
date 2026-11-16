//! Port of Pillow 12.2.0 `src/libImaging/BoxBlur.c` and `src/PIL/ImageFilter.py` (`GaussianBlur`).

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big};
use crate::ink::image::{InkImage, bad_mismatch};

/// Port of Pillow `BoxBlur.c:_gaussian_blur_radius`.
///
/// Uses fused multiply-add on the four sites contracted by macOS arm64 Pillow 12.2.0
/// `_imaging.cpython-314-darwin.so` (`_ImagingGaussianBlur` at `0x13bb0`/`0x13c18`,
/// `0x13be0`/`0x13c38`, `0x13bf0`/`0x13c44`, `0x13bf8`/`0x13c4c`).
#[must_use]
#[allow(clippy::disallowed_methods)]
pub(crate) fn gaussian_blur_radius(radius: f32, passes: i32) -> f32 {
    if radius == 0.0_f32 {
        return 0.0_f32;
    }
    let sigma2 = radius * radius / (passes as f32);
    let l_big = f64::from(sigma2).mul_add(12.0_f64, 1.0_f64).sqrt() as f32;
    let l = ((f64::from(l_big) - 1.0_f64) / 2.0_f64).floor() as f32;
    let l_p1 = l + 1.0_f32;
    let a_num = l.mul_add(2.0_f32, 1.0_f32) * l.mul_add(l_p1, -3.0_f32 * sigma2);
    let a_den = 6.0_f32 * (-l_p1).mul_add(l_p1, sigma2);
    let a = a_num / a_den;
    l + a
}

#[allow(clippy::too_many_arguments)]
fn line_box_blur_ch1(
    line_out: &mut [u8],
    line_in: &[u8],
    lastx: usize,
    radius: usize,
    edge_a: usize,
    edge_b: usize,
    ww: u32,
    fw: u32,
) {
    let mut acc = u32::from(line_in[0]).wrapping_mul((radius as u32).wrapping_add(1));
    for &px in line_in.iter().take(edge_a.saturating_sub(1)) {
        acc = acc.wrapping_add(u32::from(px));
    }
    let rem = radius.wrapping_sub(edge_a).wrapping_add(1) as u32;
    acc = acc.wrapping_add(u32::from(line_in[lastx]).wrapping_mul(rem));

    let save = |out: &mut [u8], x: usize, acc_v: u32, left: usize, right: usize| {
        let bulk = acc_v
            .wrapping_mul(ww)
            .wrapping_add((u32::from(line_in[left]) + u32::from(line_in[right])).wrapping_mul(fw));
        out[x] = (bulk.wrapping_add(1 << 23) >> 24) as u8;
    };

    if edge_a <= edge_b {
        for x in 0..edge_a {
            acc = acc
                .wrapping_add(u32::from(line_in[x + radius]))
                .wrapping_sub(u32::from(line_in[0]));
            save(line_out, x, acc, 0, x + radius + 1);
        }
        for x in edge_a..edge_b {
            let sub_idx = x - radius - 1;
            acc = acc
                .wrapping_add(u32::from(line_in[x + radius]))
                .wrapping_sub(u32::from(line_in[sub_idx]));
            save(line_out, x, acc, sub_idx, x + radius + 1);
        }
        for x in edge_b..=lastx {
            let sub_idx = x - radius - 1;
            acc = acc
                .wrapping_add(u32::from(line_in[lastx]))
                .wrapping_sub(u32::from(line_in[sub_idx]));
            save(line_out, x, acc, sub_idx, lastx);
        }
    } else {
        for x in 0..edge_b {
            acc = acc
                .wrapping_add(u32::from(line_in[x + radius]))
                .wrapping_sub(u32::from(line_in[0]));
            save(line_out, x, acc, 0, x + radius + 1);
        }
        for x in edge_b..edge_a {
            acc = acc
                .wrapping_add(u32::from(line_in[lastx]))
                .wrapping_sub(u32::from(line_in[0]));
            save(line_out, x, acc, 0, lastx);
        }
        for x in edge_a..=lastx {
            let sub_idx = x - radius - 1;
            acc = acc
                .wrapping_add(u32::from(line_in[lastx]))
                .wrapping_sub(u32::from(line_in[sub_idx]));
            save(line_out, x, acc, sub_idx, lastx);
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn line_box_blur_ch<const C: usize>(
    line_out: &mut [u8],
    line_in: &[u8],
    lastx: usize,
    radius: usize,
    edge_a: usize,
    edge_b: usize,
    ww: u32,
    fw: u32,
) {
    let mut acc = [0_u32; C];
    let rp1 = (radius as u32).wrapping_add(1);
    let rem = radius.wrapping_sub(edge_a).wrapping_add(1) as u32;
    let last_off = lastx * C;
    for ch in 0..C {
        acc[ch] = u32::from(line_in[ch]).wrapping_mul(rp1);
    }
    for x in 0..edge_a.saturating_sub(1) {
        let off = x * C;
        for ch in 0..C {
            acc[ch] = acc[ch].wrapping_add(u32::from(line_in[off + ch]));
        }
    }
    for ch in 0..C {
        acc[ch] = acc[ch].wrapping_add(u32::from(line_in[last_off + ch]).wrapping_mul(rem));
    }

    let step = |out: &mut [u8],
                acc_v: &mut [u32; C],
                x: usize,
                sub: usize,
                add: usize,
                left: usize,
                right: usize| {
        let sub_off = sub * C;
        let add_off = add * C;
        let l_off = left * C;
        let r_off = right * C;
        let out_off = x * C;
        for ch in 0..C {
            acc_v[ch] = acc_v[ch]
                .wrapping_add(u32::from(line_in[add_off + ch]))
                .wrapping_sub(u32::from(line_in[sub_off + ch]));
            let bulk = acc_v[ch].wrapping_mul(ww).wrapping_add(
                (u32::from(line_in[l_off + ch]) + u32::from(line_in[r_off + ch])).wrapping_mul(fw),
            );
            out[out_off + ch] = (bulk.wrapping_add(1 << 23) >> 24) as u8;
        }
    };

    if edge_a <= edge_b {
        for x in 0..edge_a {
            step(line_out, &mut acc, x, 0, x + radius, 0, x + radius + 1);
        }
        for x in edge_a..edge_b {
            let sub = x - radius - 1;
            step(line_out, &mut acc, x, sub, x + radius, sub, x + radius + 1);
        }
        for x in edge_b..=lastx {
            let sub = x - radius - 1;
            step(line_out, &mut acc, x, sub, lastx, sub, lastx);
        }
    } else {
        for x in 0..edge_b {
            step(line_out, &mut acc, x, 0, x + radius, 0, x + radius + 1);
        }
        for x in edge_b..edge_a {
            step(line_out, &mut acc, x, 0, lastx, 0, lastx);
        }
        for x in edge_a..=lastx {
            let sub = x - radius - 1;
            step(line_out, &mut acc, x, sub, lastx, sub, lastx);
        }
    }
}

fn horizontal_box_blur(
    out: &mut [u8],
    src: Option<&[u8]>,
    w: usize,
    h: usize,
    ch: usize,
    float_radius: f32,
) -> Result<(), CoreError> {
    if !(0.0_f32..2_147_483_648.0_f32).contains(&float_radius) {
        return Err(CoreError::new("value_error", "radius out of range"));
    }
    if w == 0 || h == 0 {
        return Ok(());
    }
    let radius = (float_radius as i32) as usize;
    #[allow(clippy::disallowed_methods)]
    // Matches `_ImagingHorizontalBoxBlur` at `0x12cb8` in `_imaging.cpython-314-darwin.so`.
    let denom = float_radius.mul_add(2.0_f32, 1.0_f32);
    let ww = (16_777_216.0_f32 / denom) as u32;
    let factor = ((radius as u32).wrapping_mul(2).wrapping_add(1)).wrapping_mul(ww);
    let fw = 16_777_216_u32.wrapping_sub(factor) / 2;

    let edge_a = (radius + 1).min(w);
    let edge_b = w.saturating_sub(radius + 1);
    let lastx = w - 1;
    let row_bytes = w * ch;
    let mut line_tmp = vec![0_u8; row_bytes];

    for y in 0..h {
        let row_start = y * row_bytes;
        let row_end = row_start + row_bytes;
        if let Some(in_buf) = src {
            let line_in = &in_buf[row_start..row_end];
            let line_out = &mut out[row_start..row_end];
            match ch {
                1 => line_box_blur_ch1(line_out, line_in, lastx, radius, edge_a, edge_b, ww, fw),
                3 => {
                    line_box_blur_ch::<3>(line_out, line_in, lastx, radius, edge_a, edge_b, ww, fw)
                }
                4 => {
                    line_box_blur_ch::<4>(line_out, line_in, lastx, radius, edge_a, edge_b, ww, fw)
                }
                _ => return Err(bad_mismatch("unsupported channel count for box blur")),
            }
        } else {
            let line_in = &out[row_start..row_end];
            match ch {
                1 => line_box_blur_ch1(
                    &mut line_tmp,
                    line_in,
                    lastx,
                    radius,
                    edge_a,
                    edge_b,
                    ww,
                    fw,
                ),
                3 => line_box_blur_ch::<3>(
                    &mut line_tmp,
                    line_in,
                    lastx,
                    radius,
                    edge_a,
                    edge_b,
                    ww,
                    fw,
                ),
                4 => line_box_blur_ch::<4>(
                    &mut line_tmp,
                    line_in,
                    lastx,
                    radius,
                    edge_a,
                    edge_b,
                    ww,
                    fw,
                ),
                _ => return Err(bad_mismatch("unsupported channel count for box blur")),
            }
            out[row_start..row_end].copy_from_slice(&line_tmp);
        }
    }
    Ok(())
}

fn transpose_buf(src: &[u8], w: usize, h: usize, ch: usize) -> Result<Vec<u8>, CoreError> {
    let total = fit_count(
        checked_mul_u64(checked_mul_u64(w as u64, h as u64)?, ch as u64)?,
        1,
    )?;
    let mut dst = Vec::new();
    dst.try_reserve_exact(total).map_err(|_| too_big())?;
    dst.resize(total, 0);
    match ch {
        1 => {
            for y in 0..h {
                let row = y * w;
                for x in 0..w {
                    dst[x * h + y] = src[row + x];
                }
            }
        }
        3 => {
            for y in 0..h {
                let row = y * w * 3;
                for x in 0..w {
                    let s = row + x * 3;
                    let d = (x * h + y) * 3;
                    dst[d] = src[s];
                    dst[d + 1] = src[s + 1];
                    dst[d + 2] = src[s + 2];
                }
            }
        }
        4 => {
            for y in 0..h {
                let row = y * w * 4;
                for x in 0..w {
                    let s = row + x * 4;
                    let d = (x * h + y) * 4;
                    dst[d..d + 4].copy_from_slice(&src[s..s + 4]);
                }
            }
        }
        _ => return Err(bad_mismatch("unsupported channel count")),
    }
    Ok(dst)
}

/// Port of Pillow `ImagingBoxBlur(imOut, imIn, xradius, yradius, n)`.
pub fn box_blur(
    im: &InkImage,
    xradius: f32,
    yradius: f32,
    passes: i32,
) -> Result<InkImage, CoreError> {
    if !xradius.is_finite() || !yradius.is_finite() || xradius < 0.0_f32 || yradius < 0.0_f32 {
        return Err(CoreError::new("value_error", "radius must be >= 0"));
    }
    if xradius >= 2_147_483_648.0_f32 || yradius >= 2_147_483_648.0_f32 {
        return Err(CoreError::new("value_error", "radius out of range"));
    }
    if passes < 1 {
        return Err(CoreError::new(
            "value_error",
            "number of passes must be greater than zero",
        ));
    }
    if im.width == 0 || im.height == 0 || (xradius == 0.0_f32 && yradius == 0.0_f32) {
        return Ok(im.clone());
    }
    let ch = im.mode.channels();
    let total = fit_count(
        checked_mul_u64(
            checked_mul_u64(im.width as u64, im.height as u64)?,
            ch as u64,
        )?,
        1,
    )?;
    let mut out = Vec::new();
    out.try_reserve_exact(total).map_err(|_| too_big())?;
    out.resize(total, 0);

    if xradius != 0.0_f32 {
        horizontal_box_blur(&mut out, Some(&im.data), im.width, im.height, ch, xradius)?;
        for _ in 1..passes {
            horizontal_box_blur(&mut out, None, im.width, im.height, ch, xradius)?;
        }
    }
    if yradius != 0.0_f32 {
        let src_for_t = if xradius == 0.0_f32 {
            im.data.as_slice()
        } else {
            out.as_slice()
        };
        let mut transposed = transpose_buf(src_for_t, im.width, im.height, ch)?;
        for _ in 0..passes {
            horizontal_box_blur(&mut transposed, None, im.height, im.width, ch, yradius)?;
        }
        out = transpose_buf(&transposed, im.height, im.width, ch)?;
    }

    Ok(InkImage {
        mode: im.mode,
        width: im.width,
        height: im.height,
        data: out,
    })
}

/// Port of Pillow `ImageFilter.GaussianBlur(radius)` (3-pass box blur).
pub fn gaussian_blur(im: &InkImage, xradius: f64, yradius: f64) -> Result<InkImage, CoreError> {
    if !xradius.is_finite() || !yradius.is_finite() || xradius < 0.0_f64 || yradius < 0.0_f64 {
        return Err(CoreError::new("value_error", "radius must be >= 0"));
    }
    let xr = gaussian_blur_radius(xradius as f32, 3);
    let yr = gaussian_blur_radius(yradius as f32, 3);
    if !xr.is_finite() || !yr.is_finite() || xr >= 2_147_483_648.0_f32 || yr >= 2_147_483_648.0_f32
    {
        return Err(CoreError::new("value_error", "radius out of range"));
    }
    box_blur(im, xr, yr, 3)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ink::image::InkMode;

    #[test]
    fn fma_sensitive_gaussian_blur_radius_and_extreme_bounds() {
        // Inputs where FMA differs from unfused in `_gaussian_blur_radius`:
        // Verify exact bit-pattern (`0x45b50850` vs unfused `0x45b50851`) and clean rejection of out-of-range radii.
        let radius = 5793.539_f32;
        let r = gaussian_blur_radius(radius, 3);
        let r_unfused = {
            let sigma2 = radius * radius / 3.0_f32;
            let l_big = (12.0_f64 * f64::from(sigma2) + 1.0_f64).sqrt() as f32;
            let l = ((f64::from(l_big) - 1.0_f64) / 2.0_f64).floor() as f32;
            let l_p1 = l + 1.0_f32;
            let a_num = (2.0_f32 * l + 1.0_f32) * (l * l_p1 - 3.0_f32 * sigma2);
            let a_den = 6.0_f32 * (sigma2 - l_p1 * l_p1);
            l + a_num / a_den
        };
        assert_eq!(r.to_bits(), 0x45b5_0850);
        assert_ne!(r.to_bits(), r_unfused.to_bits());

        // End-to-end 3-pass GaussianBlur pixel difference when `gauss_num` FMA is disabled:
        // Note: `gauss_sqrt` (`* 12.0` from f32 in f64), `gauss_2l1` (`* 2.0`), and `box_denom` (`* 2.0`)
        // have exact multiplications, so fused == unfused for all finite f32 inputs on those three sites.
        #[allow(clippy::excessive_precision)]
        let r_edge = 5833.5244140625_f32;
        let r_edge_unfused = {
            let sigma2 = r_edge * r_edge / 3.0_f32;
            let l_big = (12.0_f64 * f64::from(sigma2) + 1.0_f64).sqrt() as f32;
            let l = ((f64::from(l_big) - 1.0_f64) / 2.0_f64).floor() as f32;
            let l_p1 = l + 1.0_f32;
            let a_num = (2.0_f32 * l + 1.0_f32) * (l * l_p1 - 3.0_f32 * sigma2);
            let a_den = 6.0_f32 * (sigma2 - l_p1 * l_p1);
            l + a_num / a_den
        };
        assert_ne!(
            gaussian_blur_radius(r_edge, 3).to_bits(),
            r_edge_unfused.to_bits()
        );
        let mut row = vec![0_u8; 11_680];
        let mut state: u32 = 1_169_574_962;
        for px in &mut row {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            *px = (state >> 24) as u8;
        }
        let im_row = InkImage::from_vec(InkMode::L, 11_680, 1, row).expect("row");
        let fused_out = gaussian_blur(&im_row, f64::from(r_edge), 0.0).expect("fused");
        let unfused_out = box_blur(&im_row, r_edge_unfused, 0.0_f32, 3).expect("unfused");
        assert_ne!(fused_out.as_slice(), unfused_out.as_slice());

        // `gauss_den` (`(-l_p1).mul_add(l_p1, sigma2)`) differs in bit value at `388581.8125_f32`:
        #[allow(clippy::excessive_precision)]
        let r_den = 388581.8125_f32;
        let sigma2_den = r_den * r_den / 3.0_f32;
        let l_den = ((f64::from((f64::from(sigma2_den) * 12.0_f64 + 1.0_f64).sqrt() as f32)
            - 1.0_f64)
            / 2.0_f64)
            .floor() as f32;
        let lp1_den = l_den + 1.0_f32;
        #[allow(clippy::disallowed_methods)]
        let den_fused = (-lp1_den).mul_add(lp1_den, sigma2_den);
        let den_unfused = sigma2_den - lp1_den * lp1_den;
        assert_ne!(den_fused.to_bits(), den_unfused.to_bits());

        let empty = InkImage::new_filled(InkMode::Rgb, 0, 0, [0, 0, 0, 255]).expect("empty");
        assert!(gaussian_blur(&empty, 1e300, 1e300).is_err());
        assert!(box_blur(&empty, 2_147_483_648.0_f32, 0.0_f32, 1).is_err());
        assert!(gaussian_blur(&empty, -1.0, 0.0).is_err());
    }
}
