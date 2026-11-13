//! Port of gxr_studio/hdr/gain.py:322 `fit_curve` and :345 `compute_gain`, plus the uint8
//! quantisation inside gxr_studio/hdr/container.py:198 `encode_gainmap`.

use gxr_raw::{CoreError, Json};

use crate::container::GainMeta;
use crate::elem::{log2, log2f, tanh};
use crate::error::unsupported;
use crate::filter::{check_shape, gaussian_filter};
use crate::gain::f32_of;
use crate::np::{arange_f32, clip, count_f64, digitize, interp, max_of, maximum, median, minimum, percentile};
use crate::number::py_round;

/// Port of gxr_studio/hdr/gain.py:55 Params.
#[derive(Clone, Debug, PartialEq)]
pub struct Params {
    pub map_factor: i64,
    pub max_stops: f64,
    pub knee: f64,
    pub hdr_slope: f64,
    pub gate_lo: f64,
    pub gate_hi: f64,
    pub quality: i64,
}

impl Default for Params {
    fn default() -> Self {
        Self {
            map_factor: 4,
            max_stops: 2.5,
            knee: 0.45,
            hdr_slope: 1.0,
            gate_lo: 0.30,
            gate_hi: 0.50,
            quality: 90,
        }
    }
}

/// Port of gxr_studio/hdr/gain.py:96 GainResult. `stats` keeps the dict's key order.
#[derive(Clone, Debug)]
pub struct GainResult {
    pub delta: Vec<f32>,
    pub meta: GainMeta,
    pub stats: Json,
}

/// Port of gxr_studio/hdr/gain.py:322 fit_curve (`step=0.125`).
///
/// `x` and `y` are float32. The percentiles are float32; `edges` is the float64 `arange`.
pub fn fit_curve(x: &[f32], y: &[f32], valid: &[bool]) -> Result<(Vec<f64>, Vec<f64>), CoreError> {
    let step = 0.125;
    let mut xs = Vec::new();
    let mut ys = Vec::new();
    for ((&a, &b), &ok) in x.iter().zip(y).zip(valid) {
        if ok {
            xs.push(a);
            ys.push(b);
        }
    }
    if xs.len() < 100 {
        return Err(unsupported("no_curve", "可用于拟合的像素太少"));
    }
    let lo = percentile(&xs, 0.5);
    let hi = percentile(&xs, 99.95);
    let edges = arange_f32(lo, hi + f32_of(step), step)?;
    let mut bins: Vec<Vec<f32>> = vec![Vec::new(); edges.len() + 1];
    for (&a, &b) in xs.iter().zip(&ys) {
        bins[digitize(f64::from(a), &edges)].push(b);
    }
    let mut cx = Vec::new();
    let mut cy = Vec::new();
    for k in 1..edges.len() {
        if bins[k].len() >= 30 {
            cx.push(0.5 * (edges[k - 1] + edges[k]));
            cy.push(f64::from(median(&bins[k])));
        }
    }
    if cx.len() < 2 {
        return Err(unsupported("no_curve", "亮度范围太窄，拟合不出曲线"));
    }
    // np.maximum.accumulate
    let mut ay = Vec::with_capacity(cy.len());
    let mut run = cy[0];
    for &v in &cy {
        run = maximum(run, v);
        ay.push(run);
    }
    if ay.len() >= 5 {
        // np.convolve(np.pad(ay, 2, mode="edge"), np.ones(5) / 5, mode="valid"), each output a
        // left-to-right dot product.
        let k = 1.0 / 5.0;
        let last = ay[ay.len() - 1];
        let mut p = vec![ay[0], ay[0]];
        p.extend_from_slice(&ay);
        p.extend_from_slice(&[last, last]);
        ay = (0..ay.len())
            .map(|i| {
                let mut s = p[i] * k;
                for v in &p[i + 1..i + 5] {
                    s += v * k;
                }
                s
            })
            .collect();
    }
    Ok((cx, ay))
}

fn bool_mean(values: impl Iterator<Item = bool>) -> f64 {
    let mut n = 0usize;
    let mut on = 0usize;
    for v in values {
        n += 1;
        on += usize::from(v);
    }
    // Pairwise sums of 0.0 / 1.0 are exact integers.
    count_f64(on) / count_f64(n)
}

/// Port of gxr_studio/hdr/gain.py:345 compute_gain.
///
/// `sdr_y` and `raw_y` are float32 on the gain grid; `raw_y` is NaN where the pixel is not
/// part of the photograph.
pub fn compute_gain(sdr_y: &[f32], raw_y: &[f32], clip_mask: &[bool], h: usize, w: usize, p: &Params) -> Result<GainResult, CoreError> {
    check_shape(h, w, sdr_y.len())?;
    check_shape(h, w, raw_y.len())?;
    check_shape(h, w, clip_mask.len())?;
    let photo: Vec<bool> = raw_y.iter().map(|v| v.is_finite()).collect();
    let x: Vec<f32> = raw_y
        .iter()
        .zip(&photo)
        .map(|(&r, &ok)| log2f(if ok { maximum(r, f32_of(1e-6)) } else { 1.0 }))
        .collect();
    let y: Vec<f32> = sdr_y.iter().map(|&s| log2f(maximum(s, f32_of(1e-5)))).collect();
    let valid: Vec<bool> = (0..sdr_y.len())
        .map(|i| photo[i] && !clip_mask[i] && sdr_y[i] > f32_of(0.003) && sdr_y[i] < f32_of(0.97))
        .collect();
    let (cx, cy) = fit_curve(&x, &y, &valid)?;
    let mid: Vec<usize> = (0..sdr_y.len())
        .filter(|&i| valid[i] && sdr_y[i] > f32_of(0.10) && sdr_y[i] < f32_of(0.30))
        .collect();
    let ev = if mid.len() > 100 {
        let d: Vec<f32> = mid.iter().map(|&i| y[i] - x[i]).collect();
        f64::from(median(&d))
    } else {
        f64::NAN
    };
    let mut yk = log2(p.knee);
    let cy_max = max_of(&cy).unwrap_or(f64::NAN);
    let xk = if cy_max <= yk {
        yk = cy[cy.len() - 1];
        cx[cx.len() - 1]
    } else {
        interp(yk, &cy, &cx, cy[0], cy[cy.len() - 1])
    };
    let (xk32, yk32, slope32) = (f32_of(xk), f32_of(yk), f32_of(p.hdr_slope));
    let mut delta64 = Vec::with_capacity(x.len());
    for (i, &xv) in x.iter().enumerate() {
        let t_sdr = minimum(interp(f64::from(xv), &cx, &cy, cy[0], cy[cy.len() - 1]), 0.0);
        let y_hdr = if xv > xk32 {
            f64::from(yk32 + slope32 * (xv - xk32))
        } else {
            t_sdr
        };
        let d = maximum(y_hdr - t_sdr, 0.0);
        let d = p.max_stops * tanh(d / p.max_stops);
        delta64.push(if photo[i] { d } else { 0.0 });
    }
    #[allow(clippy::cast_possible_truncation)]
    let delta32: Vec<f32> = delta64.iter().map(|&v| v as f32).collect();
    let smooth = gaussian_filter(&delta32, h, w, 0.6)?;
    let (lo32, span32) = (f32_of(p.gate_lo), f32_of(p.gate_hi - p.gate_lo));
    let delta: Vec<f32> = smooth
        .iter()
        .enumerate()
        .map(|(i, &d)| {
            let g = clip((sdr_y[i] - lo32) / span32, 0.0, 1.0);
            let g = g * g * (3.0 - 2.0 * g);
            d * g * if photo[i] { 1.0 } else { 0.0 }
        })
        .collect();
    let peak = max_of(&delta).unwrap_or(f32::NAN);
    // builtin max(peak, 0.05) compares `0.05 > peak` with 0.05 cast to float32.
    let top = if f32_of(0.05) > peak { 0.05 } else { f64::from(peak) };
    let gmax = py_round(top, 4)?;
    let meta = GainMeta {
        gain_min: 0.0,
        gain_max: gmax,
        cap_min: 0.0,
        cap_max: gmax,
        ..GainMeta::default()
    };
    let clipped = if photo.iter().any(|&v| v) {
        bool_mean(photo.iter().zip(clip_mask).filter(|(ok, _)| **ok).map(|(_, &c)| c))
    } else {
        0.0
    };
    let wh = [i64::try_from(w).unwrap_or(i64::MAX), i64::try_from(h).unwrap_or(i64::MAX)];
    let stats = Json::object([
        ("gainmap_wh", Json::ints(&wh)),
        ("raw_clipped_frac", Json::Float(py_round(clipped, 4)?)),
        (
            "ev_raw_to_sdr_mid",
            if ev.is_nan() { Json::Null } else { Json::Float(py_round(ev, 3)?) },
        ),
        ("knee_raw_log2", Json::Float(py_round(xk, 3)?)),
        ("gain_max_stops", Json::Float(py_round(gmax, 3)?)),
        (
            "frac_gain_gt_0p1_stop",
            Json::Float(py_round(bool_mean(delta.iter().map(|&d| d > f32_of(0.1))), 4)?),
        ),
        (
            "frac_gain_gt_1_stop",
            Json::Float(py_round(bool_mean(delta.iter().map(|&d| d > 1.0)), 4)?),
        ),
    ]);
    Ok(GainResult { delta, meta, stats })
}

/// The uint8 plane `encode_gainmap` hands to Pillow:
/// `floor(clip((delta - min) / (max - min), 0, 1) ** gamma * 255 + 0.5)` in float32.
///
/// `** gamma` uses numpy's fast paths for 1, 2, 0.5 and -1; other exponents go through
/// `powf32`. NaN becomes 0, which is what the arm64 float → uint8 conversion gives.
pub fn quantise_gain(delta: &[f32], meta: &GainMeta) -> Vec<u8> {
    let min32 = f32_of(meta.gain_min);
    let span32 = f32_of(meta.gain_max - meta.gain_min);
    let g = meta.gamma;
    delta
        .iter()
        .map(|&d| {
            let r = clip((d - min32) / span32, 0.0, 1.0);
            let r = if g == 1.0 {
                r
            } else if g == 2.0 {
                r * r
            } else if g == 0.5 {
                r.sqrt()
            } else if g == -1.0 {
                1.0 / r
            } else {
                crate::elem::powf32(r, f32_of(g))
            };
            let v = (r * 255.0 + 0.5).floor();
            if v.is_nan() || v <= 0.0 {
                0
            } else if v >= 255.0 {
                255
            } else {
                // 0 < v < 255 and integral.
                #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
                let code = v as u8;
                code
            }
        })
        .collect()
}
