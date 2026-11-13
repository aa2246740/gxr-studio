//! Port of the pixel half of gxr_studio/raw/dng.py: decoding the CFA plane and reading
//! normalised values from it.

use crate::dng::Dng;
use crate::error::{CoreError, PyErr, PyResult};
use crate::pyrepr::{Scalar, py_int};

/// The raw CFA plane, row-major, before linearisation (Python: `uint16` array `(height, width)`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Mosaic {
    pub height: usize,
    pub width: usize,
    pub values: Vec<u16>,
}

/// A row-major float64 block (`normalised_block`).
#[derive(Clone, Debug, PartialEq)]
pub struct Block {
    pub rows: usize,
    pub cols: usize,
    pub values: Vec<f64>,
}

/// numpy integer indexing of a 1-D array: negative indices count from the end.
fn np_index(i: i64, n: usize) -> Result<usize, CoreError> {
    let len = crate::bounds::i64_from_len(n)?;
    let j = if i < 0 {
        i.checked_add(len).ok_or_else(|| {
            CoreError::new(
                "index_error",
                format!("index {i} is out of bounds for axis 0 with size {n}"),
            )
        })?
    } else {
        i
    };
    if (0..len).contains(&j) {
        crate::bounds::fit_count_core(u128::try_from(j).map_err(|_| crate::bounds::too_big_core())?)
    } else {
        Err(CoreError::new(
            "index_error",
            format!("index {i} is out of bounds for axis 0 with size {n}"),
        ))
    }
}

/// `np.frombuffer(data, np.uint8, count=count, offset=offset)`.
fn frombuffer(data: &[u8], count: i64, offset: i64) -> PyResult<&[u8]> {
    let ts = crate::bounds::i64_from_len(data.len()).map_err(|_| {
        PyErr::Value("offset must be non-negative and no greater than buffer length".into())
    })?;
    if offset < 0 || offset > ts {
        return Err(PyErr::Value(format!(
            "offset must be non-negative and no greater than buffer length ({ts})"
        )));
    }
    let s = ts - offset;
    let n = if count < 0 { s } else { count };
    if s < n {
        return Err(PyErr::Value("buffer is smaller than requested size".into()));
    }
    let start =
        crate::bounds::fit_count(u128::try_from(offset).map_err(|_| crate::bounds::too_big())?)?;
    let len = crate::bounds::fit_count(u128::try_from(n).map_err(|_| crate::bounds::too_big())?)?;
    let end = start.checked_add(len).ok_or_else(crate::bounds::too_big)?;
    Ok(&data[start..end])
}

/// Bits `[start, start + b)` of a row, most significant first, keeping the low 16 bits like
/// `(bits << shifts).sum(-1).astype(np.uint16)` (numpy shifts of 32 or more give 0).
fn packed_sample(row: &[u8], start: usize, b: usize) -> u16 {
    let mut v: u32 = 0;
    for j in 0..b {
        let shift = b - 1 - j;
        if shift >= 16 {
            continue;
        }
        let bit = start + j;
        let on = (row[bit / 8] >> (7 - bit % 8)) & 1;
        v |= u32::from(on) << shift;
    }
    // Unreachable fallback: `v & 0xffff` always fits in `u16`.
    u16::try_from(v & 0xffff).unwrap_or(0)
}

fn fit_dim(v: i128) -> PyResult<usize> {
    if v < 0 {
        return Err(crate::bounds::too_big());
    }
    crate::bounds::fit_count(u128::try_from(v).map_err(|_| crate::bounds::too_big())?)
}

fn decode_rows(
    rows: &[u8],
    h: usize,
    w: usize,
    b: usize,
    row: usize,
    big: bool,
    pixels: u64,
) -> PyResult<Vec<u16>> {
    let mut m = crate::bounds::reserve_u16(pixels)?;
    for y in 0..h {
        let r = &rows[y * row..(y + 1) * row];
        let out = &mut m[y * w..(y + 1) * w];
        match b {
            16 => {
                for (x, v) in out.iter_mut().enumerate() {
                    let pair = [r[2 * x], r[2 * x + 1]];
                    *v = if big {
                        u16::from_be_bytes(pair)
                    } else {
                        u16::from_le_bytes(pair)
                    };
                }
            }
            8 => {
                for (x, v) in out.iter_mut().enumerate() {
                    *v = u16::from(r[x]);
                }
            }
            12 => {
                let n2 = w / 2;
                for k in 0..n2 {
                    let (a0, a1, a2) = (
                        u16::from(r[3 * k]),
                        u16::from(r[3 * k + 1]),
                        u16::from(r[3 * k + 2]),
                    );
                    out[2 * k] = (a0 << 4) | (a1 >> 4);
                    out[2 * k + 1] = ((a1 & 15) << 8) | a2;
                }
                if w % 2 == 1 {
                    let (t0, t1) = (u16::from(r[3 * n2]), u16::from(r[3 * n2 + 1]));
                    out[w - 1] = (t0 << 4) | (t1 >> 4);
                }
            }
            _ => {
                for (x, v) in out.iter_mut().enumerate() {
                    *v = packed_sample(r, x * b, b);
                }
            }
        }
    }
    Ok(m)
}

impl Dng<'_> {
    /// Port of gxr_studio/raw/dng.py:174 Dng.mosaic: the full CFA plane, decoded once.
    pub fn mosaic(&self) -> Result<&crate::mosaic::Mosaic, CoreError> {
        if let Some(m) = self.mosaic_cache.get() {
            return Ok(m);
        }
        let decoded = self.decode_mosaic().map_err(|e| match e {
            PyErr::Value(m) | PyErr::Index(m) => {
                CoreError::raw_decode(format!("RAW 数据读不全（{m}）"))
            }
            other => CoreError::new(
                "unexpected",
                format!("{}: {}", other.type_name(), other.text()),
            ),
        })?;
        Ok(self.mosaic_cache.get_or_init(|| decoded))
    }

    /// Port of gxr_studio/raw/dng.py:186 Dng._decode_mosaic.
    fn decode_mosaic(&self) -> PyResult<Mosaic> {
        let (offs, cnts): (&[Scalar], &[Scalar]) = (&self.strips.0, &self.strips.1);
        let mut buf: Vec<u8> = Vec::new();
        let mut any = false;
        for (o, n) in offs.iter().zip(cnts.iter()) {
            let count = py_int(n)?;
            let offset = py_int(o)?;
            buf.extend_from_slice(frombuffer(self.data, count, offset)?);
            any = true;
        }
        if !any {
            return Err(PyErr::Value(
                "need at least one array to concatenate".into(),
            ));
        }
        let (h, w, b) = (
            i128::from(self.height),
            i128::from(self.width),
            i128::from(self.bps),
        );
        let row = w
            .checked_mul(b)
            .and_then(|n| n.checked_add(7))
            .ok_or_else(crate::bounds::too_big)?
            .div_euclid(8);
        let need = row.checked_mul(h).ok_or_else(crate::bounds::too_big)?;
        let buf_len = i128::try_from(buf.len()).map_err(|_| crate::bounds::too_big())?;
        if buf_len < need {
            return Err(PyErr::Value("raw strips shorter than the image".into()));
        }
        let h = fit_dim(h)?;
        let w = fit_dim(w)?;
        let b = fit_dim(b)?;
        let row = fit_dim(row)?;
        let plane = h.checked_mul(row).ok_or_else(crate::bounds::too_big)?;
        let pixels = u64::try_from(h)
            .ok()
            .and_then(|h| u64::try_from(w).ok().and_then(|w| h.checked_mul(w)))
            .ok_or_else(crate::bounds::too_big)?;
        Ok(Mosaic {
            height: h,
            width: w,
            values: decode_rows(&buf[..plane], h, w, b, row, self.big_endian, pixels)?,
        })
    }

    /// Port of gxr_studio/raw/dng.py:212 Dng.release: forget the decoded plane.
    pub fn release(&mut self) {
        self.mosaic_cache = std::cell::OnceCell::new();
    }

    /// Port of gxr_studio/raw/dng.py:225 Dng.crop_mosaic: the DefaultCrop part of the plane.
    pub fn crop_mosaic(&self) -> Result<Mosaic, CoreError> {
        let (y0, x0, h, w) = self.crop_window();
        let m = self.mosaic()?;
        let (y0, x0, h, w) = self.in_plane(y0, y0 + h, x0, x0 + w)?;
        let n = u64::try_from(h)
            .ok()
            .and_then(|h| u64::try_from(w).ok().and_then(|w| h.checked_mul(w)))
            .ok_or_else(crate::bounds::too_big_core)?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(crate::bounds::fit_count_core(u128::from(n))?)
            .map_err(|_| crate::bounds::too_big_core())?;
        for y in y0..y0 + h {
            values.extend_from_slice(&m.values[y * m.width + x0..y * m.width + x0 + w]);
        }
        Ok(Mosaic {
            height: h,
            width: w,
            values,
        })
    }

    /// `[y0, y1) × [x0, x1)` as (y0, x0, rows, cols) when it lies inside the raw plane.
    /// Python would fall back on numpy's negative-index and clamped-slice rules here; no caller
    /// of the render path can ask for that, so it is an error instead.
    fn in_plane(
        &self,
        y0: i64,
        y1: i64,
        x0: i64,
        x1: i64,
    ) -> Result<(usize, usize, usize, usize), CoreError> {
        if !(0 <= y0 && y0 <= y1 && y1 <= self.height && 0 <= x0 && x0 <= x1 && x1 <= self.width) {
            return Err(CoreError::new(
                "index_error",
                format!("block [{y0}, {y1}) x [{x0}, {x1}) outside the raw plane"),
            ));
        }
        let u = |v: i64| -> Result<usize, CoreError> {
            let n = u128::try_from(v).map_err(|_| crate::bounds::too_big_core())?;
            crate::bounds::fit_count_core(n)
        };
        Ok((u(y0)?, u(x0)?, u(y1 - y0)?, u(x1 - x0)?))
    }

    /// Port of gxr_studio/raw/dng.py:229 Dng.cfa_at: colour (0 R, 1 G, 2 B) at a raw-plane
    /// position; the pattern starts at the ActiveArea origin.
    pub fn cfa_at(&self, y: i64, x: i64) -> i64 {
        // `rem_euclid(2)` is 0 or 1. The match does not convert a file integer with a fallback.
        let r = match (y - self.active[0]).rem_euclid(2) {
            0 => 0,
            _ => 1,
        };
        let c = match (x - self.active[1]).rem_euclid(2) {
            0 => 0,
            _ => 1,
        };
        self.cfa[r][c]
    }

    /// Port of gxr_studio/raw/dng.py:234 Dng.black_at.
    pub fn black_at(&self, y: i64, x: i64) -> Result<f64, CoreError> {
        let (ry, rx) = (y - self.active[0], x - self.active[1]);
        let bp = &self.black_pattern;
        if bp.shape.len() != 2 || bp.rows == 0 || bp.cols == 0 {
            return Err(CoreError::new(
                "unexpected",
                "ValueError: black level pattern is not a 2-D repeat",
            ));
        }
        let rows = i64::try_from(bp.rows).map_err(|_| crate::bounds::too_big_core())?;
        let cols = i64::try_from(bp.cols).map_err(|_| crate::bounds::too_big_core())?;
        let at = |v: i64, n: i64| -> Result<usize, CoreError> {
            let m = v.rem_euclid(n);
            crate::bounds::fit_count_core(
                u128::try_from(m).map_err(|_| crate::bounds::too_big_core())?,
            )
        };
        let mut b = bp.at(at(ry, rows)?, at(rx, cols)?);
        if let Some(dh) = &self.black_dh {
            b += dh[np_index(rx, dh.len())?];
        }
        if let Some(dv) = &self.black_dv {
            b += dv[np_index(ry, dv.len())?];
        }
        Ok(b)
    }

    /// Port of gxr_studio/raw/dng.py:244 Dng.linearize (one value).
    pub fn linearize(&self, v: u16) -> Result<f64, CoreError> {
        match &self.lin_table {
            None => Ok(f64::from(v)),
            Some(t) => {
                let last = crate::bounds::i64_from_len(t.len())?.saturating_sub(1);
                Ok(t[np_index(i64::from(v).min(last), t.len())?])
            }
        }
    }

    /// Port of gxr_studio/raw/dng.py:250 Dng.normalised_at (one position).
    pub fn normalised_at(&self, y: i64, x: i64) -> Result<f64, CoreError> {
        let m = self.mosaic()?;
        let black = self.black_at(y, x)?;
        let yy = np_index(y, m.height)?;
        let xx = np_index(x, m.width)?;
        Ok((self.linearize(m.values[yy * m.width + xx])? - black) / (self.white - black))
    }

    /// Port of gxr_studio/raw/dng.py:255 Dng.normalised_block over `[y0, y1) × [x0, x1)`, in
    /// float64 (no caller passes another dtype).
    pub fn normalised_block(&self, y0: i64, y1: i64, x0: i64, x1: i64) -> Result<Block, CoreError> {
        let m = self.mosaic()?;
        let (py0, px0, rows, cols) = self.in_plane(y0, y1, x0, x1)?;
        if self.black_pattern.shape.len() != 2 {
            let mut right = vec![rows, cols];
            right.extend_from_slice(&self.black_pattern.shape[2..]);
            let msg = crate::bounds::broadcast_error(&[rows, cols], &right).ok_or_else(|| {
                CoreError::new("unexpected", "ValueError: black level pattern is not 2-D")
            })?;
            return Err(CoreError::new("unexpected", format!("ValueError: {msg}")));
        }
        let n = u64::try_from(rows)
            .ok()
            .and_then(|r| u64::try_from(cols).ok().and_then(|c| r.checked_mul(c)))
            .ok_or_else(crate::bounds::too_big_core)?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(crate::bounds::fit_count_core(u128::from(n))?)
            .map_err(|_| crate::bounds::too_big_core())?;
        for (r, y) in (y0..y1).enumerate() {
            for (c, x) in (x0..x1).enumerate() {
                let black = self.black_at(y, x)?;
                let v = self.linearize(m.values[(py0 + r) * m.width + px0 + c])?;
                values.push((v - black) / (self.white - black));
            }
        }
        require_finite(values.iter().copied(), "归一化码值")?;
        Ok(Block { rows, cols, values })
    }

    /// Port of gxr_studio/raw/dng.py:266 Dng.crop_cfa_phase: colours of the crop's first 2×2.
    pub fn crop_cfa_phase(&self) -> [[i64; 2]; 2] {
        let (y0, x0, _, _) = self.crop_window();
        [
            [self.cfa_at(y0, x0), self.cfa_at(y0, x0 + 1)],
            [self.cfa_at(y0 + 1, x0), self.cfa_at(y0 + 1, x0 + 1)],
        ]
    }
}

/// Port of gxr_studio/raw/dng.py:299 require_finite.
pub fn require_finite(mut values: impl Iterator<Item = f64>, what: &str) -> Result<(), CoreError> {
    if values.all(f64::is_finite) {
        Ok(())
    } else {
        Err(CoreError::raw_decode(format!(
            "这张 RAW 的{what}无效，不能显示"
        )))
    }
}
