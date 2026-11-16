//! Text & outline rasterizer exports for unit `ink-text`.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::inktext_draw_u64;
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

/// Drawn canvas buffer (`L`, `RGB`, or `RGBA`) and CLI-matching JSON report.
#[wasm_bindgen]
pub struct InkTextImage {
    height: u32,
    width: u32,
    channels: u32,
    pixels: Vec<u8>,
    report: String,
}

#[wasm_bindgen]
impl InkTextImage {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn channels(&self) -> u32 {
        self.channels
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn pixels(self) -> Vec<u8> {
        self.pixels
    }
}

/// Draw text/outline ops onto an `L`, `RGB`, or `RGBA` buffer.
#[wasm_bindgen]
pub fn inktext_draw(
    pixels: &[u8],
    w: f64,
    h: f64,
    mode: &str,
    ops_json: &str,
    font_index_json: &str,
    fonts: &[u8],
) -> Result<InkTextImage, JsValue> {
    let w_u64 = checked_dim_u64(w).unwrap_or(0);
    let h_u64 = checked_dim_u64(h).unwrap_or(0);
    let res = inktext_draw_u64(pixels, w_u64, h_u64, mode, ops_json, font_index_json, fonts)
        .map_err(rejected)?;
    let report = format!("{}\n", res.report_json().render());
    let height =
        u32::try_from(res.height).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    let width =
        u32::try_from(res.width).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    let channels = u32::try_from(res.channels)
        .map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    Ok(InkTextImage {
        height,
        width,
        channels,
        pixels: res.pixels,
        report,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dim_validation_rejects_negative_fractional_and_nonfinite() {
        assert_eq!(checked_dim_u64(0.0), Some(0));
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
