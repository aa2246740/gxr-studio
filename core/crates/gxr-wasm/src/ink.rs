//! M4 `ink` exports: `ink_ops` and `finish_logo_prepare`.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_finish::geometry::bounds::too_big;
use gxr_finish::ink::{
    InkMode, decode_logo_bundle, finish_logo_prepare as finish_logo_prepare_core,
    parse_logo_prepare_json, run_ink_ops,
};
use gxr_raw::{CoreError, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

fn dim_from_f64(v: f64) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 {
        return Err(CoreError::new(
            "value_error",
            "dimensions must be finite integers",
        ));
    }
    if v <= 0.0 {
        return Err(CoreError::new(
            "value_error",
            "height and width must be > 0",
        ));
    }
    if v > f64::from(i32::MAX) {
        return Err(too_big());
    }
    Ok(v as u64)
}

/// Output buffer and dimensions returned by [`ink_ops`].
#[wasm_bindgen]
pub struct InkOpsResult {
    width: u32,
    height: u32,
    mode: String,
    data: Vec<u8>,
}

#[wasm_bindgen]
impl InkOpsResult {
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn mode(&self) -> String {
        self.mode.clone()
    }

    pub fn data(self) -> Vec<u8> {
        self.data
    }
}

/// Stored custom logo RGBA buffer and CLI-matching JSON report returned by [`finish_logo_prepare`].
#[wasm_bindgen]
pub struct PreparedLogo {
    width: u32,
    height: u32,
    rgba: Vec<u8>,
    report: String,
}

#[wasm_bindgen]
impl PreparedLogo {
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    pub fn rgba(self) -> Vec<u8> {
        self.rgba
    }
}

/// Execute an array of M4 `ink` operations on `(canvas, w, h, mode)` with bundled RGBA logos.
#[wasm_bindgen]
pub fn ink_ops(
    canvas: &[u8],
    w: f64,
    h: f64,
    mode: &str,
    ops_json: &str,
    logo_index_json: &str,
    logo_blob: &[u8],
) -> Result<InkOpsResult, JsValue> {
    let parsed_mode = InkMode::parse(mode).map_err(rejected)?;
    let w_u64 = dim_from_f64(w).map_err(rejected)?;
    let h_u64 = dim_from_f64(h).map_err(rejected)?;
    let logos = decode_logo_bundle(logo_index_json, logo_blob).map_err(rejected)?;
    let out = run_ink_ops(canvas, w_u64, h_u64, parsed_mode, ops_json, &logos).map_err(rejected)?;
    let width = u32::try_from(out.width).map_err(|_| rejected(too_big()))?;
    let height = u32::try_from(out.height).map_err(|_| rejected(too_big()))?;
    Ok(InkOpsResult {
        width,
        height,
        mode: out.mode.as_str().to_string(),
        data: out.data,
    })
}

/// Port of `finish/logo_store.py:prepare_uploaded_logo` starting from decoded RGBA uint8 bytes.
#[wasm_bindgen]
pub fn finish_logo_prepare(
    rgba: &[u8],
    w: f64,
    h: f64,
    key_json: &str,
) -> Result<PreparedLogo, JsValue> {
    let w_u64 = dim_from_f64(w).map_err(rejected)?;
    let h_u64 = dim_from_f64(h).map_err(rejected)?;
    let opts = parse_logo_prepare_json(key_json).map_err(rejected)?;
    let res = finish_logo_prepare_core(
        rgba,
        w_u64,
        h_u64,
        opts.key_mode,
        opts.tol,
        &opts.id,
        &opts.name,
    )
    .map_err(rejected)?;
    let report = format!("{}\n", res.report_json().render());
    let width = u32::try_from(res.width).map_err(|_| rejected(too_big()))?;
    let height = u32::try_from(res.height).map_err(|_| rejected(too_big()))?;
    Ok(PreparedLogo {
        width,
        height,
        rgba: res.rgba,
        report,
    })
}
