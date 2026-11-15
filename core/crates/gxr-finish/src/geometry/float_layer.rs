//! Port of `gxr_studio/finish/geometry.py` (`warp_float`, `crop_valid` on float32 layers).

use gxr_raw::CoreError;

use super::bounds::{bad_geometry, checked_mask_dims, checked_mul_u64, fit_count, too_big};
use super::fit::crop_box;
use super::spec::Geometry;
use super::warp::{coverage, expanded_bounds, slider_s};

/// Row-major float32 layer `(height, width, channels)`.
#[derive(Clone, Debug, PartialEq)]
pub struct FloatLayer {
    pub width: usize,
    pub height: usize,
    pub channels: usize,
    pub data: Vec<f32>,
}

/// Validate `(width, height, channels)` for a float32 layer via checked `u64` arithmetic and
/// the shared element/byte limits.
pub fn checked_float_layer_dims(
    width: u64,
    height: u64,
    channels: u64,
) -> Result<(usize, usize, usize, u64), CoreError> {
    if channels == 0 {
        return Err(bad_geometry());
    }
    let (wu, hu, pixels) = checked_mask_dims(width, height)?;
    let chu = fit_count(channels, 4)?;
    let elems = checked_mul_u64(pixels, channels)?;
    let _ = fit_count(elems, 4)?;
    Ok((wu, hu, chu, elems))
}

/// Validate dimensions and verify `layer.len() == width * height * channels`.
pub fn checked_float_layer_slice(
    layer: &[f32],
    width: u64,
    height: u64,
    channels: u64,
) -> Result<(usize, usize, usize, u64), CoreError> {
    let (wu, hu, chu, elems) = checked_float_layer_dims(width, height, channels)?;
    let expected = fit_count(elems, 4)?;
    if layer.len() != expected {
        return Err(bad_geometry());
    }
    Ok((wu, hu, chu, elems))
}

/// Fallibly allocate a `Vec<f32>` of `n` elements filled with `value`.
pub fn try_vec_f32(n: u64, value: f32) -> Result<Vec<f32>, CoreError> {
    let len = fit_count(n, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| too_big())?;
    out.resize(len, value);
    Ok(out)
}

#[inline]
fn final_dims(unrot_w: usize, unrot_h: usize, turns: i64) -> (usize, usize) {
    if turns % 2 == 1 {
        (unrot_h, unrot_w)
    } else {
        (unrot_w, unrot_h)
    }
}

#[inline]
fn unrot_flip_coords(
    row: usize,
    col: usize,
    unrot_h: usize,
    unrot_w: usize,
    final_w: usize,
    turns: i64,
    flip: bool,
) -> (usize, usize) {
    let c_mid = if flip { final_w - 1 - col } else { col };
    match turns {
        0 => (row, c_mid),
        1 => (c_mid, unrot_w - 1 - row),
        2 => (unrot_h - 1 - row, unrot_w - 1 - c_mid),
        3 => (unrot_h - 1 - c_mid, row),
        _ => (row, c_mid),
    }
}

#[inline]
fn straighten_coord_at(x: usize, y: usize, cx: f64, cy: f64, c: f64, s: f64) -> (f64, f64) {
    let dx = x as f64 - cx;
    let dy = y as f64 - cy;
    (c * dx + s * dy + cx, -s * dx + c * dy + cy)
}

struct ExpandedParams {
    minx: i64,
    miny: i64,
    out_w: usize,
    out_h: usize,
    radial: f64,
    sv: f64,
    sh: f64,
    c: f64,
    s: f64,
    half_diag: f64,
    hw: f64,
    hh: f64,
    cx0: f64,
    cy0: f64,
}

impl ExpandedParams {
    fn new(width: usize, height: usize, geom: &Geometry) -> Result<Self, CoreError> {
        let radial = -(geom.distortion as f64 / 100.0) * 0.35;
        let sv = slider_s(geom.vertical as f64);
        let sh = -slider_s(geom.horizontal as f64);
        let th = geom.straighten.to_radians();
        let c = gxr_math::cos(th);
        let s = gxr_math::sin(th);
        let half_diag = gxr_math::hypot(width as f64, height as f64) / 2.0;
        let (minx, miny, out_w, out_h) = expanded_bounds(width, height, geom)?;
        Ok(Self {
            minx,
            miny,
            out_w,
            out_h,
            radial,
            sv,
            sh,
            c,
            s,
            half_diag,
            hw: width as f64 / 2.0,
            hh: height as f64 / 2.0,
            cx0: (width as f64 - 1.0) / 2.0,
            cy0: (height as f64 - 1.0) / 2.0,
        })
    }

    #[inline]
    fn coord_at(&self, col: usize, row: usize) -> (f64, f64) {
        let ox = (self.minx + col as i64) as f64 + 0.5;
        let oy = (self.miny + row as i64) as f64 + 0.5;
        let rx = self.c * ox + self.s * oy;
        let ry = -self.s * ox + self.c * oy;
        let mut nx = rx / self.hw;
        let mut ny = ry / self.hh;
        let denom = 1.0 - self.sh * nx - self.sv * ny;
        nx /= denom;
        ny /= denom;
        let cx = nx * self.hw;
        let cy = ny * self.hh;
        let r_out = gxr_math::hypot(cx, cy) / self.half_diag;
        let mut r_src = r_out;
        if self.radial.abs() >= 1e-8 {
            for _ in 0..8 {
                let f = self.radial * gxr_math::powi(r_src, 3) + r_src - r_out;
                let df = 3.0 * self.radial * gxr_math::powi(r_src, 2) + 1.0;
                let df_safe = if df.abs() < 1e-6 { 1.0 } else { df };
                r_src -= f / df_safe;
            }
        }
        let scale = if r_out > 1e-8 { r_src / r_out } else { 1.0 };
        (cx * scale + self.cx0, cy * scale + self.cy0)
    }
}

/// Bilinearly sample `src` (`h x w x ch` float32) at `(sx, sy)` matching Python's parenthesization:
/// `(src[y0, x0] * (1 - tx) + src[y0, x1] * tx) * (1 - ty) + (src[y1, x0] * (1 - tx) + src[y1, x1] * tx) * ty`
/// with `NaN` outside coverage.
#[inline]
fn sample_float(src: &[f32], w: usize, h: usize, ch: usize, sx: f64, sy: f64, dst: &mut [f32]) {
    if !coverage(w, h, sx, sy) {
        dst.fill(f32::NAN);
        return;
    }
    let sx_floor = sx.floor();
    let sy_floor = sy.floor();
    let x0 = (sx_floor as i32).clamp(0, w as i32 - 1) as usize;
    let y0 = (sy_floor as i32).clamp(0, h as i32 - 1) as usize;
    let x1 = (x0 + 1).min(w - 1);
    let y1 = (y0 + 1).min(h - 1);
    let tx = (sx - sx_floor) as f32;
    let ty = (sy - sy_floor) as f32;
    let one_tx = 1.0_f32 - tx;
    let one_ty = 1.0_f32 - ty;

    let i00 = (y0 * w + x0) * ch;
    let i01 = (y0 * w + x1) * ch;
    let i10 = (y1 * w + x0) * ch;
    let i11 = (y1 * w + x1) * ch;

    for k in 0..ch {
        let top = src[i00 + k] * one_tx + src[i01 + k] * tx;
        let bot = src[i10 + k] * one_tx + src[i11 + k] * tx;
        dst[k] = top * one_ty + bot * ty;
    }
}

/// Port of `gxr_studio/finish/geometry.py:178 warp_float` (checked `u64` entry).
pub fn warp_float_u64(
    layer: &[f32],
    width: u64,
    height: u64,
    channels: u64,
    geom: &Geometry,
) -> Result<FloatLayer, CoreError> {
    let (wu, hu, chu, _) = checked_float_layer_slice(layer, width, height, channels)?;
    let turns = geom.rotate90.rem_euclid(4);
    let flip = geom.flip;

    if geom.expands() {
        let params = ExpandedParams::new(wu, hu, geom)?;
        let (fw, fh) = final_dims(params.out_w, params.out_h, turns);
        let out_pixels = checked_mul_u64(fw as u64, fh as u64)?;
        let out_elems = checked_mul_u64(out_pixels, channels)?;
        let mut out = try_vec_f32(out_elems, f32::NAN)?;
        for r in 0..fh {
            for c in 0..fw {
                let (ur, uc) = unrot_flip_coords(r, c, params.out_h, params.out_w, fw, turns, flip);
                let (sx, sy) = params.coord_at(uc, ur);
                let dst_off = (r * fw + c) * chu;
                sample_float(layer, wu, hu, chu, sx, sy, &mut out[dst_off..dst_off + chu]);
            }
        }
        return Ok(FloatLayer {
            width: fw,
            height: fh,
            channels: chu,
            data: out,
        });
    }

    let (fw, fh) = final_dims(wu, hu, turns);
    let out_pixels = checked_mul_u64(fw as u64, fh as u64)?;
    let out_elems = checked_mul_u64(out_pixels, channels)?;

    if geom.straighten.abs() < 1e-9 {
        let mut out = try_vec_f32(out_elems, 0.0_f32)?;
        for r in 0..fh {
            for c in 0..fw {
                let (ur, uc) = unrot_flip_coords(r, c, hu, wu, fw, turns, flip);
                let src_off = (ur * wu + uc) * chu;
                let dst_off = (r * fw + c) * chu;
                out[dst_off..dst_off + chu].copy_from_slice(&layer[src_off..src_off + chu]);
            }
        }
        return Ok(FloatLayer {
            width: fw,
            height: fh,
            channels: chu,
            data: out,
        });
    }

    let mut out = try_vec_f32(out_elems, f32::NAN)?;
    let th = geom.straighten.to_radians();
    let cos_a = gxr_math::cos(th);
    let sin_a = gxr_math::sin(th);
    let cx = (wu as f64 - 1.0) / 2.0;
    let cy = (hu as f64 - 1.0) / 2.0;

    for r in 0..fh {
        for c in 0..fw {
            let (ur, uc) = unrot_flip_coords(r, c, hu, wu, fw, turns, flip);
            let (sx, sy) = straighten_coord_at(uc, ur, cx, cy, cos_a, sin_a);
            let dst_off = (r * fw + c) * chu;
            sample_float(layer, wu, hu, chu, sx, sy, &mut out[dst_off..dst_off + chu]);
        }
    }

    Ok(FloatLayer {
        width: fw,
        height: fh,
        channels: chu,
        data: out,
    })
}

/// Port of `gxr_studio/finish/geometry.py:178 warp_float`.
pub fn warp_float(
    layer: &[f32],
    width: usize,
    height: usize,
    channels: usize,
    geom: &Geometry,
) -> Result<FloatLayer, CoreError> {
    warp_float_u64(layer, width as u64, height as u64, channels as u64, geom)
}

/// Port of `gxr_studio/finish/geometry.py:596 crop_valid` for float32 layers (checked `u64` entry).
pub fn crop_valid_f32_u64(
    layer: &[f32],
    width: u64,
    height: u64,
    channels: u64,
    rect: [f64; 4],
) -> Result<FloatLayer, CoreError> {
    let (wu, hu, chu, _) = checked_float_layer_slice(layer, width, height, channels)?;
    let [x, y, rw, rh] = crop_box(wu, hu, rect)?;
    let out_pixels = checked_mul_u64(rw as u64, rh as u64)?;
    let out_elems = checked_mul_u64(out_pixels, channels)?;
    let cap = fit_count(out_elems, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(cap).map_err(|_| too_big())?;
    let row_elems = fit_count(checked_mul_u64(rw as u64, channels)?, 4)?;
    for row in y..y + rh {
        let px_off = checked_add_u64_local(checked_mul_u64(row as u64, wu as u64)?, x as u64)?;
        let off = fit_count(checked_mul_u64(px_off, channels)?, 4)?;
        let end = off.checked_add(row_elems).ok_or_else(bad_geometry)?;
        let slice = layer.get(off..end).ok_or_else(bad_geometry)?;
        out.extend_from_slice(slice);
    }
    Ok(FloatLayer {
        width: rw,
        height: rh,
        channels: chu,
        data: out,
    })
}

#[inline]
fn checked_add_u64_local(a: u64, b: u64) -> Result<u64, CoreError> {
    a.checked_add(b).ok_or_else(too_big)
}

/// Port of `gxr_studio/finish/geometry.py:596 crop_valid` for float32 layers.
pub fn crop_valid_f32(
    layer: &[f32],
    width: usize,
    height: usize,
    channels: usize,
    rect: [f64; 4],
) -> Result<FloatLayer, CoreError> {
    crop_valid_f32_u64(layer, width as u64, height as u64, channels as u64, rect)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_and_rot_flip_preserve_nan_bits() {
        let nan_payload = f32::from_bits(0x7fc0_1234);
        let src = vec![1.0_f32, 0.0, nan_payload, 1.0, 2.5, 0.0, 3.5, 1.0];
        let geom = Geometry {
            rotate90: 1,
            flip: true,
            ..Geometry::default()
        };
        let out = warp_float(&src, 2, 2, 2, &geom).unwrap();
        assert_eq!((out.width, out.height, out.channels), (2, 2, 2));
        // rot90=1 puts (r=0,c=1) at top-left, then fliplr swaps columns -> (r=1,c=1) at top-left, etc.
        assert_eq!(out.data[0..2], [3.5, 1.0]);
        assert_eq!(out.data[2].to_bits(), nan_payload.to_bits());
        assert_eq!(out.data[3], 1.0);
    }

    #[test]
    fn straighten_sets_nan_outside_coverage() {
        let src: Vec<f32> = (0..6 * 5 * 2).map(|i| (i as f32) * 0.1).collect();
        let geom = Geometry {
            straighten: 15.0,
            ..Geometry::default()
        };
        let out = warp_float(&src, 6, 5, 2, &geom).unwrap();
        assert_eq!((out.width, out.height, out.channels), (6, 5, 2));
        assert!(out.data[0].is_nan());
        assert!(out.data[1].is_nan());
        // Center pixel stays finite
        let mid = (2 * 6 + 3) * 2;
        assert!(out.data[mid].is_finite() && out.data[mid + 1].is_finite());
    }

    #[test]
    fn expanded_warp_float_grows_canvas_and_marks_outside_nan() {
        let src = vec![0.5_f32; 16 * 12 * 2];
        let geom = Geometry {
            vertical: 12,
            distortion: -10,
            ..Geometry::default()
        };
        let out = warp_float(&src, 16, 12, 2, &geom).unwrap();
        assert!(out.width > 16 || out.height > 12);
        assert!(out.data.iter().any(|v| v.is_nan()));
        assert!(out.data.iter().any(|v| v.is_finite()));
    }

    #[test]
    fn crop_valid_f32_slices_exact_bits() {
        let nan_val = f32::NAN;
        let src = vec![
            1.0, 0.0, 2.0, 0.0, 3.0, 0.0, // row 0
            4.0, 1.0, nan_val, 1.0, 6.0, 0.0, // row 1
        ];
        let out = crop_valid_f32(&src, 3, 2, 2, [1.0 / 3.0, 0.5, 2.0 / 3.0, 0.5]).unwrap();
        assert_eq!((out.width, out.height, out.channels), (2, 1, 2));
        assert!(out.data[0].is_nan());
        assert_eq!(out.data[1], 1.0);
        assert_eq!(out.data[2], 6.0);
        assert_eq!(out.data[3], 0.0);
    }

    #[test]
    fn huge_dimensions_return_too_big_without_panic() {
        let err = warp_float_u64(
            &[1.0_f32, 0.0],
            4_294_967_295,
            4_294_967_295,
            2,
            &Geometry::default(),
        )
        .unwrap_err();
        assert_eq!(err, too_big());
        let err2 =
            warp_float_u64(&[1.0_f32, 0.0], 2_147_483_648, 1, 2, &Geometry::default()).unwrap_err();
        assert_eq!(err2, too_big());
    }
}
