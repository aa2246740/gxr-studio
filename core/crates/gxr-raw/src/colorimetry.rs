//! Port of the parts of gxr_studio/lut/colorimetry.py that `lut/scene.py scene_colour` needs:
//! camera → XYZ per DNG 1.7 chapter 6.
//!
//! Python raises `ZeroDivisionError` only where both operands are Python floats (`xy_to_xyz`,
//! the `den` of `xy_to_cct`); everywhere else numpy float64 gives inf/NaN, and so does this port.

use gxr_math::hypot;

use crate::linalg::{EYE, Mat3, Vec3, blend, diag, div_scalar, inv, matmul, matvec, max3, solve};

/// Why the colour maths failed. `scene_colour` turns every one into `SceneError("bad_matrix")`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ColourError {
    /// `ZeroDivisionError` from a Python float division.
    ZeroDivision,
    /// `np.linalg.LinAlgError`.
    Singular,
    /// `ValueError` (for example a vector of the wrong length).
    Value(String),
}

impl From<crate::linalg::Singular> for ColourError {
    fn from(_: crate::linalg::Singular) -> Self {
        Self::Singular
    }
}

/// Port of gxr_studio/lut/colorimetry.py:14 D50_XY (DNG PCS white).
pub const D50_XY: [f64; 2] = [0.3457, 0.3585];
/// Port of gxr_studio/lut/colorimetry.py:17 D65_XY.
pub const D65_XY: [f64; 2] = [0.3127, 0.3290];
/// Port of gxr_studio/lut/colorimetry.py:20 BRADFORD.
pub const BRADFORD: Mat3 = [
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
];

/// Port of gxr_studio/lut/colorimetry.py:24 ILLUMINANT_K (`.get(code, 0.0)`).
pub fn illuminant_k(code: i64) -> f64 {
    match code {
        1 | 4 | 9 | 18 | 20 => 5500.0,
        2 | 14 => 4150.0,
        3 | 17 => 2850.0,
        10 | 19 | 21 => 6500.0,
        11 | 22 => 7500.0,
        12 => 6400.0,
        13 => 5050.0,
        15 => 3525.0,
        16 => 2925.0,
        23 => 5000.0,
        24 => 3200.0,
        _ => 0.0,
    }
}

/// Port of gxr_studio/lut/colorimetry.py:51 ROBERTSON: mired, u, v, slope.
pub const ROBERTSON: [[f64; 4]; 31] = [
    [0.0, 0.18006, 0.26352, -0.24341],
    [10.0, 0.18066, 0.26589, -0.25479],
    [20.0, 0.18133, 0.26846, -0.26876],
    [30.0, 0.18208, 0.27119, -0.28539],
    [40.0, 0.18293, 0.27407, -0.30470],
    [50.0, 0.18388, 0.27709, -0.32675],
    [60.0, 0.18494, 0.28021, -0.35156],
    [70.0, 0.18611, 0.28342, -0.37915],
    [80.0, 0.18740, 0.28668, -0.40955],
    [90.0, 0.18880, 0.28997, -0.44278],
    [100.0, 0.19032, 0.29326, -0.47888],
    [125.0, 0.19462, 0.30141, -0.58204],
    [150.0, 0.19962, 0.30921, -0.70471],
    [175.0, 0.20525, 0.31647, -0.84901],
    [200.0, 0.21142, 0.32312, -1.0182],
    [225.0, 0.21807, 0.32909, -1.2168],
    [250.0, 0.22511, 0.33439, -1.4512],
    [275.0, 0.23247, 0.33904, -1.7298],
    [300.0, 0.24010, 0.34308, -2.0637],
    [325.0, 0.24792, 0.34655, -2.4681],
    [350.0, 0.25591, 0.34951, -2.9641],
    [375.0, 0.26400, 0.35200, -3.5814],
    [400.0, 0.27218, 0.35407, -4.3633],
    [425.0, 0.28039, 0.35577, -5.3762],
    [450.0, 0.28863, 0.35714, -6.7262],
    [475.0, 0.29685, 0.35823, -8.5955],
    [500.0, 0.30505, 0.35907, -11.324],
    [525.0, 0.31320, 0.35968, -15.628],
    [550.0, 0.32129, 0.36011, -23.325],
    [575.0, 0.32931, 0.36038, -40.770],
    [600.0, 0.33724, 0.36051, -116.45],
];

/// Port of gxr_studio/lut/colorimetry.py:88 TINT_SCALE.
pub const TINT_SCALE: f64 = -3000.0;

/// Port of gxr_studio/lut/colorimetry.py:91 xy_to_xyz.
pub fn xy_to_xyz(xy: &[f64; 2]) -> Result<Vec3, ColourError> {
    let (x, y) = (xy[0], xy[1]);
    if y == 0.0 {
        return Err(ColourError::ZeroDivision);
    }
    Ok([x / y, 1.0, (1.0 - x - y) / y])
}

/// Port of gxr_studio/lut/colorimetry.py:96 xyz_to_xy.
pub fn xyz_to_xy(xyz: &Vec3) -> [f64; 2] {
    let s = xyz[0] + xyz[1] + xyz[2];
    if s <= 0.0 {
        return D50_XY;
    }
    [xyz[0] / s, xyz[1] / s]
}

/// `np.maximum(v, 0.0)` (NaN propagates).
fn relu(v: Vec3) -> Vec3 {
    v.map(|x| if x.is_nan() { x } else { x.max(0.0) })
}

/// `np.clip(x, lo, hi)` (NaN propagates).
fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() { x } else { x.max(lo).min(hi) }
}

/// Port of gxr_studio/lut/colorimetry.py:103 bradford: linear Bradford, cone ratios in [0.1, 10].
pub fn bradford(src_xy: &[f64; 2], dst_xy: &[f64; 2]) -> Result<Mat3, ColourError> {
    let w1 = relu(matvec(&BRADFORD, &xy_to_xyz(src_xy)?));
    let w2 = relu(matvec(&BRADFORD, &xy_to_xyz(dst_xy)?));
    let mut a = [0.0; 3];
    for i in 0..3 {
        let r = if w1[i] > 0.0 { w2[i] / w1[i] } else { 10.0 };
        a[i] = clip(r, 0.1, 10.0);
    }
    Ok(matmul(&matmul(&inv(&BRADFORD)?, &diag(&a)), &BRADFORD))
}

/// Port of gxr_studio/lut/colorimetry.py:111 xy_to_cct: (CCT, tint), Robertson's method.
pub fn xy_to_cct(xy: &[f64; 2]) -> Result<(f64, f64), ColourError> {
    let (x, y) = (xy[0], xy[1]);
    let den = 1.5 - x + 6.0 * y;
    if den == 0.0 {
        return Err(ColourError::ZeroDivision);
    }
    let (u, v) = (2.0 * x / den, 3.0 * y / den);
    let (mut last_dt, mut last_du, mut last_dv) = (0.0, 0.0, 0.0);
    for i in 1..31 {
        let [r1, u1, v1, t1] = ROBERTSON[i];
        let ln = hypot(1.0, t1);
        let (du, dv) = (1.0 / ln, t1 / ln);
        let (uu, vv) = (u - u1, v - v1);
        let mut dt = -uu * dv + vv * du;
        if dt <= 0.0 || i == 30 {
            // Python `-min(dt, 0.0)`: min keeps dt unless 0.0 is smaller
            dt = -(if 0.0 < dt { 0.0 } else { dt });
            let f = if i == 1 { 0.0 } else { dt / (last_dt + dt) };
            let r0 = ROBERTSON[i - 1];
            let temp = 1.0e6 / (r0[0] * f + r1 * (1.0 - f));
            let uu = u - (r0[1] * f + u1 * (1.0 - f));
            let vv = v - (r0[2] * f + v1 * (1.0 - f));
            let du = du * (1.0 - f) + last_du * f;
            let dv = dv * (1.0 - f) + last_dv * f;
            let ln = hypot(du, dv);
            let (du, dv) = (du / ln, dv / ln);
            return Ok((temp, (uu * du + vv * dv) * TINT_SCALE));
        }
        (last_dt, last_du, last_dv) = (dt, du, dv);
    }
    Err(ColourError::Value("unreachable".into()))
}

/// The DNG colour tags `DngColour` reads (Python passes a `SimpleNamespace`).
#[derive(Clone, Debug, PartialEq)]
pub struct ColourTags {
    pub color_matrix: [Option<Mat3>; 2],
    pub camera_calibration: [Option<Mat3>; 2],
    pub forward_matrix: [Option<Mat3>; 2],
    pub illuminant: [i64; 2],
    pub analog_balance: Vec<f64>,
}

/// Port of gxr_studio/lut/colorimetry.py:153 DngColour (`matrix="interp"`, the only mode
/// `scene_colour` uses).
#[derive(Clone, Debug, PartialEq)]
pub struct DngColour {
    pub single: bool,
    pub cm: [Mat3; 2],
    pub t: [f64; 2],
    pub cc: [Mat3; 2],
    pub fm: [Option<Mat3>; 2],
    pub ab: Mat3,
    pub illuminant: [i64; 2],
}

/// Port of gxr_studio/lut/colorimetry.py:178 DngColour.__init__.norm (NormalizeColorMatrix).
fn normalise(m: &Mat3, pcs: &Vec3) -> Mat3 {
    let mx = max3(&matvec(m, pcs));
    if mx > 0.0 && !(0.99..=1.01).contains(&mx) {
        div_scalar(m, mx)
    } else {
        *m
    }
}

impl DngColour {
    /// Port of gxr_studio/lut/colorimetry.py:160 DngColour.__init__.
    pub fn new(tags: &ColourTags) -> Result<Self, ColourError> {
        if tags.color_matrix.iter().all(Option::is_none) {
            return Err(ColourError::Value("no ColorMatrix".into()));
        }
        let pcs = xy_to_xyz(&D50_XY)?;
        let cm = tags.color_matrix.map(|m| m.map(|m| normalise(&m, &pcs)));
        let cc = tags.camera_calibration.map(|m| match m {
            Some(matrix) => matrix,
            None => EYE,
        });
        let fm = tags.forward_matrix;
        let t = tags.illuminant.map(illuminant_k);
        let ab = match tags.analog_balance.as_slice() {
            [a, b, c] => diag(&[*a, *b, *c]),
            other => {
                return Err(ColourError::Value(format!(
                    "AnalogBalance has {} values",
                    other.len()
                )));
            }
        };
        let (single, cm, t, cc, fm) = match cm {
            [Some(c0), Some(c1)] if !(t[0] <= 0.0 || t[1] <= 0.0 || t[0] == t[1]) => {
                let order = if t[0] < t[1] { [0, 1] } else { [1, 0] };
                let both = [c0, c1];
                (
                    false,
                    order.map(|i| both[i]),
                    order.map(|i| t[i]),
                    order.map(|i| cc[i]),
                    order.map(|i| fm[i]),
                )
            }
            _ => {
                let k = usize::from(cm[0].is_none());
                let c = match cm[k] {
                    Some(matrix) => matrix,
                    None => EYE,
                };
                let tk = if t[k] == 0.0 { 5000.0 } else { t[k] };
                (true, [c, c], [tk, tk], [cc[k], cc[k]], [fm[k], fm[k]])
            }
        };
        Ok(Self {
            single,
            cm,
            t,
            cc,
            fm,
            ab,
            illuminant: tags.illuminant,
        })
    }

    /// Port of gxr_studio/lut/colorimetry.py:203 DngColour.weight: (g, CCT, tint).
    pub fn weight(&self, xy: &[f64; 2]) -> Result<(f64, f64, f64), ColourError> {
        let (temp, tint) = xy_to_cct(xy)?;
        let g = if self.single || temp <= self.t[0] {
            1.0
        } else if temp >= self.t[1] {
            0.0
        } else {
            (1.0 / temp - 1.0 / self.t[1]) / (1.0 / self.t[0] - 1.0 / self.t[1])
        };
        Ok((g, temp, tint))
    }

    /// Port of gxr_studio/lut/colorimetry.py:214 DngColour.xyz_to_camera.
    pub fn xyz_to_camera(&self, xy: &[f64; 2]) -> Result<Mat3, ColourError> {
        let (g, _, _) = self.weight(xy)?;
        let cm = blend(g, &self.cm[0], &self.cm[1]);
        let cc = blend(g, &self.cc[0], &self.cc[1]);
        Ok(matmul(&matmul(&self.ab, &cc), &cm))
    }

    /// Port of gxr_studio/lut/colorimetry.py:220 DngColour.neutral_to_xy (`max_pass=30`).
    pub fn neutral_to_xy(&self, neutral: &Vec3) -> Result<([f64; 2], usize, bool), ColourError> {
        let max_pass = 30;
        let mut last = D50_XY;
        for k in 0..max_pass {
            let mut nxt = xyz_to_xy(&solve(&self.xyz_to_camera(&last)?, neutral)?);
            if (nxt[0] - last[0]).abs() + (nxt[1] - last[1]).abs() < 1e-7 {
                return Ok((nxt, k + 1, true));
            }
            if k == max_pass - 1 {
                nxt = [0.5 * (last[0] + nxt[0]), 0.5 * (last[1] + nxt[1])];
            }
            last = nxt;
        }
        Ok((last, max_pass, false))
    }

    /// Port of gxr_studio/lut/colorimetry.py:233 DngColour.camera_to_pcs.
    pub fn camera_to_pcs(&self, white_xy: &[f64; 2]) -> Result<Mat3, ColourError> {
        let m = self.xyz_to_camera(white_xy)?;
        let (g, _, _) = self.weight(white_xy)?;
        let fm = match self.fm {
            [Some(f0), Some(f1)] => Some(blend(g, &f0, &f1)),
            [Some(f0), None] => Some(f0),
            [None, _] => None,
        };
        if let Some(fm) = fm {
            let cam_white = matvec(&m, &xy_to_xyz(white_xy)?);
            let top = max3(&cam_white);
            let cam_white = cam_white.map(|v| clip(v / top, 0.001, 1.0));
            let cc = blend(g, &self.cc[0], &self.cc[1]);
            let ind_to_ref = inv(&matmul(&self.ab, &cc))?;
            let ref_white = matvec(&ind_to_ref, &cam_white);
            return Ok(matmul(
                &matmul(&fm, &diag(&ref_white.map(|v| 1.0 / v))),
                &ind_to_ref,
            ));
        }
        let p2c = matmul(&m, &bradford(&D50_XY, white_xy)?);
        let scale = max3(&matvec(&p2c, &xy_to_xyz(&D50_XY)?));
        Ok(inv(&div_scalar(&p2c, scale))?)
    }

    /// Port of gxr_studio/lut/colorimetry.py:251 DngColour.camera_to_xyz (`out_white_xy=D65_XY`).
    pub fn camera_to_xyz(&self, white_xy: &[f64; 2]) -> Result<Mat3, ColourError> {
        Ok(matmul(
            &bradford(&D50_XY, &D65_XY)?,
            &self.camera_to_pcs(white_xy)?,
        ))
    }
}

/// Port of gxr_studio/lut/colorimetry.py:139 white_from_neutral: the white point's xy. Python
/// also builds `white_info`, which calls `weight` and can raise; that check is kept.
pub fn white_from_neutral(col: &DngColour, neutral: &[f64]) -> Result<[f64; 2], ColourError> {
    let [a, b, c] = neutral else {
        return Err(ColourError::Value(format!(
            "neutral has {} values",
            neutral.len()
        )));
    };
    let top = max3(&[*a, *b, *c]);
    let n = [a / top, b / top, c / top];
    let (xy, _, _) = col.neutral_to_xy(&n)?;
    col.weight(&xy)?;
    Ok(xy)
}
