//! Checked `u64` size arithmetic, shared `u32::MAX` count limit, and fallible allocations.
//!
//! Ensures native (64-bit) and WASM (32-bit) compute identical bounds and return identical
//! `CoreError`s without panicking or wrapping on crafted inputs.

use gxr_raw::CoreError;

/// Shared `2**32 - 1` count limit before converting to `usize`.
pub fn fit_count(n: u64, err_msg: &str) -> Result<usize, CoreError> {
    if n > u64::from(u32::MAX) {
        return Err(CoreError::new("dat_error", err_msg));
    }
    usize::try_from(n).map_err(|_| CoreError::new("dat_error", err_msg))
}

pub fn mul_u64(a: u64, b: u64, err_msg: &str) -> Result<u64, CoreError> {
    a.checked_mul(b)
        .ok_or_else(|| CoreError::new("dat_error", err_msg))
}

pub fn add_u64(a: u64, b: u64, err_msg: &str) -> Result<u64, CoreError> {
    a.checked_add(b)
        .ok_or_else(|| CoreError::new("dat_error", err_msg))
}

pub fn try_vec_with_capacity<T>(cap: usize, err_msg: &str) -> Result<Vec<T>, CoreError> {
    let mut v = Vec::new();
    v.try_reserve_exact(cap)
        .map_err(|_| CoreError::new("dat_error", err_msg))?;
    Ok(v)
}

pub fn try_zeroed_vec(len: usize, err_msg: &str) -> Result<Vec<u8>, CoreError> {
    let mut v = try_vec_with_capacity(len, err_msg)?;
    v.resize(len, 0);
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_counts_above_u32_max() {
        assert!(fit_count(u64::from(u32::MAX), "big").is_ok());
        assert!(fit_count(u64::from(u32::MAX) + 1, "big").is_err());
        assert!(mul_u64(u64::from(u32::MAX), u64::from(u32::MAX), "big").is_ok());
        assert!(mul_u64(u64::MAX, 2, "big").is_err());
    }
}
