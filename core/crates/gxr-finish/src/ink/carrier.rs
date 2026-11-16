//! Port of `gxr_studio/finish/frame_texture.py`: `_wobble`, `carrier_contours`, `paint_carrier`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{fit_count, too_big};
use crate::ink::draw::bad_coords;
use crate::ink::image::InkImage;
use crate::ink::rng::Pcg64;
use crate::ink::shapes::paint_poly;

/// Paired `(outer, inner)` carrier polygons returned by [`carrier_contours`].
pub type CarrierContours = (Vec<[f64; 2]>, Vec<[f64; 2]>);

/// Port of `frame_texture.py:_wobble`.
#[allow(clippy::approx_constant)]
pub fn wobble(
    n: usize,
    rng: &mut Pcg64,
    wavelength: f64,
    notches: usize,
    notch_w: f64,
) -> Result<Vec<f64>, CoreError> {
    if !wavelength.is_finite()
        || wavelength <= 0.0_f64
        || !notch_w.is_finite()
        || notch_w <= 0.0_f64
    {
        return Err(bad_coords("wobble parameters must be positive finite"));
    }
    let _ = fit_count(n as u64, 8)?;
    let mut out = Vec::new();
    out.try_reserve_exact(n).map_err(|_| too_big())?;
    out.resize(n, 0.0_f64);

    let two_pi = 2.0_f64 * std::f64::consts::PI;
    for (k, &amp) in [1.0_f64, 0.55_f64, 0.3_f64].iter().enumerate() {
        let wl = wavelength / (1.0_f64 + 1.7_f64 * (k as f64));
        let phase = rng.uniform(0.0_f64, 6.28_f64);
        for (i, slot) in out.iter_mut().enumerate() {
            let t = i as f64;
            *slot += amp * gxr_math::sin((two_pi * t) / wl + phase);
        }
    }
    for slot in &mut out {
        *slot /= 1.85_f64;
    }
    for _ in 0..notches {
        let pos = rng.uniform(0.0_f64, n as f64);
        let wid = rng.uniform(0.5_f64, 1.3_f64) * notch_w;
        let scale = rng.choice_two(-1.0_f64, 1.0_f64) * rng.uniform(0.7_f64, 1.4_f64);
        for (i, slot) in out.iter_mut().enumerate() {
            let u = ((i as f64) - pos) / wid;
            *slot += scale * gxr_math::exp(-(u * u));
        }
    }
    for slot in &mut out {
        *slot += rng.normal(0.0_f64, 0.05_f64);
        *slot = (*slot).clamp(-1.4_f64, 1.4_f64);
    }
    Ok(out)
}

/// Port of `frame_texture.py:carrier_contours` returning `(outer, inner)` polygons.
pub fn carrier_contours(
    w: i64,
    h: i64,
    c: i64,
    jit: i64,
    seed: u64,
) -> Result<CarrierContours, CoreError> {
    if w <= 0 || h <= 0 {
        return Err(bad_coords("carrier dimensions must be positive"));
    }
    let wu = usize::try_from(w).map_err(|_| too_big())?;
    let hu = usize::try_from(h).map_err(|_| too_big())?;
    let short = (wu.min(hu)) as f64;

    let mut rng = Pcg64::from_seed(seed);
    let len_tb = wu / 2 + 4;
    let len_rl = hu / 2 + 4;
    let wob = [
        wobble(len_tb, &mut rng, 0.35 * short, 4, 0.04 * short)?,
        wobble(len_rl, &mut rng, 0.35 * short, 4, 0.04 * short)?,
        wobble(len_tb, &mut rng, 0.35 * short, 4, 0.04 * short)?,
        wobble(len_rl, &mut rng, 0.35 * short, 4, 0.04 * short)?,
    ];
    let mut gap = [Vec::new(), Vec::new(), Vec::new(), Vec::new()];
    for side in 0..4 {
        let mut g = wobble(wob[side].len(), &mut rng, 0.2 * short, 3, 0.03 * short)?;
        for v in &mut g {
            let scaled = *v * 1.4_f64 + 1.2_f64;
            *v = scaled.clamp(0.0_f64, 3.2_f64);
        }
        gap[side] = g;
    }

    let total_pts = (wu / 2 + 1) * 2 + (hu / 2 + 1) * 2;
    let _ = fit_count(total_pts as u64, 16)?;
    let mut outer = Vec::new();
    let mut inner = Vec::new();
    outer.try_reserve_exact(total_pts).map_err(|_| too_big())?;
    inner.try_reserve_exact(total_pts).map_err(|_| too_big())?;

    let c_f = c as f64;
    let jit_f = jit as f64;
    let w_f = w as f64;
    let h_f = h as f64;

    for (side_idx, length) in [wu, hu, wu, hu].into_iter().enumerate() {
        let count = length / 2 + 1;
        let last_idx = wob[side_idx].len() - 1;
        for i in 0..count {
            let idx = i.min(last_idx);
            let d = (c_f + (0.55_f64 * jit_f) * wob[side_idx][idx]) + 0.45_f64 * jit_f;
            let g = gap[side_idx][idx];
            let along = (i * 2) as f64;
            match side_idx {
                0 => {
                    outer.push([along, -d]);
                    inner.push([along, -g]);
                }
                1 => {
                    outer.push([w_f + d, along]);
                    inner.push([w_f + g, along]);
                }
                2 => {
                    outer.push([w_f - along, h_f + d]);
                    inner.push([w_f - along, h_f + g]);
                }
                _ => {
                    outer.push([-d, h_f - along]);
                    inner.push([-g, h_f - along]);
                }
            }
        }
    }
    Ok((outer, inner))
}

/// Port of `frame_texture.py:paint_carrier`.
#[allow(clippy::too_many_arguments)]
pub fn paint_carrier(
    canvas: &mut InkImage,
    x: i64,
    y: i64,
    w: i64,
    h: i64,
    c: i64,
    jit: i64,
    seed: u64,
    fill: [u8; 3],
    paper: [u8; 3],
) -> Result<(), CoreError> {
    let (mut outer, mut inner) = carrier_contours(w, h, c, jit, seed)?;
    let xf = x as f64;
    let yf = y as f64;
    for p in &mut outer {
        p[0] += xf;
        p[1] += yf;
    }
    for p in &mut inner {
        p[0] += xf;
        p[1] += yf;
    }
    paint_poly(canvas, &outer, fill, 1.0_f64)?;
    paint_poly(canvas, &inner, paper, 1.0_f64)?;
    Ok(())
}
