//! Port of `gxr_studio/hdr/gain.py`: luma grids, alignment and registration.
//!
//! The curve fit and the gain plane are in `curve.rs`, the continuous residual check in
//! `residual.rs`. dtypes follow numpy 2 (NEP 50): `sdr_y` and `raw_y` are float32, and a
//! Python float next to a float32 array is cast to float32 before the operation.

use gxr_raw::{CODE_MAX, CoreError, Dng, Json, orient_array};

use crate::curve::fit_curve;
use crate::elem::{exp2, log2, log2f, powf32};
use crate::error::unsupported;
use crate::fft::phase_shift;
use crate::filter::{check_shape, gaussian_filter};
use crate::np::{Real, View, count_f64, interp, maximum, mean, mean_of, pairwise, std, std_view, sum};
use crate::residual::local_residual;
use crate::sample::map_coordinates;

/// `LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)`.
pub const LUMA: [f32; 3] = [0.2126_f64 as f32, 0.7152_f64 as f32, 0.0722_f64 as f32];
/// `RAW_WEIGHTS`.
pub const RAW_WEIGHTS: [f32; 3] = [0.25, 0.5, 0.25];
const MIN_CORRELATION: f64 = 0.5;
const MAX_SHIFT: f64 = 0.02;
const TILES: i64 = 4;
const TILE_REACH: i64 = 3;
const TILE_TEXTURE: f64 = 0.1;
const TILE_GAIN: f64 = 0.03;
const TILE_MATCH: f64 = 0.8;
const MIN_TILE_CORRELATION: f64 = 0.5;
const SCALES: [f64; 5] = [1.0, 0.99, 1.01, 0.98, 1.02];

/// Python float → float32, as NEP 50 casts a weak scalar.
#[allow(clippy::cast_possible_truncation)]
pub const fn f32_of(v: f64) -> f32 {
    v as f32
}

/// Port of gxr_studio/hdr/gain.py:75 Alignment.
#[derive(Clone, Debug, PartialEq)]
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
    /// Every dataclass field, for parity (not `to_dict`, which rounds).
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

/// A row-major float32 plane.
#[derive(Clone, Debug, PartialEq)]
pub struct Plane32 {
    pub h: usize,
    pub w: usize,
    pub v: Vec<f32>,
}

fn bad(message: &str) -> CoreError {
    unsupported("bad_input", message)
}

/// Port of gxr_studio/hdr/gain.py:141 grid_shape (`math.ceil(h / factor)`).
pub fn grid_shape(h: usize, w: usize, factor: usize) -> Result<(usize, usize), CoreError> {
    if factor == 0 {
        return Err(bad("division by zero"));
    }
    Ok((h.div_ceil(factor), w.div_ceil(factor)))
}

/// Port of gxr_studio/hdr/gain.py:145 block_mean on a 2-D plane.
///
/// `np.pad(mode="edge")`, then `reshape(gh, f, gw, f).mean(axis=(1, 3))`. numpy reduces
/// axis 3 innermost (one pairwise sum of `f` values per block row) and adds the block rows
/// in order from `0`. With one block column the two axes are adjacent in memory and numpy
/// sums all `f * f` values of a block in one pairwise sum. The mean divides by `intp(f*f)`.
pub fn block_mean<T: Real>(a: &[T], h: usize, w: usize, factor: usize) -> Result<(Vec<T>, usize, usize), CoreError> {
    check_shape(h, w, a.len())?;
    let (gh, gw) = grid_shape(h, w, factor)?;
    if h == 0 || w == 0 {
        return Ok((Vec::new(), gh, gw));
    }
    let n = factor.checked_mul(factor).ok_or_else(|| bad("分块太大"))?;
    let at = |y: usize, x: usize| a[y.min(h - 1) * w + x.min(w - 1)];
    let mut out = Vec::with_capacity(gh * gw);
    let mut row = vec![T::ZERO; factor];
    let mut block = vec![T::ZERO; if gw == 1 { n } else { 0 }];
    for gy in 0..gh {
        for gx in 0..gw {
            let total = if gw == 1 {
                for iy in 0..factor {
                    for ix in 0..factor {
                        block[iy * factor + ix] = at(gy * factor + iy, gx * factor + ix);
                    }
                }
                sum(&block)
            } else {
                let mut acc = T::ZERO;
                for iy in 0..factor {
                    for (ix, slot) in row.iter_mut().enumerate() {
                        *slot = at(gy * factor + iy, gx * factor + ix);
                    }
                    acc = acc + pairwise(&row);
                }
                acc
            };
            out.push(mean_of(total, n));
        }
    }
    Ok((out, gh, gw))
}

/// Port of gxr_studio/hdr/gain.py:136 srgb_to_linear (uint8 in, float32 out).
pub fn srgb_to_linear(c: u8) -> f32 {
    let v = f32::from(c) / f32_of(255.0);
    if v <= f32_of(0.04045) {
        v / f32_of(12.92)
    } else {
        powf32((v + f32_of(0.055)) / f32_of(1.055), f32_of(2.4))
    }
}

/// Port of gxr_studio/hdr/gain.py:153 sdr_luma_grid: `(h, w, 3)` uint8 → float32 grid.
///
/// `@ LUMA` is written as `(r * l0 + g * l1) + b * l2` in float32.
pub fn sdr_luma_grid(rgb: &[u8], h: usize, w: usize, factor: usize) -> Result<Plane32, CoreError> {
    let n = h.checked_mul(w).ok_or_else(|| bad("数组太大"))?;
    if n.checked_mul(3) != Some(rgb.len()) {
        return Err(bad("RGB 长度和形状对不上"));
    }
    let lut: Vec<f32> = (0..=255u8).map(srgb_to_linear).collect();
    let y: Vec<f32> = rgb
        .chunks_exact(3)
        .map(|p| {
            let (r, g, b) = (lut[usize::from(p[0])], lut[usize::from(p[1])], lut[usize::from(p[2])]);
            r * LUMA[0] + g * LUMA[1] + b * LUMA[2]
        })
        .collect();
    let (v, gh, gw) = block_mean(&y, h, w, factor)?;
    Ok(Plane32 { h: gh, w: gw, v })
}

/// Port of gxr_studio/hdr/gain.py:102 raw_luma, without the capture metadata (see
/// `capture.rs`): white-balanced half-resolution luma and the RAW clip mask, both upright.
pub fn raw_luma(dng: &mut Dng<'_>) -> Result<(Plane32, Vec<bool>), CoreError> {
    let result = raw_parts(dng);
    dng.release();
    let (codes, clip) = result?;
    let scale = f32_of(CODE_MAX);
    let y: Vec<f32> = codes
        .values
        .chunks_exact(3)
        .map(|c| {
            let v = (c[0] / scale) * RAW_WEIGHTS[0] + (c[1] / scale) * RAW_WEIGHTS[1] + (c[2] / scale) * RAW_WEIGHTS[2];
            maximum(v, f32_of(1e-6))
        })
        .collect();
    let y_img = gxr_raw::Image {
        height: codes.height,
        width: codes.width,
        channels: 1,
        values: y,
    };
    let clip_img = gxr_raw::Image {
        height: codes.height,
        width: codes.width,
        channels: 1,
        values: clip,
    };
    let y_up = orient_array(&y_img, dng.orientation)?;
    let clip_up = orient_array(&clip_img, dng.orientation)?;
    Ok((
        Plane32 {
            h: y_up.height,
            w: y_up.width,
            v: y_up.values,
        },
        clip_up.values,
    ))
}

type RawParts = (gxr_raw::Image<f32>, Vec<bool>);

fn raw_parts(dng: &Dng<'_>) -> Result<RawParts, CoreError> {
    let codes = gxr_raw::half_res_codes(dng)?;
    let (h2, w2) = (codes.height, codes.width);
    let crop = dng.crop_mosaic()?;
    if crop.height < 2 * h2 || crop.width < 2 * w2 {
        return Err(bad("裁切区域比半分辨率小"));
    }
    // `uint16 >= white * 0.98` compares in float64 (an integer array and a Python float).
    let limit = dng.white * 0.98;
    let mut clip = vec![false; h2 * w2];
    for y in 0..2 * h2 {
        for x in 0..2 * w2 {
            if f64::from(crop.values[y * crop.width + x]) >= limit {
                clip[(y / 2) * w2 + x / 2] = true;
            }
        }
    }
    Ok((codes, clip))
}

/// Port of gxr_studio/hdr/gain.py:158 `_hp` on a float32 plane.
pub fn hp32(a: &[f32], h: usize, w: usize) -> Result<Vec<f32>, CoreError> {
    let logged: Vec<f32> = a.iter().map(|&v| log2f(maximum(v, f32_of(1e-4)))).collect();
    let smooth = gaussian_filter(&logged, h, w, 8.0)?;
    Ok(logged.iter().zip(&smooth).map(|(&l, &s)| l - s).collect())
}

/// Port of gxr_studio/hdr/gain.py:158 `_hp` on a float64 plane.
pub fn hp64(a: &[f64], h: usize, w: usize) -> Result<Vec<f64>, CoreError> {
    let logged: Vec<f64> = a.iter().map(|&v| log2(maximum(v, 1e-4))).collect();
    let smooth = gaussian_filter(&logged, h, w, 8.0)?;
    Ok(logged.iter().zip(&smooth).map(|(&l, &s)| l - s).collect())
}

/// Port of gxr_studio/hdr/gain.py:163 `_norm` of a contiguous array.
pub fn norm<T: Real>(a: &[T]) -> Vec<T> {
    let m = mean(a);
    let d = std(a) + T::narrow(1e-6);
    a.iter().map(|&v| (v - m) / d).collect()
}

/// `_norm` of a view; the result is a new C-order array.
fn norm_view(src: &[f32], v: View) -> Vec<f32> {
    let m = crate::np::mean_view(src, v);
    let d = std_view(src, v) + f32_of(1e-6);
    v.copy(src).iter().map(|&x| (x - m) / d).collect()
}

/// `float(np.mean(a * b))` for two float32 arrays.
fn mean_product(a: &[f32], b: &[f32]) -> f64 {
    let p: Vec<f32> = a.iter().zip(b).map(|(&x, &y)| x * y).collect();
    f64::from(mean(&p))
}

/// Port of gxr_studio/hdr/gain.py:167 warp (`order=1`).
///
/// Coordinates are float32: `(yy - dy - (h - 1) / 2) / zy + (a.h - 1) / 2`, every Python
/// float cast to float32, `zy = h / a.h * s` computed in float64 first.
pub fn warp(a: &Plane32, shape: (usize, usize), s: f64, dy: f64, dx: f64) -> Result<Vec<f32>, CoreError> {
    let (h, w) = shape;
    check_shape(a.h, a.w, a.v.len())?;
    let n = h.checked_mul(w).ok_or_else(|| bad("数组太大"))?;
    let zy = f32_of(count_f64(h) / count_f64(a.h) * s);
    let zx = f32_of(count_f64(w) / count_f64(a.w) * s);
    let (dy, dx) = (f32_of(dy), f32_of(dx));
    let (cy, cx) = (f32_of((count_f64(h) - 1.0) / 2.0), f32_of((count_f64(w) - 1.0) / 2.0));
    let (ay, ax) = (f32_of((count_f64(a.h) - 1.0) / 2.0), f32_of((count_f64(a.w) - 1.0) / 2.0));
    let mut sy = Vec::with_capacity(n);
    let mut sx = Vec::with_capacity(n);
    for r in 0..h {
        #[allow(clippy::cast_precision_loss)]
        let yy = r as f32;
        for c in 0..w {
            #[allow(clippy::cast_precision_loss)]
            let xx = c as f32;
            sy.push((yy - dy - cy) / zy + ay);
            sx.push((xx - dx - cx) / zx + ax);
        }
    }
    map_coordinates(&a.v, a.h, a.w, &sy, &sx)
}

fn to_i64(v: usize) -> i64 {
    i64::try_from(v).unwrap_or(i64::MAX)
}

fn idx(v: i64) -> usize {
    usize::try_from(v).unwrap_or(0)
}

/// Port of gxr_studio/hdr/gain.py:185 `_tile_moved`.
fn tile_moved(a: &[f32], b: &[f32], tile: View) -> bool {
    let tn = norm_view(a, tile);
    let at = |dy: i64, dx: i64| View {
        y0: idx(to_i64(tile.y0) + dy),
        x0: idx(to_i64(tile.x0) + dx),
        ..tile
    };
    let here = mean_product(&tn, &norm_view(b, at(0, 0)));
    let mut best = here;
    let mut far = false;
    for dy in -TILE_REACH..=TILE_REACH {
        for dx in -TILE_REACH..=TILE_REACH {
            let c = mean_product(&tn, &norm_view(b, at(dy, dx)));
            if c > best {
                best = c;
                far = dy.abs().max(dx.abs()) >= 1;
            }
        }
    }
    far && ((best >= TILE_MATCH && best - here > TILE_GAIN) || best - here > 0.1)
}

/// Python slice bounds `[lo, hi)` clipped to `0 .. n` (negative ends never occur here
/// except `h - r < 0`, which leaves the slice empty either way).
fn span(lo: i64, hi: i64, n: usize) -> (usize, usize) {
    let n = to_i64(n);
    let hi = if hi < 0 { (hi + n).max(0) } else { hi.min(n) };
    let lo = lo.clamp(0, n);
    (idx(lo), idx(hi.max(lo)))
}

/// Port of gxr_studio/hdr/gain.py:200 `_tiles_moved`.
fn tiles_moved(a: &[f32], b: &[f32], h: usize, w: usize) -> i64 {
    let (hh, ww) = (to_i64(h), to_i64(w));
    let r = TILE_REACH;
    let mut moved = 0;
    for i in 0..TILES {
        for j in 0..TILES {
            let (y0, y1) = span(r.max(i * hh / TILES), (hh - r).min((i + 1) * hh / TILES), h);
            let (x0, x1) = span(r.max(j * ww / TILES), (ww - r).min((j + 1) * ww / TILES), w);
            let tile = View {
                y0,
                x0,
                h: y1 - y0,
                w: x1 - x0,
                stride: w,
            };
            if tile.len() < 64 || f64::from(std_view(a, tile)) < TILE_TEXTURE {
                continue;
            }
            moved += i64::from(tile_moved(a, b, tile));
        }
    }
    moved
}

/// Port of gxr_studio/hdr/gain.py:216 `_tiles_unverified`.
fn tiles_unverified(sdr: &Plane32, raw: &[f32]) -> Result<i64, CoreError> {
    let (h, w) = (sdr.h, sdr.w);
    let x: Vec<f32> = raw.iter().map(|&v| log2f(maximum(v, f32_of(1e-6)))).collect();
    let y: Vec<f32> = sdr.v.iter().map(|&v| log2f(maximum(v, f32_of(1e-5)))).collect();
    let valid: Vec<bool> = sdr.v.iter().map(|&v| v > f32_of(0.003) && v < f32_of(0.97)).collect();
    let (cx, cy) = match fit_curve(&x, &y, &valid) {
        Ok(curve) => curve,
        Err(e) if is_unsupported(&e) => return Ok(1),
        Err(e) => return Err(e),
    };
    let a = hp32(&sdr.v, h, w)?;
    let (left, right) = (cy[0], cy[cy.len() - 1]);
    let predicted: Vec<f64> = x.iter().map(|&v| exp2(interp(f64::from(v), &cx, &cy, left, right))).collect();
    let b = hp64(&predicted, h, w)?;
    let visible: Vec<bool> = sdr.v.iter().map(|&v| v > f32_of(0.01) && v < f32_of(0.95)).collect();
    let (hh, ww) = (to_i64(h), to_i64(w));
    let (mut unverified, mut verified) = (0i64, 0i64);
    for i in 0..TILES {
        for j in 0..TILES {
            let (y0, y1) = (idx(i * hh / TILES), idx((i + 1) * hh / TILES));
            let (x0, x1) = (idx(j * ww / TILES), idx((j + 1) * ww / TILES));
            let mut ta = Vec::new();
            let mut tb = Vec::new();
            for r in y0..y1 {
                for c in x0..x1 {
                    if visible[r * w + c] {
                        ta.push(a[r * w + c]);
                        tb.push(b[r * w + c]);
                    }
                }
            }
            if ta.is_empty() || f64::from(std(&ta)) < TILE_TEXTURE {
                continue;
            }
            let low = ta.len() < 32 || {
                let na = norm(&ta);
                let nb = norm(&tb);
                let p: Vec<f64> = na.iter().zip(&nb).map(|(&p, &q)| f64::from(p) * q).collect();
                mean(&p) < MIN_TILE_CORRELATION
            };
            if low {
                unverified += 1;
            } else {
                verified += 1;
            }
        }
    }
    Ok(if verified > 0 { unverified } else { unverified.max(1) })
}

/// `except HdrUnsupported`: every reason this crate raises from the gain math.
pub fn is_unsupported(e: &CoreError) -> bool {
    matches!(e.kind, "no_curve" | "not_aligned" | "capture_mismatch" | "metadata_unreadable")
}

/// `_norm(_hp(warp(raw_y, shape, s, dy, dx)))`.
fn warped_norm(raw: &Plane32, h: usize, w: usize, s: f64, dy: i64, dx: i64) -> Result<Vec<f32>, CoreError> {
    #[allow(clippy::cast_precision_loss)]
    let warped = warp(raw, (h, w), s, dy as f64, dx as f64)?;
    Ok(norm(&hp32(&warped, h, w)?))
}

/// Port of gxr_studio/hdr/gain.py:273 align.
pub fn align(sdr: &Plane32, raw: &Plane32) -> Result<Alignment, CoreError> {
    check_shape(sdr.h, sdr.w, sdr.v.len())?;
    check_shape(raw.h, raw.w, raw.v.len())?;
    if sdr.v.is_empty() || raw.v.is_empty() {
        return Err(bad("无法对齐空图"));
    }
    let (h, w) = (sdr.h, sdr.w);
    let a = norm(&hp32(&sdr.v, h, w)?);
    let corr_at = |s: f64, dy: i64, dx: i64| -> Result<f64, CoreError> {
        Ok(mean_product(&a, &warped_norm(raw, h, w, s, dy, dx)?))
    };
    let mut best: Option<(f64, f64, i64, i64)> = None;
    for &s in &SCALES {
        let (mut dy, mut dx) = phase_shift(&a, &warped_norm(raw, h, w, s, 0, 0)?, h, w)?;
        let mut corr = corr_at(s, dy, dx)?;
        if s == 1.0 && dy == 0 && dx == 0 && corr > 0.6 {
            best = Some((corr, s, dy, dx));
            break;
        }
        let (py, px) = (dy, dx);
        for i in -1..=1 {
            for j in -1..=1 {
                if i == 0 && j == 0 {
                    continue;
                }
                let c = corr_at(s, py + i, px + j)?;
                if c > corr {
                    corr = c;
                    dy = py + i;
                    dx = px + j;
                }
            }
        }
        if best.is_none_or(|b| corr > b.0) {
            best = Some((corr, s, dy, dx));
        }
    }
    let (corr, s, dy, dx) = best.ok_or_else(|| bad("SCALES 是空的"))?;
    #[allow(clippy::cast_precision_loss)]
    let registered = warp(raw, (h, w), s, dy as f64, dx as f64)?;
    let ha = hp32(&sdr.v, h, w)?;
    let hb = hp32(&registered, h, w)?;
    let moved = tiles_moved(&ha, &hb, h, w);
    let unverified = tiles_unverified(sdr, &registered)? + i64::from(local_residual(sdr, &registered)?);
    #[allow(clippy::cast_precision_loss)]
    let shift_frac = (dy.abs() as f64 / count_f64(h)).max(dx.abs() as f64 / count_f64(w));
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

/// Python `f"{x:.2f}"` for the correlation in the not-aligned message.
fn fixed2(x: f64) -> String {
    format!("{x:.2}")
}

/// Port of gxr_studio/hdr/gain.py:303 check_alignment.
pub fn check_alignment(al: &Alignment) -> Result<(), CoreError> {
    let fail = |detail: String| Err(unsupported("not_aligned", &detail));
    if al.correlation < MIN_CORRELATION {
        return fail(format!("RAW 与 JPG 对不齐（相关系数 {}）", fixed2(al.correlation)));
    }
    if al.shift_frac > MAX_SHIFT {
        return fail(format!("RAW 与 JPG 错开太多（{}, {}）", al.dy, al.dx));
    }
    if al.tiles_moved != 0 {
        return fail(format!("RAW 与 JPG 局部对不齐（{} 块）", al.tiles_moved));
    }
    if al.tiles_unverified != 0 {
        return fail(format!("无法确认 RAW 与 JPG 局部对齐（{} 块）", al.tiles_unverified));
    }
    Ok(())
}

/// Port of gxr_studio/hdr/gain.py:314 register: luma and clip resampled to `shape`.
pub fn register(
    raw: &Plane32,
    clip: &[bool],
    shape: (usize, usize),
    al: &Alignment,
    grid: (usize, usize),
) -> Result<(Vec<f32>, Vec<bool>), CoreError> {
    check_shape(raw.h, raw.w, clip.len())?;
    if grid.0 == 0 || grid.1 == 0 {
        return Err(bad("division by zero"));
    }
    let ky = count_f64(shape.0) / count_f64(grid.0);
    let kx = count_f64(shape.1) / count_f64(grid.1);
    #[allow(clippy::cast_precision_loss)]
    let (dy, dx) = (al.dy as f64 * ky, al.dx as f64 * kx);
    let y: Vec<f32> = warp(raw, shape, al.scale, dy, dx)?
        .into_iter()
        .map(|v| maximum(v, f32_of(1e-6)))
        .collect();
    let clip_plane = Plane32 {
        h: raw.h,
        w: raw.w,
        v: clip.iter().map(|&c| if c { 1.0 } else { 0.0 }).collect(),
    };
    let c: Vec<bool> = warp(&clip_plane, shape, al.scale, dy, dx)?
        .into_iter()
        .map(|v| v > f32_of(0.01))
        .collect();
    Ok((y, c))
}
