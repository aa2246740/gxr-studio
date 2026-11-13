//! Geometry exports. Owner: wave-1 unit geometry.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::{final_box_json, parse_geometry, warp};
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

/// Port of `gpu/box.py:final_box` returning `{"box":[x,y,w,h],"valid":[vx,vy,vw,vh],"header":"..."}`.
#[wasm_bindgen]
pub fn geom_final_box(
    w: u32,
    h: u32,
    geometry_json: &str,
    crop_json: &str,
) -> Result<String, JsValue> {
    final_box_json(w as usize, h as usize, geometry_json, crop_json)
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
    w: u32,
    h: u32,
    geometry_json: &str,
) -> Result<WarpedImage, JsValue> {
    let geom = parse_geometry(geometry_json).map_err(rejected)?;
    let warped = warp(rgb_u8, w as usize, h as usize, &geom).map_err(rejected)?;
    let height = u32::try_from(warped.height)
        .map_err(|_| rejected(CoreError::new("bad_geometry", "输出尺寸过大")))?;
    let width = u32::try_from(warped.width)
        .map_err(|_| rejected(CoreError::new("bad_geometry", "输出尺寸过大")))?;
    Ok(WarpedImage {
        height,
        width,
        rgb: warped.rgb,
        mask: warped.valid,
    })
}
