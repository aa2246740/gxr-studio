//! Export exports. Owner: wave-2 export (export-pipeline: `export_plan`, `export_prepare`,
//! `export_complete`, `export_output_sizes`).
//!
//! Each export calls `gxr_export::pipeline::api`, the same code `gxr-cli export-run-*` runs, and
//! returns the same JSON text the CLI prints. Sizes and options arrive as `f64` and are checked
//! before they are narrowed (UNIT-COMMON "WASM number parameters").

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_export::pipeline::api::{self, Picture, PrepareArgs, SizesArgs};
use gxr_export::pipeline::{ExportFail, ExportOpts};
use gxr_frame::{Fonts, fonts_from_pack};
use gxr_raw::Json;
use gxr_raw::sha256::sha256_hex;

fn rejected(fail: ExportFail) -> JsValue {
    JsValue::from_str(&fail.to_json().render())
}

fn fonts(index: &str, pack: &[u8]) -> Result<Fonts, JsValue> {
    if index.trim().is_empty() && pack.is_empty() {
        return Ok(Fonts::new());
    }
    fonts_from_pack(index, pack).map_err(|e| rejected(e.into()))
}

/// `export_prepare`'s canvas (background and photo, no ink) and its JSON.
#[wasm_bindgen]
pub struct ExportPrepared {
    width: f64,
    height: f64,
    canvas: Vec<u8>,
    json: String,
}

#[wasm_bindgen]
impl ExportPrepared {
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> f64 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> f64 {
        self.height
    }

    /// The canvas, row-major RGB `(height * width * 3)`.
    pub fn canvas(&self) -> Vec<u8> {
        self.canvas.clone()
    }

    /// `{"original", "prepared": {"frame", "stamp", "stamp_on", "photo", "labels", "state"}}`.
    pub fn json(&self) -> String {
        self.json.clone()
    }
}

/// The finished file and its JSON.
#[wasm_bindgen]
pub struct ExportCompleted {
    file: Vec<u8>,
    json: String,
}

#[wasm_bindgen]
impl ExportCompleted {
    pub fn file(&self) -> Vec<u8> {
        self.file.clone()
    }

    /// `{"result": {...ExportResult fields}, "sha256"}`.
    pub fn json(&self) -> String {
        self.json.clone()
    }
}

/// `serve._plan`: `{"original": true}` when the export is the untouched camera JPG, so the
/// caller knows to pass the decoded camera JPG to `export_prepare` instead of `color_image`'s
/// picture.
#[wasm_bindgen]
pub fn export_plan(shot: &str, side: &str, defaults: &str, base: &str) -> Result<String, JsValue> {
    api::plan_json(shot.as_bytes(), side.as_bytes(), defaults.as_bytes(), base)
        .map(|j| format!("{}\n", j.render()))
        .map_err(rejected)
}

/// `export_prepare`. `picture` is `color_image`'s uint8 RGB, or the decoded camera JPG when
/// `export_plan` says `original`. `long_edge` is `undefined` for the original size.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn export_prepare(
    shot: &str,
    side: &str,
    defaults: &str,
    layout: &str,
    base: &str,
    labels: &str,
    from_raw: bool,
    quality: f64,
    long_edge: Option<f64>,
    hdr: bool,
    font_index: &str,
    font_pack: &[u8],
    picture: &[u8],
    width: f64,
    height: f64,
) -> Result<ExportPrepared, JsValue> {
    let fonts = fonts(font_index, font_pack)?;
    let args = PrepareArgs {
        shot: shot.as_bytes(),
        side: side.as_bytes(),
        defaults: defaults.as_bytes(),
        layout: layout.as_bytes(),
        base,
        labels: labels.as_bytes(),
        from_raw,
        opts: ExportOpts {
            quality,
            long_edge,
            hdr,
        },
    };
    let pic = || Picture {
        data: picture,
        width,
        height,
    };
    let (canvas, json, original) =
        api::prepare(&args, Some(pic()), Some(pic()), &fonts).map_err(rejected)?;
    // The CLI prints the same text with `println!`.
    let json = format!(
        "{}\n",
        Json::object([("original", Json::Bool(original)), ("prepared", json)]).render()
    );
    Ok(ExportPrepared {
        width: canvas.width as f64,
        height: canvas.height as f64,
        canvas: canvas.data,
        json,
    })
}

/// `export_complete` (SDR): the inked canvas, `prepared.state` and the source EXIF JSON.
#[wasm_bindgen]
pub fn export_complete(
    canvas: &[u8],
    width: f64,
    height: f64,
    state: &str,
    exif_json: &str,
) -> Result<ExportCompleted, JsValue> {
    let picture = Picture {
        data: canvas,
        width,
        height,
    };
    let done = api::complete(picture, state.as_bytes(), exif_json).map_err(rejected)?;
    let json = format!(
        "{}\n",
        Json::object([
            ("result", done.result),
            ("sha256", Json::str(sha256_hex(&done.file))),
        ])
        .render()
    );
    Ok(ExportCompleted {
        file: done.file,
        json,
    })
}

/// `finish/sizes.output_sizes` for a source of `width × height`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn export_output_sizes(
    width: f64,
    height: f64,
    side: &str,
    defaults: &str,
    layout: &str,
    rect: &str,
    preset: Option<String>,
    font_index: &str,
    font_pack: &[u8],
) -> Result<String, JsValue> {
    let fonts = fonts(font_index, font_pack)?;
    let args = SizesArgs {
        width,
        height,
        side: side.as_bytes(),
        defaults: defaults.as_bytes(),
        layout: layout.as_bytes(),
        rect: rect.as_bytes(),
        preset: preset.as_deref(),
    };
    api::sizes(&args, &fonts)
        .map(|j| format!("{}\n", j.render()))
        .map_err(rejected)
}
