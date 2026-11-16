//! Port of `gxr_studio/finish/hdr_export.py` (`_to_grid`, `finish`).

use std::collections::VecDeque;

use gxr_finish::geometry::bounds::{bad_geometry, checked_mask_dims, fit_count, too_big};
use gxr_finish::geometry::checked_float_layer_slice;
use gxr_hdr::{
    Params, compute_gain, ensure_finite, grid_shape, is_unsupported, py_round, quantise_gain,
    sdr_luma_grid,
};
use gxr_jpeg::encode_luma_jpeg;
use gxr_raw::{CoreError, Json};

use super::frame::FramedTrack;
use super::track::{HdrTrackState, NO_RAW, checked_rgb_bytes};

/// Failure reason when export format is not `"jpeg"`.
pub const NOT_JPEG: &str = "not_jpeg";

const PW_BLOCKSIZE: usize = 128;

#[allow(clippy::cast_possible_truncation)]
const W_PHOTO_THRESH: f32 = 0.999_f64 as f32;
#[allow(clippy::cast_possible_truncation)]
const W_MIN_EPS: f32 = 1e-6_f64 as f32;
#[allow(clippy::cast_possible_truncation)]
const CLIP_THRESH: f32 = 0.01_f64 as f32;

/// Output of `_to_grid`: `(raw_y, clip)` on the gain grid `(gh, gw)`.
#[derive(Clone, Debug, PartialEq)]
pub struct ToGridOutput {
    pub raw_y: Vec<f32>,
    pub clip: Vec<bool>,
    pub gh: usize,
    pub gw: usize,
}

/// Exact port of `gxr_hdr::np::pairwise` (`numpy @TYPE@_pairwise_sum`) for `f32`.
fn pairwise_f32(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        let mut res = -0.0_f32;
        for &v in a {
            res += v;
        }
        res
    } else if n <= PW_BLOCKSIZE {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - n % 8 {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_f32(&a[..n2]) + pairwise_f32(&a[n2..])
    }
}

fn sum_f32(a: &[f32]) -> f32 {
    0.0_f32 + pairwise_f32(a)
}

#[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
fn mean_of_f32(total: f32, n: usize) -> f32 {
    (f64::from(total) / (n as f64)) as f32
}

struct BoxCoeffs {
    ksize: usize,
    bounds: Vec<u32>,
    kk: Vec<f64>,
}

/// Exact port of Pillow `Resample.c:precompute_coeffs` for `Filter::Box`.
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn precompute_box_coeffs(in_size: usize, out_size: usize) -> Result<BoxCoeffs, CoreError> {
    if in_size == 0 || out_size == 0 {
        return Err(CoreError::new(
            "value_error",
            "height and width must be > 0",
        ));
    }
    let scale = f64::from(in_size as f32) / (out_size as f64);
    let filterscale = if scale < 1.0 { 1.0 } else { scale };
    let support = 0.5 * filterscale;
    let ceil_support = support.ceil();
    if !ceil_support.is_finite() || ceil_support < 0.0 || ceil_support > f64::from(i32::MAX / 2) {
        return Err(too_big());
    }
    let ksize_u64 = (ceil_support as u64)
        .checked_mul(2)
        .and_then(|v| v.checked_add(1))
        .ok_or_else(too_big)?;
    let ksize = fit_count(ksize_u64, 1)?;
    let out_u64 = u64::try_from(out_size).map_err(|_| too_big())?;
    let coeffs_len = fit_count(out_u64.checked_mul(ksize_u64).ok_or_else(too_big)?, 8)?;
    let bounds_len = fit_count(out_u64.checked_mul(2).ok_or_else(too_big)?, 4)?;

    let mut kk = Vec::new();
    kk.try_reserve_exact(coeffs_len).map_err(|_| too_big())?;
    kk.resize(coeffs_len, 0.0_f64);
    let mut bounds = Vec::new();
    bounds
        .try_reserve_exact(bounds_len)
        .map_err(|_| too_big())?;
    bounds.resize(bounds_len, 0_u32);

    let ss = 1.0 / filterscale;
    for xx in 0..out_size {
        let center = (xx as f64 + 0.5) * scale;
        let mut ww = 0.0_f64;

        let xmin_raw = (center - support + 0.5) as i64;
        let xmin = if xmin_raw < 0 {
            0_usize
        } else {
            usize::try_from(xmin_raw).unwrap_or(in_size)
        };
        let xmax_raw = (center + support + 0.5) as i64;
        let xmax_end = if xmax_raw < 0 {
            0_usize
        } else {
            usize::try_from(xmax_raw).unwrap_or(in_size).min(in_size)
        };
        let xmax = xmax_end.saturating_sub(xmin).min(ksize);

        let k = &mut kk[xx * ksize..(xx + 1) * ksize];
        for (x, slot) in k[..xmax].iter_mut().enumerate() {
            let d = (((x + xmin) as f64) - center + 0.5) * ss;
            let w = if d > -0.5 && d <= 0.5 { 1.0 } else { 0.0 };
            *slot = w;
            ww += w;
        }
        if ww != 0.0 {
            for slot in &mut k[..xmax] {
                *slot /= ww;
            }
        }
        bounds[xx * 2] = u32::try_from(xmin).map_err(|_| too_big())?;
        bounds[xx * 2 + 1] = u32::try_from(xmax).map_err(|_| too_big())?;
    }

    Ok(BoxCoeffs { ksize, bounds, kk })
}

struct PlacedLayer<'a> {
    layer: &'a [f32],
    lw: usize,
    lh: usize,
    cw: usize,
    px: usize,
    py: usize,
}

impl PlacedLayer<'_> {
    #[inline]
    fn sample(&self, sx: usize, sy: usize) -> [f32; 3] {
        if sy >= self.py && sy < self.py + self.lh && sx >= self.px && sx < self.px + self.lw {
            let idx = ((sy - self.py) * self.lw + (sx - self.px)) * 2;
            let v0 = self.layer[idx];
            if v0.is_finite() {
                return [v0, self.layer[idx + 1], 1.0_f32];
            }
        }
        [0.0_f32, 0.0_f32, 0.0_f32]
    }

    fn fill_raw_row(&self, sy: usize, dst: &mut [[f32; 3]]) {
        if sy < self.py || sy >= self.py + self.lh {
            dst.fill([0.0_f32; 3]);
            return;
        }
        dst[..self.px].fill([0.0_f32; 3]);
        let row = &self.layer[(sy - self.py) * self.lw * 2..][..self.lw * 2];
        for (slot, pair) in dst[self.px..self.px + self.lw]
            .iter_mut()
            .zip(row.chunks_exact(2))
        {
            *slot = if pair[0].is_finite() {
                [pair[0], pair[1], 1.0_f32]
            } else {
                [0.0_f32; 3]
            };
        }
        dst[self.px + self.lw..self.cw].fill([0.0_f32; 3]);
    }

    #[allow(clippy::cast_possible_truncation)]
    fn fill_horiz_row(&self, sy: usize, ch: Option<&BoxCoeffs>, dst: &mut [[f32; 3]]) {
        let Some(c) = ch else {
            self.fill_raw_row(sy, dst);
            return;
        };
        if sy < self.py || sy >= self.py + self.lh {
            dst.fill([0.0_f32; 3]);
            return;
        }
        let photo_row = &self.layer[(sy - self.py) * self.lw * 2..][..self.lw * 2];
        for (xx, slot) in dst.iter_mut().enumerate() {
            let xmin = c.bounds[xx * 2] as usize;
            let xmax = c.bounds[xx * 2 + 1] as usize;
            let k = &c.kk[xx * c.ksize..][..xmax];
            let mut s0 = 0.0_f64;
            let mut s1 = 0.0_f64;
            let mut s2 = 0.0_f64;
            for (x, &weight) in k.iter().enumerate() {
                let sx = x + xmin;
                if sx >= self.px && sx < self.px + self.lw {
                    let idx = (sx - self.px) * 2;
                    let v0 = photo_row[idx];
                    if v0.is_finite() {
                        s0 += f64::from(v0) * weight;
                        s1 += f64::from(photo_row[idx + 1]) * weight;
                        s2 += weight;
                    }
                }
            }
            *slot = [s0 as f32, s1 as f32, s2 as f32];
        }
    }
}

struct GridAccumulator {
    ow: usize,
    oh: usize,
    gw: usize,
    factor: usize,
    n: usize,
    raw_y: Vec<f32>,
    clip: Vec<bool>,
    // gw > 1 accumulators
    acc0: Vec<f32>,
    acc1: Vec<f32>,
    acc2: Vec<f32>,
    row0: Vec<f32>,
    row1: Vec<f32>,
    row2: Vec<f32>,
    // gw == 1 full-block buffers
    blk0: Vec<f32>,
    blk1: Vec<f32>,
    blk2: Vec<f32>,
}

impl GridAccumulator {
    fn new(gh: usize, gw: usize, oh: usize, ow: usize, factor: usize) -> Result<Self, CoreError> {
        let n = gxr_hdr::count(&[factor as u64, factor as u64])?;
        let gn = gxr_hdr::count(&[gh as u64, gw as u64])?;
        let mut raw_y = Vec::new();
        raw_y.try_reserve_exact(gn).map_err(|_| too_big())?;
        let mut clip = Vec::new();
        clip.try_reserve_exact(gn).map_err(|_| too_big())?;

        let (acc_len, row_len, blk_len) = if gw == 1 { (0, 0, n) } else { (gw, factor, 0) };
        let alloc_zero = |len: usize| -> Result<Vec<f32>, CoreError> {
            let mut v = Vec::new();
            v.try_reserve_exact(len).map_err(|_| too_big())?;
            v.resize(len, 0.0_f32);
            Ok(v)
        };
        Ok(Self {
            ow,
            oh,
            gw,
            factor,
            n,
            raw_y,
            clip,
            acc0: alloc_zero(acc_len)?,
            acc1: alloc_zero(acc_len)?,
            acc2: alloc_zero(acc_len)?,
            row0: alloc_zero(row_len)?,
            row1: alloc_zero(row_len)?,
            row2: alloc_zero(row_len)?,
            blk0: alloc_zero(blk_len)?,
            blk1: alloc_zero(blk_len)?,
            blk2: alloc_zero(blk_len)?,
        })
    }

    fn emit_block(&mut self, y_total: f32, c_total: f32, w_total: f32) {
        let y_mean = mean_of_f32(y_total, self.n);
        let c_mean = mean_of_f32(c_total, self.n);
        let w = mean_of_f32(w_total, self.n);
        let denom = if w > W_MIN_EPS { w } else { W_MIN_EPS };
        self.raw_y.push(if w > W_PHOTO_THRESH {
            y_mean / denom
        } else {
            f32::NAN
        });
        self.clip.push((c_mean / denom) > CLIP_THRESH);
    }

    fn push_row(&mut self, oy: usize, out_row: &[[f32; 3]]) {
        let gy = oy / self.factor;
        let iy_base = oy % self.factor;
        let repeat = if oy + 1 == self.oh {
            self.factor - iy_base
        } else {
            1
        };

        if self.gw == 1 {
            for r in 0..repeat {
                let off = (iy_base + r) * self.factor;
                for ix in 0..self.factor {
                    let px = out_row[ix.min(self.ow - 1)];
                    self.blk0[off + ix] = px[0];
                    self.blk1[off + ix] = px[1];
                    self.blk2[off + ix] = px[2];
                }
            }
            if iy_base + repeat == self.factor {
                let yt = sum_f32(&self.blk0);
                let ct = sum_f32(&self.blk1);
                let wt = sum_f32(&self.blk2);
                self.emit_block(yt, ct, wt);
            }
            return;
        }

        for _ in 0..repeat {
            for gx in 0..self.gw {
                let x_base = gx * self.factor;
                for ix in 0..self.factor {
                    let px = out_row[(x_base + ix).min(self.ow - 1)];
                    self.row0[ix] = px[0];
                    self.row1[ix] = px[1];
                    self.row2[ix] = px[2];
                }
                self.acc0[gx] += pairwise_f32(&self.row0);
                self.acc1[gx] += pairwise_f32(&self.row1);
                self.acc2[gx] += pairwise_f32(&self.row2);
            }
        }
        let _ = gy;
        if iy_base + repeat == self.factor {
            for gx in 0..self.gw {
                let (yt, ct, wt) = (self.acc0[gx], self.acc1[gx], self.acc2[gx]);
                self.emit_block(yt, ct, wt);
            }
            self.acc0.fill(0.0_f32);
            self.acc1.fill(0.0_f32);
            self.acc2.fill(0.0_f32);
        }
    }
}

/// Streaming port of `frame_track` + `gxr_studio/finish/hdr_export.py:135 _to_grid` directly
/// from a photo-sized `(lh, lw, 2)` layer placed at `(px, py)` inside `(cw, ch)`.
///
/// Produces bit-for-bit identical `(raw_y, clip)` to materialising the full canvas and calling
/// `to_grid`, without allocating the full-canvas layer or the three full-size `f32` planes.
#[allow(clippy::too_many_arguments, clippy::cast_possible_truncation)]
pub fn to_grid_placed(
    layer: &[f32],
    lw: u64,
    lh: u64,
    cw: u64,
    ch: u64,
    px: usize,
    py: usize,
    out_w: u64,
    out_h: u64,
    factor: usize,
) -> Result<ToGridOutput, CoreError> {
    let (lwu, lhu, _, _) = checked_float_layer_slice(layer, lw, lh, 2)?;
    let (cwu, chu, _) = checked_mask_dims(cw, ch)?;
    let (owu, ohu, _) = checked_mask_dims(out_w, out_h)?;
    if factor == 0
        || px.checked_add(lwu).is_none_or(|e| e > cwu)
        || py.checked_add(lhu).is_none_or(|e| e > chu)
    {
        return Err(bad_geometry());
    }
    if (cwu, chu) != (owu, ohu) {
        let _ = fit_count(cw.checked_mul(ch).ok_or_else(too_big)?, 4)?;
        let _ = fit_count(out_w.checked_mul(out_h).ok_or_else(too_big)?, 4)?;
    }

    let (gh, gw) = grid_shape(ohu, owu, factor)?;
    gxr_hdr::count(&[gh as u64, factor as u64, gw as u64, factor as u64])?;
    let mut grid = GridAccumulator::new(gh, gw, ohu, owu, factor)?;
    let placed = PlacedLayer {
        layer,
        lw: lwu,
        lh: lhu,
        cw: cwu,
        px,
        py,
    };

    let mut out_row = Vec::new();
    out_row.try_reserve_exact(owu).map_err(|_| too_big())?;
    out_row.resize(owu, [0.0_f32; 3]);

    // Tall-narrow downscale order matches Pillow Image.py:2431-2437 (vertical pass then horizontal).
    if (cwu, chu) != (owu, ohu) && chu > cwu.saturating_mul(100) && ohu < chu {
        let c_vert = precompute_box_coeffs(chu, ohu)?;
        let c_horiz = (owu != cwu)
            .then(|| precompute_box_coeffs(cwu, owu))
            .transpose()?;
        let mut vert_row = Vec::new();
        vert_row.try_reserve_exact(cwu).map_err(|_| too_big())?;
        vert_row.resize(cwu, [0.0_f32; 3]);

        for oy in 0..ohu {
            let ymin = c_vert.bounds[oy * 2] as usize;
            let ymax = c_vert.bounds[oy * 2 + 1] as usize;
            let kv = &c_vert.kk[oy * c_vert.ksize..][..ymax];
            for (sx, slot) in vert_row.iter_mut().enumerate() {
                let mut s0 = 0.0_f64;
                let mut s1 = 0.0_f64;
                let mut s2 = 0.0_f64;
                for (y, &weight) in kv.iter().enumerate() {
                    let sample = placed.sample(sx, y + ymin);
                    s0 += f64::from(sample[0]) * weight;
                    s1 += f64::from(sample[1]) * weight;
                    s2 += f64::from(sample[2]) * weight;
                }
                *slot = [s0 as f32, s1 as f32, s2 as f32];
            }
            if let Some(ch) = c_horiz.as_ref() {
                for (xx, slot) in out_row.iter_mut().enumerate() {
                    let xmin = ch.bounds[xx * 2] as usize;
                    let xmax = ch.bounds[xx * 2 + 1] as usize;
                    let kh = &ch.kk[xx * ch.ksize..][..xmax];
                    let mut s0 = 0.0_f64;
                    let mut s1 = 0.0_f64;
                    let mut s2 = 0.0_f64;
                    for (x, &weight) in kh.iter().enumerate() {
                        let sample = vert_row[x + xmin];
                        s0 += f64::from(sample[0]) * weight;
                        s1 += f64::from(sample[1]) * weight;
                        s2 += f64::from(sample[2]) * weight;
                    }
                    *slot = [s0 as f32, s1 as f32, s2 as f32];
                }
            } else {
                out_row.copy_from_slice(&vert_row);
            }
            grid.push_row(oy, &out_row);
        }
    } else {
        let c_horiz = (owu != cwu)
            .then(|| precompute_box_coeffs(cwu, owu))
            .transpose()?;
        let c_vert = (ohu != chu)
            .then(|| precompute_box_coeffs(chu, ohu))
            .transpose()?;

        if let Some(cv) = c_vert.as_ref() {
            let mut ring: VecDeque<(usize, Vec<[f32; 3]>)> = VecDeque::new();
            let mut free_bufs: Vec<Vec<[f32; 3]>> = Vec::new();
            for oy in 0..ohu {
                let ymin = cv.bounds[oy * 2] as usize;
                let ymax = cv.bounds[oy * 2 + 1] as usize;
                while let Some(&(sy, _)) = ring.front() {
                    if sy < ymin {
                        if let Some((_, buf)) = ring.pop_front() {
                            free_bufs.push(buf);
                        }
                    } else {
                        break;
                    }
                }
                if ring.front().is_some_and(|&(sy, _)| sy > ymin) {
                    while let Some((_, buf)) = ring.pop_front() {
                        free_bufs.push(buf);
                    }
                }
                for sy in ymin..ymin + ymax {
                    if ring.back().is_none_or(|&(last, _)| sy > last) {
                        let mut buf = match free_bufs.pop() {
                            Some(b) => b,
                            None => {
                                let mut b = Vec::new();
                                b.try_reserve_exact(owu).map_err(|_| too_big())?;
                                b.resize(owu, [0.0_f32; 3]);
                                b
                            }
                        };
                        placed.fill_horiz_row(sy, c_horiz.as_ref(), &mut buf);
                        ring.push_back((sy, buf));
                    }
                }
                let kv = &cv.kk[oy * cv.ksize..][..ymax];
                for (xx, slot) in out_row.iter_mut().enumerate() {
                    let mut s0 = 0.0_f64;
                    let mut s1 = 0.0_f64;
                    let mut s2 = 0.0_f64;
                    for (y, &weight) in kv.iter().enumerate() {
                        let sample = ring[y].1[xx];
                        s0 += f64::from(sample[0]) * weight;
                        s1 += f64::from(sample[1]) * weight;
                        s2 += f64::from(sample[2]) * weight;
                    }
                    *slot = [s0 as f32, s1 as f32, s2 as f32];
                }
                grid.push_row(oy, &out_row);
            }
        } else {
            for oy in 0..ohu {
                placed.fill_horiz_row(oy, c_horiz.as_ref(), &mut out_row);
                grid.push_row(oy, &out_row);
            }
        }
    }

    Ok(ToGridOutput {
        raw_y: grid.raw_y,
        clip: grid.clip,
        gh,
        gw,
    })
}

/// Port of `gxr_studio/finish/hdr_export.py:135 _to_grid`.
pub fn to_grid(
    layer: &[f32],
    lw: u64,
    lh: u64,
    out_w: u64,
    out_h: u64,
    factor: usize,
) -> Result<ToGridOutput, CoreError> {
    to_grid_placed(layer, lw, lh, lw, lh, 0, 0, out_w, out_h, factor)
}

/// Final result of `hdr_export.finish`.
#[derive(Clone, Debug, PartialEq)]
pub enum FinishOutput {
    Skipped { bytes: Vec<u8>, reason: String },
    Hdr { bytes: Vec<u8>, gain_max_stops: f64 },
}

impl FinishOutput {
    pub fn bytes(&self) -> &[u8] {
        match self {
            Self::Skipped { bytes, .. } | Self::Hdr { bytes, .. } => bytes,
        }
    }

    pub fn report_json(&self) -> Result<Json, CoreError> {
        let json = match self {
            Self::Skipped { reason, .. } => {
                Json::object([("hdr", Json::Bool(false)), ("reason", Json::str(reason))])
            }
            Self::Hdr { gain_max_stops, .. } => Json::object([
                ("hdr", Json::Bool(true)),
                ("gain_max_stops", Json::Float(*gain_max_stops)),
            ]),
        };
        ensure_finite(&json)?;
        Ok(json)
    }
}

fn finish_from_grid(
    payload: &[u8],
    grid: &ToGridOutput,
    rgb: &[u8],
    wu: usize,
    hu: usize,
    factor: usize,
    params: &Params,
) -> Result<FinishOutput, CoreError> {
    let sdr = sdr_luma_grid(rgb, hu, wu, factor)?;
    let result = match compute_gain(&sdr.v, &grid.raw_y, &grid.clip, grid.gh, grid.gw, params) {
        Ok(r) => r,
        Err(e) if is_unsupported(&e) => {
            return Ok(FinishOutput::Skipped {
                bytes: payload.to_vec(),
                reason: e.kind.to_string(),
            });
        }
        Err(e) => return Err(e),
    };
    let codes = quantise_gain(&result.delta, &result.meta);
    let gw_u32 = u32::try_from(grid.gw).map_err(|_| bad_geometry())?;
    let gh_u32 = u32::try_from(grid.gh).map_err(|_| bad_geometry())?;
    let q_i32 = i32::try_from(params.quality)
        .map_err(|_| CoreError::new("bad_quality", "品质要在 1 到 100 之间。"))?;
    let gain_jpeg = encode_luma_jpeg(&codes, gw_u32, gh_u32, q_i32)?;
    let gain_max_stops = py_round(result.meta.gain_max, 3)?;

    match gxr_hdr::assemble(payload, &gain_jpeg, &result.meta) {
        Ok(bytes) => Ok(FinishOutput::Hdr {
            bytes,
            gain_max_stops,
        }),
        Err(e) if e.kind == "container" => Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: "container".to_string(),
        }),
        Err(e) => Err(e),
    }
}

/// Streaming equivalent of [`finish`] taking a [`FramedTrack`] without ever materialising the
/// full-canvas layer or the three full-size float32 planes, and dropping the photo HDR layer
/// before `sdr_luma_grid` allocates.
pub fn finish_placed(
    payload: &[u8],
    mut framed: FramedTrack,
    fmt: &str,
    rgb: &[u8],
    w: u64,
    h: u64,
    params: &Params,
) -> Result<FinishOutput, CoreError> {
    let dims = framed.state.validate()?;
    let (wu, hu) = checked_rgb_bytes(rgb, w, h)?;
    let factor = usize::try_from(params.map_factor)
        .ok()
        .filter(|&f| f > 0)
        .ok_or_else(bad_geometry)?;

    if fmt != "jpeg" {
        return Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: NOT_JPEG.to_string(),
        });
    }
    let Some((lh, lw)) = dims else {
        return Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: framed
                .state
                .problem
                .clone()
                .unwrap_or_else(|| NO_RAW.to_string()),
        });
    };

    let (cw, ch, px, py) = match framed.placement {
        Some(p) => (p.canvas_w as u64, p.canvas_h as u64, p.photo_x, p.photo_y),
        None => (lw as u64, lh as u64, 0, 0),
    };
    let grid = to_grid_placed(
        &framed.state.layer,
        lw as u64,
        lh as u64,
        cw,
        ch,
        px,
        py,
        w,
        h,
        factor,
    )?;
    // Release the photo HDR layer before `sdr_luma_grid` allocates its canvas-sized float32 buffer.
    framed.state.layer = Vec::new();
    drop(framed);
    finish_from_grid(payload, &grid, rgb, wu, hu, factor, params)
}

/// Port of `gxr_studio/finish/hdr_export.py:147 finish`: validates format/track, runs
/// `_to_grid`, `compute_gain`, `quantise_gain`, encodes the gainmap as grayscale JPEG via
/// `gxr_jpeg::encode_luma_jpeg`, and wraps `payload` with `gxr_hdr::assemble`.
pub fn finish(
    payload: &[u8],
    state: &HdrTrackState,
    fmt: &str,
    rgb: &[u8],
    w: u64,
    h: u64,
    params: &Params,
) -> Result<FinishOutput, CoreError> {
    let dims = state.validate()?;
    let (wu, hu) = checked_rgb_bytes(rgb, w, h)?;
    let factor = usize::try_from(params.map_factor)
        .ok()
        .filter(|&f| f > 0)
        .ok_or_else(bad_geometry)?;

    if fmt != "jpeg" {
        return Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: NOT_JPEG.to_string(),
        });
    }
    let Some((lh, lw)) = dims else {
        return Ok(FinishOutput::Skipped {
            bytes: payload.to_vec(),
            reason: state.problem.clone().unwrap_or_else(|| NO_RAW.to_string()),
        });
    };

    let grid = to_grid(&state.layer, lw as u64, lh as u64, w, h, factor)?;
    finish_from_grid(payload, &grid, rgb, wu, hu, factor, params)
}

#[cfg(test)]
#[path = "finish_tests.rs"]
mod tests;
