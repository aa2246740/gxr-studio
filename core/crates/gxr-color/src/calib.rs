//! Calibration packs: the `_context` import gate, and `model_for`'s pack checks.
//!
//! Port of gxr_studio/camdata/calib.py. A pack is `manifest.json`, `ctx_<unit>_<iso>.npz`
//! (made by `np.savez`: stored zip members) and `ring_<unit>_<iso>.json`.

use gxr_raw::CoreError;

use crate::json_in::{Value, parse as parse_json};
use crate::model::{Shaped, UnitModel, word_ratio};
use crate::npy::{Npy, decode};
use crate::npz::load;

pub const SCHEMA: &str = "gxr-studio/calib/2";
pub const MODEL_ISOS: [i64; 2] = [200, 1600];
/// `ISO_SPLIT`: below it the ISO 200 model, otherwise ISO 1600.
pub const ISO_SPLIT: i64 = 566;

/// `d0`, `d1` and the input-coefficient matrix from a source npz.
#[derive(Clone, Debug, PartialEq)]
pub struct Context {
    pub d0: Vec<f64>,
    pub d1: Vec<f64>,
    pub coefficients: Vec<f64>,
    pub rows: usize,
}

fn stock_mismatch(message: &str) -> CoreError {
    CoreError::new("stock_mismatch", message)
}

fn calib(message: &str) -> CoreError {
    CoreError::new("calib", message)
}

/// `NpzFile.__getitem__` for a name that is not in the archive.
fn missing(name: &str) -> CoreError {
    CoreError::new("key", format!("'{name} is not a file in the archive'"))
}

/// `KeyError(name)` from a dict lookup.
fn dict_key(name: &str) -> CoreError {
    CoreError::new("key", format!("'{name}'"))
}

/// Port of gxr_studio/camdata/calib.py:62 _context
pub fn context(npz_bytes: &[u8], stock: &[[i64; 2]; 24]) -> Result<Context, CoreError> {
    let arrays = load(npz_bytes)?;
    let stock_arr = find(&arrays, "stock")?;
    let got = stock_arr.i64s()?;
    let mut expect = Vec::with_capacity(48);
    for pair in stock {
        expect.push(pair[0]);
        expect.push(pair[1]);
    }
    if stock_arr.shape != [24, 2] || got != expect {
        return Err(stock_mismatch("校准数据和对应单元原厂 04 的色表行不一致"));
    }
    let d0_arr = find(&arrays, "d0")?;
    let d0 = d0_arr.f64s()?;
    let d1_arr = find(&arrays, "d1")?;
    let d1 = d1_arr.f64s()?;
    let coeff = find(&arrays, "input_coefficients")?;
    let coefficients = coeff.f64s()?;
    let rows = match coeff.shape.as_slice() {
        [4, 3] => 4,
        [10, 3] => 10,
        _ => 0,
    };
    if d0_arr.shape != [24] || d1_arr.shape != [24] || rows == 0 {
        return Err(calib("校准数据的数组尺寸不正确"));
    }
    if !all_finite(&d0) || !all_finite(&d1) || !all_finite(&coefficients) {
        return Err(calib("校准数据含非有限数"));
    }
    let computed = s_eff(stock, &d0, &d1)?;
    let stored_arr = find(&arrays, "S_eff")?;
    let stored = stored_arr.complexes()?;
    if stored_arr.shape != [24] || !same_complex(&stored, &computed) {
        return Err(calib("S_eff 不能由 stock/d0/d1 逐位还原"));
    }
    Ok(Context {
        d0,
        d1,
        coefficients,
        rows,
    })
}

fn find(arrays: &[(String, Vec<u8>)], name: &str) -> Result<Npy, CoreError> {
    let (_, data) = arrays
        .iter()
        .find(|(key, _)| key == name)
        .ok_or_else(|| missing(name))?;
    decode(data)
}

fn all_finite(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite())
}

fn word_f64(v: i64) -> Result<f64, CoreError> {
    let narrow = i16::try_from(v).map_err(|_| CoreError::new("value", "stock word"))?;
    Ok(f64::from(narrow))
}

/// `((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32`, float64 then complex128.
fn s_eff(stock: &[[i64; 2]; 24], d0: &[f64], d1: &[f64]) -> Result<Vec<(f64, f64)>, CoreError> {
    let mut out = Vec::with_capacity(24);
    for i in 0..24 {
        let c = word_ratio(
            word_f64(stock[i][0])? + d0[i],
            word_f64(stock[i][1])? + d1[i],
        );
        out.push((c.re, c.im));
    }
    Ok(out)
}

fn same_complex(stored: &[(f64, f64)], computed: &[(f64, f64)]) -> bool {
    stored.len() == computed.len()
        && stored
            .iter()
            .zip(computed)
            .all(|(a, b)| a.0 == b.0 && a.1 == b.1)
}

/// The three arrays `model_for` reads from a pack's ctx npz, with their shapes.
#[derive(Clone, Debug, PartialEq)]
pub struct CtxArrays {
    pub d0: (Vec<usize>, Vec<f64>),
    pub d1: (Vec<usize>, Vec<f64>),
    pub coefficients: (Vec<usize>, Vec<f64>),
}

impl CtxArrays {
    pub fn coefficients(&self) -> Shaped<'_> {
        (&self.coefficients.0, &self.coefficients.1)
    }
}

/// `(z[k].copy() for k in ("d0", "d1", "input_coefficients"))` in `model_for`.
pub fn load_ctx(npz_bytes: &[u8]) -> Result<CtxArrays, CoreError> {
    let arrays = load(npz_bytes)?;
    let mut out = Vec::with_capacity(3);
    for name in ["d0", "d1", "input_coefficients"] {
        let a = find(&arrays, name)?;
        out.push((a.shape.clone(), a.f64s()?));
    }
    let coefficients = out.pop().unwrap_or_default();
    let d1 = out.pop().unwrap_or_default();
    let d0 = out.pop().unwrap_or_default();
    Ok(CtxArrays {
        d0,
        d1,
        coefficients,
    })
}

/// `UnitModel(stock_words_of_entry(stock04), d0, d1, ring, stock04, coefficients)` from a
/// pack's ctx and ring bytes, as the end of `model_for` builds it.
pub fn unit_model(stock04: &[u8], ctx: &[u8], ring: &[u8]) -> Result<UnitModel, CoreError> {
    let words = crate::colour::stock_words_of_entry(stock04)?;
    let arrays = load_ctx(ctx)?;
    let ring = parse_json(ring)?;
    UnitModel::new(
        &words,
        (&arrays.d0.0, &arrays.d0.1),
        (&arrays.d1.0, &arrays.d1.1),
        &ring,
        stock04,
        arrays.coefficients(),
    )
}

/// `UNIT_ISOS`.
fn unit_isos(unit: &str) -> Option<&'static [i64]> {
    match unit {
        "jlaunch" => Some(&[200, 1600]),
        "qlaunch" | "l08firm" => Some(&[200]),
        _ => None,
    }
}

/// What `model_for` picked: the two file names and its info dict.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pick {
    pub ctx: String,
    pub ring: String,
    pub iso: i64,
    pub calibrated: bool,
    pub model_iso: i64,
}

fn obj_get<'a>(v: &'a Value, key: &str) -> Result<&'a Value, CoreError> {
    if !matches!(v, Value::Object(_)) {
        return Err(CoreError::new(
            "type",
            format!("expected an object at {key:?}"),
        ));
    }
    v.get(key).ok_or_else(|| dict_key(key))
}

/// `Path(name).name == name` for a POSIX path.
fn plain_name(name: &str) -> bool {
    !name.contains('/') && name != "."
}

/// Port of gxr_studio/camdata/calib.py:121 model_for, without the filesystem.
///
/// `read(name)` returns a pack file's bytes (`None` when it is missing). Returns the pick and
/// the chosen ctx and ring bytes.
pub fn model_for(
    manifest: &[u8],
    stock04: &[u8],
    iso: i64,
    unit: &str,
    read: &dyn Fn(&str) -> Option<Vec<u8>>,
) -> Result<(Pick, Vec<u8>, Vec<u8>), CoreError> {
    let m = parse_json(manifest)?;
    if !matches!(&m, Value::Object(_))
        || !matches!(m.get("schema"), Some(Value::Str(s)) if s == SCHEMA)
    {
        return Err(calib("校准数据版本已更新，请重新导入"));
    }
    let isos = unit_isos(unit);
    if isos.is_none() {
        return Err(calib("缺少这个单元的校准数据"));
    }
    let units = obj_get(&m, "units")?;
    if !matches!(units, Value::Object(_)) || units.get(unit).is_none() {
        return Err(calib("缺少这个单元的校准数据"));
    }
    let meta = obj_get(units, unit)?;
    // `entry[115:211]`: a short entry slices short, as in Python.
    let len = stock04.len();
    let fingerprint = gxr_raw::sha256::sha256_hex(&stock04[115.min(len)..211.min(len)]);
    if !matches!(obj_get(meta, "camera_stock_sha256")?, Value::Str(s) if *s == fingerprint) {
        return Err(stock_mismatch(
            "校准数据与这个单元的原厂 04 不匹配，请重新导入",
        ));
    }
    let files = obj_get(&m, "files")?;
    let models = obj_get(meta, "models")?;
    let mut blobs: Vec<(String, Vec<u8>)> = Vec::new();
    for branch in MODEL_ISOS {
        let pair = obj_get(models, &branch.to_string())?;
        let Value::Object(entries) = pair else {
            return Err(CoreError::new(
                "attribute",
                "models entry must be an object",
            ));
        };
        for (_, name) in entries {
            let Value::Str(name) = name else {
                return Err(calib("校准数据的文件清单不正确"));
            };
            if !plain_name(name) || files.get(name).is_none() {
                return Err(calib("校准数据的文件清单不正确"));
            }
            let blob = read(name)
                .ok_or_else(|| CoreError::new("file_not_found", format!("缺少校准文件 {name}")))?;
            let want = obj_get(obj_get(files, name)?, "sha256")?;
            if !matches!(want, Value::Str(s) if *s == gxr_raw::sha256::sha256_hex(&blob)) {
                return Err(calib("校准数据被改动，请重新导入"));
            }
            blobs.push((name.clone(), blob));
        }
    }
    let model_iso = if iso >= ISO_SPLIT { 1600 } else { 200 };
    let pair = obj_get(models, &model_iso.to_string())?;
    let name_of = |key: &str| -> Result<String, CoreError> {
        match obj_get(pair, key)? {
            Value::Str(s) => Ok(s.clone()),
            _ => Err(calib("校准数据的文件清单不正确")),
        }
    };
    let (ctx_name, ring_name) = (name_of("ctx")?, name_of("ring")?);
    let blob = |name: &str| {
        blobs
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, b)| b.clone())
            .ok_or_else(|| dict_key(name))
    };
    let (ctx, ring) = (blob(&ctx_name)?, blob(&ring_name)?);
    let calibrated = isos.is_some_and(|list| list.contains(&iso));
    Ok((
        Pick {
            ctx: ctx_name,
            ring: ring_name,
            iso,
            calibrated,
            model_iso,
        },
        ctx,
        ring,
    ))
}
