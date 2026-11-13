//! Port of `gxr_studio/hdr/gain.py`: the grid, the alignment and the gain plane.
//!
//! JPEG encoding of the gain map is not here. `quantise_gain` is the uint8
//! formula inside `encode_gainmap`, before Pillow saves it.

use gxr_math::{exp2, log2, log2f, powff};
use gxr_raw::{CoreError, Json};
use libm::tanh;

use crate::error::unsupported;
use crate::fft::phase_shift;
use crate::filter::{Plane, gaussian_filter};
use crate::number::py_round;
use crate::sample::map_coordinates;

const LUMA_R: f32 = 0.2126;
const LUMA_G: f32 = 0.7152;
const LUMA_B: f32 = 0.0722;
const MIN_CORRELATION: f64 = 0.5;
const MAX_SHIFT: f64 = 0.02;
const TILES: usize = 4;
const TILE_REACH: i64 = 3;
const TILE_TEXTURE: f64 = 0.1;
const TILE_GAIN: f64 = 0.03;
const TILE_MATCH: f64 = 0.8;
const MIN_TILE_CORRELATION: f64 = 0.5;
const SCALES: [f64; 5] = [1.0, 0.99, 1.01, 0.98, 1.02];

/// Port of gxr_studio/hdr/gain.py:55 Params.
#[derive(Clone, Debug)]
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

/// Port of gxr_studio/hdr/gain.py:75 Alignment.
#[derive(Clone, Debug)]
pub struct Alignment {
    pub scale: f64,
    pub dy: i64,
    pub dx: i64,
    pub correlation: f64,
    pub tiles_moved: i64,
    pub shift_frac: f64,
    pub tiles_unverified: i64,
}

impl Alignment {
    pub fn to_json(&self) -> Json {
        Json::object([
            ("scale", Json::Float(self.scale)),
            ("dy", Json::Int(self.dy)),
            ("dx", Json::Int(self.dx)),
            ("correlation", Json::Float(self.correlation)),
            ("tiles_moved", Json::Int(self.tiles_moved)),
            ("shift_frac", Json::Float(self.shift_frac)),
            ("tiles_unverified", Json::Int(self.tiles_unverified)),
        ])
    }
}

fn len2(h: usize, w: usize) -> Result<usize, CoreError> {
    h.checked_mul(w).ok_or_else(|| unsupported("bad_shape", "数组太大"))
}

/// Port of gxr_studio/hdr/gain.py:141 grid_shape.
pub fn grid_shape(h: usize, w: usize, factor: usize) -> Result<(usize, usize), CoreError> {
    if factor == 0 {
        return Err(unsupported("bad_shape", "分块因子是 0"));
    }
    Ok((h.div_ceil(factor), w.div_ceil(factor)))
}

/// Port of gxr_studio/hdr/gain.py:145 block_mean. The mean is float64.
pub fn block_mean(a: &[f64], h: usize, w: usize, factor: usize) -> Result<(Vec<f64>, usize, usize), CoreError> {
    if len2(h, w)? != a.len() || h == 0 || w == 0 {
        return Err(unsupported("bad_shape", "数组长度和形状对不上"));
    }
    let (gh, gw) = grid_shape(h, w, factor)?;
    let count = (factor * factor) as f64;
    let mut out = vec![0.0; len2(gh, gw)?];
    for gy in 0..gh {
        for gx in 0..gw {
            let mut sum = 0.0;
            for iy in 0..factor {
                let yy = if gy * factor + iy >= h { h - 1 } else { gy * factor + iy };
                for ix in 0..factor {
                    let xx = if gx * factor + ix >= w { w - 1 } else { gx * factor + ix };
                    sum += a[yy * w + xx];
                }
            }
            out[gy * gw + gx] = sum / count;
        }
    }
    Ok((out, gh, gw))
}

/// Port of gxr_studio/hdr/gain.py:136 srgb_to_linear.
pub fn srgb_to_linear(rgb: &[u8], h: usize, w: usize) -> Result<Vec<f32>, CoreError> {
    if h.checked_mul(w).and_then(|n| n.checked_mul(3)) != Some(rgb.len()) {
        return Err(unsupported("bad_shape", "RGB 长度和形状对不上"));
    }
    let mut out = Vec::with_capacity(rgb.len());
    for &c in rgb {
        let v = f32::from(c) / 255.0;
        let lin = if v <= 0.04045 {
            v / 12.92
        } else {
            powff((v + 0.055) / 1.055, 2.4)
        };
        out.push(lin);
    }
    Ok(out)
}

/// Port of gxr_studio/hdr/gain.py:153 sdr_luma_grid.
pub fn sdr_luma_grid(rgb: &[u8], h: usize, w: usize, factor: usize) -> Result<(Vec<f64>, usize, usize), CoreError> {
    let lin = srgb_to_linear(rgb, h, w)?;
    let mut y = Vec::with_capacity(lin.len() / 3);
    let mut i = 0;
    while i + 2 < lin.len() {
        let acc = lin[i] * LUMA_R;
        let acc = acc + lin[i + 1] * LUMA_G;
        y.push(f64::from(acc + lin[i + 2] * LUMA_B));
        i += 3;
    }
    block_mean(&y, h, w, factor)
}

fn f32_of(v: f64) -> f32 {
    v as f32
}

/// Port of gxr_studio/hdr/gain.py:167 warp.
pub fn warp(
    a: &[f32],
    src: (usize, usize),
    out: (usize, usize),
    s: f64,
    dy: f64,
    dx: f64,
) -> Result<Vec<f32>, CoreError> {
    let (ah, aw) = src;
    let (out_h, out_w) = out;
    if len2(ah, aw)? != a.len() || ah == 0 || aw == 0 || out_h == 0 || out_w == 0 {
        return Err(unsupported("bad_shape", "无法缩放空图"));
    }
    let zy = f32_of(out_h as f64 / ah as f64 * s);
    let zx = f32_of(out_w as f64 / aw as f64 * s);
    if zy == 0.0 || zx == 0.0 {
        return Err(unsupported("bad_shape", "缩放是 0"));
    }
    let (dy32, dx32) = (f32_of(dy), f32_of(dx));
    let y_mid = f32_of((out_h as f64 - 1.0) / 2.0);
    let x_mid = f32_of((out_w as f64 - 1.0) / 2.0);
    let src_y = f32_of((ah as f64 - 1.0) / 2.0);
    let src_x = f32_of((aw as f64 - 1.0) / 2.0);
    let n = len2(out_h, out_w)?;
    let mut sy = Vec::with_capacity(n);
    let mut sx = Vec::with_capacity(n);
    for r in 0..out_h {
        for c in 0..out_w {
            sy.push(((r as f32 - dy32) - y_mid) / zy + src_y);
            sx.push(((c as f32 - dx32) - x_mid) / zx + src_x);
        }
    }
    match map_coordinates(&Plane::F32(a.to_vec()), ah, aw, &sy, &sx, out_h, out_w)? {
        Plane::F32(v) => Ok(v),
        _ => Err(unsupported("bad_shape", "插值类型不对")),
    }
}

fn mean_std(a: &[f64]) -> (f64, f64) {
    if a.is_empty() {
        return (f64::NAN, f64::NAN);
    }
    let mut sum = 0.0;
    for &v in a {
        sum += v;
    }
    let mean = sum / a.len() as f64;
    let mut var = 0.0;
    for &v in a {
        let d = v - mean;
        var += d * d;
    }
    (mean, (var / a.len() as f64).sqrt())
}

fn norm_of(a: &[f64]) -> Vec<f64> {
    let (mean, std) = mean_std(a);
    a.iter().map(|v| (v - mean) / (std + 1e-6)).collect()
}

fn hp64(a: &[f64], h: usize, w: usize) -> Result<Vec<f64>, CoreError> {
    let logged: Vec<f64> = a.iter().map(|v| log2(v.max(1e-4))).collect();
    let smooth = match gaussian_filter(&Plane::F64(logged.clone()), h, w, 8.0)? {
        Plane::F64(v) => v,
        _ => return Err(unsupported("bad_shape", "高通类型不对")),
    };
    Ok(logged.into_iter().zip(smooth).map(|(v, s)| v - s).collect())
}

fn hp32(a: &[f32], h: usize, w: usize) -> Result<Vec<f64>, CoreError> {
    let logged: Vec<f32> = a.iter().map(|v| log2f(v.max(1e-4))).collect();
    let smooth = match gaussian_filter(&Plane::F32(logged.clone()), h, w, 8.0)? {
        Plane::F32(v) => v,
        _ => return Err(unsupported("bad_shape", "高通类型不对")),
    };
    Ok(logged.into_iter().zip(smooth).map(|(v, s)| f64::from(v - s)).collect())
}

fn mean_product(a: &[f64], b: &[f64]) -> f64 {
    let mut sum = 0.0;
    for i in 0..a.len().min(b.len()) {
        sum += a[i] * b[i];
    }
    if a.is_empty() { f64::NAN } else { sum / a.len() as f64 }
}

fn patch(b: &[f64], bw: usize, y0: i64, x0: i64, th: usize, tw: usize) -> Vec<f64> {
    let mut out = Vec::with_capacity(th * tw);
    for r in 0..th {
        for c in 0..tw {
            let y = (y0 + r as i64) as usize;
            let x = (x0 + c as i64) as usize;
            out.push(b[y * bw + x]);
        }
    }
    out
}

/// Port of gxr_studio/hdr/gain.py:185 `_tile_moved`.
fn tile_moved(ta: &[f64], b: &[f64], bw: usize, y0: usize, x0: usize, th: usize, tw: usize) -> bool {
    let tn = norm_of(ta);
    let here = mean_product(&tn, &norm_of(&patch(b, bw, y0 as i64, x0 as i64, th, tw)));
    let mut best = here;
    let mut far = false;
    for dy in -TILE_REACH..=TILE_REACH {
        for dx in -TILE_REACH..=TILE_REACH {
            let c = mean_product(&tn, &norm_of(&patch(b, bw, y0 as i64 + dy, x0 as i64 + dx, th, tw)));
            if c > best {
                best = c;
                far = dy.abs().max(dx.abs()) >= 1;
            }
        }
    }
    far && ((best >= TILE_MATCH && best - here > TILE_GAIN) || best - here > 0.1)
}

/// Port of gxr_studio/hdr/gain.py:200 `_tiles_moved`.
fn tiles_moved(a: &[f64], b: &[f64], h: usize, w: usize) -> i64 {
    let mut moved = 0i64;
    let r = TILE_REACH as usize;
    if h <= r || w <= r {
        return 0;
    }
    for i in 0..TILES {
        for j in 0..TILES {
            let y0 = r.max(i * h / TILES);
            let y1 = (h - r).min((i + 1) * h / TILES);
            let x0 = r.max(j * w / TILES);
            let x1 = (w - r).min((j + 1) * w / TILES);
            if y1 <= y0 || x1 <= x0 {
                continue;
            }
            let th = y1 - y0;
            let tw = x1 - x0;
            if th * tw < 64 {
                continue;
            }
            let tile = patch(a, w, y0 as i64, x0 as i64, th, tw);
            if mean_std(&tile).1 < TILE_TEXTURE {
                continue;
            }
            moved += i64::from(tile_moved(&tile, b, w, y0, x0, th, tw));
        }
    }
    moved
}

fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    if sorted.len() == 1 {
        return sorted[0];
    }
    let idx = (sorted.len() as f64 - 1.0) * (q / 100.0);
    let lo = idx.floor() as usize;
    let hi = idx.ceil() as usize;
    let frac = idx - lo as f64;
    sorted[lo] * (1.0 - frac) + sorted[hi] * frac
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = values.len();
    if n == 0 {
        return f64::NAN;
    }
    if n % 2 == 1 {
        values[n / 2]
    } else {
        (values[n / 2 - 1] + values[n / 2]) * 0.5
    }
}

/// Port of gxr_studio/hdr/gain.py:322 fit_curve.
pub fn fit_curve(x: &[f64], y: &[f64], valid: &[bool]) -> Result<(Vec<f64>, Vec<f64>), CoreError> {
    let mut xs = Vec::new();
    let mut ys = Vec::new();
    for i in 0..x.len().min(y.len()).min(valid.len()) {
        if valid[i] {
            xs.push(x[i]);
            ys.push(y[i]);
        }
    }
    if xs.len() < 100 {
        return Err(unsupported("no_curve", "可用于拟合的像素太少"));
    }
    let mut order = xs.clone();
    order.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let lo = percentile(&order, 0.5);
    let hi = percentile(&order, 99.95);
    let mut edges = Vec::new();
    let mut e = lo;
    let step = 0.125;
    while e < hi + step {
        edges.push(e);
        e += step;
        if edges.len() > 1_000_000 {
            return Err(unsupported("no_curve", "亮度范围太窄，拟合不出曲线"));
        }
    }
    let mut cx = Vec::new();
    let mut cy = Vec::new();
    let mut bins: Vec<Vec<f64>> = (0..edges.len()).map(|_| Vec::new()).collect();
    for i in 0..xs.len() {
        // np.digitize(..., right=False) is searchsorted side="right".
        let idx = edges.partition_point(|edge| *edge <= xs[i]);
        if idx > 0 && idx < edges.len() {
            bins[idx].push(ys[i]);
        }
    }
    for k in 1..edges.len() {
        if bins[k].len() >= 30 {
            cx.push(0.5 * (edges[k - 1] + edges[k]));
            cy.push(median(&mut bins[k]));
        }
    }
    if cx.len() < 2 {
        return Err(unsupported("no_curve", "亮度范围太窄，拟合不出曲线"));
    }
    let mut acc = f64::NEG_INFINITY;
    for v in &mut cy {
        if *v > acc {
            acc = *v;
        }
        *v = acc;
    }
    if cy.len() >= 5 {
        let mut padded = Vec::with_capacity(cy.len() + 4);
        padded.push(cy[0]);
        padded.push(cy[0]);
        padded.extend_from_slice(&cy);
        let last = cy[cy.len() - 1];
        padded.push(last);
        padded.push(last);
        let mut smooth = Vec::with_capacity(cy.len());
        for i in 0..cy.len() {
            let mut sum = 0.0;
            for k in 0..5 {
                sum += padded[i + k];
            }
            smooth.push(sum / 5.0);
        }
        cy = smooth;
    }
    Ok((cx, cy))
}

fn interp_one(x: f64, xp: &[f64], fp: &[f64], left: f64, right: f64) -> f64 {
    if xp.is_empty() || fp.is_empty() {
        return f64::NAN;
    }
    if x < xp[0] {
        return left;
    }
    if x > xp[xp.len() - 1] {
        return right;
    }
    let lo = xp.partition_point(|v| *v < x);
    if lo == 0 {
        return fp[0];
    }
    if lo >= xp.len() {
        return fp[fp.len() - 1];
    }
    let x0 = xp[lo - 1];
    let x1 = xp[lo];
    let y0 = fp[lo - 1];
    let y1 = fp[lo];
    if x1 == x0 {
        return y0;
    }
    let slope = (y1 - y0) / (x1 - x0);
    y0 + slope * (x - x0)
}

fn fmt_fixed(v: f64, digits: u32) -> String {
    let neg = v.is_sign_negative() && v != 0.0;
    let scale = gxr_math::powi(10.0, digits as i32);
    let rounded = crate::number::round_half_even(v.abs() * scale);
    let ip = rounded.round() as u64;
    let div = 10u64.pow(digits);
    format!(
        "{}{}.{:0width$}",
        if neg { "-" } else { "" },
        ip / div,
        ip % div,
        width = digits as usize
    )
}

/// Port of gxr_studio/hdr/gain.py:273 align.
pub fn align(
    sdr_y: &[f64],
    h: usize,
    w: usize,
    raw_y: &[f32],
    raw_h: usize,
    raw_w: usize,
) -> Result<Alignment, CoreError> {
    if len2(h, w)? != sdr_y.len() || len2(raw_h, raw_w)? != raw_y.len() {
        return Err(unsupported("bad_shape", "亮度平面对不上"));
    }
    let a = norm_of(&hp64(sdr_y, h, w)?);
    let corr_at = |s: f64, dy: i64, dx: i64| -> Result<f64, CoreError> {
        let warped = warp(raw_y, (raw_h, raw_w), (h, w), s, dy as f64, dx as f64)?;
        let hb = norm_of(&hp32(&warped, h, w)?);
        Ok(mean_product(&a, &hb))
    };
    let mut best: Option<(f64, f64, i64, i64)> = None;
    for &s in &SCALES {
        let base = warp(raw_y, (raw_h, raw_w), (h, w), s, 0.0, 0.0)?;
        let template = norm_of(&hp32(&base, h, w)?);
        let (mut dy, mut dx) = phase_shift(&a, &template, h, w)?;
        let mut corr = corr_at(s, dy, dx)?;
        if s == 1.0 && dy == 0 && dx == 0 && corr > 0.6 {
            best = Some((corr, s, dy, dx));
            break;
        }
        let (phase_y, phase_x) = (dy, dx);
        for i in -1..=1 {
            for j in -1..=1 {
                if i == 0 && j == 0 {
                    continue;
                }
                let cand = corr_at(s, phase_y + i, phase_x + j)?;
                if cand > corr {
                    corr = cand;
                    dy = phase_y + i;
                    dx = phase_x + j;
                }
            }
        }
        if best.is_none_or(|b| corr > b.0) {
            best = Some((corr, s, dy, dx));
        }
    }
    let Some((corr, s, dy, dx)) = best else {
        return Err(unsupported("not_aligned", "没有可用的对齐"));
    };
    let registered = warp(raw_y, (raw_h, raw_w), (h, w), s, dy as f64, dx as f64)?;
    let ha = hp64(sdr_y, h, w)?;
    let hb = hp32(&registered, h, w)?;
    let moved = tiles_moved(&ha, &hb, h, w);
    let unverified = tiles_unverified(sdr_y, &registered, h, w)?
        + i64::from(crate::residual::local_residual(sdr_y, &registered, h, w)?);
    let shift_frac = (dy.abs() as f64 / h as f64).max(dx.abs() as f64 / w as f64);
    Ok(Alignment {
        scale: s,
        dy,
        dx,
        correlation: corr,
        tiles_moved: moved,
        shift_frac,
        tiles_unverified: unverified,
    })
}

/// Port of gxr_studio/hdr/gain.py:303 check_alignment.
pub fn check_alignment(al: &Alignment) -> Result<(), CoreError> {
    if al.correlation < MIN_CORRELATION {
        return Err(unsupported(
            "not_aligned",
            &format!("RAW 与 JPG 对不齐（相关系数 {}）", fmt_fixed(al.correlation, 2)),
        ));
    }
    if al.shift_frac > MAX_SHIFT {
        return Err(unsupported(
            "not_aligned",
            &format!("RAW 与 JPG 错开太多（{}, {}）", al.dy, al.dx),
        ));
    }
    if al.tiles_moved != 0 {
        return Err(unsupported(
            "not_aligned",
            &format!("RAW 与 JPG 局部对不齐（{} 块）", al.tiles_moved),
        ));
    }
    if al.tiles_unverified != 0 {
        return Err(unsupported(
            "not_aligned",
            &format!("无法确认 RAW 与 JPG 局部对齐（{} 块）", al.tiles_unverified),
        ));
    }
    Ok(())
}

/// Port of gxr_studio/hdr/gain.py:216 `_tiles_unverified`.
fn tiles_unverified(sdr_y: &[f64], raw_y: &[f32], h: usize, w: usize) -> Result<i64, CoreError> {
    let x: Vec<f64> = raw_y.iter().map(|v| log2(f64::from((*v).max(1e-6)))).collect();
    let y: Vec<f64> = sdr_y.iter().map(|v| log2(v.max(1e-5))).collect();
    let valid: Vec<bool> = sdr_y.iter().map(|v| *v > 0.003 && *v < 0.97).collect();
    let (cx, cy) = match fit_curve(&x, &y, &valid) {
        Ok(curve) => curve,
        Err(err) if err.kind == "no_curve" => return Ok(1),
        Err(err) => return Err(err),
    };
    let a = hp64(sdr_y, h, w)?;
    let predicted: Vec<f64> = x.iter().map(|v| exp2(interp_one(*v, &cx, &cy, cy[0], cy[cy.len() - 1]))).collect();
    let b = hp64(&predicted, h, w)?;
    let mut unverified = 0i64;
    let mut verified = 0i64;
    for i in 0..TILES {
        for j in 0..TILES {
            let y0 = i * h / TILES;
            let y1 = (i + 1) * h / TILES;
            let x0 = j * w / TILES;
            let x1 = (j + 1) * w / TILES;
            let mut ta = Vec::new();
            let mut tb = Vec::new();
            for r in y0..y1 {
                for c in x0..x1 {
                    let s = sdr_y[r * w + c];
                    if s > 0.01 && s < 0.95 {
                        ta.push(a[r * w + c]);
                        tb.push(b[r * w + c]);
                    }
                }
            }
            if ta.is_empty() || mean_std(&ta).1 < TILE_TEXTURE {
                continue;
            }
            if ta.len() < 32 || mean_product(&norm_of(&ta), &norm_of(&tb)) < MIN_TILE_CORRELATION {
                unverified += 1;
            } else {
                verified += 1;
            }
        }
    }
    Ok(if verified == 0 { unverified.max(1) } else { unverified })
}

/// The float32 gain plane and the `gmax` Python `round(..., 4)` stores.
pub struct GainPlane {
    pub delta: Vec<f32>,
    pub gmax: f64,
    pub h: usize,
    pub w: usize,
    pub stats: Json,
}

/// Port of gxr_studio/hdr/gain.py:345 compute_gain, up to the float32 plane and `gmax`.
pub fn compute_gain(
    sdr_y: &[f64],
    raw_y: &[f32],
    clip: &[u8],
    h: usize,
    w: usize,
    p: &Params,
) -> Result<GainPlane, CoreError> {
    if len2(h, w)? != sdr_y.len() || raw_y.len() != sdr_y.len() || clip.len() != sdr_y.len() {
        return Err(unsupported("bad_shape", "增益平面对不上"));
    }
    let photo: Vec<bool> = raw_y.iter().map(|v| v.is_finite()).collect();
    let x: Vec<f64> = raw_y
        .iter()
        .zip(&photo)
        .map(|(v, on)| log2(if *on { f64::from((*v).max(1e-6)) } else { 1.0 }))
        .collect();
    let ylog: Vec<f64> = sdr_y.iter().map(|v| log2(v.max(1e-5))).collect();
    let valid: Vec<bool> = photo
        .iter()
        .zip(clip)
        .zip(sdr_y)
        .map(|((on, c), s)| *on && *c == 0 && *s > 0.003 && *s < 0.97)
        .collect();
    let (cx, cy) = fit_curve(&x, &ylog, &valid)?;
    let mut mid = Vec::new();
    for i in 0..h * w {
        if valid[i] && sdr_y[i] > 0.10 && sdr_y[i] < 0.30 {
            mid.push(ylog[i] - x[i]);
        }
    }
    let ev = if mid.len() > 100 { median(&mut mid) } else { f64::NAN };
    let mut yk = log2(p.knee);
    let xk = if cy.iter().copied().fold(f64::NEG_INFINITY, f64::max) <= yk {
        yk = cy[cy.len() - 1];
        cx[cx.len() - 1]
    } else {
        interp_one(yk, &cy, &cx, cx[0], cx[cx.len() - 1])
    };
    let mut delta = Vec::with_capacity(h * w);
    for i in 0..h * w {
        let t_sdr = interp_one(x[i], &cx, &cy, cy[0], cy[cy.len() - 1]).min(0.0);
        let y_hdr = if x[i] > xk { yk + p.hdr_slope * (x[i] - xk) } else { t_sdr };
        let mut d = (y_hdr - t_sdr).max(0.0);
        d = p.max_stops * tanh(d / p.max_stops);
        delta.push(if photo[i] { d } else { 0.0 });
    }
    let narrow: Vec<f32> = delta.iter().map(|v| *v as f32).collect();
    let smooth = match gaussian_filter(&Plane::F32(narrow), h, w, 0.6)? {
        Plane::F32(v) => v,
        _ => return Err(unsupported("bad_shape", "平滑类型不对")),
    };
    let mut out = Vec::with_capacity(h * w);
    let mut gt01 = 0.0f64;
    let mut gt1 = 0.0f64;
    for i in 0..h * w {
        let span = p.gate_hi - p.gate_lo;
        let mut gate = ((sdr_y[i] - p.gate_lo) / span).clamp(0.0, 1.0);
        gate = gate * gate * (3.0 - 2.0 * gate);
        let value = (f64::from(smooth[i]) * gate) * if photo[i] { 1.0 } else { 0.0 };
        let stored = value as f32;
        if stored > 0.1 {
            gt01 += 1.0;
        }
        if stored > 1.0 {
            gt1 += 1.0;
        }
        out.push(stored);
    }
    let mut peak = 0.05f64;
    for &v in &out {
        peak = peak.max(f64::from(v));
    }
    let gmax = py_round(peak, 4)?;
    let n = (h * w) as f64;
    let clipped = if photo.iter().any(|v| *v) {
        let mut num = 0.0;
        let mut den = 0.0;
        for i in 0..photo.len() {
            if photo[i] {
                den += 1.0;
                if clip[i] != 0 {
                    num += 1.0;
                }
            }
        }
        num / den
    } else {
        0.0
    };
    let stats = Json::object([
        ("gainmap_wh", Json::ints(&[w as i64, h as i64])),
        ("raw_clipped_frac", Json::Float(py_round(clipped, 4)?)),
        ("ev_raw_to_sdr_mid", if ev.is_nan() { Json::Null } else { Json::Float(py_round(ev, 3)?) }),
        ("knee_raw_log2", Json::Float(py_round(xk, 3)?)),
        ("gain_max_stops", Json::Float(py_round(gmax, 3)?)),
        ("frac_gain_gt_0p1_stop", Json::Float(py_round(gt01 / n, 4)?)),
        ("frac_gain_gt_1_stop", Json::Float(py_round(gt1 / n, 4)?)),
        ("gmax", Json::Float(gmax)),
    ]);
    Ok(GainPlane { delta: out, gmax, h, w, stats })
}

/// Port of the quantisation in gxr_studio/hdr/container.py:198 encode_gainmap.
/// `floor(clip((delta - min) / (max - min), 0, 1) ** gamma * 255 + 0.5)`.
pub fn quantise_gain(delta: &[f32], gain_min: f64, gain_max: f64, gamma: f64) -> Result<Vec<u8>, CoreError> {
    let span = gain_max - gain_min;
    if span == 0.0 || !span.is_finite() {
        return Err(unsupported("bad_meta", "增益范围是 0"));
    }
    let mut out = Vec::with_capacity(delta.len());
    for &d in delta {
        let r = ((f64::from(d) - gain_min) / span).clamp(0.0, 1.0);
        let powered = if gamma == 1.0 { r } else { gxr_math::powf(r, gamma) };
        let code = (powered * 255.0 + 0.5).floor();
        let code = if code <= 0.0 {
            0
        } else if code >= 255.0 {
            255
        } else {
            code as u8
        };
        out.push(code);
    }
    Ok(out)
}
