//! Geometry exports. Owner: wave-1 unit geometry.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::{final_box_json_u64, parse_geometry, warp_u64};
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

const U64_UPPER_BOUND_F64: f64 = 18_446_744_073_709_551_616.0;

fn checked_dim_u64(v: f64) -> Option<u64> {
    if v.is_finite() && v.fract() == 0.0 && v >= 0.0 {
        if v < U64_UPPER_BOUND_F64 {
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            Some(v as u64)
        } else {
            Some(u64::MAX)
        }
    } else {
        None
    }
}

/// Port of `gpu/box.py:final_box` returning `{"box":[x,y,w,h],"valid":[vx,vy,vw,vh],"header":"..."}`.
#[wasm_bindgen]
pub fn geom_final_box(
    w: f64,
    h: f64,
    geometry_json: &str,
    crop_json: &str,
) -> Result<String, JsValue> {
    let w_u64 = checked_dim_u64(w).unwrap_or(u64::MAX);
    let h_u64 = checked_dim_u64(h).unwrap_or(u64::MAX);
    final_box_json_u64(w_u64, h_u64, geometry_json, crop_json)
        .map(|json| json.render())
        .map_err(rejected)
}

/// Warped uint8 RGB image and boolean valid mask (`0` or `1` per pixel) in row-major order.
#[wasm_bindgen]
pub struct WarpedImage {
    height: u32,
    width: u32,
    rgb: Vec<u8>,
    mask: Vec<u8>,
}

#[wasm_bindgen]
impl WarpedImage {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Warped RGB bytes `(height * width * 3)`.
    pub fn rgb(&self) -> Vec<u8> {
        self.rgb.clone()
    }

    /// Valid pixel mask `(height * width)` as `0` / `1` bytes.
    pub fn mask(&self) -> Vec<u8> {
        self.mask.clone()
    }
}

/// Port of `finish/geometry.py:warp` on a row-major uint8 `(h, w, 3)` RGB buffer.
#[wasm_bindgen]
pub fn geom_warp(
    rgb_u8: &[u8],
    w: f64,
    h: f64,
    geometry_json: &str,
) -> Result<WarpedImage, JsValue> {
    let geom = parse_geometry(geometry_json).map_err(rejected)?;
    let w_u64 = checked_dim_u64(w).unwrap_or(0);
    let h_u64 = checked_dim_u64(h).unwrap_or(0);
    let warped = warp_u64(rgb_u8, w_u64, h_u64, &geom).map_err(rejected)?;
    let height = u32::try_from(warped.height)
        .map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    let width = u32::try_from(warped.width)
        .map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    Ok(WarpedImage {
        height,
        width,
        rgb: warped.rgb,
        mask: warped.valid,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dim_validation_rejects_negative_fractional_and_nonfinite() {
        assert_eq!(checked_dim_u64(0.0), Some(0));
        assert_eq!(checked_dim_u64(2.0), Some(2));
        assert_eq!(checked_dim_u64(4_294_967_298.0), Some(4_294_967_298));
        assert_eq!(
            checked_dim_u64(9_007_199_254_740_992.0),
            Some(9_007_199_254_740_992)
        );
        assert_eq!(checked_dim_u64(U64_UPPER_BOUND_F64), Some(u64::MAX));
        assert_eq!(checked_dim_u64(-1.0), None);
        assert_eq!(checked_dim_u64(1.5), None);
        assert_eq!(checked_dim_u64(f64::NAN), None);
        assert_eq!(checked_dim_u64(f64::INFINITY), None);
        assert_eq!(checked_dim_u64(f64::NEG_INFINITY), None);
    }
}
