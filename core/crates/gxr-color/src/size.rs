//! Sizes that come from input (npy shapes, bake grids). The same rule as `gxr-raw`'s bounds:
//! products in checked `u64`, at most `u32::MAX` elements so wasm32 and 64-bit native take the
//! same path, then a fallible allocation. Every failure is the one `CoreError` below.

use gxr_raw::CoreError;

/// numpy's text when `arr.size * arr.dtype.itemsize` cannot be allocated.
pub const ARRAY_TOO_BIG: &str =
    "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.";

pub fn too_big() -> CoreError {
    CoreError::new("value", ARRAY_TOO_BIG)
}

/// A count both pointer widths can hold.
pub fn fit(n: u64) -> Result<usize, CoreError> {
    if n > u64::from(u32::MAX) {
        return Err(too_big());
    }
    usize::try_from(n).map_err(|_| too_big())
}

/// The product of `dims`, checked, within the shared count limit. Each dimension must fit
/// too, so a zero-sized array with a huge axis is refused the same way on both targets.
pub fn count(dims: &[u64]) -> Result<u64, CoreError> {
    let mut n: u64 = 1;
    for &d in dims {
        fit(d)?;
        n = n.checked_mul(d).ok_or_else(too_big)?;
    }
    fit(n)?;
    Ok(n)
}

/// `vec![value; n]` without aborting when the allocator says no.
pub fn try_vec<T: Clone>(n: usize, value: T) -> Result<Vec<T>, CoreError> {
    let mut out = Vec::new();
    out.try_reserve_exact(n).map_err(|_| too_big())?;
    out.resize(n, value);
    Ok(out)
}

/// A copy of `src` with a fallible allocation.
pub fn try_copy<T: Clone>(src: &[T]) -> Result<Vec<T>, CoreError> {
    let mut out = Vec::new();
    out.try_reserve_exact(src.len()).map_err(|_| too_big())?;
    out.extend_from_slice(src);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::{count, fit};

    #[test]
    fn huge_dimensions_are_refused_with_one_error() {
        let big = u64::from(u32::MAX);
        assert_eq!(
            count(&[big, big]).unwrap_err().message,
            super::ARRAY_TOO_BIG
        );
        assert!(count(&[2_147_483_648, 2]).is_err());
        assert_eq!(count(&[2_147_483_648, 1]).unwrap(), 2_147_483_648);
        assert!(count(&[0, big + 1]).is_err());
        assert!(fit(big).is_ok());
    }
}
