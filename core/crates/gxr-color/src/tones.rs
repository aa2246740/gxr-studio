//! Desktop-only adjustments: exposure / white-balance gains at the chain input, and the
//! display-domain tone table at its output.
//!
//! Port of gxr_studio/render/tones.py, with scipy's `PchipInterpolator` (derivatives from
//! `_find_derivatives` / `_edge_case`, evaluation in `evaluate_poly1`'s power form).

use gxr_raw::CoreError;

pub const WARM_EV: f64 = 0.35;
pub const TINT_EV: f64 = 0.25;
pub const TABLE_STEP: usize = 4;
pub const TABLE_SIZE: usize = 255 * TABLE_STEP + 1;
pub const SHADOW_HIGHLIGHT_AMOUNT: f64 = 0.13;

/// The tone fields of `params.Adjust` (already parsed and rounded by `parse_adjust`).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Adjust {
    pub exposure_ev: f64,
    pub warm: i64,
    pub tint: i64,
    pub contrast: i64,
    pub highlight: i64,
    pub shadow: i64,
}

impl Adjust {
    pub fn tone_is_identity(&self) -> bool {
        self.contrast == 0 && self.highlight == 0 && self.shadow == 0
    }
}

/// A curve's points, `(x, y)`, already parsed.
pub type Points = Vec<(f64, f64)>;

/// `params.Curves`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Curves {
    pub master: Option<Points>,
    pub r: Option<Points>,
    pub g: Option<Points>,
    pub b: Option<Points>,
}

/// The display table: one row of `TABLE_SIZE`, or one per channel.
#[derive(Clone, Debug, PartialEq)]
pub enum Table {
    Mono(Vec<f32>),
    Rgb([Vec<f32>; 3]),
}

/// A small integer knob (|v| <= 100 after parsing) as float64.
fn knob(v: i64) -> f64 {
    let narrow = i32::try_from(v).map_or(if v < 0 { i32::MIN } else { i32::MAX }, |n| n);
    f64::from(narrow)
}

/// Port of gxr_studio/render/tones.py:28 channel_gains
pub fn channel_gains(a: &Adjust) -> Option<[f64; 3]> {
    if a.exposure_ev == 0.0 && a.warm == 0 && a.tint == 0 {
        return None;
    }
    let warm = knob(a.warm) / 100.0;
    let tint = knob(a.tint) / 100.0;
    let r = a.exposure_ev + WARM_EV * warm + TINT_EV * tint;
    let g = a.exposure_ev;
    let b = a.exposure_ev - WARM_EV * warm + TINT_EV * tint;
    Some([gxr_math::exp2(r), gxr_math::exp2(g), gxr_math::exp2(b)])
}

/// Port of gxr_studio/render/tones.py:39 _smoothstep
fn smoothstep(t: f64) -> f64 {
    t * t * (3.0 - 2.0 * t)
}

/// Port of gxr_studio/render/tones.py:43 tone_function
pub fn tone_function(t: f64, a: &Adjust) -> f64 {
    let mut y = t;
    if a.contrast != 0 {
        let k = knob(a.contrast) / 100.0;
        y = y + k * (smoothstep(y) - y);
    }
    if a.highlight != 0 {
        let s = knob(a.highlight) / 100.0 * SHADOW_HIGHLIGHT_AMOUNT * 6.75;
        y = y + s * y * y * (1.0 - y);
    }
    if a.shadow != 0 {
        let s = knob(a.shadow) / 100.0 * SHADOW_HIGHLIGHT_AMOUNT * 6.75;
        let one = 1.0 - y;
        y = y + s * y * (one * one);
    }
    crate::model::clip(y, 0.0, 1.0)
}

/// `np.sign`.
fn sign(x: f64) -> f64 {
    if x.is_nan() {
        x
    } else if x > 0.0 {
        1.0
    } else if x < 0.0 {
        -1.0
    } else {
        0.0
    }
}

/// A PCHIP interpolant: knots and the four power-basis coefficients per interval.
pub struct Pchip {
    x: Vec<f64>,
    c: Vec<[f64; 4]>,
}

/// Port of scipy/interpolate/_cubic.py PchipInterpolator._edge_case
fn edge_case(h0: f64, h1: f64, m0: f64, m1: f64) -> f64 {
    let d = ((2.0 * h0 + h1) * m0 - h0 * m1) / (h0 + h1);
    let mask = sign(d) != sign(m0);
    let mask2 = sign(m0) != sign(m1) && d.abs() > 3.0 * m0.abs();
    if mask {
        0.0
    } else if mask2 {
        3.0 * m0
    } else {
        d
    }
}

impl Pchip {
    /// `PchipInterpolator(xs, ys)`: `_find_derivatives`, then `CubicHermiteSpline`'s
    /// coefficients.
    pub fn new(x: &[f64], y: &[f64]) -> Result<Self, CoreError> {
        let n = x.len();
        if n < 2 {
            return Err(CoreError::new("value", "`x` must contain at least 2 elements."));
        }
        let hk: Vec<f64> = x.windows(2).map(|w| w[1] - w[0]).collect();
        if hk.iter().any(|h| *h <= 0.0) {
            return Err(CoreError::new("value", "`x` must be strictly increasing sequence."));
        }
        let mk: Vec<f64> = (0..n - 1).map(|i| (y[i + 1] - y[i]) / hk[i]).collect();
        let mut dk = vec![0.0; n];
        if n == 2 {
            dk[0] = mk[0];
            dk[1] = mk[0];
        } else {
            for i in 0..n - 2 {
                let condition = sign(mk[i + 1]) != sign(mk[i]) || mk[i + 1] == 0.0 || mk[i] == 0.0;
                let w1 = 2.0 * hk[i + 1] + hk[i];
                let w2 = hk[i + 1] + 2.0 * hk[i];
                let whmean = (w1 / mk[i] + w2 / mk[i + 1]) / (w1 + w2);
                dk[i + 1] = if condition { 0.0 } else { 1.0 / whmean };
            }
            dk[0] = edge_case(hk[0], hk[1], mk[0], mk[1]);
            dk[n - 1] = edge_case(hk[n - 2], hk[n - 3], mk[n - 2], mk[n - 3]);
        }
        let mut c = Vec::with_capacity(n - 1);
        for i in 0..n - 1 {
            let dx = hk[i];
            let slope = (y[i + 1] - y[i]) / dx;
            let t = (dk[i] + dk[i + 1] - 2.0 * slope) / dx;
            c.push([t / dx, (slope - dk[i]) / dx - t, dk[i], y[i]]);
        }
        Ok(Self { x: x.to_vec(), c })
    }

    /// `PPoly.__call__` with `extrapolate=False` (NaN outside the knots).
    pub fn eval(&self, xv: f64) -> f64 {
        let n = self.x.len();
        let (a, b) = (self.x[0], self.x[n - 1]);
        if !(a <= xv && xv <= b) {
            return f64::NAN;
        }
        let i = if xv == b {
            n - 2
        } else {
            // the unique i with x[i] <= xv < x[i + 1]
            self.x.partition_point(|&k| k <= xv) - 1
        };
        let s = xv - self.x[i];
        let c = &self.c[i];
        let mut res = 0.0 + c[3];
        res = res + c[2] * s;
        let z = s * s;
        res = res + c[1] * z;
        let z = z * s;
        res + c[0] * z
    }
}

/// Port of gxr_studio/render/tones.py:56 _map_curve
fn map_curve(y: &[f64], points: Option<&Points>) -> Result<Vec<f64>, CoreError> {
    let Some(points) = points else {
        return Ok(y.to_vec());
    };
    let xs: Vec<f64> = points.iter().map(|p| p.0).collect();
    let ys: Vec<f64> = points.iter().map(|p| p.1).collect();
    let curve = Pchip::new(&xs, &ys)?;
    let (lo, hi) = (xs[0], xs[xs.len() - 1]);
    Ok(y.iter().map(|&v| crate::model::clip(curve.eval(crate::model::clip(v, lo, hi)), 0.0, 255.0)).collect())
}

/// Port of gxr_studio/render/tones.py:64 display_table
pub fn display_table(a: &Adjust, curves: Option<&Curves>) -> Result<Option<Table>, CoreError> {
    if a.tone_is_identity() && curves.is_none() {
        return Ok(None);
    }
    // np.linspace(0, 255, 1021): i * 0.25, and the last sample set to 255.
    let step = 255.0 / (TABLE_SIZE - 1) as f64;
    let y: Vec<f64> = (0..TABLE_SIZE)
        .map(|i| {
            let x = if i == TABLE_SIZE - 1 { 255.0 } else { i as f64 * step + 0.0 };
            tone_function(x / 255.0, a) * 255.0
        })
        .collect();
    let master = curves.and_then(|c| c.master.as_ref());
    let channels = match curves {
        Some(c) => [c.r.as_ref(), c.g.as_ref(), c.b.as_ref()],
        None => [None, None, None],
    };
    let f32s = |v: Vec<f64>| v.into_iter().map(|x| x as f32).collect::<Vec<f32>>();
    if channels.iter().all(Option::is_none) {
        return Ok(Some(Table::Mono(f32s(map_curve(&y, master)?))));
    }
    let mut rows: [Vec<f32>; 3] = [Vec::new(), Vec::new(), Vec::new()];
    for (row, points) in rows.iter_mut().zip(channels) {
        *row = f32s(map_curve(&map_curve(&y, points)?, master)?);
    }
    Ok(Some(Table::Rgb(rows)))
}

/// `astype(np.int32)` on arm64: toward zero, saturating, NaN to 0.
pub fn trunc_i32_f32(v: f32) -> i32 {
    v as i32
}

/// Port of gxr_studio/render/tones.py:79 apply_table, one value of channel `c`.
pub fn apply_table(value: f32, table: &Table, c: usize) -> f32 {
    let row: &[f32] = match table {
        Table::Mono(t) => t,
        Table::Rgb(rows) => &rows[c],
    };
    let pos = clip_f32(value, 0.0, 255.0) * (TABLE_STEP as f32);
    let i = trunc_i32_f32(pos).min(LAST_CELL);
    // `pos - i` is float32 minus int32: float64.
    let f = f64::from(pos) - f64::from(i);
    let at = cell_index(i);
    let (lo, hi) = (row[at], row[at + 1]);
    (f64::from(lo) + f64::from(hi - lo) * f) as f32
}

const LAST_CELL: i32 = 1019;

/// `i` is in `0..=LAST_CELL` here: `pos` was clipped to `[0, 1020]` (NaN casts to 0).
fn cell_index(i: i32) -> usize {
    match usize::try_from(i) {
        Ok(v) => v,
        Err(_) => 0,
    }
}

/// `np.clip` on float32: NaN passes through.
pub fn clip_f32(x: f32, lo: f32, hi: f32) -> f32 {
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

#[cfg(test)]
mod tests {
    use super::{Pchip, Table, apply_table};

    #[test]
    fn pchip_goes_through_its_knots() {
        let p = Pchip::new(&[0.0, 64.0, 128.0, 255.0], &[0.0, 80.0, 140.0, 255.0]).unwrap();
        assert_eq!(p.eval(0.0), 0.0);
        assert!((p.eval(64.0) - 80.0).abs() < 1e-12);
        assert!((p.eval(255.0) - 255.0).abs() < 1e-12);
        assert!(p.eval(256.0).is_nan());
        assert!(Pchip::new(&[0.0, 0.0], &[1.0, 2.0]).is_err());
    }

    #[test]
    fn apply_table_reads_the_last_cell() {
        let t = Table::Mono((0..1021).map(|i| i as f32 * 0.25).collect());
        assert_eq!(apply_table(255.0, &t, 0), 255.0);
        assert_eq!(apply_table(-3.0, &t, 0), 0.0);
        assert!(apply_table(f32::NAN, &t, 0).is_nan());
    }
}
