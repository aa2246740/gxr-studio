//! LUT and scene exports. Owner: wave-1 unit lut.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_lut::{
    Cube, LutTableResult, RenderSpec, Target, apply_u8, checked_elements, identify, lut_bytes,
    try_vec_cap,
};
use gxr_raw::{CoreError, Json, error_json};

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

/// Parse a `.cube` file and return the same JSON text as `gxr-cli lut-parse` (with trailing newline).
#[wasm_bindgen]
pub fn lut_parse(bytes: &[u8]) -> Result<String, JsValue> {
    let cube = Cube::read_bytes(bytes).map_err(rejected)?;
    Ok(format!("{}\n", cube.summary_json().render()))
}

/// Identify input/output spaces and default choice for a `.cube` file (with trailing newline).
#[wasm_bindgen]
pub fn lut_identify(bytes: &[u8], filename: &str) -> Result<String, JsValue> {
    let cube = Cube::read_bytes(bytes).map_err(rejected)?;
    Ok(format!(
        "{}\n",
        identify(&cube, filename).to_json().render()
    ))
}

/// GPU LUT table result: either `bytes` + `meta_json` or `reason`.
#[wasm_bindgen]
pub struct LutTable {
    summary_json: String,
    meta_json: String,
    reason: Option<String>,
    is_display: bool,
    bytes: Vec<u8>,
}

#[wasm_bindgen]
impl LutTable {
    /// Summary JSON matching `gxr-cli lut-table` stdout (with trailing newline).
    pub fn summary_json(&self) -> String {
        self.summary_json.clone()
    }

    /// Meta JSON (`{"n":256,...}` or `{"reason":...}`).
    pub fn meta_json(&self) -> String {
        self.meta_json.clone()
    }

    /// Refusal reason when the table is unavailable for the GPU shader.
    pub fn reason(&self) -> Option<String> {
        self.reason.clone()
    }

    /// Whether this is a 256³ uint8 RGBA display table.
    #[wasm_bindgen(getter)]
    pub fn is_display(&self) -> bool {
        self.is_display
    }

    /// Table bytes (RGBA uint8 for display, little-endian RGBA float32 for log). Consumes self.
    pub fn bytes(self) -> Vec<u8> {
        self.bytes
    }
}

/// Build the GPU LUT table (`display_lut_u8` or log 3D lattice + metadata).
#[wasm_bindgen]
pub fn lut_table(bytes: &[u8], space_json: &str) -> Result<LutTable, JsValue> {
    let cube = Cube::read_bytes(bytes).map_err(rejected)?;
    let spec = RenderSpec::from_json(space_json).map_err(rejected)?;
    let target = Target::from_spec(&cube, &spec);
    let res = lut_bytes(&target).map_err(rejected)?;
    let summary_json = format!("{}\n", res.summary_json().render());
    match res {
        LutTableResult::Table { bytes, meta } => {
            let is_display = meta.is_display();
            let meta_json = meta.to_json().render();
            Ok(LutTable {
                summary_json,
                meta_json,
                reason: None,
                is_display,
                bytes,
            })
        }
        LutTableResult::Refused { reason } => {
            let meta_json = Json::object([("reason", Json::str(reason))]).render();
            Ok(LutTable {
                summary_json,
                meta_json,
                reason: Some(reason.to_string()),
                is_display: false,
                bytes: Vec::new(),
            })
        }
    }
}

/// Quantised uint8 RGB image produced by `lut_apply_u8`.
#[wasm_bindgen]
pub struct LutImageU8 {
    height: u32,
    width: u32,
    values: Vec<u8>,
}

#[wasm_bindgen]
impl LutImageU8 {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Row-major RGB uint8 bytes (`height * width * 3`). Consumes self.
    pub fn values(self) -> Vec<u8> {
        self.values
    }
}

/// Apply a `.cube` LUT to an `(height, width, 3)` uint8 RGB image (with optional `(height*width*3)` scene XYZ).
#[wasm_bindgen]
pub fn lut_apply_u8(
    cube_bytes: &[u8],
    space_json: &str,
    rgb: &[u8],
    height: u32,
    width: u32,
    xyz: Option<Vec<f64>>,
) -> Result<LutImageU8, JsValue> {
    let cube = Cube::read_bytes(cube_bytes).map_err(rejected)?;
    let spec = RenderSpec::from_json(space_json).map_err(rejected)?;
    let target = Target::from_spec(&cube, &spec);

    let expected_len =
        checked_elements(&[u64::from(height), u64::from(width), 3]).map_err(rejected)?;
    let npixels = checked_elements(&[u64::from(height), u64::from(width)]).map_err(rejected)?;
    if rgb.len() != expected_len {
        return Err(rejected(CoreError::new(
            "value_error",
            "LUT 输入必须为有限 RGB",
        )));
    }
    let mut rgb_triples: Vec<[f64; 3]> = try_vec_cap(npixels).map_err(rejected)?;
    for c in rgb.chunks_exact(3) {
        rgb_triples.push([f64::from(c[0]), f64::from(c[1]), f64::from(c[2])]);
    }
    let xyz_triples = match xyz {
        Some(v) => {
            if v.len() != expected_len {
                return Err(rejected(CoreError::new(
                    "value_error",
                    format!(
                        "xyz length {} does not match rgb length {}",
                        v.len() / 3,
                        rgb_triples.len()
                    ),
                )));
            }
            let mut out = try_vec_cap(npixels).map_err(rejected)?;
            for c in v.chunks_exact(3) {
                out.push([c[0], c[1], c[2]]);
            }
            Some(out)
        }
        None => None,
    };

    let pixels = apply_u8(&target, &rgb_triples, xyz_triples.as_deref()).map_err(rejected)?;
    let mut values = try_vec_cap(expected_len).map_err(rejected)?;
    for px in pixels {
        values.extend_from_slice(&px);
    }
    Ok(LutImageU8 {
        height,
        width,
        values,
    })
}
