//! Port of `gxr_studio/finish/geometry.py` (warp, rotate90, flip, output_size).

use gxr_raw::CoreError;

use super::spec::Geometry;

pub const FILL: [u8; 3] = [0x1B, 0x1C, 0x1F];

/// Output of `warp`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Warped {
    pub width: usize,
    pub height: usize,
    pub rgb: Vec<u8>,
    pub valid: Vec<u8>,
}

/// Port of `gxr_studio/finish/geometry.py:19 _slider_s`
pub fn slider_s(slider: f64) -> f64 {
    let focal = 1.0 / gxr_math::tan(13.5_f64.to_radians());
    gxr_math::tan((slider / 100.0) * 30.0_f64.to_radians()) / focal
}

/// Port of `gxr_studio/finish/geometry.py:35 _coverage`
#[inline]
pub fn coverage(src_w: usize, src_h: usize, sx: f64, sy: f64) -> bool {
    sx >= -1e-3
        && sy >= -1e-3
        && sx <= (src_w as f64 - 1.0) + 1e-3
        && sy <= (src_h as f64 - 1.0) + 1e-3
}

/// Port of `gxr_studio/finish/geometry.py:76 _lens`
fn lens(geom: &Geometry, width: usize, height: usize) -> (f64, f64, f64, f64, f64, f64) {
    let radial = -(geom.distortion as f64 / 100.0) * 0.35;
    let sv = slider_s(geom.vertical as f64);
    let sh = -slider_s(geom.horizontal as f64);
    let th = geom.straighten.to_radians();
    let c = gxr_math::cos(th);
    let s = gxr_math::sin(th);
    let half_diag = gxr_math::hypot(width as f64, height as f64) / 2.0;
    (radial, sv, sh, c, s, half_diag)
}

#[inline]
fn linspace_val(start: f64, stop: f64, num: usize, idx: usize) -> f64 {
    if num <= 1 {
        return start;
    }
    if idx + 1 == num {
        return stop;
    }
    let step = (stop - start) / ((num - 1) as f64);
    start + (idx as f64) * step
}

/// Port of `gxr_studio/finish/geometry.py:86 _expanded_bounds`
pub fn expanded_bounds(
    width: usize,
    height: usize,
    geom: &Geometry,
) -> Result<(i64, i64, usize, usize), CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    let (radial, sv, sh, c, s, half_diag) = lens(geom, width, height);
    let hw = width as f64 / 2.0;
    let hh = height as f64 / 2.0;
    let cx0 = (width as f64 - 1.0) / 2.0;
    let cy0 = (height as f64 - 1.0) / 2.0;

    let forward = |x: f64, y: f64| -> (f64, f64) {
        let mut cx = x - cx0;
        let mut cy = y - cy0;
        let radius = gxr_math::hypot(cx, cy) / half_diag;
        let factor = 1.0 + radial * radius * radius;
        cx *= factor;
        cy *= factor;
        let mut nx = cx / hw;
        let mut ny = cy / hh;
        let denom = 1.0 + sv * ny + sh * nx;
        nx /= denom;
        ny /= denom;
        cx = nx * hw;
        cy = ny * hh;
        (c * cx - s * cy, s * cx + c * cy)
    };

    let mut min_fx = f64::INFINITY;
    let mut max_fx = f64::NEG_INFINITY;
    let mut min_fy = f64::INFINITY;
    let mut max_fy = f64::NEG_INFINITY;

    let mut update = |x: f64, y: f64| {
        let (fx, fy) = forward(x, y);
        if fx < min_fx {
            min_fx = fx;
        }
        if fx > max_fx {
            max_fx = fx;
        }
        if fy < min_fy {
            min_fy = fy;
        }
        if fy > max_fy {
            max_fy = fy;
        }
    };

    let w_end = (width - 1) as f64;
    let h_end = (height - 1) as f64;
    for i in 0..80 {
        update(linspace_val(0.0, w_end, 80, i), 0.0);
    }
    for i in 0..80 {
        update(linspace_val(0.0, w_end, 80, i), h_end);
    }
    for i in 0..40 {
        update(0.0, linspace_val(0.0, h_end, 40, i));
    }
    for i in 0..40 {
        update(w_end, linspace_val(0.0, h_end, 40, i));
    }

    if !min_fx.is_finite() || !max_fx.is_finite() || !min_fy.is_finite() || !max_fy.is_finite() {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }

    let minx = (min_fx - 1.5).floor() as i64;
    let miny = (min_fy - 1.5).floor() as i64;
    let maxx = (max_fx + 1.5).ceil() as i64;
    let maxy = (max_fy + 1.5).ceil() as i64;
    let out_w = (maxx - minx).max(1);
    let out_h = (maxy - miny).max(1);
    let ow = usize::try_from(out_w).map_err(|_| {
        CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        )
    })?;
    let oh = usize::try_from(out_h).map_err(|_| {
        CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        )
    })?;
    // Check pixel count doesn't overflow usize
    if ow.checked_mul(oh).and_then(|p| p.checked_mul(3)).is_none() {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    Ok((minx, miny, ow, oh))
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
        let (radial, sv, sh, c, s, half_diag) = lens(geom, width, height);
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

/// Map output coordinates `(row, col)` (after rotate90 + flip) back to pre-rot_flip `(unrot_row, unrot_col)`.
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
fn final_dims(unrot_w: usize, unrot_h: usize, turns: i64) -> (usize, usize) {
    if turns % 2 == 1 {
        (unrot_h, unrot_w)
    } else {
        (unrot_w, unrot_h)
    }
}

#[inline]
fn sample_pixel(image: &[u8], w: usize, h: usize, sx: f64, sy: f64) -> ([u8; 3], u8) {
    if !coverage(w, h, sx, sy) {
        return (FILL, 0);
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
    let w00 = one_tx * one_ty;
    let w01 = tx * one_ty;
    let w10 = one_tx * ty;
    let w11 = tx * ty;

    let i00 = (y0 * w + x0) * 3;
    let i01 = (y0 * w + x1) * 3;
    let i10 = (y1 * w + x0) * 3;
    let i11 = (y1 * w + x1) * 3;

    let mut out = [0_u8; 3];
    for c in 0..3 {
        let s = ((f32::from(image[i00 + c]) * w00) + (f32::from(image[i01 + c]) * w01))
            + (f32::from(image[i10 + c]) * w10)
            + (f32::from(image[i11 + c]) * w11);
        out[c] = (s + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
    }
    (out, 1)
}

/// Port of `gxr_studio/finish/geometry.py:252 output_size`
pub fn output_size(
    width: usize,
    height: usize,
    geom: &Geometry,
) -> Result<(usize, usize), CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    let turns = geom.rotate90.rem_euclid(4);
    if geom.is_identity() || !geom.expands() {
        return Ok(final_dims(width, height, turns));
    }
    let (_minx, _miny, ow, oh) = expanded_bounds(width, height, geom)?;
    Ok(final_dims(ow, oh, turns))
}

/// Port of `gxr_studio/finish/geometry.py:264 needs_valid_crop`
pub fn needs_valid_crop(geom: &Geometry) -> bool {
    geom.straighten.abs() > 1e-9 || geom.expands()
}

/// Port of `gxr_studio/finish/geometry.py:157 warp_valid`
pub fn warp_valid(
    width: usize,
    height: usize,
    geom: &Geometry,
) -> Result<(usize, usize, Vec<u8>), CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    let turns = geom.rotate90.rem_euclid(4);
    let flip = geom.flip;

    if geom.expands() {
        let params = ExpandedParams::new(width, height, geom)?;
        let (fw, fh) = final_dims(params.out_w, params.out_h, turns);
        let mut mask = vec![0_u8; fw * fh];
        for r in 0..fh {
            for c in 0..fw {
                let (ur, uc) = unrot_flip_coords(r, c, params.out_h, params.out_w, fw, turns, flip);
                let (sx, sy) = params.coord_at(uc, ur);
                mask[r * fw + c] = u8::from(coverage(width, height, sx, sy));
            }
        }
        return Ok((fw, fh, mask));
    }

    let (fw, fh) = final_dims(width, height, turns);
    if geom.straighten.abs() < 1e-9 {
        return Ok((fw, fh, vec![1_u8; fw * fh]));
    }

    let th = geom.straighten.to_radians();
    let cos_a = gxr_math::cos(th);
    let sin_a = gxr_math::sin(th);
    let cx = (width as f64 - 1.0) / 2.0;
    let cy = (height as f64 - 1.0) / 2.0;

    let mut mask = vec![0_u8; fw * fh];
    for r in 0..fh {
        for c in 0..fw {
            let (ur, uc) = unrot_flip_coords(r, c, height, width, fw, turns, flip);
            let (sx, sy) = straighten_coord_at(uc, ur, cx, cy, cos_a, sin_a);
            mask[r * fw + c] = u8::from(coverage(width, height, sx, sy));
        }
    }
    Ok((fw, fh, mask))
}

/// Port of `gxr_studio/finish/geometry.py:169 warp`
pub fn warp(
    image: &[u8],
    width: usize,
    height: usize,
    geom: &Geometry,
) -> Result<Warped, CoreError> {
    if width == 0 || height == 0 {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    let expected = width
        .checked_mul(height)
        .and_then(|p| p.checked_mul(3))
        .ok_or_else(|| {
            CoreError::new(
                "bad_geometry",
                "纠正参数格式不对。请重新打开这张照片后再试。",
            )
        })?;
    if image.len() != expected {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }

    let turns = geom.rotate90.rem_euclid(4);
    let flip = geom.flip;

    if geom.expands() {
        let params = ExpandedParams::new(width, height, geom)?;
        let (fw, fh) = final_dims(params.out_w, params.out_h, turns);
        let total = fw.checked_mul(fh).ok_or_else(|| {
            CoreError::new(
                "bad_geometry",
                "纠正参数格式不对。请重新打开这张照片后再试。",
            )
        })?;
        let rgb_len = total.checked_mul(3).ok_or_else(|| {
            CoreError::new(
                "bad_geometry",
                "纠正参数格式不对。请重新打开这张照片后再试。",
            )
        })?;
        let mut rgb = vec![0_u8; rgb_len];
        let mut valid = vec![0_u8; total];
        for r in 0..fh {
            for c in 0..fw {
                let (ur, uc) = unrot_flip_coords(r, c, params.out_h, params.out_w, fw, turns, flip);
                let (sx, sy) = params.coord_at(uc, ur);
                let (px, v) = sample_pixel(image, width, height, sx, sy);
                let idx = r * fw + c;
                valid[idx] = v;
                let off = idx * 3;
                rgb[off..off + 3].copy_from_slice(&px);
            }
        }
        return Ok(Warped {
            width: fw,
            height: fh,
            rgb,
            valid,
        });
    }

    let (fw, fh) = final_dims(width, height, turns);
    let total = fw * fh;
    let mut rgb = vec![0_u8; total * 3];
    let mut valid = vec![0_u8; total];

    if geom.straighten.abs() < 1e-9 {
        for r in 0..fh {
            for c in 0..fw {
                let (ur, uc) = unrot_flip_coords(r, c, height, width, fw, turns, flip);
                let idx = r * fw + c;
                valid[idx] = 1;
                let src_off = (ur * width + uc) * 3;
                let dst_off = idx * 3;
                rgb[dst_off..dst_off + 3].copy_from_slice(&image[src_off..src_off + 3]);
            }
        }
        return Ok(Warped {
            width: fw,
            height: fh,
            rgb,
            valid,
        });
    }

    let th = geom.straighten.to_radians();
    let cos_a = gxr_math::cos(th);
    let sin_a = gxr_math::sin(th);
    let cx = (width as f64 - 1.0) / 2.0;
    let cy = (height as f64 - 1.0) / 2.0;

    for r in 0..fh {
        for c in 0..fw {
            let (ur, uc) = unrot_flip_coords(r, c, height, width, fw, turns, flip);
            let (sx, sy) = straighten_coord_at(uc, ur, cx, cy, cos_a, sin_a);
            let (px, v) = sample_pixel(image, width, height, sx, sy);
            let idx = r * fw + c;
            valid[idx] = v;
            let off = idx * 3;
            rgb[off..off + 3].copy_from_slice(&px);
        }
    }
    Ok(Warped {
        width: fw,
        height: fh,
        rgb,
        valid,
    })
}
