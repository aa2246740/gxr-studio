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

pub fn bad_geometry() -> CoreError {
    CoreError::new(
        "bad_geometry",
        "纠正参数格式不对。请重新打开这张照片后再试。",
    )
}

pub fn bad_rect() -> CoreError {
    CoreError::new("bad_rect", "裁剪框超出画面范围")
}

const I64_MIN_F64: f64 = -9_223_372_036_854_775_808.0;
const I64_UPPER_BOUND_F64: f64 = 9_223_372_036_854_775_808.0;

#[inline]
pub(crate) fn round_even(v: f64) -> Result<i64, CoreError> {
    if !v.is_finite() {
        return Err(bad_rect());
    }
    let r = v.round_ties_even();
    if !r.is_finite() || !(I64_MIN_F64..I64_UPPER_BOUND_F64).contains(&r) {
        return Err(bad_rect());
    }
    Ok(r as i64)
}

/// Validate normalized rect against `(width, height)` pixel bounds and return `(px, py, pw, ph)`.
pub(crate) fn rect_to_pixels(
    rect: [f64; 4],
    width: usize,
    height: usize,
) -> Result<(i64, i64, i64, i64), CoreError> {
    if width == 0 || height == 0 {
        return Err(bad_rect());
    }
    let _ = i64::try_from(width).map_err(|_| bad_rect())?;
    let _ = i64::try_from(height).map_err(|_| bad_rect())?;
    let [x, y, rw, rh] = rect;
    if !x.is_finite() || !y.is_finite() || !rw.is_finite() || !rh.is_finite() {
        return Err(bad_rect());
    }
    let wf = width as f64;
    let hf = height as f64;
    let xw = x * wf;
    let yh = y * hf;
    let rww = rw * wf;
    let rhh = rh * hf;
    let cxw = (x + rw / 2.0) * wf;
    let cyh = (y + rh / 2.0) * hf;
    if !xw.is_finite()
        || !yh.is_finite()
        || !rww.is_finite()
        || !rhh.is_finite()
        || !cxw.is_finite()
        || !cyh.is_finite()
    {
        return Err(bad_rect());
    }
    let px = round_even(xw)?;
    let py = round_even(yh)?;
    let pw_raw = round_even(rww)?;
    let ph_raw = round_even(rhh)?;
    let pw = 1_i64.max(pw_raw);
    let ph = 1_i64.max(ph_raw);
    let _ = px.checked_add(pw_raw).ok_or_else(bad_rect)?;
    let _ = py.checked_add(ph_raw).ok_or_else(bad_rect)?;
    let _ = px.checked_add(pw).ok_or_else(bad_rect)?;
    let _ = py.checked_add(ph).ok_or_else(bad_rect)?;
    Ok((px, py, pw, ph))
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

/// Fallibly reserve capacity for a `Vec<usize>` of up to `n` elements.
pub(crate) fn try_vec_usize_with_capacity(n: u64) -> Result<Vec<usize>, CoreError> {
    let cap = fit_count(n, std::mem::size_of::<usize>() as u64)?;
    let mut out = Vec::new();
    out.try_reserve_exact(cap).map_err(|_| too_big())?;
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

/// Check mask dimensions and verify `valid.len() == w * h`.
pub fn checked_mask_slice(valid: &[u8], w: u64, h: u64) -> Result<(usize, usize, u64), CoreError> {
    let (wu, hu, pixels) = checked_mask_dims(w, h)?;
    let expected = fit_count(pixels, 1)?;
    if valid.len() != expected {
        return Err(bad_geometry());
    }
    Ok((wu, hu, pixels))
}

/// Check RGB dimensions and verify `rgb.len() == w * h * 3`.
pub fn checked_rgb_slice(
    rgb: &[u8],
    w: u64,
    h: u64,
) -> Result<(usize, usize, u64, u64), CoreError> {
    let (wu, hu, pixels) = checked_mask_dims(w, h)?;
    let rgb_len = checked_mul_u64(pixels, 3)?;
    let expected = fit_count(rgb_len, 1)?;
    if rgb.len() != expected {
        return Err(bad_geometry());
    }
    Ok((wu, hu, pixels, rgb_len))
}

/// Check integral sheet dimensions `(w + 1) * (h + 1)` and return row stride `w + 1`.
pub(crate) fn checked_integral_slice(
    sheet: &[i32],
    w: usize,
    h: usize,
) -> Result<usize, CoreError> {
    let _ = checked_mask_dims(w as u64, h as u64)?;
    let w1 = checked_add_u64(w as u64, 1)?;
    let h1 = checked_add_u64(h as u64, 1)?;
    let stride = fit_count(w1, 4)?;
    let elems = checked_mul_u64(h1, w1)?;
    let expected = fit_count(elems, 4)?;
    if sheet.len() != expected {
        return Err(bad_geometry());
    }
    Ok(stride)
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
