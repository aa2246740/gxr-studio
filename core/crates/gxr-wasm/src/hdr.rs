//! HDR exports (wave-1 unit hdr): alignment, the uint8 gain plane with its metadata, and
//! the Ultra HDR container. Strings are the bytes `gxr-cli` prints (JSON plus the newline
//! `println!` adds). A core error is thrown as `{"error": {"kind", "message"}}`.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_hdr as hdr;
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

fn plane(values: &[f32], height: u32, width: u32) -> Result<hdr::Plane32, JsValue> {
    let h = usize::try_from(height).map_err(|_| rejected(CoreError::new("bad_input", "数组太大")))?;
    let w = usize::try_from(width).map_err(|_| rejected(CoreError::new("bad_input", "数组太大")))?;
    if h.checked_mul(w) != Some(values.len()) {
        return Err(rejected(CoreError::new("bad_input", "数组长度和形状对不上")));
    }
    Ok(hdr::Plane32 {
        h,
        w,
        v: values.to_vec(),
    })
}

/// `align(sdr_y, raw_y)` and `check_alignment`, as `gxr-cli hdr-align` prints them.
#[wasm_bindgen]
pub fn hdr_align(sdr: &[f32], sdr_height: u32, sdr_width: u32, raw: &[f32], raw_height: u32, raw_width: u32) -> Result<String, JsValue> {
    let report = hdr::align_report(&plane(sdr, sdr_height, sdr_width)?, &plane(raw, raw_height, raw_width)?).map_err(rejected)?;
    Ok(format!("{}\n", report.render()))
}

/// The gain map on the grid: uint8 codes for the gain JPEG, and the JSON report.
#[wasm_bindgen]
pub struct HdrGain {
    height: u32,
    width: u32,
    codes: Vec<u8>,
    report: String,
}

#[wasm_bindgen]
impl HdrGain {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// `GainMeta`, its fractions and the stats, as `gxr-cli hdr-gain` prints them.
    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    /// Row-major uint8 codes (`encode_gainmap` before the JPEG encode). Consumes the object.
    pub fn codes(self) -> Vec<u8> {
        self.codes
    }
}

/// `compute_gain(sdr_y, raw_y, clip, Params(**params))` and the uint8 quantisation.
/// `clip` is one byte per pixel (non-zero = clipped); `raw` is NaN outside the photo.
#[wasm_bindgen]
pub fn hdr_gain(sdr: &[f32], raw: &[f32], clip: &[u8], height: u32, width: u32, params_json: &str) -> Result<HdrGain, JsValue> {
    let s = plane(sdr, height, width)?;
    let r = plane(raw, height, width)?;
    let mask: Vec<bool> = clip.iter().map(|&b| b != 0).collect();
    let params = if params_json.is_empty() {
        hdr::Params::default()
    } else {
        hdr::params_from_json(params_json).map_err(rejected)?
    };
    let result = hdr::compute_gain(&s.v, &r.v, &mask, s.h, s.w, &params).map_err(rejected)?;
    let report = hdr::gain_report(&result).map_err(rejected)?;
    Ok(HdrGain {
        height,
        width,
        codes: hdr::quantise_gain(&result.delta, &result.meta),
        report: format!("{}\n", report.render()),
    })
}

/// `assemble(primary, gain_jpeg, GainMeta(**meta))`: the HDR JPEG bytes.
#[wasm_bindgen]
pub fn hdr_assemble(primary: &[u8], gain_jpeg: &[u8], meta_json: &str) -> Result<Vec<u8>, JsValue> {
    let meta = hdr::meta_from_json(meta_json).map_err(rejected)?;
    hdr::assemble(primary, gain_jpeg, &meta).map_err(rejected)
}
