//! Port of gxr_studio/raw/dng.py: the `Dng` reader (tags and checks). Pixel access lives in
//! `mosaic.rs`, `summary()` in `summary.rs`.

use std::cell::OnceCell;

use crate::error::{CoreError, PyErr, PyResult};
use crate::pyrepr::{
    Scalar, py_float, py_float_list_repr, py_float_repr, py_int, py_int_list_repr,
    py_int_tuple_repr, py_list_repr, py_round, py_round_float,
};
use crate::tiff::{Ifd, Tiff, Value, val};

/// A float64 array (`black_pattern`). Ordinary patterns are 2-D; an n-D repeat is kept
/// so `raw_layout` can return `black_level_pattern` the way Python does.
#[derive(Clone, Debug, PartialEq)]
pub struct Grid {
    pub rows: usize,
    pub cols: usize,
    /// Numpy shape in C order. A 2-D pattern is `[rows, cols]`.
    pub shape: Vec<usize>,
    pub values: Vec<f64>,
}

impl Grid {
    pub fn at(&self, r: usize, c: usize) -> f64 {
        self.values[r * self.cols + c]
    }
}

/// One EXIF value as `Dng.exif` stores it.
#[derive(Clone, Debug, PartialEq)]
pub enum ExifValue {
    /// A finite float that was rational 0/0 (or any non-finite float): `None`.
    None,
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Ints(Vec<i64>),
    Floats(Vec<f64>),
}

/// Port of gxr_studio/raw/dng.py:32 Dng.
#[derive(Debug)]
pub struct Dng<'a> {
    pub data: &'a [u8],
    /// `self.path.name`: the file's base name, or `<bytes>`.
    pub name: String,
    pub big_endian: bool,
    pub width: i64,
    pub height: i64,
    pub bps: i64,
    pub(crate) strips: (Vec<Scalar>, Vec<Scalar>),
    /// Always 2×2 after construction (`_check_window`), row-major.
    pub cfa: [[i64; 2]; 2],
    pub lin_table: Option<Vec<f64>>,
    /// (top, left, bottom, right).
    pub active: [i64; 4],
    pub black_pattern: Grid,
    pub black_dh: Option<Vec<f64>>,
    pub black_dv: Option<Vec<f64>>,
    pub white: f64,
    pub default_scale: Vec<f64>,
    /// (x, y) relative to the ActiveArea.
    pub crop_origin: (i64, i64),
    /// (w, h).
    pub crop_size: (i64, i64),
    pub orientation: i64,
    pub as_shot_neutral: Option<Vec<f64>>,
    pub as_shot_white_xy: Option<Vec<f64>>,
    pub analog_balance: Vec<f64>,
    pub baseline_exposure: f64,
    /// In Python's insertion order; `"error"` when the EXIF IFD could not be read.
    pub exif: Vec<(&'static str, ExifValue)>,
    pub(crate) mosaic_cache: OnceCell<crate::mosaic::Mosaic>,
}

const EXIF_TAGS: [(u16, &str); 7] = [
    (34855, "iso"),
    (33434, "t"),
    (33437, "f"),
    (36867, "when"),
    (41987, "wb_exif"),
    (37383, "metering"),
    (34850, "program"),
];
const EXIF_BIAS: (u16, &str) = (37380, "bias_ev");

fn none_type_subscript() -> PyErr {
    PyErr::Type("'NoneType' object is not subscriptable".into())
}

fn none_type_iter() -> PyErr {
    PyErr::Type("'NoneType' object is not iterable".into())
}

/// `val(d, tag)[0]` with no default.
fn first_required(d: &Ifd, tag: u16) -> PyResult<Scalar> {
    val(d, tag).ok_or_else(none_type_subscript)?.first()
}

/// `val(d, tag, [default])[0]`.
fn first_or(d: &Ifd, tag: u16, default: Scalar) -> PyResult<Scalar> {
    match val(d, tag) {
        Some(v) => v.first(),
        None => Ok(default),
    }
}

/// `[int(v) for v in val(d, tag, default)]`.
fn ints_or(d: &Ifd, tag: u16, default: &[i64]) -> PyResult<Vec<i64>> {
    match val(d, tag) {
        Some(v) => v.items().iter().map(py_int).collect(),
        None => Ok(default.to_vec()),
    }
}

/// `[float(v) for v in val(d, tag, default)]`.
fn floats_or(d: &Ifd, tag: u16, default: &[f64]) -> PyResult<Vec<f64>> {
    match val(d, tag) {
        Some(v) => v.items().iter().map(py_float).collect(),
        None => Ok(default.to_vec()),
    }
}

/// `float((1 << bps) - 1)`; Python evaluates the shift even when the tag is present.
fn default_white(bps: i64) -> PyResult<Option<f64>> {
    if bps < 0 {
        return Err(PyErr::Value("negative shift count".into()));
    }
    if bps >= 1024 {
        return Ok(None);
    }
    if bps <= 53 {
        #[allow(clippy::cast_precision_loss)]
        return Ok(Some(((1u64 << bps) - 1) as f64));
    }
    // 2**bps - 1 rounds to 2**bps in float64. `bps` is in `54..1024`, so the biased
    // exponent fits in `u64`; a failure is a `CoreError`, not a zero exponent.
    let exponent = u64::try_from(1023 + bps)
        .map_err(|_| PyErr::Overflow("int too large to convert to float".into()))?;
    Ok(Some(f64::from_bits(exponent << 52)))
}

fn exif_value(v: &Value) -> ExifValue {
    let single = match v {
        Value::Ints(x) if x.len() == 1 => Some(ExifValue::Int(x[0])),
        Value::Floats(x) if x.len() == 1 => Some(if x[0].is_finite() {
            ExifValue::Float(x[0])
        } else {
            ExifValue::None
        }),
        _ => None,
    };
    single.unwrap_or_else(|| match v {
        Value::Ascii(s) => ExifValue::Text(s.clone()),
        Value::Bytes(b) => ExifValue::Bytes(b.clone()),
        Value::Ints(x) => ExifValue::Ints(x.clone()),
        Value::Floats(x) => ExifValue::Floats(x.clone()),
    })
}

fn read_exif(t: &Tiff<'_>, root: &Ifd) -> Vec<(&'static str, ExifValue)> {
    let mut out = Vec::new();
    let Some(entry) = root.get(&34665) else {
        return out;
    };
    let ex = entry.value.first().and_then(|o| t.ifd_at(&o));
    match ex {
        Ok(ex) => {
            for (tag, name) in EXIF_TAGS.iter().chain(std::iter::once(&EXIF_BIAS)) {
                if let Some(v) = val(&ex, *tag) {
                    out.push((*name, exif_value(v)));
                }
            }
        }
        Err(e) => out.push(("error", ExifValue::Text(e.text().to_string()))),
    }
    out
}

/// Port of the black-level block in `Dng.__init__`: expand a single value with
/// `np.full(bdim[0] * bdim[1], …)`, then `reshape(bdim)`.
fn black_grid(bdim: &[i64], mut bl: Vec<f64>) -> PyResult<Grid> {
    let missing = || PyErr::Index("list index out of range".into());
    let b0 = *bdim.first().ok_or_else(missing)?;
    let b1 = *bdim.get(1).ok_or_else(missing)?;
    let prod = i128::from(b0) * i128::from(b1);
    if bl.len() == 1 {
        if prod < 0 {
            return Err(PyErr::Value("negative dimensions are not allowed".into()));
        }
        let fill = bl[0];
        bl = crate::bounds::fill_f64(
            u128::try_from(prod).map_err(|_| crate::bounds::too_big())?,
            fill,
        )?;
    }
    let bl_size = i128::try_from(bl.len()).map_err(|_| crate::bounds::too_big())?;
    if bl_size != prod {
        return Err(PyErr::NotImplemented(format!(
            "BlackLevel count {bl_size} for repeat {}",
            py_int_list_repr(bdim)
        )));
    }
    let shape = crate::bounds::numpy_reshape(bl_size, bdim)?;
    let (rows, cols) = match shape.as_slice() {
        [r, c, ..] => (*r, *c),
        // `bdim[1]` was required above, and reshape keeps every axis, so this is unreachable.
        _ => (0, 0),
    };
    Ok(Grid {
        rows,
        cols,
        shape,
        values: bl,
    })
}

/// `np.max` over a non-empty float64 array (NaN propagates).
pub(crate) fn np_max(values: &[f64]) -> PyResult<f64> {
    let mut it = values.iter();
    let Some(&first) = it.next() else {
        return Err(PyErr::Value(
            "zero-size array to reduction operation maximum which has no identity".into(),
        ));
    };
    Ok(it.fold(first, |m, &v| {
        if m.is_nan() || v.is_nan() {
            f64::NAN
        } else {
            m.max(v)
        }
    }))
}

struct Raw<'d> {
    ifd: &'d Ifd,
}

fn select_raw<'d>(root: &'d Ifd, subs: &'d [Ifd]) -> PyResult<Option<Raw<'d>>> {
    for d in std::iter::once(root).chain(subs.iter()) {
        let photometric = first_or(d, 262, Scalar::Int(i64::MIN))?;
        if photometric.eq_int(32803) && first_or(d, 254, Scalar::Int(0))?.eq_int(0) {
            return Ok(Some(Raw { ifd: d }));
        }
    }
    Ok(None)
}

impl<'a> Dng<'a> {
    /// Port of gxr_studio/raw/dng.py:305 open_dng: read a DNG, turning every parse failure into
    /// the `RawDecodeError` text Python shows. `name` plays the role of `path.name`.
    pub fn open(data: &'a [u8], name: &str) -> Result<Self, CoreError> {
        Self::new(data, name).map_err(PyErr::into_open_error)
    }

    /// Port of gxr_studio/raw/dng.py:35 Dng.__init__.
    #[allow(clippy::too_many_lines)]
    pub(crate) fn new(data: &'a [u8], name: &str) -> PyResult<Self> {
        let t = Tiff::new(data)?;
        let root = t.ifd(i64::from(t.root), None)?;
        let subs: Vec<Ifd> = match val(&root, 330) {
            Some(v) => v
                .items()
                .iter()
                .map(|o| t.ifd_at(o))
                .collect::<PyResult<_>>()?,
            None => Vec::new(),
        };
        let raw = select_raw(&root, &subs)?
            .ok_or_else(|| PyErr::Value(format!("{name}: no full-resolution CFA IFD")))?
            .ifd;
        let width = py_int(&first_required(raw, 256)?)?;
        let height = py_int(&first_required(raw, 257)?)?;
        let bps = py_int(&first_required(raw, 258)?)?;
        if !first_or(raw, 259, Scalar::Int(1))?.eq_int(1) {
            let shown = match val(raw, 259) {
                Some(Value::Ascii(s)) => s.clone(),
                Some(v) => py_list_repr(&v.items()),
                None => "None".into(),
            };
            return Err(PyErr::NotImplemented(format!("compression {shown}")));
        }
        if !first_or(raw, 277, Scalar::Int(1))?.eq_int(1) {
            return Err(PyErr::NotImplemented(
                "SamplesPerPixel != 1 (not a CFA image)".into(),
            ));
        }
        if !first_or(raw, 266, Scalar::Int(1))?.eq_int(1) {
            return Err(PyErr::NotImplemented("FillOrder 2".into()));
        }
        if raw.contains_key(&322) || raw.contains_key(&324) {
            return Err(PyErr::NotImplemented("tiled raw".into()));
        }
        let strips = (
            val(raw, 273).ok_or_else(none_type_iter)?.items(),
            val(raw, 279).ok_or_else(none_type_iter)?.items(),
        );
        let dim = ints_or(raw, 33421, &[2, 2])?;
        let pat: Vec<i64> = match val(raw, 33422) {
            None => return Err(none_type_iter()),
            Some(Value::Bytes(b)) => b.iter().map(|&v| i64::from(v)).collect(),
            Some(v) => v.items().iter().map(py_int).collect::<PyResult<_>>()?,
        };
        // `np.array(pat, int).reshape(dim)`: the same checks in numpy's order (at most 64 axes,
        // one unknown axis, size), and the shape numpy resolves is the one kept.
        let size = i128::try_from(pat.len()).map_err(|_| crate::bounds::too_big())?;
        let dim: Vec<i64> = crate::bounds::numpy_reshape(size, &dim)?
            .into_iter()
            .map(|d| i64::try_from(d).map_err(|_| crate::bounds::too_big()))
            .collect::<PyResult<_>>()?;
        let plane = match val(raw, 50710) {
            Some(v) => v.items(),
            None => vec![Scalar::Int(0), Scalar::Int(1), Scalar::Int(2)],
        };
        let plane_ok = plane.len() == 3 && (0..3).all(|i| plane[i].eq_int(i64::from(i as u8)));
        let mut colours = pat.clone();
        colours.sort_unstable();
        colours.dedup();
        if !plane_ok || colours != [0, 1, 2] {
            return Err(PyErr::NotImplemented(format!(
                "CFA colours {} / pattern {}",
                py_list_repr(&plane),
                py_int_list_repr(&pat)
            )));
        }
        if !first_or(raw, 50711, Scalar::Int(1))?.eq_int(1) {
            return Err(PyErr::NotImplemented("non-rectangular CFALayout".into()));
        }
        let lin_table = match raw.get(&50712) {
            None => None,
            Some(e) if e.typ != 3 => {
                return Err(PyErr::Value("LinearizationTable must be SHORT".into()));
            }
            Some(e) => Some(e.value.as_f64()?),
        };
        let aa: Vec<i64> = match val(raw, 50829) {
            Some(v) => v.items().iter().map(py_round).collect::<PyResult<_>>()?,
            None => vec![0, 0, height, width],
        };
        let tuple_index = || PyErr::Index("tuple index out of range".into());
        let aw = aa.get(3).ok_or_else(tuple_index)? - aa[1];
        let ah = aa[2] - aa[0];
        let bdim = ints_or(raw, 50713, &[1, 1])?;
        let bl = match val(raw, 50714) {
            Some(v) => v.as_f64()?,
            None => vec![0.0],
        };
        let list_index = || PyErr::Index("list index out of range".into());
        let black_pattern = black_grid(&bdim, bl)?;
        let black_dh = val(raw, 50715).map(Value::as_f64).transpose()?;
        let black_dv = val(raw, 50716).map(Value::as_f64).transpose()?;
        if black_dh
            .as_ref()
            .is_some_and(|v| i64::try_from(v.len()).ok() != Some(aw))
        {
            return Err(PyErr::Value("BlackLevelDeltaH length".into()));
        }
        if black_dv
            .as_ref()
            .is_some_and(|v| i64::try_from(v.len()).ok() != Some(ah))
        {
            return Err(PyErr::Value("BlackLevelDeltaV length".into()));
        }
        let white_default = default_white(bps)?;
        let white = match val(raw, 50717) {
            Some(v) => py_float(&v.first()?)?,
            None => white_default
                .ok_or_else(|| PyErr::Overflow("int too large to convert to float".into()))?,
        };
        let default_scale = floats_or(raw, 50718, &[1.0, 1.0])?;
        let co = floats_or(raw, 50719, &[0.0, 0.0])?;
        #[allow(clippy::cast_precision_loss)]
        let cs = floats_or(raw, 50720, &[aw as f64, ah as f64])?;
        for &v in co.iter().chain(cs.iter()) {
            py_round_float(v)?;
            // `v - round(v)` is exact in Python (an int); rounding in float64 gives the same value
            if (v - v.round_ties_even()).abs() > 1e-9 {
                return Err(PyErr::NotImplemented(format!(
                    "fractional default crop {} {}",
                    py_float_list_repr(&co),
                    py_float_list_repr(&cs)
                )));
            }
        }
        let pick = |v: &[f64], i: usize| -> PyResult<i64> {
            py_round_float(*v.get(i).ok_or_else(list_index)?)
        };
        let crop_origin = (pick(&co, 0)?, pick(&co, 1)?);
        let crop_size = (pick(&cs, 0)?, pick(&cs, 1)?);
        let orientation = py_int(&first_or(&root, 274, Scalar::Int(1))?)?;
        let as_shot_neutral = val(&root, 50728).map(Value::as_f64).transpose()?;
        let as_shot_white_xy = val(&root, 50729).map(Value::as_f64).transpose()?;
        let analog_balance = match val(&root, 50727) {
            Some(v) => v.as_f64()?,
            None => vec![1.0, 1.0, 1.0],
        };
        let baseline_exposure = py_float(&first_or(&root, 50730, Scalar::Float(0.0))?)?;
        let exif = read_exif(&t, &root);
        let cfa_shape = dim.clone();
        let cfa = if cfa_shape == [2, 2] {
            [[pat[0], pat[1]], [pat[2], pat[3]]]
        } else {
            [[0; 2]; 2]
        };
        let active: [i64; 4] = match aa.as_slice() {
            [a, b, c, d] => [*a, *b, *c, *d],
            _ => {
                // `top, left, bottom, right = self.active` in _check_window, after _check_finite
                // and _check_levels; those run first below.
                [i64::MIN; 4]
            }
        };
        let dng = Self {
            data,
            name: name.to_string(),
            big_endian: t.big_endian,
            width,
            height,
            bps,
            strips,
            cfa,
            lin_table,
            active,
            black_pattern,
            black_dh,
            black_dv,
            white,
            default_scale,
            crop_origin,
            crop_size,
            orientation,
            as_shot_neutral,
            as_shot_white_xy,
            analog_balance,
            baseline_exposure,
            exif,
            mosaic_cache: OnceCell::new(),
        };
        dng.check_finite()?;
        dng.check_levels()?;
        if aa.len() != 4 {
            return Err(PyErr::Value(format!(
                "too many values to unpack (expected 4, got {})",
                aa.len()
            )));
        }
        dng.check_window(&cfa_shape)?;
        Ok(dng)
    }

    /// Port of gxr_studio/raw/dng.py:140 Dng._check_finite.
    fn check_finite(&self) -> PyResult<()> {
        let white = [self.white];
        let baseline = [self.baseline_exposure];
        let checks: [(&str, Option<&[f64]>); 8] = [
            ("BlackLevel", Some(&self.black_pattern.values)),
            ("BlackLevelDeltaH", self.black_dh.as_deref()),
            ("BlackLevelDeltaV", self.black_dv.as_deref()),
            ("WhiteLevel", Some(&white)),
            ("DefaultScale", Some(&self.default_scale)),
            ("LinearizationTable", self.lin_table.as_deref()),
            ("AnalogBalance", Some(&self.analog_balance)),
            ("BaselineExposure", Some(&baseline)),
        ];
        for (name, values) in checks {
            if values.is_some_and(|v| !v.iter().all(|x| x.is_finite())) {
                return Err(PyErr::Value(format!("{name} has a zero denominator")));
            }
        }
        Ok(())
    }

    /// Port of gxr_studio/raw/dng.py:155 Dng._check_levels.
    fn check_levels(&self) -> PyResult<()> {
        let mut black = np_max(&self.black_pattern.values)?;
        black += match &self.black_dh {
            Some(v) if !v.is_empty() => np_max(v)?,
            _ => 0.0,
        };
        black += match &self.black_dv {
            Some(v) if !v.is_empty() => np_max(v)?,
            _ => 0.0,
        };
        // a NaN must fail this, as Python's `not self.white > black` does
        #[allow(clippy::neg_cmp_op_on_partial_ord)]
        if !(self.white > black) {
            return Err(PyErr::Value(format!(
                "WhiteLevel {} must be greater than BlackLevel {}",
                py_float_repr(self.white),
                py_float_repr(black)
            )));
        }
        Ok(())
    }

    /// Port of gxr_studio/raw/dng.py:163 Dng._check_window.
    fn check_window(&self, cfa_shape: &[i64]) -> PyResult<()> {
        let [top, left, bottom, right] = self.active;
        if !(0 <= top
            && top < bottom
            && bottom <= self.height
            && 0 <= left
            && left < right
            && right <= self.width)
        {
            return Err(PyErr::Value(format!(
                "ActiveArea {} outside the {}x{} raw plane",
                py_int_tuple_repr(&self.active),
                self.width,
                self.height
            )));
        }
        let (y0, x0, h, w) = self.crop_window();
        if h <= 0 || w <= 0 || y0 + h > bottom || x0 + w > right {
            return Err(PyErr::Value(format!(
                "DefaultCrop outside the ActiveArea: {}",
                py_int_tuple_repr(&[y0, x0, h, w])
            )));
        }
        if cfa_shape != [2, 2] {
            return Err(PyErr::NotImplemented(format!(
                "CFA repeat {}",
                py_int_tuple_repr(cfa_shape)
            )));
        }
        Ok(())
    }

    /// Port of gxr_studio/raw/dng.py:216 Dng.crop_window: (y0, x0, h, w) in raw-plane coordinates.
    pub fn crop_window(&self) -> (i64, i64, i64, i64) {
        (
            self.active[0] + self.crop_origin.1,
            self.active[1] + self.crop_origin.0,
            self.crop_size.1,
            self.crop_size.0,
        )
    }

    /// `self.endian`: `"big"` or `"little"`.
    pub fn endian(&self) -> &'static str {
        if self.big_endian { "big" } else { "little" }
    }

    /// `self._strips`: (StripOffsets, StripByteCounts) as Python holds them.
    pub fn strip_values(&self) -> (&[Scalar], &[Scalar]) {
        (&self.strips.0, &self.strips.1)
    }
}
