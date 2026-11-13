//! The online recipe bake: the linear matrix kept as is, the sector table baked into a complex
//! gain grid on the (Cb, Cr) plane, and the per-pixel apply.
//!
//! Port of gxr_studio/render/chain.py (`bake_chroma`, `_chroma_chunk`, `chroma_float`,
//! `chroma_u8`, `quantize`) and gxr_studio/gpu/tables.py `bake_bytes`.

use gxr_raw::CoreError;

use crate::cplx::{C, J};
use crate::hull::equations;
use crate::matrix::{MatrixRow, matvec};
use crate::model::{Codes, Coefficients, ENTRY_BYTES, UnitModel, clip, input_codes};
use crate::tones::{Table, apply_table, clip_f32};

/// `CODE_MAX`.
pub const CODE_MAX: usize = 4095;
/// `PREVIEW_STEP` / `EXPORT_STEP` in render/service.py.
pub const STEP: f64 = 0.375;
/// The largest grid the port allocates (cells). Step 0.375 needs about 683 x 683.
pub const MAX_CELLS: u64 = 1 << 24;
/// Floats before the gain grid in `bake_bytes`: pre, matrix, offset, input, lookup.
pub const HEAD_FLOATS: usize = 4096 * 3 + 9 + 3 + 30 + 9 + 3;

fn value(message: impl Into<String>) -> CoreError {
    CoreError::new("value", message)
}

/// `ChromaBake`.
#[derive(Clone, Debug, PartialEq)]
pub struct ChromaBake {
    /// (4096, 3) float32, row-major.
    pub pre: Vec<f32>,
    pub matrix: [f32; 9],
    pub offset: [f32; 3],
    /// (ny, nx, 2) float32: real then imaginary.
    pub gain: Vec<f32>,
    pub nx: usize,
    pub ny: usize,
    pub origin: [f64; 2],
    pub step: f64,
    pub input_coefficients: Option<Coefficients>,
    pub lookup_matrix: [f32; 9],
    pub lookup_offset: [f32; 3],
}

fn f32x9(m: &[[f64; 3]; 3]) -> [f32; 9] {
    let mut out = [0.0f32; 9];
    for (i, v) in out.iter_mut().enumerate() {
        *v = m[i / 3][i % 3] as f32;
    }
    out
}

/// `row.offsets - np.array([0.0, 128.0, 128.0])`.
fn centred(row: &MatrixRow) -> [f64; 3] {
    let off = row.offsets();
    [off[0] - 0.0, off[1] - 128.0, off[2] - 128.0]
}

/// `int(round(v))` on a float64: half to even, then a checked integer.
fn round_count(v: f64) -> Result<u64, CoreError> {
    let r = v.round_ties_even();
    if !r.is_finite() {
        return Err(value("cannot convert float NaN to integer"));
    }
    if !(0.0..=1e12).contains(&r) {
        return Err(CoreError::new("memory", "bake grid is too large"));
    }
    // r is a non-negative integer below 1e12: exact in u64.
    Ok(r as u64)
}

/// Port of gxr_studio/render/chain.py:224 bake_chroma
pub fn bake_chroma(model: &UnitModel, entry: &[u8], step: f64) -> Result<ChromaBake, CoreError> {
    if entry.len() != ENTRY_BYTES {
        return Err(value("entry must be 552 bytes"));
    }
    if !(step.is_finite() && step > 0.0) {
        return Err(value("step must be a positive number"));
    }
    let ramp: Vec<[f64; 3]> = (0..=CODE_MAX)
        .map(|v| {
            let x = f64::from(u16::try_from(v).unwrap_or(u16::MAX));
            [x, x, x]
        })
        .collect();
    let pre = model.model.pre(entry, &ramp)?;
    let row = MatrixRow::from_bytes(&entry[216..244])?;
    let m = row.effective();
    let off = centred(&row);
    let lookup = MatrixRow::from_bytes(&model.factory[216..244])?;
    let lookup_matrix = lookup.effective();
    let lookup_offset = centred(&lookup);
    let mut corners = Vec::with_capacity(8);
    for r in [0.0, 255.0] {
        for g in [0.0, 255.0] {
            for b in [0.0, 255.0] {
                let v = matvec(&lookup_matrix, [r, g, b]);
                corners.push([v[1] + lookup_offset[1], v[2] + lookup_offset[2]]);
            }
        }
    }
    let mut lo = [0.0f64; 2];
    let mut hi = [0.0f64; 2];
    for axis in 0..2 {
        let (mut mn, mut mx) = (corners[0][axis], corners[0][axis]);
        for c in &corners[1..] {
            mn = mn.min(c[axis]);
            mx = mx.max(c[axis]);
        }
        lo[axis] = (mn / step).floor() * step - step;
        hi[axis] = (mx / step).ceil() * step + step;
    }
    let nx = round_count((hi[0] - lo[0]) / step)? + 1;
    let ny = round_count((hi[1] - lo[1]) / step)? + 1;
    let cells = nx
        .checked_mul(ny)
        .filter(|&n| n <= MAX_CELLS)
        .ok_or_else(|| CoreError::new("memory", "bake grid is too large"))?;
    let (nx, ny, cells) = (usize_of(nx)?, usize_of(ny)?, usize_of(cells)?);
    let cb: Vec<f64> = (0..nx).map(|k| lo[0] + step * index_f64(k)).collect();
    let cr: Vec<f64> = (0..ny).map(|k| lo[1] + step * index_f64(k)).collect();
    let mut gain = vec![C::new(1.0, 0.0); cells];
    if entry[0] != 3 {
        let zrow = model.model.sector_z(entry)?;
        let q = model.model.table.coupling(&zrow);
        let eqs = equations(&corners)?;
        let thr = 2.0 * step * 2.0f64.sqrt();
        for iy in 0..ny {
            for ix in 0..nx {
                let c = C::real(cb[ix]).plus(J.times(C::real(cr[iy])));
                let near = eqs.iter().all(|e| {
                    let mut s = e[0] * c.re;
                    s = s + e[1] * c.im;
                    s + e[2] <= thr
                });
                if !near {
                    continue;
                }
                let probe = if c.abs() < 1e-9 { C::new(1e-3, 0.0) } else { c };
                gain[iy * nx + ix] = model.model.table.forward(probe, &q)?.over(probe);
            }
        }
    }
    let mut g = Vec::with_capacity(cells * 2);
    for v in &gain {
        g.push(v.re as f32);
        g.push(v.im as f32);
    }
    let mut pre32 = Vec::with_capacity(pre.len() * 3);
    for p in &pre {
        pre32.extend(p.iter().map(|&v| v as f32));
    }
    Ok(ChromaBake {
        pre: pre32,
        matrix: f32x9(&m),
        offset: [off[0] as f32, off[1] as f32, off[2] as f32],
        gain: g,
        nx,
        ny,
        origin: lo,
        step,
        input_coefficients: Some(model.coefficients.clone()),
        lookup_matrix: f32x9(&lookup_matrix),
        lookup_offset: [
            lookup_offset[0] as f32,
            lookup_offset[1] as f32,
            lookup_offset[2] as f32,
        ],
    })
}

fn usize_of(v: u64) -> Result<usize, CoreError> {
    usize::try_from(v).map_err(|_| CoreError::new("memory", "bake grid is too large"))
}

/// A grid index (below `MAX_CELLS`) as float64.
fn index_f64(k: usize) -> f64 {
    f64::from(u32::try_from(k).unwrap_or(u32::MAX))
}

/// Port of gxr_studio/gpu/tables.py:32 bake_bytes
///
/// The little-endian float32 table the shader reads. The coefficients are zero-padded to
/// (10, 3) and stored as float32, as Python does.
pub fn bake_bytes(bk: &ChromaBake) -> Vec<u8> {
    let mut out = Vec::with_capacity((HEAD_FLOATS + bk.gain.len()) * 4);
    let mut put = |v: f32| out.extend_from_slice(&v.to_le_bytes());
    bk.pre.iter().copied().for_each(&mut put);
    bk.matrix.iter().copied().for_each(&mut put);
    bk.offset.iter().copied().for_each(&mut put);
    let mut coef = [0.0f32; 30];
    if let Some(c) = &bk.input_coefficients {
        for (slot, &v) in coef.iter_mut().zip(&c.values) {
            *slot = v as f32;
        }
    }
    coef.iter().copied().for_each(&mut put);
    bk.lookup_matrix.iter().copied().for_each(&mut put);
    bk.lookup_offset.iter().copied().for_each(&mut put);
    bk.gain.iter().copied().for_each(&mut put);
    out
}

/// `astype(np.int32)` of a float64 on arm64: toward zero, saturating, NaN to 0.
fn trunc_i32(v: f64) -> i32 {
    v as i32
}

/// `np.float32(1.402)` and friends, used as float64 operands.
const K1402: f32 = 1.402;
const K0344: f32 = 0.344_136;
const K0714: f32 = 0.714_136;
const K1772: f32 = 1.772;

/// Port of gxr_studio/render/chain.py:274 _chroma_chunk, one pixel.
pub fn chroma_pixel(
    x: [f32; 3],
    gains: Option<[f32; 3]>,
    bk: &ChromaBake,
    table: Option<&Table>,
) -> [f32; 3] {
    let x = match gains {
        Some(g) => [x[0] * g[0], x[1] * g[1], x[2] * g[2]],
        None => x,
    };
    let codes = input_codes(Codes::F32(&x), bk.input_coefficients.as_ref());
    let mut p = [0.0f64; 3];
    for ch in 0..3 {
        let xc = clip(codes[0][ch], 0.0, 4095.0);
        let i = trunc_i32(xc).min(4094);
        let at = pre_index(i, ch);
        let v0 = bk.pre[at];
        let v1 = bk.pre[at + 3];
        // (v1 - v0) is float32; times the float64 fraction, plus v0: float64.
        p[ch] = f64::from(v0) + f64::from(v1 - v0) * (xc - f64::from(i));
    }
    let ycc = mat32(&bk.matrix, p, &bk.offset);
    let lk = mat32(&bk.lookup_matrix, p, &bk.lookup_offset);
    let scale = f64::from((1.0 / bk.step) as f32);
    let u = (lk[1] - f64::from(bk.origin[0] as f32)) * scale;
    let v = (lk[2] - f64::from(bk.origin[1] as f32)) * scale;
    let nx = clamp_cells(bk.nx);
    let ny = clamp_cells(bk.ny);
    let iu = trunc_i32(u).clamp(0, nx - 2);
    let iv = trunc_i32(v).clamp(0, ny - 2);
    let fu = u - f64::from(iu);
    let fv = v - f64::from(iv);
    let at = cell(iv, iu, bk.nx);
    let g = |k: usize, off: usize| f64::from(bk.gain[2 * (at + off) + k]);
    let mut gg = [0.0f64; 2];
    for (k, slot) in gg.iter_mut().enumerate() {
        *slot = (g(k, 0) * (1.0 - fu) + g(k, 1) * fu) * (1.0 - fv)
            + (g(k, bk.nx) * (1.0 - fu) + g(k, bk.nx + 1) * fu) * fv;
    }
    let (y, cb, cr) = (ycc[0], ycc[1], ycc[2]);
    let cbo = cb * gg[0] - cr * gg[1];
    let cro = cb * gg[1] + cr * gg[0];
    let out = [
        (y + f64::from(K1402) * cro) as f32,
        (y - f64::from(K0344) * cbo - f64::from(K0714) * cro) as f32,
        (y + f64::from(K1772) * cbo) as f32,
    ];
    let mut out = out.map(|v| clip_f32(v, 0.0, 255.0));
    if let Some(t) = table {
        for (ch, v) in out.iter_mut().enumerate() {
            *v = apply_table(*v, t, ch);
        }
    }
    out
}

/// `p @ m.T + offset` with a float32 matrix promoted to float64.
fn mat32(m: &[f32; 9], p: [f64; 3], off: &[f32; 3]) -> [f64; 3] {
    let mut out = [0.0; 3];
    for (r, o) in out.iter_mut().enumerate() {
        let mut s = p[0] * f64::from(m[3 * r]);
        s = s + p[1] * f64::from(m[3 * r + 1]);
        s = s + p[2] * f64::from(m[3 * r + 2]);
        *o = s + f64::from(off[r]);
    }
    out
}

/// `i * 3 + channel` for `i` in `0..=4094` (clip, then `minimum`; NaN casts to 0).
fn pre_index(i: i32, ch: usize) -> usize {
    // u32 to usize widens on every target.
    i.clamp(0, 4094).unsigned_abs() as usize * 3 + ch
}

/// A grid size (at most `MAX_CELLS`, at least 2 after `from_bytes`) as int32.
fn clamp_cells(n: usize) -> i32 {
    i32::try_from(n).unwrap_or(i32::MAX)
}

/// `iv * nx + iu` for clipped, non-negative indices.
fn cell(iv: i32, iu: i32, nx: usize) -> usize {
    let (Ok(iv), Ok(iu)) = (usize::try_from(iv), usize::try_from(iu)) else {
        return 0;
    };
    iv * nx + iu
}

/// Port of gxr_studio/render/chain.py:69 quantize, on the float32 the chain stores.
pub fn quantize(v: f32) -> u8 {
    let r = (clip_f32(v, 0.0, 255.0) + 0.5f32).floor();
    if r.is_nan() { 0 } else { r as u8 }
}

/// Port of gxr_studio/render/chain.py:308 chroma_float
pub fn chroma_float(
    codes: Codes<'_>,
    bk: &ChromaBake,
    gains: Option<[f64; 3]>,
    table: Option<&Table>,
) -> Vec<f32> {
    let g32 = gains.map(|g| [g[0] as f32, g[1] as f32, g[2] as f32]);
    let n = codes.pixels();
    let mut out = Vec::with_capacity(n * 3);
    for i in 0..n {
        let x = match codes {
            Codes::F32(v) => [v[3 * i], v[3 * i + 1], v[3 * i + 2]],
            Codes::F64(v) => [v[3 * i] as f32, v[3 * i + 1] as f32, v[3 * i + 2] as f32],
        };
        out.extend(chroma_pixel(x, g32, bk, table));
    }
    out
}

/// Port of gxr_studio/render/chain.py:324 chroma_u8
pub fn chroma_u8(
    codes: Codes<'_>,
    bk: &ChromaBake,
    gains: Option<[f64; 3]>,
    table: Option<&Table>,
) -> Vec<u8> {
    chroma_float(codes, bk, gains, table)
        .into_iter()
        .map(quantize)
        .collect()
}

/// A [`ChromaBake`] back from `bake_bytes` output and its meta (`nx`, `ny`, `origin`, `step`,
/// and this port's float64 `input_coefficients`).
pub fn from_bytes(
    bytes: &[u8],
    nx: usize,
    ny: usize,
    origin: [f64; 2],
    step: f64,
    input: Option<Coefficients>,
) -> Result<ChromaBake, CoreError> {
    if nx < 2 || ny < 2 {
        return Err(value("bake grid needs nx >= 2 and ny >= 2"));
    }
    let cells = u64::try_from(nx)
        .ok()
        .zip(u64::try_from(ny).ok())
        .and_then(|(a, b)| a.checked_mul(b))
        .filter(|&n| n <= MAX_CELLS);
    let cells = cells.ok_or_else(|| CoreError::new("memory", "bake grid is too large"))?;
    let floats = usize_of(cells)? * 2 + HEAD_FLOATS;
    if bytes.len() != floats * 4 {
        return Err(value("bake table size does not match nx and ny"));
    }
    let f: Vec<f32> = bytes
        .chunks_exact(4)
        .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
        .collect();
    let take3 = |at: usize| [f[at], f[at + 1], f[at + 2]];
    let take9 = |at: usize| {
        let mut m = [0.0f32; 9];
        m.copy_from_slice(&f[at..at + 9]);
        m
    };
    Ok(ChromaBake {
        pre: f[..12288].to_vec(),
        matrix: take9(12288),
        offset: take3(12297),
        gain: f[HEAD_FLOATS..].to_vec(),
        nx,
        ny,
        origin,
        step,
        input_coefficients: input,
        lookup_matrix: take9(12330),
        lookup_offset: take3(12339),
    })
}
