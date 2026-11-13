//! Host prediction: a 552-byte entry plus white-balanced codes to sRGB.
//!
//! Port of gxr_studio/engine/model.py (`Model.__init__`, `pre`, `predict`),
//! gxr_studio/engine/colour.py `Z_of_words`, and gxr_studio/camdata/unit_model.py.

use gxr_raw::CoreError;

use crate::cplx::{C, J};
use crate::json_in::Value;
use crate::knobs::dec;
use crate::matrix::{MatrixRow, rgb_from_ycc};
use crate::pretable::PreTable;
use crate::sector::hue_deg;

/// Entries are always 552 bytes on this path (`datcodec` assembles them).
pub const ENTRY_BYTES: usize = 552;

fn value(message: impl Into<String>) -> CoreError {
    CoreError::new("value", message)
}

/// Codes as the caller stored them. `input_codes` keeps float32 arithmetic for float32 input.
#[derive(Clone, Copy, Debug)]
pub enum Codes<'a> {
    F32(&'a [f32]),
    F64(&'a [f64]),
}

impl Codes<'_> {
    /// Pixels (rows of three).
    pub fn pixels(&self) -> usize {
        match self {
            Codes::F32(v) => v.len() / 3,
            Codes::F64(v) => v.len() / 3,
        }
    }

    fn get(&self, i: usize) -> [f64; 3] {
        match self {
            Codes::F32(v) => [
                f64::from(v[3 * i]),
                f64::from(v[3 * i + 1]),
                f64::from(v[3 * i + 2]),
            ],
            Codes::F64(v) => [v[3 * i], v[3 * i + 1], v[3 * i + 2]],
        }
    }
}

/// The calibration's input mapping: 4 or 10 rows of 3, float64, row-major.
#[derive(Clone, Debug, PartialEq)]
pub struct Coefficients {
    pub rows: usize,
    pub values: Vec<f64>,
}

/// One pixel of [`input_codes`] with the float32 operand rules.
fn input_row_f32(x: [f32; 3], c: &Coefficients) -> [f64; 3] {
    // `u = raw / 4095` stays float32 under NEP 50; `np.c_` with `np.ones` promotes to float64.
    let u = [x[0] / 4095.0f32, x[1] / 4095.0f32, x[2] / 4095.0f32];
    let mut basis = [0.0f64; 10];
    basis[0] = 1.0;
    for j in 0..3 {
        basis[1 + j] = f64::from(u[j]);
    }
    if c.rows == 10 {
        let (r, g, b) = (u[0], u[1], u[2]);
        let quad = [r * r, g * g, b * b, r * g, r * b, g * b];
        for (slot, q) in basis[4..].iter_mut().zip(quad) {
            *slot = f64::from(q);
        }
    }
    finish_row(
        [f64::from(x[0]), f64::from(x[1]), f64::from(x[2])],
        &basis,
        c,
    )
}

fn input_row_f64(x: [f64; 3], c: &Coefficients) -> [f64; 3] {
    let u = [x[0] / 4095.0, x[1] / 4095.0, x[2] / 4095.0];
    let mut basis = [0.0f64; 10];
    basis[0] = 1.0;
    basis[1..4].copy_from_slice(&u);
    if c.rows == 10 {
        let (r, g, b) = (u[0], u[1], u[2]);
        basis[4..].copy_from_slice(&[r * r, g * g, b * b, r * g, r * b, g * b]);
    }
    finish_row(x, &basis, c)
}

/// `raw + 4095 * (basis @ coefficients)`, the dot product left to right.
fn finish_row(raw: [f64; 3], basis: &[f64; 10], c: &Coefficients) -> [f64; 3] {
    let mut out = [0.0; 3];
    for (col, o) in out.iter_mut().enumerate() {
        let mut s = basis[0] * c.values[col];
        for k in 1..c.rows {
            s = s + basis[k] * c.values[3 * k + col];
        }
        *o = raw[col] + 4095.0 * s;
    }
    out
}

/// Port of gxr_studio/camdata/unit_model.py:13 input_codes
pub fn input_codes(raw: Codes<'_>, coefficients: Option<&Coefficients>) -> Vec<[f64; 3]> {
    let n = raw.pixels();
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        out.push(match (coefficients, raw) {
            (None, _) => raw.get(i),
            (Some(c), Codes::F32(v)) => input_row_f32([v[3 * i], v[3 * i + 1], v[3 * i + 2]], c),
            (Some(c), Codes::F64(v)) => input_row_f64([v[3 * i], v[3 * i + 1], v[3 * i + 2]], c),
        });
    }
    out
}

/// numpy `binary_search_with_guess` (`compiled_base.c`), including the guess it carries.
fn search(key: f64, arr: &[f64], guess: isize) -> isize {
    const LIKELY_IN_CACHE_SIZE: usize = 8;
    let len = arr.len();
    if key > arr[len - 1] {
        return len as isize;
    } else if key < arr[0] {
        return -1;
    }
    if len <= 4 {
        let mut i = 1;
        while i < len && key >= arr[i] {
            i += 1;
        }
        return i as isize - 1;
    }
    // numpy clamps the carried guess (which may be -1 or len) into [1, len - 3].
    let top = len - 3;
    let guess = if guess < 1 {
        1
    } else {
        usize::try_from(guess).map_or(top, |g| g.min(top))
    };
    let mut imin = 0usize;
    let mut imax = len;
    if key < arr[guess] {
        if key < arr[guess - 1] {
            imax = guess - 1;
            if guess > LIKELY_IN_CACHE_SIZE && key >= arr[guess - LIKELY_IN_CACHE_SIZE] {
                imin = guess - LIKELY_IN_CACHE_SIZE;
            }
        } else {
            return guess as isize - 1;
        }
    } else if key < arr[guess + 1] {
        return guess as isize;
    } else if key < arr[guess + 2] {
        return guess as isize + 1;
    } else {
        imin = guess + 2;
        if guess + LIKELY_IN_CACHE_SIZE + 1 < len && key < arr[guess + LIKELY_IN_CACHE_SIZE] {
            imax = guess + LIKELY_IN_CACHE_SIZE;
        }
    }
    while imin < imax {
        let imid = imin + ((imax - imin) >> 1);
        if key >= arr[imid] {
            imin = imid + 1;
        } else {
            imax = imid;
        }
    }
    imin as isize - 1
}

/// `np.interp(x, xp, fp)` with the default left/right values (`arr_interp`).
pub fn interp(xs: &[f64], xp: &[f64], fp: &[f64]) -> Vec<f64> {
    let len = xp.len();
    let lval = fp[0];
    let rval = fp[len - 1];
    let mut out = Vec::with_capacity(xs.len());
    let mut j: isize = 0;
    for &x in xs {
        if x.is_nan() {
            out.push(x);
            continue;
        }
        j = search(x, xp, j);
        let v = if j == -1 {
            lval
        } else if j == len as isize {
            rval
        } else {
            let u = j as usize;
            if u == len - 1 || xp[u] == x {
                fp[u]
            } else {
                let slope = (fp[u + 1] - fp[u]) / (xp[u + 1] - xp[u]);
                let mut r = slope * (x - xp[u]) + fp[u];
                if r.is_nan() {
                    r = slope * (x - xp[u + 1]) + fp[u + 1];
                    if r.is_nan() && fp[u] == fp[u + 1] {
                        r = fp[u];
                    }
                }
                r
            }
        };
        out.push(v);
    }
    out
}

/// `np.clip(x, lo, hi)`: NaN passes through.
pub fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    if x < lo {
        lo
    } else if x > hi {
        hi
    } else {
        x
    }
}

/// `Model(stock_words, d0, d1, ring_model)`.
#[derive(Clone, Debug)]
pub struct Model {
    pub d0: [f64; 24],
    pub d1: [f64; 24],
    pub s_eff: [C; 24],
    pub table: PreTable,
}

fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Int(_) => "int",
        Value::Float(_) => "float",
        Value::Str(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `((a + d0) - 1j * (b + d1)) / 32`, the shared form of `S_eff` and `Z_of_words`.
pub(crate) fn word_ratio(a: f64, b: f64) -> C {
    C::real(a).minus(J.times(C::real(b))).over(C::real(32.0))
}

fn arr24(v: &[f64]) -> [f64; 24] {
    let mut out = [0.0; 24];
    out.copy_from_slice(v);
    out
}

impl Model {
    /// Port of gxr_studio/engine/model.py:25 Model.__init__
    ///
    /// `d0` / `d1` come with their numpy shapes.
    pub fn new(
        stock: &[[i64; 2]; 24],
        d0: (&[usize], &[f64]),
        d1: (&[usize], &[f64]),
        ring: &Value,
    ) -> Result<Self, CoreError> {
        if !matches!(ring, Value::Object(_)) {
            return Err(CoreError::new(
                "attribute",
                format!("'{}' object has no attribute 'get'", py_type(ring)),
            ));
        }
        if !matches!(ring.get("schema"), Some(Value::Str(s)) if s == "gxr-sector-model/2") {
            return Err(value("需要 v2 pre-table 模型"));
        }
        if d0.0 != [24] || d1.0 != [24] || d0.1.len() != 24 || d1.1.len() != 24 {
            return Err(value("stock/d0/d1 shape"));
        }
        let (d0, d1) = (arr24(d0.1), arr24(d1.1));
        let mut s_eff = [C::real(0.0); 24];
        for s in 0..24 {
            s_eff[s] = word_ratio(word(stock[s][0])? + d0[s], word(stock[s][1])? + d1[s]);
        }
        let table = PreTable::new(ring, s_eff)?;
        Ok(Self {
            d0,
            d1,
            s_eff,
            table,
        })
    }

    /// Port of gxr_studio/engine/model.py:37 Model.pre
    pub fn pre(&self, b: &[u8], x: &[[f64; 3]]) -> Result<Vec<[f64; 3]>, CoreError> {
        let mut xp = [0.0f64; 33];
        for i in 0..31 {
            xp[i + 1] = f64::from(u16::from_be_bytes([b[20 + 2 * i], b[21 + 2 * i]]));
        }
        xp[32] = 4095.0;
        let active = b[1] & 4 != 0;
        let spans: [(usize, usize); 3] = if active {
            [(379, 412), (82, 115), (412, 445)]
        } else {
            [(82, 115); 3]
        };
        let mut out = vec![[0.0; 3]; x.len()];
        for (j, (lo, hi)) in spans.into_iter().enumerate() {
            let fp: Vec<f64> = b[lo..hi].iter().map(|&v| f64::from(v)).collect();
            let col: Vec<f64> = x.iter().map(|p| clip(p[j], 0.0, 4095.0)).collect();
            for (o, v) in out.iter_mut().zip(interp(&col, &xp, &fp)) {
                o[j] = v;
            }
        }
        Ok(out)
    }

    /// `_sector_z` / the coefficient lines of `predict`: decode, check symmetry, `Z_of_words`.
    pub fn sector_z(&self, b: &[u8]) -> Result<[C; 24], CoreError> {
        let mut ab = [[0i64; 2]; 24];
        for (s, slot) in ab.iter_mut().enumerate() {
            let w: Vec<i64> = b[115 + 4 * s..119 + 4 * s]
                .iter()
                .map(|&v| dec(i64::from(v), 128))
                .collect();
            if w[0] != w[3] || w[1] != -w[2] {
                return Err(value(
                    "frozen symmetric sector model cannot predict asymmetric rows",
                ));
            }
            *slot = [w[0], w[1]];
        }
        // The check runs over all 24 rows before Z is formed, as numpy does.
        let mut z = [C::real(0.0); 24];
        for s in 0..24 {
            z[s] = self.z_of_words(s, word(ab[s][0])?, word(ab[s][1])?);
        }
        Ok(z)
    }

    /// Port of gxr_studio/engine/colour.py:92 Z_of_words
    fn z_of_words(&self, s: usize, a: f64, b: f64) -> C {
        word_ratio(a + self.d0[s], b + self.d1[s]).over(self.s_eff[s])
    }
}

/// A sector word (|v| <= 255) as float64.
fn word(v: i64) -> Result<f64, CoreError> {
    let narrow = i32::try_from(v).map_err(|_| value("sector word out of range"))?;
    Ok(f64::from(narrow))
}

/// `UnitModel(stock, d0, d1, ring, factory, coefficients)`.
#[derive(Clone, Debug)]
pub struct UnitModel {
    pub model: Model,
    pub factory: Vec<u8>,
    pub coefficients: Coefficients,
}

/// The shape of a coefficient array and its values.
pub type Shaped<'a> = (&'a [usize], &'a [f64]);

impl UnitModel {
    /// Port of gxr_studio/camdata/unit_model.py:26 UnitModel.__init__
    pub fn new(
        stock: &[[i64; 2]; 24],
        d0: Shaped<'_>,
        d1: Shaped<'_>,
        ring: &Value,
        factory: &[u8],
        coefficients: Shaped<'_>,
    ) -> Result<Self, CoreError> {
        let rows = match coefficients.0 {
            [4, 3] => 4,
            [10, 3] => 10,
            _ => return Err(value("校准输入映射的尺寸或数值不正确")),
        };
        if coefficients.1.len() != rows * 3 || !coefficients.1.iter().all(|v| v.is_finite()) {
            return Err(value("校准输入映射的尺寸或数值不正确"));
        }
        let model = Model::new(stock, d0, d1, ring)?;
        let t = &model.table;
        let increasing = t.nodes.windows(2).all(|w| w[1] - w[0] > 0.0);
        let finite = t
            .nodes
            .iter()
            .chain(&t.gain)
            .chain([&t.k, &t.s0, &t.kappa])
            .all(|v| v.is_finite());
        if !increasing || !finite {
            return Err(value("校准色相环的节点或参数不正确"));
        }
        if factory.len() != ENTRY_BYTES {
            return Err(value("entry must be 552 bytes"));
        }
        Ok(Self {
            model,
            factory: factory.to_vec(),
            coefficients: Coefficients {
                rows,
                values: coefficients.1.to_vec(),
            },
        })
    }
}

/// Which predict: the calibrated `UnitModel.predict`, or the frozen `Model.predict`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavour {
    Unit,
    Base,
}

/// Port of gxr_studio/camdata/unit_model.py:42 UnitModel.predict (and model.py:43
/// Model.predict for [`Flavour::Base`]), before quantising: clipped float64 RGB.
pub fn predict(
    unit: &UnitModel,
    b: &[u8],
    x: Codes<'_>,
    half: usize,
    flavour: Flavour,
) -> Result<Vec<[f64; 3]>, CoreError> {
    if b.len() != ENTRY_BYTES {
        return Err(value("entry must be 552 bytes"));
    }
    if half > 1 {
        return Err(value("half must be 0 or 1"));
    }
    let model = &unit.model;
    let codes = match flavour {
        Flavour::Unit => input_codes(x, Some(&unit.coefficients)),
        Flavour::Base => input_codes(x, None),
    };
    let pre = model.pre(b, &codes)?;
    let at = 216 + half * 28;
    let row = MatrixRow::from_bytes(&b[at..at + 28])?;
    let zrow = model.sector_z(b)?;
    let q = model.table.coupling(&zrow);
    let m = row.effective();
    let lookup = if b[0] != 3 && flavour == Flavour::Unit {
        let f = MatrixRow::from_bytes(&unit.factory[at..at + 28])?;
        Some((f, f.effective()))
    } else {
        None
    };
    let table = &model.table;
    let mut out = Vec::with_capacity(pre.len());
    for p in pre {
        let (y, ci) = row.apply(&m, p);
        let co = if b[0] == 3 {
            ci
        } else if let Some((f, fm)) = &lookup {
            let cl = f.apply(fm, p).1;
            let (hue, magnitude) = (hue_deg(cl), cl.abs());
            let w = table.kernel_weights(hue, magnitude)?;
            let s_of = table.dot_s(&w);
            ci.times(s_of).times(table.z(&q, &w, s_of))
        } else {
            table.forward(ci, &q)?
        };
        let rgb = rgb_from_ycc(y, co);
        out.push([
            clip(rgb[0], 0.0, 255.0),
            clip(rgb[1], 0.0, 255.0),
            clip(rgb[2], 0.0, 255.0),
        ]);
    }
    Ok(out)
}

/// `np.floor(rgb + .5).astype(np.uint8)` on clipped float64.
pub fn quantize_f64(rgb: &[[f64; 3]]) -> Vec<u8> {
    let mut out = Vec::with_capacity(rgb.len() * 3);
    for p in rgb {
        for &v in p {
            out.push(u8_of((v + 0.5).floor()));
        }
    }
    out
}

/// `astype(np.uint8)` of a value already in `[0, 255.5)`, or NaN (arm64 numpy gives 0).
pub fn u8_of(v: f64) -> u8 {
    if v.is_nan() {
        0
    } else {
        clip(v, 0.0, 255.0) as u8
    }
}

#[cfg(test)]
mod tests {
    use super::interp;

    #[test]
    fn interp_matches_numpy_on_nodes_and_between() {
        let xp = [0.0, 10.0, 20.0, 30.0, 40.0, 4095.0];
        let fp = [0.0, 5.0, 5.0, 9.0, 12.0, 255.0];
        let got = interp(
            &[0.0, 5.0, 10.0, 25.0, 4095.0, 5000.0, -1.0, 39.0],
            &xp,
            &fp,
        );
        assert_eq!(got, vec![0.0, 2.5, 5.0, 7.0, 255.0, 255.0, 0.0, 11.7]);
    }
}
