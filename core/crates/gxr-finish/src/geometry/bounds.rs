//! Checked `u64` size arithmetic and fallible allocations shared by native and WASM.
//!
//! Element counts are capped at `u32::MAX` (`2**32 - 1`, matching `gxr-raw/src/bounds.rs`) and
//! single-allocation byte sizes are capped at `i32::MAX` (`wasm32`'s `isize::MAX`) before
//! converting to `usize`, so 64-bit native and 32-bit WASM reject the exact same oversized inputs
//! with the exact same `CoreError` without panicking or trapping.

use gxr_raw::CoreError;

/// numpy's `ValueError` when `nelem * itemsize` exceeds the maximum possible size.
pub const ARRAY_TOO_BIG: &str =
    "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.";

/// Maximum element count shared with `gxr-raw` (`2**32 - 1`).
pub const MAX_COUNT: u64 = u32::MAX as u64;

/// Maximum byte size of a single `Vec` on `wasm32` (`isize::MAX == i32::MAX`).
pub const MAX_BYTES: u64 = i32::MAX as u64;

pub fn too_big() -> CoreError {
    CoreError::new("bad_geometry", ARRAY_TOO_BIG)
}

pub fn checked_mul_u64(a: u64, b: u64) -> Result<u64, CoreError> {
    a.checked_mul(b).ok_or_else(too_big)
}

pub fn checked_add_u64(a: u64, b: u64) -> Result<u64, CoreError> {
    a.checked_add(b).ok_or_else(too_big)
}

/// Validate element count `n` and byte size `n * item_size` for both 64-bit native and wasm32.
pub fn fit_count(n: u64, item_size: u64) -> Result<usize, CoreError> {
    if n > MAX_COUNT {
        return Err(too_big());
    }
    let nbytes = checked_mul_u64(n, item_size)?;
    if nbytes > MAX_BYTES {
        return Err(too_big());
    }
    usize::try_from(n).map_err(|_| too_big())
}

/// Fallibly allocate a `Vec<u8>` of length `n` filled with `value`.
pub fn try_vec_u8(n: u64, value: u8) -> Result<Vec<u8>, CoreError> {
    let len = fit_count(n, 1)?;
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| too_big())?;
    out.resize(len, value);
    Ok(out)
}

/// Fallibly allocate a `Vec<i32>` of length `n` filled with `value`.
pub fn try_vec_i32(n: u64, value: i32) -> Result<Vec<i32>, CoreError> {
    let len = fit_count(n, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| too_big())?;
    out.resize(len, value);
    Ok(out)
}

/// Check `(w, h)` for mask allocation and convert to `usize`.
pub fn checked_mask_dims(w: u64, h: u64) -> Result<(usize, usize, u64), CoreError> {
    if w == 0 || h == 0 {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    }
    let _ = fit_count(w, 1)?;
    let _ = fit_count(h, 1)?;
    let pixels = checked_mul_u64(w, h)?;
    let _ = fit_count(pixels, 1)?;
    let wu = usize::try_from(w).map_err(|_| too_big())?;
    let hu = usize::try_from(h).map_err(|_| too_big())?;
    Ok((wu, hu, pixels))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_huge_counts_and_bytes_identically() {
        assert_eq!(checked_mul_u64(u64::MAX, 2).unwrap_err(), too_big());
        assert_eq!(
            checked_mask_dims(4_294_967_295, 4_294_967_295).unwrap_err(),
            too_big()
        );
        assert_eq!(checked_mask_dims(2_147_483_648, 1).unwrap_err(), too_big());
        assert_eq!(
            fit_count(u64::from(u32::MAX) + 1, 1).unwrap_err(),
            too_big()
        );
        assert_eq!(fit_count(2_147_483_648, 1).unwrap_err(), too_big());
        assert_eq!(try_vec_u8(2_147_483_648, 1).unwrap_err(), too_big());
        assert_eq!(try_vec_i32(536_870_912, 0).unwrap_err(), too_big());
        assert!(fit_count(2_147_483_647, 1).is_ok());
    }
}
