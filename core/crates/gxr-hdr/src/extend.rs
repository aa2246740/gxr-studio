//! Index extension for scipy `mode="reflect"` and `mode="nearest"`.
//!
//! Reflect does not repeat the edge sample: index −1 of a length-4 axis is 1.
//! The period is `2n − 2`. A length of 0 or 1 maps every index to 0.

use gxr_raw::CoreError;

use crate::error::container;

/// scipy `mode="reflect"` (`_nd_image` extend, connectivity of the border).
pub fn reflect(index: i64, n: i64) -> Result<i64, CoreError> {
    if n <= 1 {
        return Ok(0);
    }
    let period = n
        .checked_mul(2)
        .and_then(|v| v.checked_sub(2))
        .ok_or_else(|| container("轴太长"))?;
    let mut m = index.rem_euclid(period);
    if m >= n {
        m = period - m;
    }
    Ok(m)
}

/// scipy `mode="nearest"`: clamp into `0 .. n`.
pub fn nearest(index: i64, n: i64) -> i64 {
    if n <= 0 {
        return 0;
    }
    if index < 0 {
        0
    } else if index >= n {
        n - 1
    } else {
        index
    }
}
