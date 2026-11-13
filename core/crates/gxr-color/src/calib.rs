//! Calibration npz checks. `_context` is the import-time gate.

use gxr_raw::CoreError;

use crate::npy::Npy;
use crate::npz::load;

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

fn missing(name: &str) -> CoreError {
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
    let d0 = find(&arrays, "d0")?.f64s()?;
    let d1 = find(&arrays, "d1")?.f64s()?;
    let coeff = find(&arrays, "input_coefficients")?;
    let coefficients = coeff.f64s()?;
    let rows = match coeff.shape.as_slice() {
        [4, 3] => 4,
        [10, 3] => 10,
        _ => return Err(calib("校准数据的数组尺寸不正确")),
    };
    if find(&arrays, "d0")?.shape != [24] || find(&arrays, "d1")?.shape != [24] {
        return Err(calib("校准数据的数组尺寸不正确"));
    }
    if !all_finite(&d0) || !all_finite(&d1) || !all_finite(&coefficients) {
        return Err(calib("校准数据含非有限数"));
    }
    let stored = find(&arrays, "S_eff")?.complexes()?;
    let computed = s_eff(stock, &d0, &d1)?;
    if stored.len() != 24 || !same_complex(&stored, &computed) {
        return Err(calib("S_eff 不能由 stock/d0/d1 逐位还原"));
    }
    Ok(Context { d0, d1, coefficients, rows })
}

fn find<'a>(arrays: &'a [(String, Npy)], name: &str) -> Result<&'a Npy, CoreError> {
    arrays.iter().find(|(key, _)| key == name).map(|(_, array)| array).ok_or_else(|| missing(name))
}

fn all_finite(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite())
}

/// `((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32`, float64.
fn word_f64(v: i64) -> Result<f64, CoreError> {
    let narrow = i16::try_from(v).map_err(|_| value_word())?;
    Ok(f64::from(narrow))
}

fn value_word() -> CoreError {
    CoreError::new("value", "stock word")
}

fn s_eff(stock: &[[i64; 2]; 24], d0: &[f64], d1: &[f64]) -> Result<Vec<(f64, f64)>, CoreError> {
    let mut out = Vec::with_capacity(24);
    for i in 0..24 {
        let re = word_f64(stock[i][0])? + d0[i];
        let im = word_f64(stock[i][1])? + d1[i];
        out.push((re / 32.0, -(im / 32.0)));
    }
    Ok(out)
}

fn same_complex(stored: &[(f64, f64)], computed: &[(f64, f64)]) -> bool {
    stored.len() == computed.len()
        && stored.iter().zip(computed).all(|(a, b)| a.0 == b.0 && a.1 == b.1)
}
