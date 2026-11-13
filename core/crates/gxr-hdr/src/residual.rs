//! Port of gxr_studio/hdr/gain.py:242 `_local_residual`.
//!
//! The planes are coarsened with float32 `block_mean`, logged and blurred in float32, then
//! cast to float64. Everything after that is float64 elementwise numpy, which this port
//! repeats operation by operation.

use gxr_raw::CoreError;

use crate::elem::log2f;
use crate::filter::{gaussian_filter, minimum_filter, uniform_filter};
use crate::gain::{Plane32, block_mean, f32_of};
use crate::label::label;
use crate::np::{clip, maximum};

const RESIDUAL_EDGE: usize = 192;
const RESIDUAL_WINDOWS: [(i64, usize); 3] = [(16, 4), (32, 8), (64, 32)];

fn mul(a: &[f64], b: &[f64]) -> Vec<f64> {
    a.iter().zip(b).map(|(x, y)| x * y).collect()
}

/// `true` when a connected patch of the brightness residual is too large for one capture.
pub fn local_residual(sdr: &Plane32, raw_y: &[f32]) -> Result<bool, CoreError> {
    let (h, w) = (sdr.h, sdr.w);
    // math.ceil(max(shape) / RESIDUAL_EDGE); max(shape) is far below 2**53.
    let factor = h.max(w).div_ceil(RESIDUAL_EDGE);
    let (s, gh, gw) = block_mean(&sdr.v, h, w, factor)?;
    let (r, _, _) = block_mean(raw_y, h, w, factor)?;
    let log_s: Vec<f32> = s.iter().map(|&v| log2f(maximum(v, f32_of(1e-5)))).collect();
    let log_r: Vec<f32> = r.iter().map(|&v| log2f(maximum(v, f32_of(1e-6)))).collect();
    let a: Vec<f64> = gaussian_filter(&log_s, gh, gw, 0.5)?
        .iter()
        .map(|&v| f64::from(v))
        .collect();
    let b: Vec<f64> = gaussian_filter(&log_r, gh, gw, 0.5)?
        .iter()
        .map(|&v| f64::from(v))
        .collect();
    let inside: Vec<f64> = s
        .iter()
        .map(|&v| {
            if v > f32_of(0.01) && v < f32_of(0.95) {
                1.0
            } else {
                0.0
            }
        })
        .collect();
    let mask = minimum_filter(&inside, gh, gw, 3)?;
    let am = mul(&a, &mask);
    let bm = mul(&b, &mask);
    let aam = mul(&mul(&a, &a), &mask);
    let bbm = mul(&mul(&b, &b), &mask);
    let abm = mul(&mul(&a, &b), &mask);
    for &(size, support) in &RESIDUAL_WINDOWS {
        let mean = |v: &[f64]| uniform_filter(v, gh, gw, size);
        let weight = mean(&mask)?;
        let (ma_n, mb_n) = (mean(&am)?, mean(&bm)?);
        let (va_n, vb_n, cov_n) = (mean(&aam)?, mean(&bbm)?, mean(&abm)?);
        let mut uncertain = vec![false; s.len()];
        for i in 0..s.len() {
            let wt = maximum(weight[i], 1e-6);
            let (ma, mb) = (ma_n[i] / wt, mb_n[i] / wt);
            let va = maximum(va_n[i] / wt - ma * ma, 0.0);
            let vb = maximum(vb_n[i] / wt - mb * mb, 0.0);
            let cov = cov_n[i] / wt - ma * mb;
            let slope = clip(cov / maximum(vb, 1e-8), 0.0, 8.0);
            let residual = ((a[i] - ma) - slope * (b[i] - mb)).abs();
            let sigma = maximum(va - 2.0 * slope * cov + slope * slope * vb, 0.0).sqrt();
            uncertain[i] =
                residual > maximum(0.125, 5.0 * sigma) && weight[i] > 0.5 && mask[i] > 0.0;
        }
        let (labels, count) = label(&uncertain, gh, gw)?;
        let mut counts = vec![0usize; usize::try_from(count).unwrap_or(0) + 1];
        for &l in &labels {
            counts[usize::try_from(l).unwrap_or(0)] += 1;
        }
        let as_u8: Vec<u8> = uncertain.iter().map(|&u| u8::from(u)).collect();
        let blocks = minimum_filter(&as_u8, gh, gw, 2)?;
        let hit = labels
            .iter()
            .zip(&blocks)
            .any(|(&l, &blk)| blk > 0 && counts[usize::try_from(l).unwrap_or(0)] >= support);
        if hit {
            return Ok(true);
        }
    }
    Ok(false)
}
