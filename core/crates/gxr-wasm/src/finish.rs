//! Finish raster exports. Owner: wave-2 unit raster.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::raster::logo::KeyMode;
use gxr_finish::{draw_frame_canvas_u64, finish_array_u64, fit_logo_json, prepare_logo_key};
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

/// Finished unframed RGB uint8 image with optional normalized valid-rect header and CLI-matching
/// JSON report.
#[wasm_bindgen]
pub struct FinishImage {
    height: u32,
    width: u32,
    rgb: Vec<u8>,
    header: Option<String>,
    report: String,
}

#[wasm_bindgen]
impl FinishImage {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn header(&self) -> Option<String> {
        self.header.clone()
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn rgb(self) -> Vec<u8> {
        self.rgb
    }
}

/// Framed canvas uint8 RGB image and CLI-matching JSON report.
#[wasm_bindgen]
pub struct FrameCanvasImage {
    height: u32,
    width: u32,
    rgb: Vec<u8>,
    report: String,
}

#[wasm_bindgen]
impl FrameCanvasImage {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn rgb(self) -> Vec<u8> {
        self.rgb
    }
}

/// Keyed and trimmed custom logo RGBA image and CLI-matching JSON report.
#[wasm_bindgen]
pub struct KeyedLogo {
    height: u32,
    width: u32,
    rgba: Vec<u8>,
    report: String,
}

#[wasm_bindgen]
impl KeyedLogo {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn rgba(self) -> Vec<u8> {
        self.rgba
    }
}

/// Port of `finish/compose.py:finish_unframed` (without `apply_stamp` or `HdrTrack`).
#[wasm_bindgen]
pub fn finish_array(
    rgb: &[u8],
    w: u32,
    h: u32,
    edit_json: &str,
    options_json: &str,
) -> Result<FinishImage, JsValue> {
    let res = finish_array_u64(rgb, u64::from(w), u64::from(h), edit_json, options_json)
        .map_err(rejected)?;
    let report = format!("{}\n", res.report_json().render());
    let height =
        u32::try_from(res.height).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    let width =
        u32::try_from(res.width).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    Ok(FinishImage {
        height,
        width,
        rgb: res.rgb,
        header: res.header,
        report,
    })
}

/// Port of `finish/frame_paint.py:background` + `finish/frame.py:draw_frame` (`photo` paste only).
#[wasm_bindgen]
pub fn finish_frame_canvas(
    rgb: &[u8],
    w: u32,
    h: u32,
    layout_json: &str,
) -> Result<FrameCanvasImage, JsValue> {
    let res =
        draw_frame_canvas_u64(rgb, u64::from(w), u64::from(h), layout_json).map_err(rejected)?;
    let report = format!("{}\n", res.report_json().render());
    let height =
        u32::try_from(res.height).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    let width =
        u32::try_from(res.width).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    Ok(FrameCanvasImage {
        height,
        width,
        rgb: res.rgb,
        report,
    })
}

/// Port of `finish/logo_store.py` keying + trimming + stats on decoded sRGB RGBA uint8 bytes.
#[wasm_bindgen]
pub fn finish_logo_key(rgba: &[u8], w: u32, h: u32) -> Result<KeyedLogo, JsValue> {
    let res = prepare_logo_key(rgba, u64::from(w), u64::from(h), KeyMode::Auto, 30, "", "")
        .map_err(rejected)?;
    let report = format!("{}\n", res.report_json().render());
    let height =
        u32::try_from(res.height).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    let width =
        u32::try_from(res.width).map_err(|_| rejected(gxr_finish::geometry::bounds::too_big()))?;
    Ok(KeyedLogo {
        height,
        width,
        rgba: res.rgba,
        report,
    })
}

/// Port of `finish/logo_math.py` sizing and `clogo` item layout.
#[wasm_bindgen]
pub fn finish_logo_fit(info_json: &str, request_json: &str) -> Result<String, JsValue> {
    let out = fit_logo_json(info_json, request_json).map_err(rejected)?;
    Ok(format!("{out}\n"))
}
