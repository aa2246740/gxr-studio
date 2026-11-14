//! The absolute pre-table sector model, forward direction only.
//!
//! Port of gxr_studio/engine/pretable.py. `inverse_stock`, `_radius`, `brentq` and the
//! monotone scan's result are not ported (PORT-WAVE-1 A.7). The scan's one side effect that
//! can fail, `ValueError(kernel)` for a kernel name `weights` does not know, is kept.

use gxr_raw::CoreError;

use crate::cplx::{C, J};
use crate::json_in::Value;
use crate::sector::{Kernel, effective, hue_deg, maximum, weights};

/// The kernel name as the ring JSON gives it.
#[derive(Clone, Debug, PartialEq)]
pub enum KernelName {
    Known(Kernel),
    /// Any other value. `weights` raises `ValueError(str(name))` when it is reached; the
    /// Fourier branch treats it as `hard`.
    Other(String),
}

/// `PreTable(model, stock)`.
#[derive(Clone, Debug)]
pub struct PreTable {
    pub s: [C; 24],
    pub k: f64,
    pub nodes: [f64; 24],
    pub s0: f64,
    pub kappa: f64,
    pub kernel: KernelName,
    pub gain: [f64; 24],
    coef: Vec<[C; 24]>,
    dc: [f64; 24],
    om: [f64; 32],
}

fn key(name: &str) -> CoreError {
    CoreError::new("key", format!("'{name}'"))
}

fn number(v: &Value, what: &str) -> Result<f64, CoreError> {
    match v {
        Value::Int(i) => Ok(*i as f64),
        Value::Float(f) => Ok(*f),
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        _ => Err(CoreError::new("type", format!("{what} must be a number"))),
    }
}

fn need<'a>(obj: &'a Value, name: &str) -> Result<&'a Value, CoreError> {
    obj.get(name).ok_or_else(|| key(name))
}

/// `str(value)` for the JSON scalars a kernel name can be.
fn py_str(v: &Value) -> String {
    match v {
        Value::Str(s) => s.clone(),
        Value::Int(i) => i.to_string(),
        Value::Float(f) => gxr_raw::json::float_text(*f),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        Value::Array(_) | Value::Object(_) => "<container>".into(),
    }
}

/// The deviation every malformed-shape ring takes (see docs/mobile.md): Python fails later
/// with a numpy broadcast error; the port refuses the ring with the node check's text.
fn bad_ring() -> CoreError {
    CoreError::new("value", "校准色相环的节点或参数不正确")
}

fn list24(v: &Value, what: &str) -> Result<[f64; 24], CoreError> {
    let items = v.as_array().ok_or_else(bad_ring)?;
    let mut out = [0.0; 24];
    match items.len() {
        24 => {
            for (slot, item) in out.iter_mut().zip(items) {
                *slot = number(item, what)?;
            }
        }
        1 if what == "gain" => out = [number(&items[0], what)?; 24],
        _ => return Err(bad_ring()),
    }
    Ok(out)
}

impl PreTable {
    /// Port of gxr_studio/engine/pretable.py:18 PreTable.__init__
    pub fn new(model: &Value, stock: [C; 24]) -> Result<Self, CoreError> {
        let k = number(need(model, "k_coupling")?, "k_coupling")?;
        let nodes = list24(need(model, "nodes_unwrapped_deg")?, "nodes")?;
        let kern = need(model, "kernel")?;
        if !matches!(kern, Value::Object(_)) {
            return Err(CoreError::new("attribute", "kernel must be an object"));
        }
        let s0 = match kern.get("blur_deg") {
            Some(v) => number(v, "blur_deg")?,
            None => 0.0,
        };
        let kappa = match kern.get("blur_kappa") {
            Some(v) => number(v, "blur_kappa")?,
            None => 0.0,
        };
        let kernel = match need(kern, "type")? {
            Value::Str(s) if s == "linear" => KernelName::Known(Kernel::Linear),
            Value::Str(s) if s == "hard" => KernelName::Known(Kernel::Hard),
            other => KernelName::Other(py_str(other)),
        };
        let per = model.get("gain_per_sector").filter(|v| truthy(v));
        let gain = match per {
            Some(v) => list24(v, "gain")?,
            None => [number(need(model, "gain")?, "gain")?; 24],
        };
        let (coef, dc, om) = fourier(&nodes, &kernel);
        let table = Self {
            s: stock,
            k,
            nodes,
            s0,
            kappa,
            kernel,
            gain,
            coef,
            dc,
            om,
        };
        table.scan()?;
        Ok(table)
    }

    /// The monotone scan in `__init__` evaluates `S_of` at radii 5, 10, 15, 20 and 40. Only
    /// its failure is observable: a kernel `weights` rejects, reached on the `sigma <= 30`
    /// branch.
    fn scan(&self) -> Result<(), CoreError> {
        if let KernelName::Other(name) = &self.kernel {
            for r in [5.0, 10.0, 15.0, 20.0, 40.0] {
                if self.sigma(r) <= 30.0 {
                    return Err(CoreError::new("value", name.clone()));
                }
            }
        }
        Ok(())
    }

    /// `np.sqrt(s0**2 + (kappa / np.maximum(cm, 1e-12))**2)`.
    fn sigma(&self, cm: f64) -> f64 {
        let t = self.kappa / maximum(cm, 1e-12);
        (self.s0 * self.s0 + t * t).sqrt()
    }

    /// Port of gxr_studio/engine/pretable.py:28 PreTable.kernel_weights
    ///
    /// One hue and its chroma magnitude. Rows are independent in Python, so one at a time is
    /// the same arithmetic.
    pub fn kernel_weights(&self, h: f64, cm: f64) -> Result<[f64; 24], CoreError> {
        let sigma = self.sigma(cm);
        let mut w = if sigma <= 30.0 {
            let kernel = match &self.kernel {
                KernelName::Known(k) => *k,
                KernelName::Other(name) => return Err(CoreError::new("value", name.clone())),
            };
            weights(&[h], &self.nodes, kernel, &[maximum(sigma, 1e-4)])[0]
        } else {
            self.fourier_row(h, sigma)
        };
        for v in &mut w {
            *v = maximum(*v, 0.0);
        }
        let total = maximum(0.0 + sum24(&w), 1e-12);
        for v in &mut w {
            *v /= total;
        }
        Ok(w)
    }

    /// `dc + 2 * real(phase @ coef)` for one hue: the periodic Gaussian in Fourier form.
    fn fourier_row(&self, h: f64, sigma: f64) -> [f64; 24] {
        let mut phase = [C::real(0.0); 32];
        for (m, slot) in phase.iter_mut().enumerate() {
            let om = self.om[m];
            let rot = J.times(C::real(h)).times(C::real(om)).exp();
            let so = sigma * om;
            let damp = gxr_math::exp(-0.5 * (so * so));
            *slot = rot.times(C::real(damp));
        }
        let mut row = [0.0; 24];
        for s in 0..24 {
            // complex GEMM, one column: running real sums of re*re and im*im, then their
            // difference (the real part), sequentially, no fused multiply-add.
            let mut rr = phase[0].re * self.coef[0][s].re;
            let mut ii = phase[0].im * self.coef[0][s].im;
            for m in 1..32 {
                rr = rr + phase[m].re * self.coef[m][s].re;
                ii = ii + phase[m].im * self.coef[m][s].im;
            }
            row[s] = self.dc[s] + 2.0 * (rr - ii);
        }
        row
    }

    /// `W @ self.S` for one row of weights.
    pub fn dot_s(&self, w: &[f64; 24]) -> C {
        let mut re = w[0] * self.s[0].re;
        let mut im = w[0] * self.s[0].im;
        for s in 1..24 {
            re = re + w[s] * self.s[s].re;
            im = im + w[s] * self.s[s].im;
        }
        C::new(re, im)
    }

    /// Port of gxr_studio/engine/pretable.py:45 PreTable.S_of
    pub fn s_of(&self, h: f64, cm: f64) -> Result<C, CoreError> {
        Ok(self.dot_s(&self.kernel_weights(h, cm)?))
    }

    /// `self.S * (sm.effective(Zrow, self.k) - 1)`, which `z` uses for every pixel.
    pub fn coupling(&self, zrow: &[C; 24]) -> [C; 24] {
        let mut out = [C::real(0.0); 24];
        for s in 0..24 {
            let e = effective(zrow[s], self.k);
            out[s] = self.s[s].times(C::new(e.re - 1.0, e.im - 0.0));
        }
        out
    }

    /// Port of gxr_studio/engine/pretable.py:47 PreTable.z
    ///
    /// `q` is [`PreTable::coupling`] of the entry's sector row.
    pub fn z(&self, q: &[C; 24], w: &[f64; 24], t: C) -> C {
        let mut re = 0.0;
        let mut im = 0.0;
        for s in 0..24 {
            let wg = w[s] * self.gain[s];
            if s == 0 {
                re = wg * q[s].re;
                im = wg * q[s].im;
            } else {
                re = re + wg * q[s].re;
                im = im + wg * q[s].im;
            }
        }
        let ratio = C::new(re, im).over(t);
        C::new(1.0 + ratio.re, 0.0 + ratio.im)
    }

    /// Port of gxr_studio/engine/pretable.py:52 PreTable.forward
    ///
    /// Returns `cb * z(...)`, the first element of Python's tuple.
    pub fn forward(&self, c_in: C, q: &[C; 24]) -> Result<C, CoreError> {
        let h = hue_deg(c_in);
        let m = c_in.abs();
        let w = self.kernel_weights(h, m)?;
        let s_of = self.dot_s(&w);
        let cb = c_in.times(s_of);
        Ok(cb.times(self.z(q, &w, s_of)))
    }
}

/// Python truthiness of a JSON value (`model.get(...) or ...`).
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Int(i) => *i != 0,
        Value::Float(f) => *f != 0.0,
        Value::Str(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// numpy's pairwise sum of 24 float64 values: eight running sums, then a tree.
pub fn sum24(a: &[f64; 24]) -> f64 {
    let mut r = [0.0; 8];
    r.copy_from_slice(&a[..8]);
    for base in [8, 16] {
        for j in 0..8 {
            r[j] += a[base + j];
        }
    }
    ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]))
}

/// The table-only part of the Fourier branch: `coef` (32 x 24), `dc` and `om`.
fn fourier(n: &[f64; 24], kernel: &KernelName) -> (Vec<[C; 24]>, [f64; 24], [f64; 32]) {
    let mut a = [0.0; 24];
    let mut c = [0.0; 24];
    for s in 0..24 {
        a[s] = if s == 0 { n[23] - 360.0 } else { n[s - 1] };
        c[s] = if s == 23 { n[0] + 360.0 } else { n[s + 1] };
    }
    let mut om = [0.0; 32];
    for (m, slot) in om.iter_mut().enumerate() {
        let mode = (m + 1) as f64;
        *slot = 2.0 * std::f64::consts::PI * mode / 360.0;
    }
    // `-1j` in Python is `-(1j)`: (-0.0, -1.0).
    let minus_j = J.negate();
    let e = |om: f64, x: f64| minus_j.times(C::real(om)).times(C::real(x)).exp();
    let linear = matches!(kernel, KernelName::Known(Kernel::Linear));
    let mut coef = Vec::with_capacity(32);
    for &o in &om {
        let mut row = [C::real(0.0); 24];
        for s in 0..24 {
            row[s] = if linear {
                let na = n[s] - a[s];
                let cn = c[s] - n[s];
                let first = e(o, a[s]).over(C::real(na));
                let second = e(o, n[s]).times(C::real(1.0 / na + 1.0 / cn));
                let third = e(o, c[s]).over(C::real(cn));
                first
                    .minus(second)
                    .plus(third)
                    .negate()
                    .over(C::real(360.0 * (o * o)))
            } else {
                // `360 * 1j` is a Python int times a complex: (0.0, 360.0).
                let den = C::new(360.0 * J.re, 360.0 * J.im).times(C::real(o));
                e(o, n[s]).minus(e(o, c[s])).over(den)
            };
        }
        coef.push(row);
    }
    let mut dc = [0.0; 24];
    for s in 0..24 {
        dc[s] = if linear {
            (c[s] - a[s]) / 720.0
        } else {
            (c[s] - n[s]) / 360.0
        };
    }
    (coef, dc, om)
}
