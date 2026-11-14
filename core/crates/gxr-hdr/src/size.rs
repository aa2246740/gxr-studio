//! Sizes that come from the caller (dimensions, factors, shapes).
//!
//! Products are checked `u64`. A count becomes `usize` only when it is at most `2**32 - 1`,
//! the limit `gxr-raw::bounds::fit_count` applies (that helper is crate-private, so this
//! module repeats the rule). Allocations go through `try_reserve_exact`. Every failure is
//! the same `CoreError`, so native and wasm32 refuse the same inputs with the same text.

use gxr_raw::CoreError;

/// numpy's text for an array whose byte size exceeds `2**63 - 1`.
pub const ARRAY_TOO_BIG: &str =
    "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.";

/// The one error for a size this port refuses.
pub fn too_big() -> CoreError {
    CoreError::new("value_error", ARRAY_TOO_BIG)
}

/// The element count of `dims`, refused above `2**32 - 1`.
pub fn count(dims: &[u64]) -> Result<usize, CoreError> {
    let mut n: u64 = 1;
    for &d in dims {
        n = n.checked_mul(d).ok_or_else(too_big)?;
    }
    if n > u64::from(u32::MAX) {
        return Err(too_big());
    }
    usize::try_from(n).map_err(|_| too_big())
}

/// `usize` → `u64` for a dimension already held in memory.
pub fn dim(n: usize) -> u64 {
    u64::try_from(n).unwrap_or(u64::MAX)
}

/// An empty vector with room for `n` elements, or `too_big`.
pub fn with_capacity<T>(n: usize) -> Result<Vec<T>, CoreError> {
    let mut out = Vec::new();
    out.try_reserve_exact(n).map_err(|_| too_big())?;
    Ok(out)
}

/// `n` copies of `value`, or `too_big`.
pub fn filled<T: Clone>(n: usize, value: T) -> Result<Vec<T>, CoreError> {
    let mut out = with_capacity(n)?;
    out.resize(n, value);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn huge_products_are_refused_alike() {
        assert_eq!(
            count(&[4_294_967_295, 4_294_967_295]).unwrap_err().message,
            ARRAY_TOO_BIG
        );
        assert_eq!(count(&[u64::MAX, 2]).unwrap_err().kind, "value_error");
        assert_eq!(count(&[2_147_483_648, 1]).unwrap(), 2_147_483_648);
        assert_eq!(count(&[]).unwrap(), 1);
    }
}
