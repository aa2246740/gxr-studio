//! Port of gxr_studio/raw/render.py:57 render_full with any renderer: white-balanced
//! demosaic strips (`STRIP_ROWS` rows at a time), `require_finite`, `display`, orientation.
//!
//! Peak memory is the DNG, its decoded mosaic, one float32 strip and the uint8 output (and
//! its oriented copy); there is never a full-frame float buffer.

use gxr_color::{Codes, chroma_u8};
use gxr_raw::codes::{STRIP_ROWS, crop_shape};
use gxr_raw::mosaic::require_finite;
use gxr_raw::{CoreError, Dng, Image, demosaic_strips, orient_array, wb_gains};

use super::plan::RecipeDisplay;

/// numpy's text when `arr.size * arr.dtype.itemsize` cannot be allocated, as `gxr-raw` raises it.
const TOO_BIG: &str =
    "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.";

fn too_big() -> CoreError {
    CoreError::raw_decode(TOO_BIG)
}

/// The count limit every core crate shares: at most `u32::MAX` elements, so wasm32 and 64-bit
/// native refuse the same inputs.
fn fit(n: u64) -> Result<usize, CoreError> {
    if n > u64::from(u32::MAX) {
        return Err(too_big());
    }
    usize::try_from(n).map_err(|_| too_big())
}

/// The crop-window check `gxr_raw::render_full` makes before allocating (`check_crop_window`
/// is private to `gxr-raw`; this is the same test with the same errors), so a recipe render
/// refuses a bad DNG exactly as the neutral render does.
fn check_crop_window(dng: &Dng<'_>) -> Result<(i64, i64), CoreError> {
    let (h, w) = crop_shape(dng);
    if h < 4 || w < 4 {
        return Err(CoreError::raw_decode("画面太小"));
    }
    let cy0 = dng.active[0]
        .checked_add(dng.crop_origin.1)
        .ok_or_else(too_big)?;
    let cx0 = dng.active[1]
        .checked_add(dng.crop_origin.0)
        .ok_or_else(too_big)?;
    let cy1 = cy0.checked_add(h).ok_or_else(too_big)?;
    let cx1 = cx0.checked_add(w).ok_or_else(too_big)?;
    if dng.crop_origin.0 < 0
        || dng.crop_origin.1 < 0
        || cy0 < dng.active[0]
        || cx0 < dng.active[1]
        || cy1 > dng.active[2]
        || cx1 > dng.active[3]
        || cy0 < 0
        || cx0 < 0
        || cy1 > dng.height
        || cx1 > dng.width
    {
        return Err(CoreError::new(
            "index_error",
            format!("block [{cy0}, {cy1}) x [{cx0}, {cx1}) outside the raw plane"),
        ));
    }
    Ok((h, w))
}

/// Port of gxr_studio/raw/render.py:57 render_full.
///
/// `display(strip, out)` writes `strip.values.len()` uint8 codes for one float32
/// `(rows, w, 3)` strip, as `Renderer.display` does.
pub fn render_full_with(
    dng: &Dng<'_>,
    mut display: impl FnMut(&Image<f32>, &mut [u8]) -> Result<(), CoreError>,
) -> Result<Image<u8>, CoreError> {
    // The same order as `gxr_raw::render_full`: window, white balance and mosaic before the
    // output buffer, so a truncated file reports its missing strips.
    let (h, w) = check_crop_window(dng)?;
    wb_gains(dng)?;
    dng.mosaic()?;
    let uh = u64::try_from(h).map_err(|_| too_big())?;
    let uw = u64::try_from(w).map_err(|_| too_big())?;
    let row = uw.checked_mul(3).ok_or_else(too_big)?;
    let len = fit(uh.checked_mul(row).ok_or_else(too_big)?)?;
    let mut values: Vec<u8> = Vec::new();
    values.try_reserve_exact(len).map_err(|_| too_big())?;
    values.resize(len, 0);
    let row = fit(row)?;
    demosaic_strips(dng, STRIP_ROWS, |y0, strip| {
        require_finite(strip.values.iter().map(|&v| f64::from(v)), "去马赛克结果")?;
        let start = usize::try_from(y0)
            .ok()
            .and_then(|y| y.checked_mul(row))
            .ok_or_else(too_big)?;
        let end = start
            .checked_add(strip.values.len())
            .filter(|&e| e <= values.len())
            .ok_or_else(too_big)?;
        display(&strip, &mut values[start..end])
    })?;
    let out = Image {
        height: fit(uh)?,
        width: fit(uw)?,
        channels: 3,
        values,
    };
    if dng.orientation == 1 {
        Ok(out)
    } else {
        orient_array(&out, dng.orientation)
    }
}

/// `chain.chroma_u8(codes, bk, gains, table)` on one strip: the recipe renderer's `display`.
pub fn recipe_display(r: &RecipeDisplay, strip: &Image<f32>, out: &mut [u8]) {
    let codes = chroma_u8(
        Codes::F32(&strip.values),
        &r.bake,
        r.gains,
        r.table.as_ref(),
    );
    out.copy_from_slice(&codes);
}

/// `render_full(dng, renderer)` for a recipe renderer.
pub fn render_recipe(dng: &Dng<'_>, r: &RecipeDisplay) -> Result<Image<u8>, CoreError> {
    render_full_with(dng, |strip, out| {
        recipe_display(r, strip, out);
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn huge_counts_are_refused() {
        assert_eq!(fit(u64::from(u32::MAX) + 1).unwrap_err().message, TOO_BIG);
        assert!(fit(u64::from(u32::MAX)).is_ok());
    }
}
