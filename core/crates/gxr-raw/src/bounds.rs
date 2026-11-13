//! Sizes that come from a file. Products and sums stay in `i128` or `u64`.
//! A count becomes `usize` only after it fits in `u32`, so wasm32 and 64-bit
//! native take the same path. Nothing here uses `unwrap_or(0)` on a file value.

use crate::error::{CoreError, PyErr};
use crate::pyrepr::np_shape_text;

/// numpy's `ValueError` when `nelem * itemsize` exceeds `2**63 - 1`.
pub(crate) const ARRAY_TOO_BIG: &str =
    "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.";

/// `PY_SSIZE_T_MAX` on the 64-bit Python this port is checked against.
const I64_MAX_U128: u128 = 9_223_372_036_854_775_807;

/// `true` when numpy's 64-bit build refuses the allocation.
pub(crate) fn numpy_nbytes_too_big(nelem: u128, itemsize: u128) -> bool {
    match nelem.checked_mul(itemsize) {
        Some(nbytes) => nbytes > I64_MAX_U128,
        None => true,
    }
}

pub(crate) fn too_big() -> PyErr {
    PyErr::Value(ARRAY_TOO_BIG.into())
}

pub(crate) fn too_big_core() -> CoreError {
    CoreError::raw_decode(ARRAY_TOO_BIG)
}

/// A slice or `Vec` length fits in `i64` because it is at most `isize::MAX`.
pub(crate) fn i64_from_len(n: usize) -> Result<i64, CoreError> {
    i64::try_from(n).map_err(|_| too_big_core())
}

/// A non-negative file count that both pointer widths can store.
pub(crate) fn fit_count(n: u128) -> Result<usize, PyErr> {
    if numpy_nbytes_too_big(n, 1) && n > I64_MAX_U128 {
        return Err(too_big());
    }
    let Ok(n) = u64::try_from(n) else {
        return Err(too_big());
    };
    // `u32::MAX` fits in wasm32 `usize` and in 64-bit `usize`. A larger count
    // would succeed on one target and fail on the other, so both refuse it.
    if n > u64::from(u32::MAX) {
        return Err(too_big());
    }
    usize::try_from(n).map_err(|_| too_big())
}

pub(crate) fn fit_count_core(n: u128) -> Result<usize, CoreError> {
    fit_count(n).map_err(|_| too_big_core())
}

pub(crate) fn mul_u64(a: u64, b: u64) -> Result<u64, CoreError> {
    a.checked_mul(b).ok_or_else(too_big_core)
}

pub(crate) fn add_i64(a: i64, b: i64) -> Result<i64, CoreError> {
    a.checked_add(b)
        .ok_or_else(|| CoreError::new("unexpected", "OverflowError: int too big"))
}

/// `np.full(n, value)` for a float64 fill, without a pointer-width branch.
pub(crate) fn fill_f64(n: u128, value: f64) -> Result<Vec<f64>, PyErr> {
    if n > 0 && numpy_nbytes_too_big(n, 8) {
        return Err(too_big());
    }
    let n_us = fit_count(n)?;
    let mut out = Vec::new();
    out.try_reserve_exact(n_us).map_err(|_| too_big())?;
    out.resize(n_us, value);
    Ok(out)
}

pub(crate) fn reserve_u16(n: u64) -> Result<Vec<u16>, PyErr> {
    if numpy_nbytes_too_big(u128::from(n), 2) {
        return Err(too_big());
    }
    let n_us = fit_count(u128::from(n))?;
    let mut out = Vec::new();
    out.try_reserve_exact(n_us).map_err(|_| too_big())?;
    out.resize(n_us, 0);
    Ok(out)
}

pub(crate) fn reserve_f64(n: u64) -> Result<Vec<f64>, CoreError> {
    if numpy_nbytes_too_big(u128::from(n), 8) {
        return Err(too_big_core());
    }
    let n_us = fit_count_core(u128::from(n))?;
    let mut out = Vec::new();
    out.try_reserve_exact(n_us).map_err(|_| too_big_core())?;
    out.resize(n_us, 0.0);
    Ok(out)
}

fn cannot_reshape(size: i128, dims: &[i64]) -> PyErr {
    let shown: Vec<i64> = dims.iter().map(|&d| if d < 0 { 0 } else { d }).collect();
    // Negatives are numpy's "unknown" axis. The common message prints the
    // requested shape; a leading unknown next to a zero collapses in numpy and
    // is not reproduced character for character (see docs/mobile.md).
    let shape = if dims.iter().any(|&d| d < 0) && dims.contains(&0) {
        np_shape_text(&shown)
    } else if dims.iter().any(|&d| d < 0) {
        let parts: Vec<String> = dims
            .iter()
            .map(|&d| {
                if d < 0 {
                    "newaxis".into()
                } else {
                    d.to_string()
                }
            })
            .collect();
        format!("({})", parts.join(","))
    } else {
        np_shape_text(dims)
    };
    PyErr::Value(format!(
        "cannot reshape array of size {size} into shape {shape}"
    ))
}

/// `ndarray.reshape(dims)` for a C-order array of `size` elements.
pub(crate) fn numpy_reshape(size: i128, dims: &[i64]) -> Result<Vec<usize>, PyErr> {
    if dims.iter().filter(|&&d| d < 0).count() > 1 {
        return Err(PyErr::Value(
            "can only specify one unknown dimension".into(),
        ));
    }
    let mut known: i128 = 1;
    let mut unknown: Option<usize> = None;
    for (i, &dim) in dims.iter().enumerate() {
        if dim < 0 {
            unknown = Some(i);
        } else {
            known = known.saturating_mul(i128::from(dim));
        }
    }
    let mut resolved = dims.to_vec();
    if let Some(slot) = unknown {
        if known == 0 || size.checked_rem(known) != Some(0) {
            return Err(cannot_reshape(size, dims));
        }
        resolved[slot] = i64::try_from(size / known).map_err(|_| too_big())?;
    } else {
        let mut prod: i128 = 1;
        for &dim in dims {
            prod = prod.saturating_mul(i128::from(dim));
        }
        if prod != size {
            return Err(cannot_reshape(size, dims));
        }
    }
    let mut shape = Vec::with_capacity(resolved.len());
    for dim in resolved {
        if dim < 0 {
            return Err(PyErr::Value("negative dimensions are not allowed".into()));
        }
        shape.push(fit_count(u128::try_from(dim).map_err(|_| too_big())?)?);
    }
    Ok(shape)
}

pub(crate) fn shape_text(dims: &[usize]) -> String {
    if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        let parts: Vec<String> = dims.iter().map(ToString::to_string).collect();
        format!("({})", parts.join(","))
    }
}

fn can_broadcast(left: &[usize], right: &[usize]) -> bool {
    let n = left.len().max(right.len());
    for i in 0..n {
        let a = if i < left.len() {
            left[left.len() - 1 - i]
        } else {
            1
        };
        let b = if i < right.len() {
            right[right.len() - 1 - i]
        } else {
            1
        };
        if a != b && a != 1 && b != 1 {
            return false;
        }
    }
    true
}

/// numpy's message for `(v - black)` when the shapes do not broadcast.
/// The text ends with the trailing space numpy 2.4 prints.
pub(crate) fn broadcast_error(left: &[usize], right: &[usize]) -> Option<String> {
    if can_broadcast(left, right) {
        return None;
    }
    Some(format!(
        "operands could not be broadcast together with shapes {} {} ",
        shape_text(left),
        shape_text(right)
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn huge_repeat_is_the_numpy_message() {
        let n = 1u128 << 62;
        assert!(numpy_nbytes_too_big(n, 8));
        assert_eq!(fill_f64(n, 0.0).unwrap_err(), too_big());
    }

    #[test]
    fn reshape_follows_numpy_for_the_black_level_cases() {
        assert_eq!(
            numpy_reshape(1, &[-1, -1]).unwrap_err(),
            PyErr::Value("can only specify one unknown dimension".into())
        );
        assert_eq!(numpy_reshape(1, &[1, 1, 1]).unwrap(), vec![1, 1, 1]);
        assert_eq!(numpy_reshape(0, &[0, 2]).unwrap(), vec![0, 2]);
        assert_eq!(numpy_reshape(4, &[2, 2, -1]).unwrap(), vec![2, 2, 1]);
        assert_eq!(numpy_reshape(4, &[-1, 2]).unwrap(), vec![2, 2]);
    }

    #[test]
    fn broadcast_message_matches_the_one_dimensional_repeat() {
        let msg = broadcast_error(&[4, 6], &[4, 6, 1]).unwrap();
        assert_eq!(
            msg,
            "operands could not be broadcast together with shapes (4,6) (4,6,1) "
        );
    }
}
