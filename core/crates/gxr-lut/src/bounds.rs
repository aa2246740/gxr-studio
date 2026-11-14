//! Checked `u64` size arithmetic, shared `u32::MAX` element limit, and fallible allocations.
//! Mirrors `gxr_raw::bounds` so native (64-bit) and wasm32 behave identically.

use gxr_raw::CoreError;

/// numpy's `ValueError` message when `nelem * itemsize` exceeds the limit.
pub const ARRAY_TOO_BIG: &str =
    "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.";

#[inline]
pub fn too_big_core() -> CoreError {
    CoreError::raw_decode(ARRAY_TOO_BIG)
}

#[inline]
pub fn mul_u64(a: u64, b: u64) -> Result<u64, CoreError> {
    a.checked_mul(b).ok_or_else(too_big_core)
}

#[inline]
pub fn add_u64(a: u64, b: u64) -> Result<u64, CoreError> {
    a.checked_add(b).ok_or_else(too_big_core)
}

/// Shared element count limit (`u32::MAX`) before casting to `usize`.
#[inline]
pub fn fit_count_u64(n: u64) -> Result<usize, CoreError> {
    if n > u64::from(u32::MAX) {
        return Err(too_big_core());
    }
    usize::try_from(n).map_err(|_| too_big_core())
}

/// Checked product of `u64` dimensions.
pub fn checked_product_u64(dims: &[u64]) -> Result<u64, CoreError> {
    let mut acc = 1u64;
    for &d in dims {
        acc = mul_u64(acc, d)?;
    }
    Ok(acc)
}

/// Checked product of `u64` dimensions, capped at `u32::MAX` before converting to `usize`.
pub fn checked_elements(dims: &[u64]) -> Result<usize, CoreError> {
    fit_count_u64(checked_product_u64(dims)?)
}

/// Fallible `Vec` capacity reservation, mapping allocation errors to `too_big_core()`.
pub fn try_vec_cap<T>(n: usize) -> Result<Vec<T>, CoreError> {
    let mut out = Vec::new();
    out.try_reserve_exact(n).map_err(|_| too_big_core())?;
    Ok(out)
}

/// Fallible `Vec` filled with `n` copies of `val`.
pub fn try_vec_filled<T: Clone>(n: usize, val: T) -> Result<Vec<T>, CoreError> {
    let mut out = try_vec_cap(n)?;
    out.resize(n, val);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn huge_dimensions_return_identical_error() {
        assert_eq!(
            checked_elements(&[4_294_967_295, 4_294_967_295, 3]).unwrap_err(),
            too_big_core()
        );
        assert_eq!(
            checked_elements(&[2_147_483_648, 1, 3]).unwrap_err(),
            too_big_core()
        );
        assert_eq!(
            fit_count_u64(u64::from(u32::MAX) + 1).unwrap_err(),
            too_big_core()
        );
        assert_eq!(
            fit_count_u64(u64::from(u32::MAX)).unwrap(),
            u32::MAX as usize
        );
    }
}
