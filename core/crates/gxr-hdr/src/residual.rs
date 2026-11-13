//! Port of gxr_studio/hdr/gain.py:242 `_local_residual`.

use gxr_math::log2;
use gxr_raw::CoreError;

use crate::error::unsupported;
use crate::filter::{Plane, gaussian_filter, minimum_filter, uniform_filter};
use crate::gain::block_mean;
use crate::label::label_f64;

const RESIDUAL_EDGE: f64 = 192.0;
const RESIDUAL_WINDOWS: [(i64, i64); 3] = [(16, 4), (32, 8), (64, 32)];

fn take_f64(plane: Plane) -> Result<Vec<f64>, CoreError> {
    match plane {
        Plane::F64(v) => Ok(v),
        _ => Err(unsupported("bad_shape", "滤波类型不对")),
    }
}

/// Continuous brightness residual. `true` means the pair is not one capture.
pub fn local_residual(sdr_y: &[f64], raw_y: &[f32], h: usize, w: usize) -> Result<bool, CoreError> {
    let factor = (h.max(w) as f64 / RESIDUAL_EDGE).ceil().max(1.0) as usize;
    let (sdr, gh, gw) = block_mean(sdr_y, h, w, factor)?;
    let raw_f: Vec<f64> = raw_y.iter().map(|v| f64::from(*v)).collect();
    let (raw, _, _) = block_mean(&raw_f, h, w, factor)?;
    let log_s: Vec<f64> = sdr.iter().map(|v| log2(v.max(1e-5))).collect();
    let log_r: Vec<f64> = raw.iter().map(|v| log2(v.max(1e-6))).collect();
    let a = take_f64(gaussian_filter(&Plane::F64(log_s), gh, gw, 0.5)?)?;
    let b = take_f64(gaussian_filter(&Plane::F64(log_r), gh, gw, 0.5)?)?;
    let mask_in: Vec<f64> = sdr.iter().map(|v| if *v > 0.01 && *v < 0.95 { 1.0 } else { 0.0 }).collect();
    let mask = take_f64(minimum_filter(&Plane::F64(mask_in), gh, gw, 3)?)?;
    for &(size, support) in &RESIDUAL_WINDOWS {
        let mean = |value: &[f64]| uniform_filter(&Plane::F64(value.to_vec()), gh, gw, size).and_then(take_f64);
        let weight = mean(&mask)?;
        let mut wgt = Vec::with_capacity(weight.len());
        for &v in &weight {
            wgt.push(v.max(1e-6));
        }
        let am: Vec<f64> = a.iter().zip(&mask).map(|(v, m)| v * m).collect();
        let bm: Vec<f64> = b.iter().zip(&mask).map(|(v, m)| v * m).collect();
        let a2: Vec<f64> = a.iter().zip(&mask).map(|(v, m)| v * v * m).collect();
        let b2: Vec<f64> = b.iter().zip(&mask).map(|(v, m)| v * v * m).collect();
        let ab: Vec<f64> = a.iter().zip(&b).zip(&mask).map(|((av, bv), m)| av * bv * m).collect();
        let ma_n = mean(&am)?;
        let mb_n = mean(&bm)?;
        let mut ma = Vec::with_capacity(wgt.len());
        let mut mb = Vec::with_capacity(wgt.len());
        for i in 0..wgt.len() {
            ma.push(ma_n[i] / wgt[i]);
            mb.push(mb_n[i] / wgt[i]);
        }
        let va_n = mean(&a2)?;
        let vb_n = mean(&b2)?;
        let cov_n = mean(&ab)?;
        let mut uncertain = vec![0.0f64; gh * gw];
        let mut uncertain_u8 = vec![0u8; gh * gw];
        for i in 0..wgt.len() {
            let va = (va_n[i] / wgt[i] - ma[i] * ma[i]).max(0.0);
            let vb = (vb_n[i] / wgt[i] - mb[i] * mb[i]).max(0.0);
            let cov = cov_n[i] / wgt[i] - ma[i] * mb[i];
            let slope = (cov / vb.max(1e-8)).clamp(0.0, 8.0);
            let residual = ((a[i] - ma[i]) - slope * (b[i] - mb[i])).abs();
            let sigma = (va - 2.0 * slope * cov + slope * slope * vb).max(0.0).sqrt();
            let bad = residual > 0.125_f64.max(5.0 * sigma) && weight[i] > 0.5 && mask[i] > 0.0;
            uncertain[i] = if bad { 1.0 } else { 0.0 };
            uncertain_u8[i] = u8::from(bad);
        }
        let (labels, _) = label_f64(&uncertain, gh, gw)?;
        let max_lab = labels.iter().copied().fold(0, i32::max);
        let mut counts = vec![0i64; max_lab.max(0) as usize + 1];
        for &lab in &labels {
            if lab >= 0 {
                counts[lab as usize] += 1;
            }
        }
        let blocks = minimum_filter(&Plane::U8(uncertain_u8), gh, gw, 2)?;
        let Plane::U8(blocks) = blocks else {
            return Err(unsupported("bad_shape", "滤波类型不对"));
        };
        for i in 0..labels.len() {
            let lab = labels[i];
            if blocks[i] > 0 && lab > 0 && counts[lab as usize] >= support {
                return Ok(true);
            }
        }
    }
    Ok(false)
}
